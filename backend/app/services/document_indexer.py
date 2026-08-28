"""Index validated legal document chunks into OpenSearch."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Any, Final

from opensearchpy import OpenSearch
from opensearchpy.exceptions import (
    NotFoundError,
    OpenSearchException,
)
from opensearchpy.helpers import bulk

from app.clients.opensearch import (
    get_opensearch_client,
)
from app.models.document import DocumentChunk
from app.services.document_chunk_builder import (
    CONTACT_SUBSECTION,
)
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


def _delete_chunks_except(
    *,
    client: OpenSearch,
    filters: Sequence[dict[str, Any]],
    keep_chunk_ids: Sequence[str],
    context: str,
) -> int:
    """
    Delete every chunk matching every filter in `filters` whose
    chunk_id is not in `keep_chunk_ids`.

    Mission "ORDER 5C" corrective gate, section 4: the single, shared
    mechanism behind every staleness-cleanup AND every snapshot-
    restore operation in this module - only the filters and which
    chunk_ids to keep differ between callers. A raw OpenSearchException
    (or any malformed response) is never allowed to escape this
    boundary - every caller downstream (reindex, upload/replace,
    section edit) can rely on catching DocumentIndexingError alone,
    never a driver-level exception type.
    """

    query: dict[str, Any] = {
        "bool": {
            "filter": list(filters),
        }
    }

    if keep_chunk_ids:
        query["bool"]["must_not"] = [
            {
                "terms": {
                    "chunk_id": list(keep_chunk_ids),
                }
            }
        ]

    try:
        response = client.delete_by_query(
            index=LEGAL_DOCUMENTS_ALIAS,
            body={
                "query": query,
            },
            conflicts="proceed",
            refresh=True,
        )

    except OpenSearchException as error:
        raise DocumentIndexingError(
            f"OpenSearch cleanup failed ({context})."
        ) from error

    if not isinstance(response, dict):
        raise DocumentIndexingError(
            f"OpenSearch returned an invalid cleanup response ({context})."
        )

    try:
        return int(
            response.get(
                "deleted",
                0,
            )
        )

    except (TypeError, ValueError) as error:
        raise DocumentIndexingError(
            f"OpenSearch returned an invalid cleanup count ({context})."
        ) from error


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

    return _delete_chunks_except(
        client=client,
        filters=[
            {
                "term": {
                    "document_id": document_id,
                }
            }
        ],
        keep_chunk_ids=current_chunk_ids,
        context=f"document {document_id!r}",
    )


def replace_document_chunks(
    chunks: Sequence[DocumentChunk],
    client: OpenSearch | None = None,
    bulk_chunk_size: int = DEFAULT_BULK_CHUNK_SIZE,
) -> DocumentIndexingResult:
    """
    Replace one complete legal document in OpenSearch, atomically.

    Mission "ORDER 5C" corrective gate, section 4: identical
    transactional shape to replace_country_document_chunks and
    replace_document_section_chunks - the document is snapshotted
    BEFORE any mutation; new chunks are indexed; stale ones deleted;
    on ANY failure past the snapshot (a bulk error, or the stale-
    delete step itself failing), the document is restored to exactly
    that snapshot before the error is raised. reindex_indexed_document
    (the sole live caller) already wraps this in its own, broader,
    country-level rollback - this inner layer means that outer one is
    always restoring an already-consistent state, never masking a
    document left half-migrated.
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

    document_id = (
        chunk_list[0].document_id
    )

    document_snapshot = _snapshot_document_chunks(
        client=opensearch_client,
        document_id=document_id,
    )

    try:
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

        stale_chunks_deleted = _delete_stale_chunks(
            client=opensearch_client,
            document_id=document_id,
            current_chunk_ids=[
                chunk.chunk_id
                for chunk in chunk_list
            ],
        )

    except Exception as original_error:
        try:
            _restore_document_snapshot(
                client=opensearch_client,
                document_id=document_id,
                snapshot=document_snapshot,
                bulk_chunk_size=bulk_chunk_size,
            )

        except Exception as rollback_error:
            raise DocumentIndexingError(
                f"Document replacement failed for {document_id!r}, "
                "and its OpenSearch rollback also failed."
            ) from rollback_error

        if isinstance(
            original_error,
            DocumentIndexingError,
        ):
            raise

        raise DocumentIndexingError(
            f"OpenSearch document replacement failed for {document_id!r}."
        ) from original_error

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


def _delete_stale_section_chunks(
    client: OpenSearch,
    document_id: str,
    legal_topic: str,
    current_chunk_ids: Sequence[str],
) -> int:
    """
    Delete chunks from an older version of the same (document_id,
    legal_topic) section only - mission "ORDER 5C": scoped exactly
    like _delete_stale_chunks, but with an additional legal_topic
    filter, so an edit to ONE section can never delete or orphan any
    OTHER topic's chunks for the same document.
    """

    return _delete_chunks_except(
        client=client,
        filters=[
            {
                "term": {
                    "document_id": document_id,
                }
            },
            {
                "term": {
                    "legal_topic": legal_topic,
                }
            },
        ],
        keep_chunk_ids=current_chunk_ids,
        context=f"document {document_id!r} legal_topic {legal_topic!r}",
    )


def _snapshot_document_chunks(
    *,
    client: OpenSearch,
    document_id: str,
) -> list[dict[str, Any]]:
    """Capture all currently-indexed chunks for one document_id."""

    return _fetch_all_chunks(
        client=client,
        field="document_id",
        value=document_id,
    )


def _restore_document_snapshot(
    *,
    client: OpenSearch,
    document_id: str,
    snapshot: Sequence[dict[str, Any]],
    bulk_chunk_size: int,
) -> None:
    """
    Restore document_id to exactly the WHOLE-DOCUMENT chunk set
    captured in `snapshot` - every chunk currently indexed for this
    document_id that the snapshot does not contain is deleted, and
    every snapshot chunk is re-indexed. `snapshot` must be a complete
    document-level snapshot (every legal_topic this document_id had
    at capture time) - never a subset such as one section's own
    chunks alone (see replace_document_section_chunks's own,
    separately-scoped internal rollback for that case) - otherwise
    every OTHER section's chunks would be wiped as "not in snapshot".
    """

    _reindex_snapshot_chunks(
        client=client,
        snapshot=snapshot,
        bulk_chunk_size=bulk_chunk_size,
        context=f"document {document_id!r}",
    )

    _delete_chunks_except(
        client=client,
        filters=[
            {
                "term": {
                    "document_id": document_id,
                }
            }
        ],
        keep_chunk_ids=[item["_id"] for item in snapshot],
        context=f"document {document_id!r} snapshot restore",
    )


def _restore_section_snapshot(
    *,
    client: OpenSearch,
    document_id: str,
    legal_topic: str,
    snapshot: Sequence[dict[str, Any]],
    bulk_chunk_size: int,
) -> None:
    """
    Restore exactly one (document_id, legal_topic) section to the
    chunk set captured in `snapshot`: wipe whatever currently exists
    for this section (whatever mix of old/new chunks a failed
    mutation left behind) and reindex the snapshot. Deliberately
    scoped to document_id AND legal_topic together - never document-
    wide - so every OTHER section for this same document is left
    completely untouched. This is the shared rollback primitive
    behind BOTH replace_document_section_chunks's own internal
    atomicity AND update_effective_section's outer rollback when the
    durable state-file commit fails after OpenSearch already
    succeeded (mission "ORDER 5C" corrective gate, sections 1-2 and
    4: one shared, generic mechanism, not two near-identical hacks).
    """

    context = f"document {document_id!r} legal_topic {legal_topic!r}"

    _delete_chunks_except(
        client=client,
        filters=[
            {"term": {"document_id": document_id}},
            {"term": {"legal_topic": legal_topic}},
        ],
        keep_chunk_ids=[],
        context=f"{context} rollback wipe",
    )

    _reindex_snapshot_chunks(
        client=client,
        snapshot=snapshot,
        bulk_chunk_size=bulk_chunk_size,
        context=f"{context} rollback restore",
    )


def replace_document_contact_chunk(
    document_id: str,
    chunk: DocumentChunk | None,
    client: OpenSearch | None = None,
    bulk_chunk_size: int = DEFAULT_BULK_CHUNK_SIZE,
) -> DocumentIndexingResult:
    """
    Replace (or remove) the one Contact-subsection chunk for one
    document_id, atomically (mission "ORDER 8G-B1", section 9).

    chunk=None means "this document currently has zero contacts" - any
    existing indexed Contact chunk is deleted, nothing new is indexed.
    Scoped to subsection == CONTACT_SUBSECTION only via
    "subsection.keyword" (the mapped exact-match sub-field - see
    opensearch_index.py; "subsection" itself is "type": "text") - every
    legal-topic chunk for this document is left completely untouched,
    exactly like replace_document_section_chunks's own topic scoping.

    Deliberately a separate function rather than a call into
    replace_document_section_chunks: that function's own stale-chunk
    filter is a "term" query on the required-string "legal_topic"
    field, which the Contact chunk always carries as None - a query
    value replace_document_section_chunks's own filter was never built
    to represent safely.
    """

    opensearch_client = (
        client
        if client is not None
        else get_opensearch_client()
    )

    ensure_legal_documents_index(
        client=opensearch_client
    )

    contact_snapshot = [
        item
        for item in _snapshot_document_chunks(
            client=opensearch_client,
            document_id=document_id,
        )
        if item["_source"].get("subsection") == CONTACT_SUBSECTION
    ]

    chunk_list = (
        [chunk]
        if chunk is not None
        else []
    )

    if chunk_list:
        _validate_chunks(
            chunk_list
        )

        mismatched_subsections = {
            item.subsection
            for item in chunk_list
            if item.subsection != CONTACT_SUBSECTION
        }

        if mismatched_subsections:
            raise InvalidDocumentChunksError(
                "All chunks must have subsection "
                f"{CONTACT_SUBSECTION!r}; found "
                f"{sorted(mismatched_subsections)!r}."
            )

    if bulk_chunk_size <= 0:
        raise ValueError(
            "bulk_chunk_size must be greater than zero."
        )

    contact_filters: list[dict[str, Any]] = [
        {
            "term": {
                "document_id": document_id,
            }
        },
        {
            "term": {
                "subsection.keyword": CONTACT_SUBSECTION,
            }
        },
    ]

    context = f"document {document_id!r} contact chunk"

    try:
        if chunk_list:
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
                    "OpenSearch returned an inconsistent indexed "
                    f"count: expected {len(chunk_list)}, received "
                    f"{indexed_count}."
                )

        else:
            indexed_count = 0

        stale_chunks_deleted = _delete_chunks_except(
            client=opensearch_client,
            filters=contact_filters,
            keep_chunk_ids=[
                item.chunk_id
                for item in chunk_list
            ],
            context=context,
        )

    except Exception as original_error:
        try:
            _delete_chunks_except(
                client=opensearch_client,
                filters=contact_filters,
                keep_chunk_ids=[],
                context=f"{context} rollback wipe",
            )

            _reindex_snapshot_chunks(
                client=opensearch_client,
                snapshot=contact_snapshot,
                bulk_chunk_size=bulk_chunk_size,
                context=f"{context} rollback restore",
            )

        except Exception as rollback_error:
            raise DocumentIndexingError(
                f"Contact chunk replacement failed for document "
                f"{document_id!r}, and its OpenSearch rollback also "
                "failed."
            ) from rollback_error

        if isinstance(
            original_error,
            DocumentIndexingError,
        ):
            raise

        raise DocumentIndexingError(
            "OpenSearch contact chunk replacement failed for "
            f"document {document_id!r}."
        ) from original_error

    return DocumentIndexingResult(
        index_alias=LEGAL_DOCUMENTS_ALIAS,
        document_id=document_id,
        source_filename=(
            chunk_list[0].source_filename
            if chunk_list
            else ""
        ),
        requested_chunks=len(
            chunk_list
        ),
        indexed_chunks=int(
            indexed_count
        ),
        stale_chunks_deleted=stale_chunks_deleted,
    )


def replace_document_section_chunks(
    chunks: Sequence[DocumentChunk],
    legal_topic: str,
    client: OpenSearch | None = None,
    bulk_chunk_size: int = DEFAULT_BULK_CHUNK_SIZE,
) -> DocumentIndexingResult:
    """
    Replace exactly one (document_id, legal_topic) section's chunks,
    atomically.

    Mission "ORDER 5C" corrective gate: identical transactional shape
    to replace_country_document_chunks (snapshot the affected scope
    BEFORE any mutation; index new chunks; delete stale ones; on ANY
    failure past the snapshot, restore it exactly) - scoped to this
    one (document_id, legal_topic) section instead of a whole country,
    so every OTHER section/topic for this same document is never
    touched, never re-indexed, never considered for deletion or
    restoration. Every chunk passed in must already carry this exact
    legal_topic (checked explicitly, since _validate_chunks itself
    does not check topic consistency - real documents legitimately
    span many topics, but a single section edit must never span more
    than one).

    On success, OpenSearch holds exactly the new section content. On
    any failure, OpenSearch holds exactly what it held before this
    call - never a partial mix of old and new chunks for this section.
    """

    chunk_list = list(
        chunks
    )

    _validate_chunks(
        chunk_list
    )

    mismatched_topics = {
        chunk.legal_topic
        for chunk in chunk_list
        if chunk.legal_topic != legal_topic
    }

    if mismatched_topics:
        raise InvalidDocumentChunksError(
            "All chunks must share the requested legal_topic "
            f"{legal_topic!r}; found {sorted(mismatched_topics)!r}."
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

    document_id = (
        chunk_list[0].document_id
    )

    section_snapshot = [
        item
        for item in _snapshot_document_chunks(
            client=opensearch_client,
            document_id=document_id,
        )
        if item["_source"].get("legal_topic") == legal_topic
    ]

    current_chunk_ids = [
        chunk.chunk_id
        for chunk in chunk_list
    ]

    try:
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

        stale_chunks_deleted = _delete_stale_section_chunks(
            client=opensearch_client,
            document_id=document_id,
            legal_topic=legal_topic,
            current_chunk_ids=current_chunk_ids,
        )

    except Exception as original_error:
        try:
            _restore_section_snapshot(
                client=opensearch_client,
                document_id=document_id,
                legal_topic=legal_topic,
                snapshot=section_snapshot,
                bulk_chunk_size=bulk_chunk_size,
            )

        except Exception as rollback_error:
            raise DocumentIndexingError(
                "Section replacement failed for document "
                f"{document_id!r}, legal_topic {legal_topic!r}, and "
                "its OpenSearch rollback also failed."
            ) from rollback_error

        if isinstance(
            original_error,
            DocumentIndexingError,
        ):
            raise

        raise DocumentIndexingError(
            "OpenSearch section replacement failed for document "
            f"{document_id!r}, legal_topic {legal_topic!r}."
        ) from original_error

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

# Mission "ORDER 3B" root cause: a single client.search(size=10000, ...)
# with no explicit track_total_hits reports hits.total as {"value":
# 10000, "relation": "gte"} once a query genuinely matches 10,000 or
# more documents - OpenSearch's own default total-hit-tracking ceiling,
# which coincides exactly with index.max_result_window's own default
# (10,000). The pre-existing safety check here compared total.value
# against len(hits) without ever looking at total.relation, so a
# "gte 10000" lower bound was silently accepted as if it were the
# real, exact total (10000 == 10000, the check never fires) - the
# actual bug was invisible precisely because both ceilings are the
# same number. Fixed by paginating exhaustively (never trusting a
# single bounded search for a real total) rather than by raising
# index.max_result_window (explicitly out of scope) or capping
# documents below 25 MiB/whatever chunk count they happen to produce.
_EXHAUSTIVE_FETCH_PAGE_SIZE: Final[int] = 5000


def _fetch_all_chunks(
    *,
    client: OpenSearch,
    field: str,
    value: str,
) -> list[dict[str, Any]]:
    """
    Fetch every chunk matching one term query (field=value),
    exhaustively - the single, centralized mechanism every caller that
    needs a complete chunk set (country snapshot, existing-country
    lookup) must use, instead of each hand-rolling its own bounded
    search (mission "ORDER 3B", section 4).

    Paginates via search_after on chunk_id - a real, verified-unique,
    sortable "keyword" field in the production mapping (never assumed;
    confirmed against the real index while building this fix). Always
    requests track_total_hits=True so the total this function checks
    itself against is always the real, exact count, never OpenSearch's
    own default "at least N" lower bound. Detects a duplicate hit
    across pages (an unstable sort would produce one) and, when
    finished, verifies the number of chunks actually retrieved against
    OpenSearch's own reported total - never returns a silently
    truncated result.
    """

    all_hits: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    search_after: list[Any] | None = None
    expected_total: int | None = None

    while True:
        body: dict[str, Any] = {
            "size": _EXHAUSTIVE_FETCH_PAGE_SIZE,
            "track_total_hits": True,
            "sort": [
                {"chunk_id": "asc"},
            ],
            "query": {
                "term": {
                    field: value,
                },
            },
        }

        if search_after is not None:
            body["search_after"] = search_after

        try:
            response = client.search(
                index=LEGAL_DOCUMENTS_ALIAS,
                body=body,
            )

        except NotFoundError:
            return []

        except OpenSearchException as error:
            raise DocumentIndexingError(
                "OpenSearch exhaustive chunk fetch failed for "
                f"{field}={value!r}."
            ) from error

        if not isinstance(response, dict):
            raise DocumentIndexingError(
                "OpenSearch returned an invalid exhaustive fetch "
                "response."
            )

        hits_container = response.get("hits")

        if not isinstance(hits_container, dict):
            raise DocumentIndexingError(
                "OpenSearch returned invalid exhaustive fetch results."
            )

        page_hits = hits_container.get("hits")

        if not isinstance(page_hits, list):
            raise DocumentIndexingError(
                "OpenSearch returned an invalid exhaustive fetch "
                "hit list."
            )

        if expected_total is None:
            total = hits_container.get("total")

            if isinstance(total, dict):
                total = total.get("value")

            try:
                expected_total = int(total)

            except (TypeError, ValueError) as error:
                raise DocumentIndexingError(
                    "OpenSearch returned an invalid exhaustive "
                    "fetch total."
                ) from error

        if not page_hits:
            break

        for hit in page_hits:
            if not isinstance(hit, dict):
                raise DocumentIndexingError(
                    "OpenSearch returned an invalid exhaustive "
                    "fetch hit."
                )

            hit_id = hit.get("_id")
            source = hit.get("_source")

            if (
                not isinstance(hit_id, str)
                or not hit_id
                or not isinstance(source, dict)
            ):
                raise DocumentIndexingError(
                    "OpenSearch returned incomplete exhaustive "
                    "fetch data."
                )

            if hit_id in seen_ids:
                raise DocumentIndexingError(
                    "OpenSearch exhaustive fetch returned the same "
                    f"chunk twice across pages ({hit_id!r}) - "
                    "pagination is not stable."
                )

            seen_ids.add(hit_id)
            all_hits.append(
                {
                    "_id": hit_id,
                    "_source": source,
                }
            )

        sort_values = page_hits[-1].get("sort")

        if not isinstance(sort_values, list) or not sort_values:
            raise DocumentIndexingError(
                "OpenSearch returned no sort values to paginate "
                "the exhaustive chunk fetch."
            )

        search_after = sort_values

        if len(page_hits) < _EXHAUSTIVE_FETCH_PAGE_SIZE:
            break

    if expected_total is not None and len(all_hits) != expected_total:
        raise DocumentIndexingError(
            "OpenSearch exhaustive fetch retrieved "
            f"{len(all_hits)} chunk(s) for {field}={value!r}, but the "
            f"real total was {expected_total} - pagination did not "
            "exhaust the result set."
        )

    return all_hits


def _snapshot_country_chunks(
    *,
    client: OpenSearch,
    country_code: str,
) -> list[dict[str, Any]]:
    """Capture all active chunks for one country before replacement."""

    return _fetch_all_chunks(
        client=client,
        field="country_code",
        value=country_code,
    )


def _delete_country_chunks(
    *,
    client: OpenSearch,
    country_code: str,
    keep_chunk_ids: Sequence[str] = (),
) -> int:
    """Delete country chunks, optionally preserving the current IDs."""

    return _delete_chunks_except(
        client=client,
        filters=[
            {
                "term": {
                    "country_code": country_code,
                }
            }
        ],
        keep_chunk_ids=keep_chunk_ids,
        context=f"country {country_code!r}",
    )


def _reindex_snapshot_chunks(
    *,
    client: OpenSearch,
    snapshot: Sequence[dict[str, Any]],
    bulk_chunk_size: int,
    context: str,
) -> None:
    """
    Re-index every chunk captured in `snapshot` verbatim.

    The shared, reindex-only half of every snapshot restore in this
    module (mission "ORDER 5C" corrective gate, section 4) - never
    deletes anything itself. Every chunk_id is deterministic, so
    re-indexing one that is still present is a harmless overwrite.
    A caller that needs "the scope now holds exactly this snapshot,
    nothing more" combines this with its own appropriately-scoped
    _delete_chunks_except call - the two are never fused into one
    function, because "appropriately scoped" differs by caller (a
    whole country, a whole document, or one document's single
    section) and fusing them was the exact bug this corrective gate
    caught: a section-scoped snapshot restored through a document-
    wide delete-except would wipe every OTHER section's chunks too.
    """

    if not snapshot:
        return

    actions = [
        {
            "_op_type": "index",
            "_index": LEGAL_DOCUMENTS_ALIAS,
            "_id": item["_id"],
            "_source": item["_source"],
        }
        for item in snapshot
    ]

    restored_count, errors = bulk(
        client=client,
        actions=actions,
        chunk_size=bulk_chunk_size,
        max_retries=3,
        initial_backoff=1,
        max_backoff=8,
        raise_on_error=False,
        raise_on_exception=False,
        refresh=True,
    )

    if errors or restored_count != len(actions):
        raise DocumentIndexingError(
            f"OpenSearch snapshot restoration failed ({context})."
        )


def _restore_country_snapshot(
    *,
    client: OpenSearch,
    snapshot: Sequence[dict[str, Any]],
    bulk_chunk_size: int,
) -> None:
    """Restore a previously captured country snapshot."""

    _reindex_snapshot_chunks(
        client=client,
        snapshot=snapshot,
        bulk_chunk_size=bulk_chunk_size,
        context="country snapshot",
    )


def replace_country_document_chunks(
    chunks: Sequence[DocumentChunk],
    client: OpenSearch | None = None,
    bulk_chunk_size: int = DEFAULT_BULK_CHUNK_SIZE,
) -> DocumentIndexingResult:
    """
    Atomically replace every indexed version for one country.

    The previous country snapshot is restored if indexing or cleanup fails.
    """

    chunk_list = list(chunks)
    _validate_chunks(chunk_list)

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

    country_code = chunk_list[0].country_code
    snapshot = _snapshot_country_chunks(
        client=opensearch_client,
        country_code=country_code,
    )

    current_chunk_ids = [
        chunk.chunk_id
        for chunk in chunk_list
    ]

    try:
        indexed_count, errors = bulk(
            client=opensearch_client,
            actions=_build_bulk_actions(chunk_list),
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

        if indexed_count != len(chunk_list):
            raise DocumentIndexingError(
                "OpenSearch returned an inconsistent indexed count: "
                f"expected {len(chunk_list)}, "
                f"received {indexed_count}."
            )

        stale_chunks_deleted = _delete_country_chunks(
            client=opensearch_client,
            country_code=country_code,
            keep_chunk_ids=current_chunk_ids,
        )

    except Exception as original_error:
        try:
            _delete_country_chunks(
                client=opensearch_client,
                country_code=country_code,
            )
            _restore_country_snapshot(
                client=opensearch_client,
                snapshot=snapshot,
                bulk_chunk_size=bulk_chunk_size,
            )

        except Exception as rollback_error:
            raise DocumentIndexingError(
                "Country replacement failed and its OpenSearch "
                "rollback also failed."
            ) from rollback_error

        if isinstance(
            original_error,
            DocumentIndexingError,
        ):
            raise

        raise DocumentIndexingError(
            "OpenSearch country replacement failed."
        ) from original_error

    document_id = chunk_list[0].document_id

    return DocumentIndexingResult(
        index_alias=LEGAL_DOCUMENTS_ALIAS,
        document_id=document_id,
        source_filename=chunk_list[0].source_filename,
        requested_chunks=len(chunk_list),
        indexed_chunks=int(indexed_count),
        stale_chunks_deleted=stale_chunks_deleted,
    )
