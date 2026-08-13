"""Safe country-level upload and replacement for admin DOCX documents."""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from opensearchpy import OpenSearch
from opensearchpy.exceptions import (
    OpenSearchException,
)

from app.clients.opensearch import get_opensearch_client
from app.core.admin_country_policy import is_admin_country_allowed
from app.core.country_registry import CountryMetadataMismatchError
from app.models.admin_documents import AdminDocumentUploadResponse
from app.models.document import DocumentChunk
from app.services.admin_documents import (
    AdminDocumentStorageError,
    DocumentCorruptError,
    DocumentCountryUndeterminedError,
    DocumentParseFailedError,
    InvalidDocumentUploadError,
    _safe_unlink,
    _sanitize_filename,
    _write_upload,
)
from app.services.country_lock import (
    DEFAULT_LOCK_TIMEOUT_SECONDS,
    country_lock,
)
from app.services.document_chunk_builder import (
    DOCUMENT_FAMILY,
    AmbiguousDocumentCountryError,
    InvalidDocxFormatError,
    UndeterminableDocumentCountryError,
    build_document_chunks_from_docx,
    storage_filename_for_country,
)
from app.services.document_indexer import (
    DocumentIndexingError,
    DocumentIndexingResult,
    _fetch_all_chunks,
    replace_country_document_chunks,
)
from app.services.document_section_state import (
    delete_section_edit_state,
)
from app.services.document_source_resolver import (
    resolve_country_source_paths,
)
from app.services.document_warnings import (
    TopicCoverageWarning,
    evaluate_topic_coverage,
)
from app.services.opensearch_index import LEGAL_DOCUMENTS_ALIAS


@dataclass(frozen=True, slots=True)
class ExistingCountryDocument:
    """One distinct indexed document already active for a country."""

    document_id: str
    source_filename: str
    country: str
    country_code: str
    reference_year: int | None


class AdminDocumentCountryNotAllowedError(ValueError):
    """
    Raised when a document's country is correctly detected but is not
    on the ADMIN upload allowlist (app.core.admin_country_policy).

    Deliberately a distinct error from DocumentCountryUndeterminedError
    (mission "ORDER 5C": a country the registry could not identify at
    all, and a country identified perfectly but not currently
    accepted for new uploads, are different failures with different
    remediations - conflating them into one generic "undetermined"
    message would hide which one actually happened).
    """

    def __init__(
        self,
        *,
        country: str,
        country_code: str,
    ) -> None:
        self.country = country
        self.country_code = country_code

        super().__init__(
            f"{country} ({country_code}) is not currently accepted "
            "for new document uploads."
        )

    def to_detail(self) -> dict[str, object]:
        """Return a structured HTTP 422 payload."""

        return {
            "code": "document_country_not_allowed",
            "message": str(self),
            "operation": "upload",
            "country_code": self.country_code,
            "country_name": self.country,
        }


class AdminDocumentReplacementRequiredError(ValueError):
    """Raised when an existing country needs explicit admin approval."""

    def __init__(
        self,
        *,
        country: str,
        country_code: str,
        existing_documents: Sequence[ExistingCountryDocument],
    ) -> None:
        self.country = country
        self.country_code = country_code
        self.existing_documents = tuple(existing_documents)

        super().__init__(
            f"A document already exists for {country}. "
            "Confirm replacement to keep the uploaded DOCX as the "
            "only active version for this country."
        )

    def to_detail(self) -> dict[str, object]:
        """Return a structured HTTP 409 payload."""

        return {
            "code": "document_replacement_required",
            "message": str(self),
            "country": self.country,
            "country_code": self.country_code,
            "existing_document_ids": [
                document.document_id
                for document in self.existing_documents
            ],
        }


class AdminDocumentAlreadyCurrentError(ValueError):
    """Raised when the uploaded bytes already match the active source."""

    def __init__(
        self,
        *,
        country: str,
        country_code: str,
    ) -> None:
        self.country = country
        self.country_code = country_code

        super().__init__(
            f"The uploaded DOCX is identical to the current "
            f"{country} source. No reindexing was performed."
        )

    def to_detail(self) -> dict[str, object]:
        """Return a structured HTTP 409 payload."""

        return {
            "code": "document_already_current",
            "message": str(self),
            "country": self.country,
            "country_code": self.country_code,
        }


class AdminDocumentWarningConfirmationRequiredError(ValueError):
    """
    Raised when a document parses successfully but its topic coverage
    warrants admin confirmation (confirm_warnings=True) before it is
    indexed - never raised together with AdminDocumentAlreadyCurrentError
    (an identical re-upload is always a no-op, regardless of warnings)
    and never in place of AdminDocumentReplacementRequiredError when no
    warning applies (mission "ORDER 3", section 14: that simpler,
    already-supported contract must stay unchanged when it is the only
    pending decision).
    """

    def __init__(
        self,
        *,
        country: str,
        country_code: str,
        warnings: Sequence[TopicCoverageWarning],
        replacement_required: bool,
        existing_document_ids: Sequence[str],
    ) -> None:
        self.country = country
        self.country_code = country_code
        self.warnings = tuple(warnings)
        self.replacement_required = replacement_required
        self.existing_document_ids = tuple(existing_document_ids)

        super().__init__(
            "The document is technically valid but its content "
            "requires confirmation before indexing. Set "
            "confirm_warnings=true to proceed."
        )

    def to_detail(self) -> dict[str, object]:
        """Return a structured HTTP 409 payload."""

        return {
            "code": "document_warning_confirmation_required",
            "message": str(self),
            "operation": "upload",
            "country_code": self.country_code,
            "country_name": self.country,
            "replacement_required": self.replacement_required,
            "existing_document_ids": list(self.existing_document_ids),
            "warnings": [
                {
                    "code": warning.code,
                    "message": warning.message,
                    "recognized_topics_count": (
                        warning.recognized_topics_count
                    ),
                    "expected_topics_count": (
                        warning.expected_topics_count
                    ),
                    "missing_topics": list(warning.missing_topics),
                }
                for warning in self.warnings
            ],
        }


ChunkBuilder = Callable[[Path], list[DocumentChunk]]
CountryDocumentLookup = Callable[
    [str, OpenSearch | None],
    list[ExistingCountryDocument],
]
CountryDocumentIndexer = Callable[..., DocumentIndexingResult]


def _required_string(
    source: dict[str, object],
    field: str,
) -> str:
    """Read one required string from OpenSearch metadata."""

    value = source.get(field)

    if not isinstance(value, str) or not value.strip():
        raise AdminDocumentStorageError(
            f"Indexed document metadata is invalid: {field}."
        )

    return value.strip()


def lookup_existing_country_documents(
    country_code: str,
    client: OpenSearch | None = None,
) -> list[ExistingCountryDocument]:
    """Return distinct indexed documents for one detected country."""

    normalized_country_code = country_code.strip().upper()

    if not normalized_country_code:
        raise ValueError("country_code must not be empty.")

    opensearch_client = (
        client
        if client is not None
        else get_opensearch_client()
    )

    try:
        hits = _fetch_all_chunks(
            client=opensearch_client,
            field="country_code",
            value=normalized_country_code,
        )

    except DocumentIndexingError as error:
        raise AdminDocumentStorageError(
            "OpenSearch returned an invalid country lookup response."
        ) from error

    documents_by_id: dict[str, ExistingCountryDocument] = {}

    for hit in hits:
        if not isinstance(hit, dict):
            raise AdminDocumentStorageError(
                "OpenSearch returned an invalid country hit."
            )

        source = hit.get("_source")

        if not isinstance(source, dict):
            raise AdminDocumentStorageError(
                "OpenSearch returned invalid country metadata."
            )

        document_id = _required_string(source, "document_id")

        if document_id in documents_by_id:
            continue

        indexed_country_code = _required_string(
            source,
            "country_code",
        ).upper()

        if indexed_country_code != normalized_country_code:
            raise AdminDocumentStorageError(
                "OpenSearch returned metadata for a different country."
            )

        reference_year = source.get("reference_year")

        if reference_year is not None:
            try:
                reference_year = int(reference_year)

            except (TypeError, ValueError) as error:
                raise AdminDocumentStorageError(
                    "Indexed reference_year metadata is invalid."
                ) from error

        documents_by_id[document_id] = ExistingCountryDocument(
            document_id=document_id,
            source_filename=_required_string(
                source,
                "source_filename",
            ),
            country=_required_string(source, "country"),
            country_code=indexed_country_code,
            reference_year=reference_year,
        )

    return [
        documents_by_id[document_id]
        for document_id in sorted(documents_by_id)
    ]


def _sha256_file(path: Path) -> str:
    """Return the SHA-256 digest of one file."""

    digest = hashlib.sha256()

    with path.open("rb") as file_handle:
        while True:
            block = file_handle.read(1024 * 1024)

            if not block:
                break

            digest.update(block)

    return digest.hexdigest()


def _restore_backups(
    backups: Sequence[tuple[Path, Path]],
) -> None:
    """Restore every source path moved to a temporary backup."""

    for original_path, backup_path in reversed(backups):
        if backup_path.exists():
            os.replace(backup_path, original_path)


def safe_upload_and_index_document(
    *,
    filename: str,
    file_stream: BinaryIO,
    source_directory: Path,
    processed_directory: Path,
    maximum_bytes: int,
    replace_existing: bool = False,
    confirm_warnings: bool = False,
    client: OpenSearch | None = None,
    chunk_builder: ChunkBuilder = build_document_chunks_from_docx,
    country_document_lookup: CountryDocumentLookup = (
        lookup_existing_country_documents
    ),
    country_document_indexer: CountryDocumentIndexer = (
        replace_country_document_chunks
    ),
    lock_timeout_seconds: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
) -> AdminDocumentUploadResponse:
    """
    Upload one DOCX with explicit country-level replacement approval.

    file_stream is staged and chunk_builder is invoked exactly once,
    regardless of whether the detected country is brand new or already
    active - a fresh country and a confirmed replacement share the
    exact same write-then-index tail below (country_document_indexer
    tolerates a country with zero prior chunks - see
    replace_country_document_chunks's own snapshot/delete-stale logic -
    so there is no separate "fresh" indexing implementation to fall
    back to). Earlier versions re-staged and re-parsed the upload a
    second time through a delegated legacy implementation for a fresh
    country, which depended on file_stream still being fully re-
    readable after already being consumed once - fragile by
    construction, and never necessary in the first place (mission
    "HOTFIX 0.4.9").

    An existing country is never changed unless replace_existing=True.

    A document that parses successfully but whose topic coverage is
    atypical (see app.services.document_warnings) is not indexed
    unless confirm_warnings=True is also passed - the warning itself
    is always recomputed here, from the real uploaded bytes, never
    trusted from the caller (mission "ORDER 3", section 13).

    Every read/decide/mutate step from the country lookup onward runs
    under a per-country lock (country_lock) - never held during
    chunk_builder's own parsing, which does not touch shared state.
    """

    safe_filename = _sanitize_filename(filename)

    try:
        source_directory.mkdir(
            parents=True,
            exist_ok=True,
        )
        processed_directory.mkdir(
            parents=True,
            exist_ok=True,
        )

        with tempfile.TemporaryDirectory(
            prefix="document-upload-",
            dir=processed_directory,
        ) as temporary_directory:
            staged_path = (
                Path(temporary_directory)
                / safe_filename
            )

            uploaded_bytes = _write_upload(
                file_stream=file_stream,
                destination=staged_path,
                maximum_bytes=maximum_bytes,
            )

            try:
                chunks = chunk_builder(staged_path)

            except InvalidDocxFormatError as error:
                raise DocumentCorruptError(
                    f"DOCX validation failed: {error}"
                ) from error

            except (
                UndeterminableDocumentCountryError,
                AmbiguousDocumentCountryError,
                CountryMetadataMismatchError,
            ) as error:
                raise DocumentCountryUndeterminedError(
                    f"DOCX validation failed: {error}"
                ) from error

            except Exception as error:
                raise DocumentParseFailedError(
                    f"DOCX validation failed: {error}"
                ) from error

            if not chunks:
                raise DocumentParseFailedError(
                    "The uploaded DOCX produced no legal chunks."
                )

            first_chunk = chunks[0]
            country_code = first_chunk.country_code.strip().upper()

            # Mission "ORDER 5C", section 10: the allowlist check runs
            # AFTER country detection/normalization but BEFORE any
            # mutation - no source commit, no OpenSearch write, no
            # staging durable, no country_lock acquired yet. The
            # TemporaryDirectory context above (still open here) means
            # the staged upload is cleaned up automatically the moment
            # this raises, exactly like every other pre-mutation
            # validation failure in this same function.
            if not is_admin_country_allowed(country_code):
                raise AdminDocumentCountryNotAllowedError(
                    country=first_chunk.country,
                    country_code=country_code,
                )

            topic_warning = evaluate_topic_coverage(chunks)

            # Everything from here on reads-then-mutates shared,
            # country-scoped state (the OpenSearch catalog and the
            # source filesystem) - held under one lock so two
            # concurrent uploads for the SAME country can never both
            # observe "no existing document" and both proceed (mission
            # "ORDER 3", section 17). chunk_builder above never touches
            # that shared state, so it deliberately runs outside the
            # lock.
            with country_lock(
                source_directory,
                country_code,
                timeout_seconds=lock_timeout_seconds,
            ):
                try:
                    existing_documents = country_document_lookup(
                        country_code,
                        client,
                    )

                except OpenSearchException as error:
                    raise AdminDocumentStorageError(
                        "The existing country catalog could not be "
                        "checked before upload."
                    ) from error

                existing_paths = resolve_country_source_paths(
                    source_root=source_directory,
                    country_code=country_code,
                    source_filenames=[
                        document.source_filename
                        for document in existing_documents
                    ],
                )

                unique_document_ids = {
                    document.document_id
                    for document in existing_documents
                }

                if (
                    len(unique_document_ids) == 1
                    and len(existing_paths) == 1
                    and _sha256_file(staged_path)
                    == _sha256_file(existing_paths[0])
                ):
                    raise AdminDocumentAlreadyCurrentError(
                        country=first_chunk.country,
                        country_code=country_code,
                    )

                replacement_pending = (
                    bool(existing_documents)
                    and not replace_existing
                )

                if (
                    topic_warning is not None
                    and not confirm_warnings
                ):
                    raise AdminDocumentWarningConfirmationRequiredError(
                        country=first_chunk.country,
                        country_code=country_code,
                        warnings=[topic_warning],
                        replacement_required=replacement_pending,
                        existing_document_ids=sorted(
                            unique_document_ids
                        ),
                    )

                if replacement_pending:
                    raise AdminDocumentReplacementRequiredError(
                        country=first_chunk.country,
                        country_code=country_code,
                        existing_documents=existing_documents,
                    )

                operation_id = uuid.uuid4().hex
                storage_filename = storage_filename_for_country(
                    country_code
                )
                final_path = source_directory / storage_filename
                incoming_path = (
                    source_directory
                    / f".{operation_id}.{storage_filename}.incoming"
                )
                backups: list[tuple[Path, Path]] = []
                new_final_installed = False

                shutil.copyfile(staged_path, incoming_path)

                try:
                    for existing_path in existing_paths:
                        backup_path = (
                            existing_path.parent
                            / (
                                f".{operation_id}."
                                f"{existing_path.name}.backup"
                            )
                        )

                        os.replace(
                            existing_path,
                            backup_path,
                        )
                        backups.append(
                            (
                                existing_path,
                                backup_path,
                            )
                        )

                    os.replace(
                        incoming_path,
                        final_path,
                    )
                    new_final_installed = True

                    indexing_result = country_document_indexer(
                        chunks=chunks,
                        client=client,
                    )

                except Exception:
                    if new_final_installed:
                        _safe_unlink(final_path)

                    _safe_unlink(incoming_path)
                    _restore_backups(backups)
                    raise

                for _, backup_path in backups:
                    _safe_unlink(backup_path)

                # Mission "ORDER 5C", section 34: a CONFIRMED replace
                # is a full country document reset - every persisted
                # edit belonging to the document(s) just replaced must
                # be gone, so it can never silently reapply to the new
                # DOCX (document_id is deterministic by country_code +
                # family + language, so the new document commonly
                # reuses the very same id the old edits were keyed
                # under). Only reached once the new document is fully
                # and successfully indexed - a fresh upload with no
                # existing_documents has nothing to clear.
                if existing_documents:
                    try:
                        for old_document_id in unique_document_ids:
                            delete_section_edit_state(
                                source_directory,
                                old_document_id,
                            )

                    except OSError as error:
                        raise AdminDocumentStorageError(
                            "The document was replaced, but its "
                            "previous section-edit state could not "
                            "be fully cleared."
                        ) from error

            return AdminDocumentUploadResponse(
                status=(
                    "replaced"
                    if existing_documents
                    else "uploaded"
                ),
                document_id=indexing_result.document_id,
                source_filename=safe_filename,
                country=first_chunk.country,
                country_code=country_code,
                reference_year=first_chunk.reference_year,
                document_family=DOCUMENT_FAMILY,
                uploaded_bytes=uploaded_bytes,
                indexed_chunks=indexing_result.indexed_chunks,
                stale_chunks_deleted=(
                    indexing_result.stale_chunks_deleted
                ),
                replaced_source_file=bool(existing_paths),
                replaced_document_ids=sorted(
                    unique_document_ids
                ),
            )

    except (
        InvalidDocumentUploadError,
        AdminDocumentReplacementRequiredError,
        AdminDocumentAlreadyCurrentError,
        ValueError,
    ):
        raise

    except OSError as error:
        raise AdminDocumentStorageError(
            "The uploaded document could not be persisted safely."
        ) from error
