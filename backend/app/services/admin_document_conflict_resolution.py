"""
Generic country-conflict review and resolution.

Mission "ORDER 8E-A1", sections 18/22-28: more than one active
indexed document for a country is a genuine conflict the ordinary
upload/replace decision must never guess its way through (see
AdminDocumentCountryConflictReviewRequiredError in
admin_document_replacement.py, which blocks upload from reaching it at
all). This module is the one, dedicated, generically-applicable way to
resolve such a conflict - never any Italy/overview/comparator-specific
logic, never a filename-string match as the eligibility test for
"these are the same document."

Three resolution modes:

- AUTO_DEDUPLICATE: only when every conflicting record's own resolved
  source file shares the same content (by SHA-256) - strong, generic
  evidence that they are all the same physical/current DOCX, never
  merely a filename coincidence.
- CHOOSE_DOCUMENT: the Admin picks one of several genuinely distinct
  documents to keep; the others' indexed chunks (and, when distinct,
  their own source files) are removed.
- REPLACE_WITH_DOCUMENT: the Admin supplies an authoritative DOCX
  through the exact same upload validation flow (see
  safe_upload_and_index_document's resolve_country_conflict flag) -
  never a "contact developer" dead end.

Every mutation here snapshots OpenSearch state first and restores it
verbatim on any failure, mirroring document_indexer.py's own existing
snapshot/restore helpers rather than reinventing them.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from opensearchpy import OpenSearch

from app.clients.opensearch import get_opensearch_client
from app.core.country_registry import (
    canonical_country_name,
    normalize_country_code,
)
from app.services.admin_document_replacement import (
    CountryConflictCandidate,
    ExistingCountryDocument,
    _build_conflict_candidates,
    _sha256_file,
    lookup_existing_country_documents,
)
from app.services.admin_documents import (
    AdminDocumentStorageError,
    _safe_unlink,
)
from app.services.country_lock import (
    DEFAULT_LOCK_TIMEOUT_SECONDS,
    country_lock,
)
from app.services.document_chunk_builder import storage_filename_for_country
from app.services.document_indexer import (
    DEFAULT_BULK_CHUNK_SIZE,
    _delete_country_chunks,
    _restore_country_snapshot,
    _snapshot_country_chunks,
)
from app.services.document_section_state import delete_section_edit_state
from app.services.document_source_resolver import (
    DocumentSourceConflictError,
    resolve_country_source_paths,
    resolve_document_source_path,
)


AUTO_DEDUPLICATE = "AUTO_DEDUPLICATE"
CHOOSE_DOCUMENT = "CHOOSE_DOCUMENT"
RESOLUTION_MODES = (AUTO_DEDUPLICATE, CHOOSE_DOCUMENT)


class CountryConflictNotFoundError(ValueError):
    """
    Raised when a resolution (or review) is requested for a country
    that does not currently have more than one active document -
    there is nothing to resolve, and this is never silently treated
    as a success.
    """

    def __init__(self, *, country_code: str) -> None:
        self.country_code = country_code

        super().__init__(
            f"{country_code} is not currently in a conflict state - "
            "there is nothing to resolve."
        )

    def to_detail(self) -> dict[str, object]:
        return {
            "code": "country_conflict_not_found",
            "message": str(self),
            "country_code": self.country_code,
        }


class CountryConflictResolutionError(ValueError):
    """
    Raised for an invalid, stale, or unsupported resolution request -
    e.g. an unknown resolution_mode, an AUTO_DEDUPLICATE request for a
    country whose conflicting records lack strong same-source
    evidence, or a keep_document_id that no longer matches the
    country's current candidates (the conflict state changed between
    the Admin's review and this request - never trusted as still
    valid without revalidating immediately before mutation).
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)

    def to_detail(self) -> dict[str, object]:
        return {
            "code": "country_conflict_resolution_invalid",
            "message": str(self),
        }


@dataclass(frozen=True, slots=True)
class CountryConflictReview:
    """A safe, business-facing snapshot of one country's conflict."""

    country_code: str
    country: str
    candidates: tuple[CountryConflictCandidate, ...]
    auto_deduplicate_available: bool


@dataclass(frozen=True, slots=True)
class CountryConflictResolutionResult:
    """The outcome of one successful conflict resolution."""

    country_code: str
    resolution_mode: str
    kept_document_id: str
    removed_document_ids: tuple[str, ...]
    stale_chunks_deleted: int


def _resolved_paths_by_document(
    existing_documents: Sequence[ExistingCountryDocument],
    *,
    source_directory: Path,
    country_code: str,
) -> dict[str, Path | None]:
    """Each existing document's own resolved source path, or None."""

    resolved: dict[str, Path | None] = {}

    for document in existing_documents:
        try:
            resolved_source = resolve_document_source_path(
                source_root=source_directory,
                country_code=country_code,
                source_filename=document.source_filename,
            )
            resolved[document.document_id] = resolved_source.path

        except DocumentSourceConflictError:
            resolved[document.document_id] = None

    return resolved


def _auto_deduplicate_keep_document_id(
    existing_documents: Sequence[ExistingCountryDocument],
    *,
    source_directory: Path,
    country_code: str,
) -> str | None:
    """
    Return the document_id to keep under AUTO_DEDUPLICATE, or None
    when the strong, generic same-source evidence it requires is
    absent.

    Evidence: every real file resolvable for this country (however
    many distinct on-disk paths that is) shares one identical SHA-256
    digest - proving every conflicting record is backed by the same
    physical, current DOCX content. A document whose own filename does
    not resolve to any of those files is never selected as the one to
    keep, even if it would otherwise win the tie-break below.
    """

    existing_paths = resolve_country_source_paths(
        source_root=source_directory,
        country_code=country_code,
        source_filenames=[
            document.source_filename
            for document in existing_documents
        ],
    )

    if not existing_paths:
        return None

    distinct_shas = {_sha256_file(path) for path in existing_paths}

    if len(distinct_shas) != 1:
        return None

    resolved_by_document = _resolved_paths_by_document(
        existing_documents,
        source_directory=source_directory,
        country_code=country_code,
    )

    eligible = [
        document
        for document in existing_documents
        if resolved_by_document.get(document.document_id) is not None
    ]

    if not eligible:
        return None

    canonical_name = storage_filename_for_country(country_code)
    canonical_matches = [
        document
        for document in eligible
        if document.source_filename == canonical_name
    ]

    if len(canonical_matches) == 1:
        return canonical_matches[0].document_id

    with_year = [
        document
        for document in eligible
        if document.reference_year is not None
    ]

    if with_year:
        best_year = max(document.reference_year for document in with_year)
        best_year_ids = sorted(
            document.document_id
            for document in with_year
            if document.reference_year == best_year
        )
        return best_year_ids[-1]

    return sorted(document.document_id for document in eligible)[-1]


def build_country_conflict_review(
    country_code: str,
    *,
    source_directory: Path,
    client: OpenSearch | None = None,
    country_document_lookup=lookup_existing_country_documents,
) -> CountryConflictReview:
    """
    Return a read-only, safe review of one country's current conflict.

    Raises CountryConflictNotFoundError when the country does not
    currently have more than one active document.
    """

    normalized_code = normalize_country_code(country_code)
    existing_documents = country_document_lookup(normalized_code, client)
    unique_ids = {document.document_id for document in existing_documents}

    if len(unique_ids) <= 1:
        raise CountryConflictNotFoundError(country_code=normalized_code)

    keep_id = _auto_deduplicate_keep_document_id(
        existing_documents,
        source_directory=source_directory,
        country_code=normalized_code,
    )

    return CountryConflictReview(
        country_code=normalized_code,
        country=canonical_country_name(normalized_code),
        candidates=tuple(
            _build_conflict_candidates(
                existing_documents,
                source_directory=source_directory,
                country_code=normalized_code,
            )
        ),
        auto_deduplicate_available=keep_id is not None,
    )


def resolve_country_conflict(
    country_code: str,
    resolution_mode: str,
    *,
    source_directory: Path,
    keep_document_id: str | None = None,
    client: OpenSearch | None = None,
    country_document_lookup=lookup_existing_country_documents,
    bulk_chunk_size: int = DEFAULT_BULK_CHUNK_SIZE,
    lock_timeout_seconds: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
) -> CountryConflictResolutionResult:
    """
    Resolve a country's conflict via AUTO_DEDUPLICATE or
    CHOOSE_DOCUMENT (REPLACE_WITH_DOCUMENT is a distinct upload call -
    see safe_upload_and_index_document's resolve_country_conflict
    flag, which reuses the exact same validation flow as a normal
    upload rather than a second implementation here).

    The conflict is revalidated immediately before mutation, under the
    country's own lock, exactly once - never trusting a client-supplied
    keep_document_id or an earlier review as still current. On any
    indexing failure the previous OpenSearch state is restored exactly,
    and any source file already moved aside is restored too - the
    country is left with precisely one active document only on a
    verified success, never a partial one.
    """

    if resolution_mode not in RESOLUTION_MODES:
        raise CountryConflictResolutionError(
            f"Unknown resolution_mode: {resolution_mode!r}. Expected "
            f"one of {RESOLUTION_MODES}."
        )

    normalized_code = normalize_country_code(country_code)
    opensearch_client = (
        client if client is not None else get_opensearch_client()
    )

    with country_lock(
        source_directory,
        normalized_code,
        timeout_seconds=lock_timeout_seconds,
    ):
        existing_documents = country_document_lookup(
            normalized_code, opensearch_client
        )
        unique_ids = {
            document.document_id for document in existing_documents
        }

        if len(unique_ids) <= 1:
            raise CountryConflictNotFoundError(
                country_code=normalized_code
            )

        if resolution_mode == AUTO_DEDUPLICATE:
            resolved_keep_id = _auto_deduplicate_keep_document_id(
                existing_documents,
                source_directory=source_directory,
                country_code=normalized_code,
            )

            if resolved_keep_id is None:
                raise CountryConflictResolutionError(
                    "AUTO_DEDUPLICATE is not available for "
                    f"{normalized_code} - its conflicting records are "
                    "not proven to be the same physical document. Use "
                    "CHOOSE_DOCUMENT or REPLACE_WITH_DOCUMENT instead."
                )

            if (
                keep_document_id is not None
                and keep_document_id != resolved_keep_id
            ):
                raise CountryConflictResolutionError(
                    "keep_document_id does not match the document "
                    "AUTO_DEDUPLICATE would keep for this country."
                )

            effective_keep_id = resolved_keep_id

        else:
            if (
                keep_document_id is None
                or keep_document_id not in unique_ids
            ):
                raise CountryConflictResolutionError(
                    "keep_document_id is not one of this country's "
                    "current candidates - the conflict state may have "
                    "changed. Refresh the review and try again."
                )

            effective_keep_id = keep_document_id

        resolved_by_document = _resolved_paths_by_document(
            existing_documents,
            source_directory=source_directory,
            country_code=normalized_code,
        )
        kept_path = resolved_by_document.get(effective_keep_id)

        operation_id = f"conflict-resolution-{normalized_code}"
        backups: list[tuple[Path, Path]] = []

        # Captured before any mutation - the one true pre-resolution
        # state to restore on any failure below, never a re-fetch of
        # whatever OpenSearch happens to hold after a partial failure.
        snapshot = _snapshot_country_chunks(
            client=opensearch_client,
            country_code=normalized_code,
        )
        keep_chunk_ids = [
            item["_id"]
            for item in snapshot
            if item.get("_source", {}).get("document_id")
            == effective_keep_id
        ]

        if not keep_chunk_ids:
            raise CountryConflictResolutionError(
                "The chosen document has no indexed chunks to keep - "
                "refusing to leave the country with zero active "
                "documents."
            )

        try:
            for document in existing_documents:
                if document.document_id == effective_keep_id:
                    continue

                candidate_path = resolved_by_document.get(
                    document.document_id
                )

                if (
                    candidate_path is None
                    or candidate_path == kept_path
                ):
                    continue

                backup_path = (
                    candidate_path.parent
                    / (
                        f".{operation_id}."
                        f"{candidate_path.name}.backup"
                    )
                )
                os.replace(candidate_path, backup_path)
                backups.append((candidate_path, backup_path))

            stale_chunks_deleted = _delete_country_chunks(
                client=opensearch_client,
                country_code=normalized_code,
                keep_chunk_ids=keep_chunk_ids,
            )

            remaining = country_document_lookup(
                normalized_code, opensearch_client
            )
            remaining_ids = {
                document.document_id for document in remaining
            }

            if remaining_ids != {effective_keep_id}:
                raise AdminDocumentStorageError(
                    "The country did not end with exactly one active "
                    "document after resolution."
                )

        except Exception:
            for original_path, backup_path in reversed(backups):
                if backup_path.exists():
                    os.replace(backup_path, original_path)

            _restore_country_snapshot(
                client=opensearch_client,
                snapshot=snapshot,
                bulk_chunk_size=bulk_chunk_size,
            )

            raise

        for _, backup_path in backups:
            _safe_unlink(backup_path)

        removed_document_ids = sorted(unique_ids - {effective_keep_id})

        for old_document_id in removed_document_ids:
            try:
                delete_section_edit_state(
                    source_directory, old_document_id
                )

            except OSError as error:
                raise AdminDocumentStorageError(
                    "The conflict was resolved, but a previous "
                    "document's section-edit state could not be "
                    "fully cleared."
                ) from error

        return CountryConflictResolutionResult(
            country_code=normalized_code,
            resolution_mode=resolution_mode,
            kept_document_id=effective_keep_id,
            removed_document_ids=tuple(removed_document_ids),
            stale_chunks_deleted=stale_chunks_deleted,
        )
