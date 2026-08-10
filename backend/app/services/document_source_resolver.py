"""
Centralized resolution of an indexed document's on-disk source path.

Mission "HOTFIX 0.4.4 - Mission 1/2": the 17 documents ingested before
country-keyed storage existed are still physically stored under their
own historical filenames (e.g. "Labour and Employment Law in Spain
2026.docx"), never under storage_filename_for_country's canonical
{COUNTRY_CODE}.docx scheme. list_indexed_documents, Reindex, Replace,
Delete, and Rollback must all resolve the same real file for the same
document, whichever naming generation it belongs to - this module is
the single place that decides how, so no two call sites can silently
disagree with each other again.

Never scans source_root, and never infers a country from any
filename - a document's country always comes from OpenSearch metadata
(country_code), exactly as before this fix.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from app.services.document_chunk_builder import (
    storage_filename_for_country,
)


MAX_SOURCE_FILENAME_LENGTH: Final[int] = 255


class DocumentSourceConflictError(RuntimeError):
    """
    Raised when two distinct, safe metadata fields both resolve to
    two distinct real files on disk for the same document.

    Never resolved silently by picking one - the caller must refuse
    Reindex, Replace, and Delete, and change nothing on disk.
    """

    def __init__(
        self,
        message: str,
        *,
        conflicting_paths: tuple[Path, ...],
    ) -> None:
        super().__init__(message)

        self.conflicting_paths = conflicting_paths


@dataclass(frozen=True, slots=True)
class ResolvedDocumentSource:
    """The result of resolving one document's on-disk source path."""

    path: Path | None
    origin: str


def _is_safe_source_filename(
    name: str,
) -> bool:
    """
    A metadata-provided filename is usable as a path component only
    when it is exactly this safe - never a business format check
    (mission "CONTINUATION PATCH 0.4.3", section 4 - the same
    safety-only contract, reused here for reading rather than
    writing).
    """

    if not name:
        return False

    if "\x00" in name:
        return False

    if "/" in name or "\\" in name:
        return False

    if Path(name).name != name:
        return False

    if len(name) > MAX_SOURCE_FILENAME_LENGTH:
        return False

    if Path(name).suffix.casefold() != ".docx":
        return False

    return True


def _resolve_candidate(
    source_root: Path,
    name: str,
) -> Path | None:
    """
    Resolve one candidate filename to a safe, existing regular file
    under source_root - or None when unsafe, escaping, or absent.
    """

    if not _is_safe_source_filename(
        name
    ):
        return None

    resolved_root = source_root.resolve()

    candidate_path = (
        resolved_root / name
    ).resolve()

    if candidate_path.parent != resolved_root:
        # Rejects path traversal and any symlink whose real target
        # escapes source_root - resolve() dereferences symlinks, so
        # an internal symlink (still resolving under source_root)
        # remains accepted, exactly as intended.
        return None

    if not candidate_path.is_file():
        return None

    return candidate_path


def resolve_document_source_path(
    *,
    source_root: Path,
    country_code: str,
    storage_filename: str | None = None,
    source_filename: str | None = None,
    original_filename: str | None = None,
) -> ResolvedDocumentSource:
    """
    Resolve the one real on-disk file backing an indexed document.

    Tries, in this order:

    1. storage_filename - an explicit stored on-disk name, if the
       caller's metadata ever carries one (no metadata does today;
       this is a forward-compatible hook only).
    2. source_filename - the document's historical, exact stored
       filename. This is what makes every one of the 17 legacy
       documents resolve correctly: they were physically saved under
       this exact name before country-keyed storage existed.
    3. original_filename - kept distinct from source_filename only
       for callers whose model separates the two; this pipeline's own
       metadata does not, so it is unused today.
    4. The current, canonical {COUNTRY_CODE}.docx path (see
       storage_filename_for_country) - what every upload since mission
       "CONTINUATION PATCH 0.4.3" is physically stored under.

    Never scans source_root, never infers a country from a filename.

    Raises DocumentSourceConflictError when two of these candidates
    are both safe, both exist, and are two distinct real files.
    """

    candidates: list[
        tuple[str, str | None]
    ] = [
        ("storage_filename", storage_filename),
        ("source_filename", source_filename),
        ("original_filename", original_filename),
        (
            "canonical",
            storage_filename_for_country(
                country_code
            ),
        ),
    ]

    matches: list[tuple[str, Path]] = []
    seen_paths: set[Path] = set()

    for origin, name in candidates:
        if not name:
            continue

        resolved_path = _resolve_candidate(
            source_root,
            name,
        )

        if resolved_path is None:
            continue

        if resolved_path in seen_paths:
            continue

        seen_paths.add(
            resolved_path
        )

        matches.append(
            (
                origin,
                resolved_path,
            )
        )

    if not matches:
        return ResolvedDocumentSource(
            path=None,
            origin="missing",
        )

    if len(matches) > 1:
        raise DocumentSourceConflictError(
            "Multiple distinct source files resolve for this "
            "document.",
            conflicting_paths=tuple(
                path
                for _, path in matches
            ),
        )

    origin, path = matches[0]

    return ResolvedDocumentSource(
        path=path,
        origin=origin,
    )


def resolve_country_source_paths(
    *,
    source_root: Path,
    country_code: str,
    source_filenames: Sequence[str],
) -> tuple[Path, ...]:
    """
    Return every distinct real source file that belongs to one country.

    This is intentionally broader than resolve_document_source_path:
    confirmed replacement must retire all historical and canonical source
    files for a country instead of refusing when both generations coexist.
    """

    candidate_names = [
        *source_filenames,
        storage_filename_for_country(country_code),
    ]

    matches: list[Path] = []
    seen_paths: set[Path] = set()

    for name in candidate_names:
        resolved_path = _resolve_candidate(
            source_root,
            name,
        )

        if resolved_path is None:
            continue

        if resolved_path in seen_paths:
            continue

        seen_paths.add(resolved_path)
        matches.append(resolved_path)

    return tuple(matches)
