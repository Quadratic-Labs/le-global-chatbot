"""Index validated legal document chunks into OpenSearch."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Any, Final

from opensearchpy import OpenSearch
from opensearchpy.helpers import bulk

from app.clients.opensearch import (
    get_opensearch_client,
)
from app.models.document import DocumentChunk
from app.services.opensearch_index import (
    LEGAL_DOCUMENTS_ALIAS,
    ensure_legal_documents_index,
)


DEFAULT_BULK_CHUNK_SIZE: Final[int] = 200


class DocumentIndexingError(RuntimeError):
    """Raised when OpenSearch indexing does not complete successfully."""


class InvalidDocumentChunksError(ValueError):
    """Raised when chunks do not represent one valid source document."""


@dataclass(frozen=True, slots=True)
class DocumentIndexingResult:
    """Result of replacing one document in OpenSearch."""

    index_alias: str
    document_id: str
    source_filename: str
    requested_chunks: int
    indexed_chunks: int
    stale_chunks_deleted: int

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-serializable representation."""

        return {
            "index_alias": self.index_alias,
            "document_id": self.document_id,
            "source_filename": self.source_filename,
            "requested_chunks": self.requested_chunks,
            "indexed_chunks": self.indexed_chunks,
            "stale_chunks_deleted": self.stale_chunks_deleted,
        }


def _validate_chunks(
    chunks: Sequence[DocumentChunk],
) -> None:
    """
    Validate that chunks belong to exactly one logical document.

    Country, filename, language, and document ID must be consistent.
    Chunk IDs must be unique.
    """

    if not chunks:
        raise InvalidDocumentChunksError(
            "At least one DocumentChunk is required."
        )

    document_ids = {
        chunk.document_id
        for chunk in chunks
    }

    if len(document_ids) != 1:
        raise InvalidDocumentChunksError(
            "All chunks must have the same document_id."
        )

    source_filenames = {
        chunk.source_filename
        for chunk in chunks
    }

    if len(source_filenames) != 1:
        raise InvalidDocumentChunksError(
            "All chunks must have the same source_filename."
        )

    countries = {
        (
            chunk.country,
            chunk.country_code,
        )
        for chunk in chunks
    }

    if len(countries) != 1:
        raise InvalidDocumentChunksError(
            "All chunks must have the same country metadata."
        )

    languages = {
        chunk.language
        for chunk in chunks
    }

    if len(languages) != 1:
        raise InvalidDocumentChunksError(
            "All chunks must have the same language."
        )

    chunk_id_counts = Counter(
        chunk.chunk_id
        for chunk in chunks
    )

    duplicate_chunk_ids = sorted(
        chunk_id
        for chunk_id, count in chunk_id_counts.items()
        if count > 1
    )

    if duplicate_chunk_ids:
        raise InvalidDocumentChunksError(
            "Duplicate chunk IDs detected: "
            + ", ".join(
                duplicate_chunk_ids
            )
        )


def _build_bulk_actions(
    chunks: Sequence[DocumentChunk],
) -> Iterator[dict[str, Any]]:
    """Yield one OpenSearch index action per chunk."""

    for chunk in chunks:
        yield {
            "_op_type": "index",
            "_index": LEGAL_DOCUMENTS_ALIAS,
            "_id": chunk.chunk_id,
            "_source": chunk.to_document(),
        }


def _summarize_bulk_errors(
    errors: Sequence[dict[str, Any]],
) -> str:
    """Build a compact error message without exposing full legal text."""

    summaries: list[str] = []

    for error_item in errors[:5]:
        if not error_item:
            summaries.append(
                "Unknown OpenSearch bulk error."
            )
            continue

        operation, details = next(
            iter(
                error_item.items()
            )
        )

        if not isinstance(
            details,
            dict,
        ):
            summaries.append(
                f"{operation}: {details}"
            )
            continue

        chunk_id = details.get(
            "_id",
            "<unknown>",
        )

        status = details.get(
            "status",
            "<unknown>",
        )

        error_details = details.get(
            "error",
            {},
        )

        if isinstance(
            error_details,
            dict,
        ):
            error_type = error_details.get(
                "type",
                "unknown_error",
            )

            reason = error_details.get(
                "reason",
                "No reason returned.",
            )

        else:
            error_type = "unknown_error"
            reason = str(
                error_details
            )

        summaries.append(
            f"{operation} "
            f"id={chunk_id} "
            f"status={status} "
            f"type={error_type} "
            f"reason={reason}"
        )

    remaining_errors = (
        len(errors)
        - len(summaries)
    )

    if remaining_errors > 0:
        summaries.append(
            f"{remaining_errors} additional error(s)."
        )

    return " | ".join(
        summaries
    )


def _delete_stale_chunks(
    client: OpenSearch,
    document_id: str,
    current_chunk_ids: Sequence[str],
) -> int:
    """
    Delete chunks from an older version of the same document.

    Current chunks are indexed first. Stale chunks are deleted only
    after the complete bulk operation succeeds.
    """

    response = client.delete_by_query(
        index=LEGAL_DOCUMENTS_ALIAS,
        body={
            "query": {
                "bool": {
                    "filter": [
                        {
                            "term": {
                                "document_id": document_id,
                            }
                        }
                    ],
                    "must_not": [
                        {
                            "terms": {
                                "chunk_id": list(
                                    current_chunk_ids
                                ),
                            }
                        }
                    ],
                }
            }
        },
        conflicts="proceed",
        refresh=True,
    )

    return int(
        response.get(
            "deleted",
            0,
        )
    )


def replace_document_chunks(
    chunks: Sequence[DocumentChunk],
    client: OpenSearch | None = None,
    bulk_chunk_size: int = DEFAULT_BULK_CHUNK_SIZE,
) -> DocumentIndexingResult:
    """
    Replace one complete legal document in OpenSearch.

    Behaviour:

    1. validates that all chunks belong to one document;
    2. creates the index and alias when necessary;
    3. indexes every current chunk with its deterministic chunk ID;
    4. stops immediately if one bulk item fails;
    5. deletes obsolete chunks from the previous document version.
    """

    chunk_list = list(
        chunks
    )

    _validate_chunks(
        chunk_list
    )

    if bulk_chunk_size <= 0:
        raise ValueError(
            "bulk_chunk_size must be greater than zero."
        )

    opensearch_client = (
        client
        if client is not None
        else get_opensearch_client()
    )

    ensure_legal_documents_index(
        client=opensearch_client
    )

    indexed_count, errors = bulk(
        client=opensearch_client,
        actions=_build_bulk_actions(
            chunk_list
        ),
        chunk_size=bulk_chunk_size,
        max_retries=3,
        initial_backoff=1,
        max_backoff=8,
        raise_on_error=False,
        raise_on_exception=False,
        refresh=True,
    )

    if errors:
        raise DocumentIndexingError(
            "OpenSearch bulk indexing failed for "
            f"{len(errors)} chunk(s): "
            f"{_summarize_bulk_errors(errors)}"
        )

    if indexed_count != len(
        chunk_list
    ):
        raise DocumentIndexingError(
            "OpenSearch returned an inconsistent indexed count: "
            f"expected {len(chunk_list)}, "
            f"received {indexed_count}."
        )

    document_id = (
        chunk_list[0].document_id
    )

    stale_chunks_deleted = _delete_stale_chunks(
        client=opensearch_client,
        document_id=document_id,
        current_chunk_ids=[
            chunk.chunk_id
            for chunk in chunk_list
        ],
    )

    return DocumentIndexingResult(
        index_alias=LEGAL_DOCUMENTS_ALIAS,
        document_id=document_id,
        source_filename=(
            chunk_list[0].source_filename
        ),
        requested_chunks=len(
            chunk_list
        ),
        indexed_chunks=int(
            indexed_count
        ),
        stale_chunks_deleted=stale_chunks_deleted,
    )