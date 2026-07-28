"""Validate or index the L&E Global DOCX corpus."""

from __future__ import annotations

import argparse
import sys
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from app.clients.opensearch import get_opensearch_client
from app.services.document_chunk_builder import (
    build_document_chunks_from_docx,
)
from app.services.document_indexer import (
    replace_document_chunks,
)


DEFAULT_SOURCE_DIRECTORY = Path(
    "/data/documents/source"
)
DEFAULT_FILE_PATTERN = "*.docx"


@dataclass(slots=True)
class CorpusIndexingSummary:
    """Aggregate execution statistics."""

    discovered_documents: int = 0
    successful_documents: int = 0
    failed_documents: int = 0
    prepared_chunks: int = 0
    indexed_chunks: int = 0
    stale_chunks_deleted: int = 0


def _build_argument_parser() -> argparse.ArgumentParser:
    """Create the command-line argument parser."""

    parser = argparse.ArgumentParser(
        description=(
            "Validate or index L&E Global DOCX documents "
            "into OpenSearch."
        )
    )

    parser.add_argument(
        "--source-dir",
        type=Path,
        default=DEFAULT_SOURCE_DIRECTORY,
        help=(
            "Directory containing the source DOCX files. "
            f"Default: {DEFAULT_SOURCE_DIRECTORY}"
        ),
    )

    parser.add_argument(
        "--pattern",
        default=DEFAULT_FILE_PATTERN,
        help=(
            "Glob pattern used to discover documents. "
            f"Default: {DEFAULT_FILE_PATTERN}"
        ),
    )

    parser.add_argument(
        "--file",
        action="append",
        dest="requested_files",
        default=[],
        help=(
            "Index only this filename from the source directory. "
            "This option may be repeated."
        ),
    )

    parser.add_argument(
        "--validate-only",
        action="store_true",
        help=(
            "Parse and build chunks without writing "
            "anything to OpenSearch."
        ),
    )

    parser.add_argument(
        "--fail-fast",
        action="store_true",
        help=(
            "Stop immediately when one document fails. "
            "By default, remaining documents are processed."
        ),
    )

    parser.add_argument(
        "--traceback",
        action="store_true",
        help=(
            "Display the complete Python traceback "
            "when a document fails."
        ),
    )

    return parser


def _is_within_directory(
    file_path: Path,
    directory: Path,
) -> bool:
    """Return whether a path is located inside a directory."""

    return (
        file_path == directory
        or directory in file_path.parents
    )


def _discover_documents(
    source_directory: Path,
    pattern: str,
    requested_files: Sequence[str],
) -> list[Path]:
    """
    Return the DOCX files selected for processing.

    Explicit filenames are resolved inside the source directory.
    Temporary Microsoft Word files beginning with ``~$`` are ignored.
    """

    resolved_source_directory = (
        source_directory.expanduser().resolve()
    )

    if not resolved_source_directory.exists():
        raise FileNotFoundError(
            "Source directory does not exist: "
            f"{resolved_source_directory}"
        )

    if not resolved_source_directory.is_dir():
        raise NotADirectoryError(
            "Source path is not a directory: "
            f"{resolved_source_directory}"
        )

    candidates: list[Path] = []

    if requested_files:
        for requested_file in requested_files:
            requested_path = (
                resolved_source_directory
                / requested_file
            ).resolve()

            if not _is_within_directory(
                file_path=requested_path,
                directory=resolved_source_directory,
            ):
                raise ValueError(
                    "Requested file must be located inside "
                    f"{resolved_source_directory}: "
                    f"{requested_file}"
                )

            candidates.append(
                requested_path
            )

    else:
        candidates.extend(
            resolved_source_directory.glob(
                pattern
            )
        )

    selected_documents: dict[Path, Path] = {}

    for candidate in candidates:
        resolved_candidate = (
            candidate.expanduser().resolve()
        )

        if not resolved_candidate.exists():
            raise FileNotFoundError(
                "Requested document does not exist: "
                f"{resolved_candidate}"
            )

        if not resolved_candidate.is_file():
            continue

        if resolved_candidate.name.startswith(
            "~$"
        ):
            continue

        if resolved_candidate.suffix.casefold() != ".docx":
            continue

        selected_documents[
            resolved_candidate
        ] = resolved_candidate

    return sorted(
        selected_documents.values(),
        key=lambda file_path: (
            file_path.name.casefold()
        ),
    )


def _close_client(
    client: Any,
) -> None:
    """Close the OpenSearch client when supported."""

    close_method = getattr(
        client,
        "close",
        None,
    )

    if callable(
        close_method
    ):
        close_method()


def _print_summary(
    summary: CorpusIndexingSummary,
    validate_only: bool,
) -> None:
    """Print the final execution summary."""

    mode = (
        "validation"
        if validate_only
        else "indexation"
    )

    print()
    print("=" * 80)
    print(f"Résumé de {mode}")
    print("=" * 80)
    print(
        "Documents détectés : "
        f"{summary.discovered_documents}"
    )
    print(
        "Documents réussis : "
        f"{summary.successful_documents}"
    )
    print(
        "Documents en erreur : "
        f"{summary.failed_documents}"
    )
    print(
        "Chunks préparés : "
        f"{summary.prepared_chunks}"
    )

    if not validate_only:
        print(
            "Chunks indexés : "
            f"{summary.indexed_chunks}"
        )
        print(
            "Anciens chunks supprimés : "
            f"{summary.stale_chunks_deleted}"
        )


def run(
    arguments: argparse.Namespace,
) -> int:
    """
    Execute corpus validation or indexing.

    Return codes:

    - 0: every selected document succeeded;
    - 1: at least one document failed;
    - 2: invalid input or no document selected.
    """

    try:
        documents = _discover_documents(
            source_directory=arguments.source_dir,
            pattern=arguments.pattern,
            requested_files=arguments.requested_files,
        )

    except (
        FileNotFoundError,
        NotADirectoryError,
        ValueError,
    ) as error:
        print(
            f"[ERROR] {error}",
            file=sys.stderr,
        )
        return 2

    if not documents:
        print(
            "[ERROR] No DOCX document was found.",
            file=sys.stderr,
        )
        return 2

    summary = CorpusIndexingSummary(
        discovered_documents=len(
            documents
        )
    )

    mode = (
        "VALIDATION"
        if arguments.validate_only
        else "INDEXATION"
    )

    print(
        f"L&E Global DOCX corpus — {mode}"
    )
    print(
        f"Source directory: "
        f"{arguments.source_dir.expanduser().resolve()}"
    )
    print(
        f"Documents selected: {len(documents)}"
    )
    print()

    opensearch_client: Any | None = None

    try:
        for document_path in documents:
            try:
                chunks = (
                    build_document_chunks_from_docx(
                        document_path
                    )
                )

                summary.prepared_chunks += len(
                    chunks
                )

                country_code = (
                    chunks[0].country_code
                )

                if arguments.validate_only:
                    summary.successful_documents += 1

                    print(
                        f"[VALID] {document_path.name}: "
                        f"country={country_code}, "
                        f"chunks={len(chunks)}"
                    )

                    continue

                if opensearch_client is None:
                    opensearch_client = (
                        get_opensearch_client()
                    )

                result = replace_document_chunks(
                    chunks=chunks,
                    client=opensearch_client,
                )

                summary.successful_documents += 1
                summary.indexed_chunks += (
                    result.indexed_chunks
                )
                summary.stale_chunks_deleted += (
                    result.stale_chunks_deleted
                )

                print(
                    f"[INDEXED] {result.source_filename}: "
                    f"country={country_code}, "
                    f"chunks={result.indexed_chunks}, "
                    "stale_deleted="
                    f"{result.stale_chunks_deleted}"
                )

            except Exception as error:
                summary.failed_documents += 1

                print(
                    f"[ERROR] {document_path.name}: "
                    f"{type(error).__name__}: {error}",
                    file=sys.stderr,
                )

                if arguments.traceback:
                    traceback.print_exc()

                if arguments.fail_fast:
                    break

    finally:
        if opensearch_client is not None:
            _close_client(
                opensearch_client
            )

    _print_summary(
        summary=summary,
        validate_only=arguments.validate_only,
    )

    if summary.failed_documents:
        return 1

    return 0


def main(
    argv: Sequence[str] | None = None,
) -> int:
    """Parse arguments and execute the command."""

    parser = _build_argument_parser()
    arguments = parser.parse_args(
        argv
    )

    return run(
        arguments
    )


if __name__ == "__main__":
    raise SystemExit(
        main()
    )