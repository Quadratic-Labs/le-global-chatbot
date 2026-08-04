from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

from app.core.legal_taxonomy import (
    LEGAL_TOPICS,
)
from app.services.document_chunk_builder import (
    build_document_chunks_from_docx,
    metadata_from_content,
)


def _validate_file(
    file_path: Path,
) -> dict[str, object]:
    """
    Run the real production ingestion path on one DOCX.

    The validation uses the same content-based metadata detection,
    country registry, DOCX parser, taxonomy, and chunk builder as
    production ingestion (mission "CONTINUATION PATCH 0.4.3" - the
    filename is no longer a metadata source at all).
    """

    metadata = metadata_from_content(
        file_path=file_path
    )

    chunks = build_document_chunks_from_docx(
        file_path=file_path,
        country_code=metadata.country_code,
        language=metadata.language,
    )

    legal_topics = {
        chunk.legal_topic
        for chunk in chunks
        if chunk.legal_topic is not None
    }

    missing_topics = [
        topic
        for topic in LEGAL_TOPICS
        if topic not in legal_topics
    ]

    document_ids = {
        chunk.document_id
        for chunk in chunks
    }

    chunk_ids = [
        chunk.chunk_id
        for chunk in chunks
    ]

    duplicate_chunk_ids = sorted(
        chunk_id
        for chunk_id, count in Counter(
            chunk_ids
        ).items()
        if count > 1
    )

    document_type_counts = Counter(
        chunk.document_type
        for chunk in chunks
    )

    max_content_length = max(
        (
            len(chunk.content)
            for chunk in chunks
        ),
        default=0,
    )

    return {
        "filename": file_path.name,
        "country": metadata.country,
        "country_code": metadata.country_code,
        "reference_year": metadata.reference_year,
        "chunks": len(chunks),
        "overview_chunks": document_type_counts.get(
            "overview",
            0,
        ),
        "comparator_chunks": document_type_counts.get(
            "comparator",
            0,
        ),
        "topics_found": len(
            legal_topics
        ),
        "missing_topics": missing_topics,
        "unique_document_ids": len(
            document_ids
        ),
        "unique_chunk_ids": len(
            set(chunk_ids)
        ),
        "duplicate_chunk_ids": duplicate_chunk_ids,
        "max_content_length": max_content_length,
    }


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit(
            "Usage: python -m scripts.validate_corpus_parser "
            "<source-directory>"
        )

    source_directory = Path(
        sys.argv[1]
    ).resolve()

    if not source_directory.is_dir():
        raise NotADirectoryError(
            "Source directory not found: "
            f"{source_directory}"
        )

    files = sorted(
        source_directory.glob(
            "*.docx"
        ),
        key=lambda path: path.name.casefold(),
    )

    if not files:
        raise FileNotFoundError(
            "No DOCX files found in: "
            f"{source_directory}"
        )

    successful_files = 0
    attention_files = 0

    print(
        "L&E production ingestion validation"
    )

    print(
        "=" * 80
    )

    for file_path in files:
        try:
            result = _validate_file(
                file_path
            )

        except Exception as error:
            attention_files += 1

            print()
            print(
                f"[ERROR] {file_path.name}"
            )

            print(
                f"  {type(error).__name__}: {error}"
            )

            continue

        successful_files += 1

        is_valid = (
            result["topics_found"]
            == len(LEGAL_TOPICS)
            and not result[
                "missing_topics"
            ]
            and result[
                "unique_document_ids"
            ] == 1
            and result[
                "unique_chunk_ids"
            ] == result[
                "chunks"
            ]
            and not result[
                "duplicate_chunk_ids"
            ]
        )

        status = (
            "OK"
            if is_valid
            else "CHECK"
        )

        if not is_valid:
            attention_files += 1

        print()
        print(
            f"[{status}] {result['filename']}"
        )

        print(
            "  Country: "
            f"{result['country']} "
            f"({result['country_code']})"
        )

        print(
            "  Reference year: "
            f"{result['reference_year'] or '<missing>'}"
        )

        print(
            f"  Total chunks: {result['chunks']}"
        )

        print(
            "  Overview / comparator: "
            f"{result['overview_chunks']} / "
            f"{result['comparator_chunks']}"
        )

        print(
            "  Legal topic coverage: "
            f"{result['topics_found']}/"
            f"{len(LEGAL_TOPICS)}"
        )

        print(
            "  Unique document IDs: "
            f"{result['unique_document_ids']}"
        )

        print(
            "  Unique chunk IDs: "
            f"{result['unique_chunk_ids']}"
        )

        print(
            "  Max content length: "
            f"{result['max_content_length']}"
        )

        missing_topics = result[
            "missing_topics"
        ]

        if missing_topics:
            print(
                "  Missing topics: "
                + ", ".join(
                    missing_topics
                )
            )

        duplicate_chunk_ids = result[
            "duplicate_chunk_ids"
        ]

        if duplicate_chunk_ids:
            print(
                "  Duplicate chunk IDs: "
                + ", ".join(
                    duplicate_chunk_ids
                )
            )

    print()
    print(
        "=" * 80
    )

    print(
        f"Files analysed: {len(files)}"
    )

    print(
        "Files successfully processed: "
        f"{successful_files}"
    )

    print(
        "Files requiring attention: "
        f"{attention_files}"
    )

    if attention_files:
        raise SystemExit(
            1
        )


if __name__ == "__main__":
    main()