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
    NotFoundError,
    OpenSearchException,
)

from app.clients.opensearch import get_opensearch_client
from app.models.admin_documents import AdminDocumentUploadResponse
from app.models.document import DocumentChunk
from app.services.admin_documents import (
    AdminDocumentStorageError,
    InvalidDocumentUploadError,
    _safe_unlink,
    _sanitize_filename,
    _write_upload,
)
from app.services.document_chunk_builder import (
    DOCUMENT_FAMILY,
    build_document_chunks_from_docx,
    storage_filename_for_country,
)
from app.services.document_indexer import (
    DocumentIndexingResult,
    replace_country_document_chunks,
)
from app.services.document_source_resolver import (
    resolve_country_source_paths,
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
        response = opensearch_client.search(
            index=LEGAL_DOCUMENTS_ALIAS,
            body={
                "size": 10000,
                "_source": [
                    "document_id",
                    "source_filename",
                    "country",
                    "country_code",
                    "reference_year",
                ],
                "query": {
                    "term": {
                        "country_code": normalized_country_code,
                    }
                },
            },
        )

    except NotFoundError:
        return []

    except OpenSearchException:
        raise

    if not isinstance(response, dict):
        raise AdminDocumentStorageError(
            "OpenSearch returned an invalid country lookup response."
        )

    hits_container = response.get("hits")

    if not isinstance(hits_container, dict):
        raise AdminDocumentStorageError(
            "OpenSearch returned invalid country lookup results."
        )

    hits = hits_container.get("hits")

    if not isinstance(hits, list):
        raise AdminDocumentStorageError(
            "OpenSearch returned an invalid country hit list."
        )

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
    client: OpenSearch | None = None,
    chunk_builder: ChunkBuilder = build_document_chunks_from_docx,
    country_document_lookup: CountryDocumentLookup = (
        lookup_existing_country_documents
    ),
    country_document_indexer: CountryDocumentIndexer = (
        replace_country_document_chunks
    ),
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

            except Exception as error:
                raise InvalidDocumentUploadError(
                    f"DOCX validation failed: {error}"
                ) from error

            if not chunks:
                raise InvalidDocumentUploadError(
                    "The uploaded DOCX produced no legal chunks."
                )

            first_chunk = chunks[0]
            country_code = first_chunk.country_code.strip().upper()

            try:
                existing_documents = country_document_lookup(
                    country_code,
                    client,
                )

            except OpenSearchException as error:
                raise AdminDocumentStorageError(
                    "The existing country catalog could not be checked "
                    "before upload."
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

            if existing_documents and not replace_existing:
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
