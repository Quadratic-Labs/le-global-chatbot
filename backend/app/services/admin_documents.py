"""Validate, persist, index, and list legal DOCX documents."""

from __future__ import annotations

import os
import shutil
import tempfile
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import (
    Any,
    BinaryIO,
    Final,
)

from opensearchpy import OpenSearch
from opensearchpy.exceptions import (
    OpenSearchException,
)

from app.clients.opensearch import (
    get_opensearch_client,
)
from app.models.admin_documents import (
    AdminDocumentListResponse,
    AdminDocumentSummary,
    AdminDocumentUploadResponse,
)
from app.models.document import DocumentChunk
from app.services.document_chunk_builder import (
    DOCUMENT_FAMILY,
    build_document_chunks_from_docx,
    storage_filename_for_country,
)
from app.services.document_indexer import (
    DocumentIndexingResult,
    replace_document_chunks,
)
from app.services.document_source_resolver import (
    DocumentSourceConflictError,
    resolve_document_source_path,
)
from app.services.opensearch_index import (
    LEGAL_DOCUMENTS_ALIAS,
)


UPLOAD_READ_SIZE: Final[int] = 1024 * 1024
MAX_ADMIN_DOCUMENTS: Final[int] = 1000

# No project-wide filename length limit existed before this mission;
# 255 is the conservative, standard filesystem-safe ceiling (the
# common ext4/NTFS/APFS per-component limit), applied here since the
# filename itself is now an arbitrary, safety-checked string rather
# than a fixed business format (mission "CONTINUATION PATCH 0.4.3").
MAX_FILENAME_LENGTH: Final[int] = 255


ChunkBuilder = Callable[
    [Path],
    list[DocumentChunk],
]

DocumentIndexer = Callable[
    ...,
    DocumentIndexingResult,
]

ExistingSourceLookup = Callable[
    [str, "OpenSearch | None"],
    "str | None",
]


class InvalidDocumentUploadError(ValueError):
    """Raised when an uploaded document is invalid."""


class AdminDocumentStorageError(RuntimeError):
    """Raised when a document cannot be persisted safely."""


class AdminDocumentCatalogError(RuntimeError):
    """Raised when indexed documents cannot be listed."""


def _sanitize_filename(
    filename: str,
) -> str:
    """
    Validate an uploaded source filename for safety only - never for
    a business naming format (mission "CONTINUATION PATCH 0.4.3",
    section 4). Any filename that is non-empty, ends in .docx, has no
    null byte, no path component, and stays within a reasonable
    length is accepted verbatim - spaces, accents, parentheses,
    dashes, and underscores all included.
    """

    if "\x00" in filename:
        raise InvalidDocumentUploadError(
            "The uploaded filename must not contain a null byte."
        )

    normalized_filename = filename.strip()

    if not normalized_filename:
        raise InvalidDocumentUploadError(
            "The uploaded document has no filename."
        )

    if len(normalized_filename) > MAX_FILENAME_LENGTH:
        raise InvalidDocumentUploadError(
            "The uploaded filename is too long."
        )

    basename = (
        normalized_filename
        .replace("\\", "/")
        .rsplit("/", maxsplit=1)[-1]
    )

    if basename != normalized_filename:
        raise InvalidDocumentUploadError(
            "The uploaded filename must not contain a path."
        )

    if basename.startswith("~$"):
        raise InvalidDocumentUploadError(
            "Temporary Microsoft Word files are not accepted."
        )

    if Path(basename).suffix.casefold() != ".docx":
        raise InvalidDocumentUploadError(
            "Only DOCX documents are accepted."
        )

    return basename


def _write_upload(
    file_stream: BinaryIO,
    destination: Path,
    maximum_bytes: int,
) -> int:
    """Stream an uploaded file to disk with a size limit."""

    if maximum_bytes <= 0:
        raise ValueError(
            "maximum_bytes must be greater than zero."
        )

    try:
        file_stream.seek(0)

    except (AttributeError, OSError):
        pass

    written_bytes = 0

    with destination.open("wb") as output_file:
        while True:
            data = file_stream.read(
                UPLOAD_READ_SIZE
            )

            if not data:
                break

            if not isinstance(
                data,
                bytes,
            ):
                raise InvalidDocumentUploadError(
                    "The uploaded document did not "
                    "contain binary data."
                )

            written_bytes += len(
                data
            )

            if written_bytes > maximum_bytes:
                raise InvalidDocumentUploadError(
                    "The uploaded DOCX exceeds "
                    "the configured size limit."
                )

            output_file.write(
                data
            )

    if written_bytes == 0:
        raise InvalidDocumentUploadError(
            "The uploaded DOCX is empty."
        )

    return written_bytes


def _safe_unlink(
    path: Path,
) -> None:
    """Remove a temporary file when it exists."""

    try:
        path.unlink(
            missing_ok=True
        )

    except OSError:
        pass


def _lookup_existing_source_filename(
    document_id: str,
    client: OpenSearch | None,
) -> str | None:
    """
    Return the source_filename of an already-indexed document sharing
    this exact deterministic document_id, or None when no such
    document exists yet.

    This is the only way an upload can learn a pre-existing, possibly
    legacy document's historical on-disk filename before writing a
    replacement - source_directory is never scanned (mission "HOTFIX
    0.4.4", section 4).
    """

    opensearch_client = (
        client
        if client is not None
        else get_opensearch_client()
    )

    response = opensearch_client.search(
        index=LEGAL_DOCUMENTS_ALIAS,
        body={
            "size": 1,
            "_source": ["source_filename"],
            "query": {
                "term": {
                    "document_id": document_id,
                }
            },
        },
    )

    if not isinstance(response, dict):
        return None

    hits_container = response.get("hits")

    if not isinstance(hits_container, dict):
        return None

    hits = hits_container.get("hits")

    if not isinstance(hits, list) or not hits:
        return None

    first_hit = hits[0]

    if not isinstance(first_hit, dict):
        return None

    source = first_hit.get("_source")

    if not isinstance(source, dict):
        return None

    value = source.get("source_filename")

    if not isinstance(value, str) or not value.strip():
        return None

    return value.strip()


def upload_and_index_document(
    *,
    filename: str,
    file_stream: BinaryIO,
    source_directory: Path,
    processed_directory: Path,
    maximum_bytes: int,
    client: OpenSearch | None = None,
    chunk_builder: ChunkBuilder = (
        build_document_chunks_from_docx
    ),
    document_indexer: DocumentIndexer = (
        replace_document_chunks
    ),
    existing_source_lookup: ExistingSourceLookup = (
        _lookup_existing_source_filename
    ),
) -> AdminDocumentUploadResponse:
    """
    Validate, persist, and index one DOCX document.

    The previous source file is restored if indexing fails.
    """

    safe_filename = _sanitize_filename(
        filename
    )

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
                chunks = chunk_builder(
                    staged_path
                )

            except Exception as error:
                raise InvalidDocumentUploadError(
                    "DOCX validation failed: "
                    f"{error}"
                ) from error

            if not chunks:
                raise InvalidDocumentUploadError(
                    "The uploaded DOCX produced no legal chunks."
                )

            # Stored on disk under a name derived from the document's
            # own detected country - never the user-supplied filename
            # (mission "CONTINUATION PATCH 0.4.3", section 10). Two
            # unrelated uploads that happen to share the exact same
            # original filename (e.g. both "final.docx", one for
            # Canada and one for Spain) must never overwrite each
            # other's stored source file; keying storage by country
            # also keeps it consistent with document identity itself
            # (see document_chunk_builder._build_document_id), so a
            # new upload for a country already active always lands
            # on the exact same storage path it is meant to replace.
            country_code = chunks[0].country_code
            storage_filename = storage_filename_for_country(
                country_code
            )

            # A country already active before country-keyed storage
            # existed is still physically stored under its own
            # historical filename (mission "HOTFIX 0.4.4") - looked up
            # by this upload's own deterministic document_id (never by
            # scanning source_directory), so the resolver finds that
            # file too and replacing it backs up and restores the
            # real active file, never just a {COUNTRY_CODE}.docx path
            # that may not exist yet.
            try:
                existing_source_filename = existing_source_lookup(
                    chunks[0].document_id,
                    client,
                )

            except OpenSearchException as error:
                raise AdminDocumentStorageError(
                    "The existing document catalog could not be "
                    "checked before upload."
                ) from error

            try:
                existing_source = resolve_document_source_path(
                    source_root=source_directory,
                    country_code=country_code,
                    source_filename=existing_source_filename,
                )

            except DocumentSourceConflictError as error:
                raise InvalidDocumentUploadError(
                    "Multiple distinct source files already exist "
                    "for this country; resolve the conflict "
                    "manually before uploading a replacement."
                ) from error

            existing_active_path = existing_source.path
            replaced_source_file = existing_active_path is not None

            operation_id = uuid.uuid4().hex

            final_path = (
                source_directory
                / storage_filename
            )

            incoming_path = (
                source_directory
                / (
                    f".{operation_id}."
                    f"{storage_filename}.incoming"
                )
            )

            backup_path = (
                (
                    existing_active_path.parent
                    / (
                        f".{operation_id}."
                        f"{existing_active_path.name}.backup"
                    )
                )
                if existing_active_path is not None
                else None
            )

            shutil.copyfile(
                staged_path,
                incoming_path,
            )

            try:
                if existing_active_path is not None:
                    os.replace(
                        existing_active_path,
                        backup_path,
                    )

                os.replace(
                    incoming_path,
                    final_path,
                )

                indexing_result = (
                    document_indexer(
                        chunks=chunks,
                        client=client,
                    )
                )

            except Exception:
                _safe_unlink(
                    final_path
                )

                if (
                    backup_path is not None
                    and backup_path.exists()
                ):
                    os.replace(
                        backup_path,
                        existing_active_path,
                    )

                _safe_unlink(
                    incoming_path
                )

                raise

            if backup_path is not None:
                _safe_unlink(
                    backup_path
                )

            first_chunk = chunks[0]

            return AdminDocumentUploadResponse(
                status="indexed",
                document_id=(
                    indexing_result.document_id
                ),
                source_filename=safe_filename,
                country=first_chunk.country,
                country_code=(
                    first_chunk.country_code
                ),
                reference_year=(
                    first_chunk.reference_year
                ),
                document_family=DOCUMENT_FAMILY,
                uploaded_bytes=uploaded_bytes,
                indexed_chunks=(
                    indexing_result.indexed_chunks
                ),
                stale_chunks_deleted=(
                    indexing_result
                    .stale_chunks_deleted
                ),
                replaced_source_file=(
                    replaced_source_file
                ),
            )

    except (
        InvalidDocumentUploadError,
        ValueError,
    ):
        raise

    except OSError as error:
        raise AdminDocumentStorageError(
            "The uploaded document could not "
            "be persisted safely."
        ) from error


def build_admin_document_catalog_body(
) -> dict[str, Any]:
    """Build the indexed-document aggregation request."""

    return {
        "size": 0,
        "aggs": {
            "documents": {
                "terms": {
                    "field": "document_id",
                    "size": MAX_ADMIN_DOCUMENTS,
                    "order": {
                        "_key": "asc",
                    },
                },
                "aggs": {
                    "metadata": {
                        "top_hits": {
                            "size": 1,
                            "_source": [
                                "document_id",
                                "source_filename",
                                "country",
                                "country_code",
                                "language",
                                "document_type",
                                "reference_year",
                            ],
                        }
                    }
                },
            }
        },
    }


def _extract_metadata_source(
    bucket: dict[str, Any],
) -> dict[str, Any]:
    """Extract document metadata from a top-hits bucket."""

    metadata = bucket.get(
        "metadata"
    )

    if not isinstance(
        metadata,
        dict,
    ):
        raise AdminDocumentCatalogError(
            "OpenSearch returned invalid "
            "document metadata."
        )

    hits_container = metadata.get(
        "hits"
    )

    if not isinstance(
        hits_container,
        dict,
    ):
        raise AdminDocumentCatalogError(
            "OpenSearch returned invalid "
            "document metadata hits."
        )

    hits = hits_container.get(
        "hits"
    )

    if (
        not isinstance(hits, list)
        or not hits
        or not isinstance(hits[0], dict)
    ):
        raise AdminDocumentCatalogError(
            "OpenSearch returned no document metadata."
        )

    source = hits[0].get(
        "_source"
    )

    if not isinstance(
        source,
        dict,
    ):
        raise AdminDocumentCatalogError(
            "OpenSearch returned invalid "
            "document source metadata."
        )

    return source


def _required_string(
    source: dict[str, Any],
    field: str,
) -> str:
    """Read one required string metadata field."""

    value = source.get(
        field
    )

    if not isinstance(
        value,
        str,
    ) or not value.strip():
        raise AdminDocumentCatalogError(
            f"Document metadata field is invalid: {field}"
        )

    return value.strip()


def list_indexed_documents(
    *,
    source_directory: Path,
    client: OpenSearch | None = None,
) -> AdminDocumentListResponse:
    """Return one administration row per indexed document."""

    opensearch_client = (
        client
        if client is not None
        else get_opensearch_client()
    )

    try:
        response = opensearch_client.search(
            index=LEGAL_DOCUMENTS_ALIAS,
            body=build_admin_document_catalog_body(),
        )

    except OpenSearchException as error:
        raise AdminDocumentCatalogError(
            "OpenSearch document catalog request failed."
        ) from error

    if not isinstance(
        response,
        dict,
    ):
        raise AdminDocumentCatalogError(
            "OpenSearch returned an invalid response."
        )

    aggregations = response.get(
        "aggregations"
    )

    if not isinstance(
        aggregations,
        dict,
    ):
        raise AdminDocumentCatalogError(
            "OpenSearch returned no aggregations."
        )

    documents_aggregation = aggregations.get(
        "documents"
    )

    if not isinstance(
        documents_aggregation,
        dict,
    ):
        raise AdminDocumentCatalogError(
            "OpenSearch returned no document aggregation."
        )

    buckets = documents_aggregation.get(
        "buckets"
    )

    if not isinstance(
        buckets,
        list,
    ):
        raise AdminDocumentCatalogError(
            "OpenSearch returned invalid document buckets."
        )

    documents: list[
        AdminDocumentSummary
    ] = []

    for bucket in buckets:
        if not isinstance(
            bucket,
            dict,
        ):
            raise AdminDocumentCatalogError(
                "OpenSearch returned an invalid document bucket."
            )

        source = _extract_metadata_source(
            bucket
        )

        source_filename = _required_string(
            source,
            "source_filename",
        )

        country_code = _required_string(
            source,
            "country_code",
        )

        # Resolved centrally (mission "HOTFIX 0.4.4"): a document
        # indexed before country-keyed storage existed is still
        # physically stored under its own historical source_filename,
        # never under storage_filename_for_country's canonical name -
        # the resolver checks both, so a legacy document is never
        # reported as missing.
        try:
            resolved_source = resolve_document_source_path(
                source_root=source_directory,
                country_code=country_code,
                source_filename=source_filename,
            )

            source_file_present = (
                resolved_source.path is not None
            )

            document_status = (
                "indexed"
                if source_file_present
                else "indexed_source_missing"
            )

        except DocumentSourceConflictError:
            source_file_present = False
            document_status = "indexed_source_conflict"

        reference_year = source.get(
            "reference_year"
        )

        if reference_year is not None:
            reference_year = int(
                reference_year
            )

        documents.append(
            AdminDocumentSummary(
                document_id=_required_string(
                    source,
                    "document_id",
                ),
                source_filename=source_filename,
                country=_required_string(
                    source,
                    "country",
                ),
                country_code=_required_string(
                    source,
                    "country_code",
                ),
                language=_required_string(
                    source,
                    "language",
                ),
                document_type=_required_string(
                    source,
                    "document_type",
                ),
                reference_year=reference_year,
                chunk_count=int(
                    bucket.get(
                        "doc_count",
                        0,
                    )
                ),
                source_file_present=(
                    source_file_present
                ),
                status=document_status,
            )
        )

    documents.sort(
        key=lambda document: (
            document.country.casefold(),
            document.source_filename.casefold(),
        )
    )

    return AdminDocumentListResponse(
        total=len(
            documents
        ),
        documents=documents,
    )