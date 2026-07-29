"""Reindex and delete managed legal documents safely."""

from __future__ import annotations

import os
import re
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final

from opensearchpy import OpenSearch
from opensearchpy.exceptions import (
    OpenSearchException,
)

from app.clients.opensearch import (
    get_opensearch_client,
)
from app.models.admin_document_lifecycle import (
    AdminDocumentDeleteResponse,
    AdminDocumentReindexResponse,
)
from app.models.document import DocumentChunk
from app.services.document_chunk_builder import (
    build_document_chunks_from_docx,
)
from app.services.document_indexer import (
    DocumentIndexingResult,
    replace_document_chunks,
)
from app.services.opensearch_index import (
    LEGAL_DOCUMENTS_ALIAS,
)


DOCUMENT_ID_PATTERN: Final[
    re.Pattern[str]
] = re.compile(
    r"^doc_[0-9a-f]{64}$"
)


DOCUMENT_METADATA_FIELDS: Final[
    list[str]
] = [
    "document_id",
    "source_filename",
    "country",
    "country_code",
    "reference_year",
]


ChunkBuilder = Callable[
    [Path],
    list[DocumentChunk],
]

DocumentIndexer = Callable[
    ...,
    DocumentIndexingResult,
]


class InvalidAdminDocumentIdError(ValueError):
    """Raised when a document identifier is invalid."""


class AdminDocumentNotFoundError(LookupError):
    """Raised when an indexed document cannot be found."""


class AdminDocumentSourceMissingError(FileNotFoundError):
    """Raised when the indexed source DOCX is missing."""


class AdminDocumentLifecycleError(RuntimeError):
    """Raised when a document lifecycle operation fails."""


def _validate_document_id(
    document_id: str,
) -> str:
    """Validate one deterministic document identifier."""

    normalized_document_id = (
        document_id.strip()
    )

    if not DOCUMENT_ID_PATTERN.fullmatch(
        normalized_document_id
    ):
        raise InvalidAdminDocumentIdError(
            "The document identifier is invalid."
        )

    return normalized_document_id


def _get_client(
    client: OpenSearch | None,
) -> OpenSearch:
    """Return the supplied or configured OpenSearch client."""

    return (
        client
        if client is not None
        else get_opensearch_client()
    )


def _required_string(
    source: dict[str, Any],
    field: str,
) -> str:
    """Read one required string field."""

    value = source.get(
        field
    )

    if (
        not isinstance(value, str)
        or not value.strip()
    ):
        raise AdminDocumentLifecycleError(
            "Indexed document metadata is invalid: "
            f"{field}."
        )

    return value.strip()


def _get_document_metadata(
    *,
    document_id: str,
    client: OpenSearch,
) -> dict[str, Any]:
    """Return metadata for one indexed document."""

    try:
        response = client.search(
            index=LEGAL_DOCUMENTS_ALIAS,
            body={
                "size": 1,
                "_source": (
                    DOCUMENT_METADATA_FIELDS
                ),
                "query": {
                    "term": {
                        "document_id": document_id,
                    }
                },
            },
        )

    except OpenSearchException as error:
        raise AdminDocumentLifecycleError(
            "OpenSearch document lookup failed."
        ) from error

    if not isinstance(
        response,
        dict,
    ):
        raise AdminDocumentLifecycleError(
            "OpenSearch returned an invalid "
            "document lookup response."
        )

    hits_container = response.get(
        "hits"
    )

    if not isinstance(
        hits_container,
        dict,
    ):
        raise AdminDocumentLifecycleError(
            "OpenSearch returned invalid "
            "document lookup results."
        )

    hits = hits_container.get(
        "hits"
    )

    if not isinstance(
        hits,
        list,
    ):
        raise AdminDocumentLifecycleError(
            "OpenSearch returned an invalid "
            "document hit list."
        )

    if not hits:
        raise AdminDocumentNotFoundError(
            "The indexed document was not found."
        )

    first_hit = hits[0]

    if not isinstance(
        first_hit,
        dict,
    ):
        raise AdminDocumentLifecycleError(
            "OpenSearch returned an invalid "
            "document hit."
        )

    source = first_hit.get(
        "_source"
    )

    if not isinstance(
        source,
        dict,
    ):
        raise AdminDocumentLifecycleError(
            "OpenSearch returned invalid "
            "document metadata."
        )

    indexed_document_id = _required_string(
        source,
        "document_id",
    )

    if indexed_document_id != document_id:
        raise AdminDocumentLifecycleError(
            "OpenSearch returned metadata for "
            "a different document."
        )

    return source


def _resolve_source_path(
    *,
    source_directory: Path,
    source_filename: str,
) -> Path:
    """Resolve a safe source file path."""

    if Path(
        source_filename
    ).name != source_filename:
        raise AdminDocumentLifecycleError(
            "Indexed source filename is unsafe."
        )

    resolved_source_directory = (
        source_directory.resolve()
    )

    source_path = (
        resolved_source_directory
        / source_filename
    ).resolve()

    if (
        source_path.parent
        != resolved_source_directory
    ):
        raise AdminDocumentLifecycleError(
            "Indexed source path is unsafe."
        )

    return source_path


def _delete_document_chunks(
    *,
    document_id: str,
    client: OpenSearch,
) -> int:
    """Delete every indexed chunk belonging to one document."""

    try:
        response = client.delete_by_query(
            index=LEGAL_DOCUMENTS_ALIAS,
            body={
                "query": {
                    "term": {
                        "document_id": document_id,
                    }
                }
            },
            conflicts="proceed",
            refresh=True,
        )

    except OpenSearchException as error:
        raise AdminDocumentLifecycleError(
            "OpenSearch document deletion failed."
        ) from error

    if not isinstance(
        response,
        dict,
    ):
        raise AdminDocumentLifecycleError(
            "OpenSearch returned an invalid "
            "document deletion response."
        )

    try:
        return int(
            response.get(
                "deleted",
                0,
            )
        )

    except (
        TypeError,
        ValueError,
    ) as error:
        raise AdminDocumentLifecycleError(
            "OpenSearch returned an invalid "
            "deleted chunk count."
        ) from error


def reindex_indexed_document(
    *,
    document_id: str,
    source_directory: Path,
    client: OpenSearch | None = None,
    chunk_builder: ChunkBuilder = (
        build_document_chunks_from_docx
    ),
    document_indexer: DocumentIndexer = (
        replace_document_chunks
    ),
) -> AdminDocumentReindexResponse:
    """
    Rebuild and replace an indexed document from its source DOCX.

    When the metadata changes enough to produce a new document ID,
    the previous indexed document is removed after the new version
    has been indexed successfully.
    """

    validated_document_id = (
        _validate_document_id(
            document_id
        )
    )

    opensearch_client = _get_client(
        client
    )

    metadata = _get_document_metadata(
        document_id=validated_document_id,
        client=opensearch_client,
    )

    source_filename = _required_string(
        metadata,
        "source_filename",
    )

    source_path = _resolve_source_path(
        source_directory=source_directory,
        source_filename=source_filename,
    )

    if not source_path.is_file():
        raise AdminDocumentSourceMissingError(
            "The source DOCX file is missing."
        )

    try:
        chunks = chunk_builder(
            source_path
        )

    except Exception as error:
        raise AdminDocumentLifecycleError(
            "The source DOCX could not be parsed."
        ) from error

    if not chunks:
        raise AdminDocumentLifecycleError(
            "The source DOCX produced no legal chunks."
        )

    indexing_result = document_indexer(
        chunks=chunks,
        client=opensearch_client,
    )

    current_document_id = (
        indexing_result.document_id
    )

    document_id_changed = (
        current_document_id
        != validated_document_id
    )

    previous_chunks_deleted = 0

    if document_id_changed:
        try:
            previous_chunks_deleted = (
                _delete_document_chunks(
                    document_id=(
                        validated_document_id
                    ),
                    client=opensearch_client,
                )
            )

            if previous_chunks_deleted <= 0:
                raise AdminDocumentLifecycleError(
                    "The previous indexed document "
                    "could not be removed."
                )

        except Exception:
            try:
                _delete_document_chunks(
                    document_id=current_document_id,
                    client=opensearch_client,
                )

            except Exception:
                pass

            raise

    first_chunk = chunks[0]

    return AdminDocumentReindexResponse(
        status="reindexed",
        previous_document_id=(
            validated_document_id
        ),
        document_id=current_document_id,
        document_id_changed=(
            document_id_changed
        ),
        source_filename=source_filename,
        country=first_chunk.country,
        country_code=(
            first_chunk.country_code
        ),
        reference_year=(
            first_chunk.reference_year
        ),
        indexed_chunks=(
            indexing_result.indexed_chunks
        ),
        stale_chunks_deleted=(
            indexing_result.stale_chunks_deleted
        ),
        previous_chunks_deleted=(
            previous_chunks_deleted
        ),
    )


def delete_indexed_document(
    *,
    document_id: str,
    source_directory: Path,
    processed_directory: Path,
    client: OpenSearch | None = None,
) -> AdminDocumentDeleteResponse:
    """
    Delete an indexed document and its source DOCX safely.

    When the source exists, it is temporarily moved before the
    OpenSearch deletion and restored if the deletion fails.
    """

    validated_document_id = (
        _validate_document_id(
            document_id
        )
    )

    opensearch_client = _get_client(
        client
    )

    metadata = _get_document_metadata(
        document_id=validated_document_id,
        client=opensearch_client,
    )

    source_filename = _required_string(
        metadata,
        "source_filename",
    )

    source_path = _resolve_source_path(
        source_directory=source_directory,
        source_filename=source_filename,
    )

    source_file_present = (
        source_path.is_file()
    )

    backup_path: Path | None = None

    if source_file_present:
        try:
            processed_directory.mkdir(
                parents=True,
                exist_ok=True,
            )

            backup_path = (
                processed_directory
                / (
                    ".delete-backup-"
                    f"{uuid.uuid4().hex}-"
                    f"{source_filename}"
                )
            )

            os.replace(
                source_path,
                backup_path,
            )

        except OSError as error:
            raise AdminDocumentLifecycleError(
                "The source DOCX could not be "
                "prepared for deletion."
            ) from error

    try:
        deleted_chunks = (
            _delete_document_chunks(
                document_id=(
                    validated_document_id
                ),
                client=opensearch_client,
            )
        )

        if deleted_chunks <= 0:
            raise AdminDocumentLifecycleError(
                "No indexed chunk was deleted."
            )

    except Exception:
        if (
            backup_path is not None
            and backup_path.exists()
        ):
            try:
                os.replace(
                    backup_path,
                    source_path,
                )

            except OSError:
                pass

        raise

    if (
        backup_path is not None
        and backup_path.exists()
    ):
        try:
            backup_path.unlink()

        except OSError as error:
            raise AdminDocumentLifecycleError(
                "The indexed document was deleted, "
                "but its source backup could not "
                "be removed."
            ) from error

    return AdminDocumentDeleteResponse(
        status="deleted",
        document_id=validated_document_id,
        source_filename=source_filename,
        deleted_chunks=deleted_chunks,
        source_file_deleted=(
            source_file_present
        ),
    )