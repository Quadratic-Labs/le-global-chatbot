"""Reindex and delete managed legal documents safely."""

from __future__ import annotations

import os
import re
import tempfile
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
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
from app.services.admin_document_replacement import (
    ExistingCountryDocument,
    lookup_existing_country_documents,
)
from app.services.country_lock import (
    country_lock,
)
from app.services.contact_state import (
    read_contact_state,
)
from app.services.document_chunk_builder import (
    build_document_chunks_from_docx,
)
from app.services.document_contact_materializer import (
    materialize_effective_docx,
)
from app.services.document_indexer import (
    DEFAULT_BULK_CHUNK_SIZE,
    DocumentIndexingResult,
    _restore_country_snapshot,
    _snapshot_country_chunks,
    replace_document_chunks,
)
from app.services.document_section_state import (
    delete_section_edit_state,
)
from app.services.document_source_resolver import (
    DocumentSourceConflictError,
    resolve_country_source_paths,
    resolve_document_source_path,
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


class AdminDocumentSourceConflictError(RuntimeError):
    """
    Raised when two distinct metadata fields resolve to two distinct
    real source files - Reindex and Delete both refuse to guess which
    one is active (mission "HOTFIX 0.4.4", section 5).
    """


class AdminDocumentLifecycleError(RuntimeError):
    """Raised when a document lifecycle operation fails."""


class AdminDocumentRollbackError(AdminDocumentLifecycleError):
    """
    Raised when a lifecycle operation failed AND the rollback attempted
    afterwards was itself incomplete - the indexed/filesystem state may
    now differ from both the pre-operation and the intended post-
    operation state, and needs manual verification. Always a subclass
    of AdminDocumentLifecycleError, so a caller matching only the
    parent class still catches this - but a caller matching this
    subclass specifically (mission "ORDER 2": the admin router does)
    can flag it as the more urgent condition it actually is.
    """


class AdminDocumentCountryConflictError(RuntimeError):
    """
    Raised when more than one active document_id already exists for a
    document's country before a normal mutation - Edit/Add/Reindex
    must never arbitrarily pick one of them (ORDER 8A, section 23).
    Protects legacy duplicate-country states (e.g. Italy today) from
    being silently mutated through only one of their two IDs. A
    confirmed Upload/Replace is the one legitimate way to resolve such
    a conflict (it retires every candidate source and chunk for the
    country atomically) and is deliberately not gated by this check.
    """

    def __init__(
        self,
        *,
        country_code: str,
        document_ids: Sequence[str],
        operation: str = "section_update",
    ) -> None:
        self.country_code = country_code
        self.document_ids = tuple(document_ids)
        self.operation = operation

        super().__init__(
            f"Country {country_code!r} has {len(self.document_ids)} "
            "active documents - refusing to mutate any single one "
            "until the conflict is resolved."
        )

    def to_detail(self) -> dict[str, object]:
        return {
            "code": "country_document_conflict",
            "message": str(self),
            "operation": self.operation,
            "country_code": self.country_code,
            "document_ids": list(self.document_ids),
        }


def _ensure_no_country_conflict(
    *,
    country_code: str,
    client: OpenSearch,
    operation: str = "section_update",
) -> None:
    """
    ORDER 8A section 23: refuse any normal mutation, with zero effect,
    when more than one active document_id already exists for this
    country - never guess which one is "the real one".
    """

    try:
        existing_documents = lookup_existing_country_documents(
            country_code,
            client,
        )

    except Exception as error:
        raise AdminDocumentLifecycleError(
            "The country catalog could not be checked before this "
            "operation."
        ) from error

    if len(existing_documents) > 1:
        raise AdminDocumentCountryConflictError(
            country_code=country_code,
            operation=operation,
            document_ids=[
                document.document_id
                for document in existing_documents
            ],
        )


def _tag_country_code(
    error: Exception,
    country_code: str | None,
) -> Exception:
    """
    Attach country_code to a business exception for structured
    logging (mission "ORDER 2"), without changing any exception
    class's constructor signature. A plain attribute, not a new
    __init__ parameter: every one of these exception types is also
    constructed directly in existing tests with just a message, and
    this must never require touching those call sites.
    """

    error.country_code = country_code  # type: ignore[attr-defined]

    return error


class DeleteBackupRestoreError(RuntimeError):
    """
    Raised when restoring one or more delete backups to their
    original, active path fails - the caller must never assume a
    silent, complete rollback happened just because it *attempted*
    one (mission "HOTFIX 0.4.9" review 2, section 4). Every backup is
    still attempted, even after an earlier one fails; this reports
    every path that could not be restored, never just the first.
    """

    def __init__(self, failed_paths: Sequence[Path]) -> None:
        self.failed_paths = tuple(failed_paths)

        super().__init__(
            "Failed to restore backup(s) for: "
            + ", ".join(str(path) for path in failed_paths)
        )


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


@dataclass(frozen=True, slots=True)
class DocumentDownload:
    """
    The file backing one Download response.

    `path` is what FileResponse should actually stream - either the
    real persisted source DOCX (contacts_materialized=False, no
    structured Contact state to reflect - never currently expected in
    production, only a defensive fallback), or a freshly-built
    temporary "effective" copy (contacts_materialized=True) combining
    that same source's legal content with its CURRENT structured
    Contact state (mission "ORDER 8G-B2.1"). `cleanup_path`, when set,
    is a temporary file the caller must delete once the response has
    been sent - never the persisted source itself.
    """

    path: Path
    download_filename: str
    contacts_materialized: bool = False
    cleanup_path: Path | None = None


def get_document_download(
    *,
    document_id: str,
    source_directory: Path,
    client: OpenSearch | None = None,
) -> DocumentDownload:
    """
    Resolve the effective DOCX to stream for GET .../download (mission
    "ORDER 3", section 25; mission "ORDER 8G-B2.1" for the Contact
    materialization added here).

    Reuses exactly the same resolver reindex/delete already trust
    (resolve_document_source_path) - never a second, independent way
    to pick a source file, and never a client-supplied path: the
    client provides only document_id, this function does the rest.
    Read-only against the persisted source and against structured
    Contact state - never acquires the per-country lock, since a
    download does not mutate anything and must not be serialized
    behind writes, and never writes a Contact sidecar.
    """

    validated_document_id = _validate_document_id(
        document_id
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

    country_code = _required_string(
        metadata,
        "country_code",
    )

    try:
        resolved_source = resolve_document_source_path(
            source_root=source_directory,
            country_code=country_code,
            source_filename=source_filename,
        )

    except DocumentSourceConflictError as error:
        raise _tag_country_code(
            AdminDocumentSourceConflictError(
                str(error)
            ),
            country_code,
        ) from error

    if resolved_source.path is None:
        raise _tag_country_code(
            AdminDocumentSourceMissingError(
                "The source DOCX file is missing."
            ),
            country_code,
        )

    contact_state = read_contact_state(
        source_directory,
        validated_document_id,
    )

    if contact_state is not None:
        # Deferred import: admin_contacts.py imports from this module,
        # so this module cannot import admin_contacts.py at module
        # level without creating a cycle.
        from app.services.admin_contacts import (
            _record_to_extracted_contact,
        )

        effective_bytes = materialize_effective_docx(
            source_path=resolved_source.path,
            contacts=[
                _record_to_extracted_contact(record)
                for record in contact_state.contacts
            ],
        )

        temporary_file = tempfile.NamedTemporaryFile(
            suffix=".docx",
            delete=False,
        )

        try:
            temporary_file.write(effective_bytes)
        finally:
            temporary_file.close()

        temporary_path = Path(temporary_file.name)

        return DocumentDownload(
            path=temporary_path,
            download_filename=source_filename,
            contacts_materialized=True,
            cleanup_path=temporary_path,
        )

    return DocumentDownload(
        path=resolved_source.path,
        download_filename=source_filename,
    )


def _validate_delete_by_query_response(
    response: Any,
    *,
    expected_chunks: int | None = None,
) -> int:
    """
    Validate one delete_by_query response and return its deleted
    count - shared by every delete_by_query caller in this module
    (mission "HOTFIX 0.4.9" review 3, section 1; review 4 extends
    this to the stray-chunk cleanup used by reindex's rollback).

    A response is trusted only once every integrity field it can
    report has been validated. conflicts="proceed" means the query
    keeps going past a version conflict rather than aborting it - so
    a response can legitimately report chunks genuinely deleted
    server-side alongside a non-zero version_conflicts count, a
    per-shard failure, or a timeout. None of that is acceptable for a
    destructive ADMIN operation: a PARTIAL deletion must never be
    reported as success. When expected_chunks is supplied, deleted
    must equal it exactly - anything else, including a smaller
    positive count, is an incomplete deletion.
    """

    if not isinstance(
        response,
        dict,
    ):
        raise AdminDocumentLifecycleError(
            "OpenSearch returned an invalid "
            "document deletion response."
        )

    if response.get("timed_out", False):
        raise AdminDocumentLifecycleError(
            "OpenSearch document deletion timed out."
        )

    failures = response.get("failures", [])

    if not isinstance(failures, list):
        raise AdminDocumentLifecycleError(
            "OpenSearch returned an invalid "
            "document deletion failure list."
        )

    if failures:
        raise AdminDocumentLifecycleError(
            "OpenSearch document deletion reported "
            f"{len(failures)} failure(s)."
        )

    try:
        version_conflicts = int(
            response.get("version_conflicts", 0)
        )

    except (TypeError, ValueError) as error:
        raise AdminDocumentLifecycleError(
            "OpenSearch returned an invalid "
            "document deletion version_conflicts count."
        ) from error

    if version_conflicts != 0:
        raise AdminDocumentLifecycleError(
            "OpenSearch document deletion reported "
            f"{version_conflicts} version conflict(s)."
        )

    total: int | None = None

    if "total" in response:
        try:
            total = int(response["total"])

        except (TypeError, ValueError) as error:
            raise AdminDocumentLifecycleError(
                "OpenSearch returned an invalid "
                "document deletion total count."
            ) from error

        if total < 0:
            raise AdminDocumentLifecycleError(
                "OpenSearch returned an invalid "
                "document deletion total count."
            )

    try:
        deleted = int(
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

    if deleted < 0:
        raise AdminDocumentLifecycleError(
            "OpenSearch returned an invalid "
            "deleted chunk count."
        )

    # total is the number of documents delete_by_query matched and
    # processed; deleted is how many of those it actually deleted.
    # Cross-checking the two catches a partial deletion on its own,
    # independent of expected_chunks - the only guard available to a
    # caller (reindex_indexed_document's previous-document cleanup)
    # that has no country snapshot to derive an expected count from.
    if total is not None and deleted != total:
        raise AdminDocumentLifecycleError(
            "OpenSearch document deletion was incomplete: deleted "
            f"{deleted} of {total} matched chunk(s)."
        )

    if (
        expected_chunks is not None
        and deleted != expected_chunks
    ):
        raise AdminDocumentLifecycleError(
            "OpenSearch document deletion was incomplete: deleted "
            f"{deleted} of {expected_chunks} expected chunk(s)."
        )

    return deleted


def _delete_document_chunks(
    *,
    document_id: str,
    client: OpenSearch,
    expected_chunks: int | None = None,
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

    return _validate_delete_by_query_response(
        response,
        expected_chunks=expected_chunks,
    )


def _delete_stray_country_chunks(
    *,
    client: OpenSearch,
    country_code: str,
    keep_chunk_ids: Sequence[str],
) -> int:
    """
    Delete every chunk in one country NOT among keep_chunk_ids.

    Mission "HOTFIX 0.4.9" review 4: a country-level snapshot restore
    (_restore_country_snapshot) only re-indexes what it captured - it
    never deletes anything outside that set. Reindex's rollback pairs
    a snapshot restore with this to remove whatever a failed reindex
    attempt actually created, regardless of which document_id it
    ended up under: the new document_id when reindex produced one,
    or stray new chunk_ids under the SAME document_id when it did not
    (e.g. document_indexer's own internal indexing or stale-chunk
    cleanup failing partway through). keep_chunk_ids is the complete
    pre-mutation snapshot's own chunk IDs - every sibling document in
    the country is included in it, so this never touches a document
    that was never part of this reindex attempt.
    """

    keep_ids = list(keep_chunk_ids)

    query: dict[str, Any] = (
        {
            "term": {
                "country_code": country_code,
            }
        }
        if not keep_ids
        else {
            "bool": {
                "filter": [
                    {
                        "term": {
                            "country_code": country_code,
                        }
                    }
                ],
                "must_not": [
                    {
                        "terms": {
                            "chunk_id": keep_ids,
                        }
                    }
                ],
            }
        }
    )

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
        raise AdminDocumentLifecycleError(
            "OpenSearch stray-chunk cleanup failed."
        ) from error

    return _validate_delete_by_query_response(response)


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

    A reindex is transactional (mission "HOTFIX 0.4.9" review 4):
    either it ends in the new state (new document fully indexed, old
    document fully removed when the ID changed) or it ends in exactly
    the state that existed before it started - never anything in
    between, and never a stray chunk left over from a failed attempt
    regardless of which document_id it landed under. A country_code
    snapshot is captured before document_indexer runs at all, so both
    document_indexer's own indexing AND the subsequent old-document
    deletion are covered by the same rollback: on any failure past
    that point, the snapshot is restored and every chunk in the
    country NOT present in that snapshot is removed - covering both
    the ID-changed case (the new document_id's chunks) and the
    ID-unchanged case (stray new chunk_ids under the same document_id
    from a document_indexer step that failed partway through).

    Mission "ORDER 3", section 17: held under a per-country lock so a
    concurrent upload/reindex/delete for the same country can never
    interleave with this one. A cheap, unlocked metadata read only
    learns which country to lock (document_id alone does not encode
    it) - the authoritative read _reindex_indexed_document_locked
    performs happens again, fresh, once the lock is held.
    """

    validated_document_id = (
        _validate_document_id(
            document_id
        )
    )

    opensearch_client = _get_client(
        client
    )

    preliminary_metadata = _get_document_metadata(
        document_id=validated_document_id,
        client=opensearch_client,
    )

    country_code_for_lock = _required_string(
        preliminary_metadata,
        "country_code",
    )

    with country_lock(
        source_directory,
        country_code_for_lock,
    ):
        return _reindex_indexed_document_locked(
            validated_document_id=validated_document_id,
            source_directory=source_directory,
            opensearch_client=opensearch_client,
            chunk_builder=chunk_builder,
            document_indexer=document_indexer,
        )


def _reindex_indexed_document_locked(
    *,
    validated_document_id: str,
    source_directory: Path,
    opensearch_client: OpenSearch,
    chunk_builder: ChunkBuilder,
    document_indexer: DocumentIndexer,
) -> AdminDocumentReindexResponse:
    """The real reindex logic - always called with the country's lock
    already held by reindex_indexed_document."""

    metadata = _get_document_metadata(
        document_id=validated_document_id,
        client=opensearch_client,
    )

    source_filename = _required_string(
        metadata,
        "source_filename",
    )

    old_country_code = _required_string(
        metadata,
        "country_code",
    )

    _ensure_no_country_conflict(
        country_code=old_country_code,
        client=opensearch_client,
        operation="reindex",
    )

    try:
        resolved_source = resolve_document_source_path(
            source_root=source_directory,
            country_code=old_country_code,
            source_filename=source_filename,
        )

    except DocumentSourceConflictError as error:
        raise _tag_country_code(
            AdminDocumentSourceConflictError(
                str(error)
            ),
            old_country_code,
        ) from error

    if resolved_source.path is None:
        raise _tag_country_code(
            AdminDocumentSourceMissingError(
                "The source DOCX file is missing."
            ),
            old_country_code,
        )

    source_path = resolved_source.path

    try:
        chunks = chunk_builder(
            source_path
        )

    except Exception as error:
        raise _tag_country_code(
            AdminDocumentLifecycleError(
                "The source DOCX could not be parsed: "
                f"{error}"
            ),
            old_country_code,
        ) from error

    if not chunks:
        raise AdminDocumentLifecycleError(
            "The source DOCX produced no legal chunks."
        )

    new_country_code = chunks[0].country_code

    if new_country_code != old_country_code:
        # A country change during Reindex is not a supported product
        # behavior today (no existing caller relies on it, and the
        # source file's own resolved location already encodes its
        # country) - refusing explicitly, before any mutation, keeps
        # the rollback model simple and avoids ever restoring only
        # one of two countries. Upload/Replace already has its own,
        # separately-guarded workflow for a genuine country change.
        raise _tag_country_code(
            AdminDocumentLifecycleError(
                "Reindexing this document would change its country "
                f"from {old_country_code} to {new_country_code}, "
                "which Reindex does not support - use Upload/Replace "
                "instead."
            ),
            old_country_code,
        )

    # Mission "ORDER 8G-B1", section 10: an ordinary Refresh/Reindex of
    # the SAME DOCX must never silently replace Admin-edited contacts
    # with stale contact text re-parsed straight from that DOCX. When
    # a structured contact state already exists for this document_id,
    # its Contact chunk (built the same way Admin Contact CRUD builds
    # it) replaces whatever chunk_builder just parsed fresh from the
    # DOCX; every legal/topic chunk chunk_builder produced is returned
    # completely untouched either way. No sidecar is created merely
    # because Reindex ran - a legacy document with no structured state
    # yet keeps today's existing parsed-DOCX contact behavior exactly.
    # Deferred import (function-local) to avoid a real circular
    # import: admin_contacts imports from this module.
    from app.services.admin_contacts import (
        apply_structured_contact_state_to_chunks,
    )

    chunks = apply_structured_contact_state_to_chunks(
        chunks=chunks,
        document_id=validated_document_id,
        source_directory=source_directory,
    )

    chunk_snapshot = _snapshot_country_chunks(
        client=opensearch_client,
        country_code=old_country_code,
    )

    old_snapshot_chunks = [
        chunk
        for chunk in chunk_snapshot
        if (
            chunk.get("_source", {}).get("document_id")
            == validated_document_id
        )
    ]

    expected_old_chunks = len(old_snapshot_chunks)

    if expected_old_chunks <= 0:
        raise AdminDocumentLifecycleError(
            "The document's indexed chunks could not be found in "
            "the country snapshot just before reindexing."
        )

    try:
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
            previous_chunks_deleted = (
                _delete_document_chunks(
                    document_id=(
                        validated_document_id
                    ),
                    client=opensearch_client,
                    expected_chunks=expected_old_chunks,
                )
            )

    except Exception as reindex_error:
        # The invariant is "the country's indexed state matches the
        # pre-reindex snapshot exactly" - not merely "the new
        # document_id's chunks are gone." A snapshot restore alone
        # only re-indexes what it captured; it never deletes anything
        # outside that set. That matters regardless of whether this
        # reindex produced a different document_id: if it did not
        # (the common case - the same document_id, just edited
        # content), document_indexer's own internal indexing or
        # stale-chunk cleanup can still fail partway through and
        # leave stray new chunk_ids under that SAME document_id,
        # which the snapshot restore alone would never remove either.
        # _delete_stray_country_chunks closes both cases uniformly by
        # deleting anything in the country not present in the
        # snapshot, whatever document_id it ended up under.
        index_restored = True

        try:
            _restore_country_snapshot(
                client=opensearch_client,
                snapshot=chunk_snapshot,
                bulk_chunk_size=DEFAULT_BULK_CHUNK_SIZE,
            )

        except Exception:
            index_restored = False

        extra_chunks_removed = True

        try:
            _delete_stray_country_chunks(
                client=opensearch_client,
                country_code=old_country_code,
                keep_chunk_ids=[
                    chunk["_id"]
                    for chunk in chunk_snapshot
                ],
            )

        except Exception:
            extra_chunks_removed = False

        if not index_restored or not extra_chunks_removed:
            raise _tag_country_code(
                AdminDocumentRollbackError(
                    "Reindexing failed, and the rollback afterwards was "
                    "incomplete (previous index state "
                    + (
                        "restored"
                        if index_restored
                        else "NOT restored"
                    )
                    + ", extra chunks "
                    + (
                        "removed"
                        if extra_chunks_removed
                        else "NOT removed"
                    )
                    + ") - manual recovery is required."
                ),
                old_country_code,
            ) from reindex_error

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


def _restore_delete_backups(
    backups: Sequence[tuple[Path, Path]],
) -> None:
    """
    Restore every source path moved to a temporary delete backup.

    Only ever called before the index-delete commit point (mission
    "HOTFIX 0.4.9" review 2, section 2) - at that point every backup
    is still guaranteed to exist, since nothing has unlinked any of
    them yet. Every restoration is attempted even if an earlier one
    fails - never stops at the first error - and raises
    DeleteBackupRestoreError naming every backup that could not be
    restored if any failed, so the caller can never mistake an
    incomplete rollback for a complete one.
    """

    failed_paths: list[Path] = []

    for original_path, backup_path in reversed(list(backups)):
        if not backup_path.exists():
            # Should never happen before the commit point - nothing
            # has unlinked a backup yet - but the original is still
            # genuinely unrestored, so this counts as a failure too,
            # never a silent skip.
            failed_paths.append(backup_path)
            continue

        try:
            os.replace(backup_path, original_path)

        except OSError:
            failed_paths.append(backup_path)

    if failed_paths:
        raise DeleteBackupRestoreError(failed_paths)


def delete_indexed_document(
    *,
    document_id: str,
    source_directory: Path,
    processed_directory: Path,
    client: OpenSearch | None = None,
    country_document_lookup: Callable[
        [str, OpenSearch | None],
        list[ExistingCountryDocument],
    ] = lookup_existing_country_documents,
) -> AdminDocumentDeleteResponse:
    """
    Delete one indexed document, acting on the requested document_id
    first - never by first demanding that exactly one source file
    resolve for its whole country (mission "HOTFIX 0.4.9": a country
    left with duplicate or conflicting legacy source files by an
    earlier defect must still allow every one of its document_ids to
    be deleted safely, never a server error merely because the
    country has several sources).

    Physical source files are touched only when this is the last
    active document for the country: only then is nothing else still
    depending on any of that country's candidate source files, so
    every one of them (historical and canonical alike) can be safely
    retired together. When other documents remain for the country,
    the index entry for document_id alone is removed and no file is
    touched at all - which specific file backs which remaining
    document cannot be proven unambiguously, so none is deleted
    (source_cleanup_deferred=True signals this to the caller).

    The backup is created next to each source file (its own parent),
    not in processed_directory: those are separate bind mounts in
    production, and os.replace() cannot perform an atomic rename
    across mount points (OSError: Invalid cross-device link).
    processed_directory is kept only for public-signature stability.

    Mission "ORDER 3", section 17: held under a per-country lock, for
    the same reason and in the same shape as reindex_indexed_document
    - a cheap, unlocked metadata read only learns which country to
    lock; the authoritative read happens again once the lock is held.
    """

    validated_document_id = (
        _validate_document_id(
            document_id
        )
    )

    opensearch_client = _get_client(
        client
    )

    preliminary_metadata = _get_document_metadata(
        document_id=validated_document_id,
        client=opensearch_client,
    )

    country_code_for_lock = _required_string(
        preliminary_metadata,
        "country_code",
    )

    with country_lock(
        source_directory,
        country_code_for_lock,
    ):
        return _delete_indexed_document_locked(
            validated_document_id=validated_document_id,
            source_directory=source_directory,
            opensearch_client=opensearch_client,
            country_document_lookup=country_document_lookup,
        )


def _delete_indexed_document_locked(
    *,
    validated_document_id: str,
    source_directory: Path,
    opensearch_client: OpenSearch,
    country_document_lookup: Callable[
        [str, OpenSearch | None],
        list[ExistingCountryDocument],
    ],
) -> AdminDocumentDeleteResponse:
    """The real delete logic - always called with the country's lock
    already held by delete_indexed_document."""

    metadata = _get_document_metadata(
        document_id=validated_document_id,
        client=opensearch_client,
    )

    source_filename = _required_string(
        metadata,
        "source_filename",
    )

    country_code = _required_string(
        metadata,
        "country_code",
    )

    try:
        country_documents = country_document_lookup(
            country_code,
            opensearch_client,
        )

    except OpenSearchException as error:
        raise AdminDocumentLifecycleError(
            "The country catalog could not be checked before deletion."
        ) from error

    other_document_ids = {
        document.document_id
        for document in country_documents
        if document.document_id != validated_document_id
    }

    if other_document_ids:
        # Other documents remain for this country - deleting only
        # this document_id's index entry is always safe; touching any
        # file is not, since ownership between the remaining
        # document_ids and the country's candidate files cannot be
        # proven unambiguously (this is exactly the "Multiple distinct
        # source files resolve" state a pre-existing conflict leaves
        # behind - never a reason to fail the delete itself).
        #
        # This branch never touches a file, but it must still be
        # rollbackable on the index side (mission "HOTFIX 0.4.9"
        # review 3, section 2): the country snapshot is captured
        # BEFORE the target document_id's chunks are deleted, its own
        # expected chunk count is derived from that same snapshot (so
        # a delete_by_query response is only ever trusted against a
        # count taken at the same instant), and the whole snapshot is
        # restored if the deletion fails or is incomplete.
        chunk_snapshot = _snapshot_country_chunks(
            client=opensearch_client,
            country_code=country_code,
        )

        target_snapshot_chunks = [
            chunk
            for chunk in chunk_snapshot
            if (
                chunk.get("_source", {}).get("document_id")
                == validated_document_id
            )
        ]

        expected_target_chunks = len(target_snapshot_chunks)

        if expected_target_chunks <= 0:
            raise AdminDocumentLifecycleError(
                "The document's indexed chunks could not be found "
                "in the country snapshot just before deletion."
            )

        try:
            deleted_chunks = _delete_document_chunks(
                document_id=validated_document_id,
                client=opensearch_client,
                expected_chunks=expected_target_chunks,
            )

        except Exception as index_error:
            index_restored = True

            try:
                _restore_country_snapshot(
                    client=opensearch_client,
                    snapshot=chunk_snapshot,
                    bulk_chunk_size=DEFAULT_BULK_CHUNK_SIZE,
                )

            except Exception:
                index_restored = False

            if not index_restored:
                raise _tag_country_code(
                    AdminDocumentRollbackError(
                        "The document could not be deleted, and "
                        "restoring the country's indexed chunks "
                        "afterwards also failed - manual recovery is "
                        "required."
                    ),
                    country_code,
                ) from index_error

            raise

        # Mission "ORDER 5C", section 38: Delete removes the document,
        # its chunks, its source, AND any persisted section-edit state
        # - never leaving an orphan edit behind (NO_ORPHAN_SECTION_
        # STATE). Only reached once the OpenSearch delete has already
        # succeeded; a failure here is still fully recoverable by
        # restoring the very snapshot just captured, exactly like an
        # index_error above.
        try:
            delete_section_edit_state(
                source_directory,
                validated_document_id,
            )

        except OSError as state_error:
            index_restored = True

            try:
                _restore_country_snapshot(
                    client=opensearch_client,
                    snapshot=chunk_snapshot,
                    bulk_chunk_size=DEFAULT_BULK_CHUNK_SIZE,
                )

            except Exception:
                index_restored = False

            if not index_restored:
                raise _tag_country_code(
                    AdminDocumentRollbackError(
                        "The document was deleted, but clearing its "
                        "section-edit state afterwards failed, and "
                        "restoring the country's indexed chunks "
                        "afterwards also failed - manual recovery is "
                        "required."
                    ),
                    country_code,
                ) from state_error

            raise _tag_country_code(
                AdminDocumentLifecycleError(
                    "The document could not be deleted: its "
                    "section-edit state could not be cleared."
                ),
                country_code,
            ) from state_error

        return AdminDocumentDeleteResponse(
            status="deleted",
            document_id=validated_document_id,
            source_filename=source_filename,
            deleted_chunks=deleted_chunks,
            source_file_deleted=False,
            source_cleanup_deferred=True,
        )

    # The last active document for this country - every candidate
    # source file (historical and canonical) may now be retired.
    #
    # Order matters (mission "HOTFIX 0.4.9" review 2, section 1): the
    # snapshot is acquired FIRST, before any filesystem mutation at
    # all. If it fails, nothing has been touched yet - the exception
    # propagates directly, no rollback needed. Only once a usable
    # snapshot exists do sources move to their backup names; only
    # once that succeeds is the index delete attempted. Everything up
    # to and including a successful index delete is fully
    # rollbackable. The index delete succeeding is the commit point:
    # finalization (removing the now-unused .bak files) after that
    # point is deliberately best-effort cleanup, never a trigger to
    # undo the already-successful delete (section 2) - a backup file
    # already unlinked cannot be "restored", so treating any single
    # finalization failure as grounds to roll back everything would
    # misrepresent whichever backups were already, irreversibly,
    # removed.
    candidate_paths = resolve_country_source_paths(
        source_root=source_directory,
        country_code=country_code,
        source_filenames=[
            document.source_filename
            for document in country_documents
        ],
    )

    chunk_snapshot = _snapshot_country_chunks(
        client=opensearch_client,
        country_code=country_code,
    )

    # The snapshot is a strictly more recent read than the earlier
    # country_document_lookup call above, so it - not the lookup - is
    # the authority consulted immediately before any mutation happens
    # (mission "HOTFIX 0.4.9" review 3, section 3):
    #   A/B. the target document's own chunks must actually be
    #        present in the snapshot, and there must be at least one;
    #   C.   since country_document_lookup already asserted this is
    #        the LAST active document for the country, the snapshot
    #        must contain no *other* document_id - if one appears
    #        anyway, the state changed concurrently between the two
    #        reads and every source file for this country may no
    #        longer be safe to retire together.
    # Either violation aborts here, before candidate_paths is ever
    # touched and before any chunk is deleted.
    target_snapshot_chunks = [
        chunk
        for chunk in chunk_snapshot
        if (
            chunk.get("_source", {}).get("document_id")
            == validated_document_id
        )
    ]

    expected_target_chunks = len(target_snapshot_chunks)

    if expected_target_chunks <= 0:
        raise AdminDocumentLifecycleError(
            "The document's indexed chunks could not be found in "
            "the country snapshot just before deletion."
        )

    sibling_document_ids_in_snapshot = {
        chunk.get("_source", {}).get("document_id")
        for chunk in chunk_snapshot
    } - {validated_document_id}

    if sibling_document_ids_in_snapshot:
        raise AdminDocumentLifecycleError(
            "The country snapshot revealed another indexed document "
            "that the prior lookup did not report - refusing to "
            "retire this country's source file(s) under a possibly "
            "stale state."
        )

    backups: list[tuple[Path, Path]] = []

    try:
        for candidate_path in candidate_paths:
            backup_path = (
                candidate_path.parent
                / (
                    ".delete-backup-"
                    f"{uuid.uuid4().hex}-"
                    f"{candidate_path.name}.bak"
                )
            )

            os.replace(
                candidate_path,
                backup_path,
            )
            backups.append((candidate_path, backup_path))

    except OSError as staging_error:
        try:
            _restore_delete_backups(backups)

        except Exception as restore_error:
            # Never narrower than DeleteBackupRestoreError: an
            # unexpected exception type here must still be reported
            # as an explicit, controlled failure - never left to
            # propagate uncontrolled past this handler.
            raise _tag_country_code(
                AdminDocumentRollbackError(
                    "The source DOCX could not be prepared for "
                    "deletion, and restoring the source(s) already "
                    "moved also failed - manual recovery is required."
                ),
                country_code,
            ) from restore_error

        raise _tag_country_code(
            AdminDocumentLifecycleError(
                "The source DOCX could not be "
                "prepared for deletion."
            ),
            country_code,
        ) from staging_error

    try:
        deleted_chunks = (
            _delete_document_chunks(
                document_id=(
                    validated_document_id
                ),
                client=opensearch_client,
                expected_chunks=expected_target_chunks,
            )
        )

    except Exception as index_error:
        files_restored = True

        try:
            _restore_delete_backups(backups)

        except Exception:
            # _restore_delete_backups only ever raises
            # DeleteBackupRestoreError today, but this must never be
            # narrower than that: an unexpected exception type here
            # must still be treated as an incomplete file rollback,
            # never allowed to skip the index-restore attempt below
            # or propagate uncontrolled past this handler.
            files_restored = False

        index_restored = True

        try:
            _restore_country_snapshot(
                client=opensearch_client,
                snapshot=chunk_snapshot,
                bulk_chunk_size=DEFAULT_BULK_CHUNK_SIZE,
            )

        except Exception:
            index_restored = False

        if not files_restored or not index_restored:
            raise _tag_country_code(
                AdminDocumentRollbackError(
                    "The document could not be deleted, and the "
                    "rollback afterwards was incomplete (source files "
                    + (
                        "restored"
                        if files_restored
                        else "NOT restored"
                    )
                    + ", index "
                    + (
                        "restored"
                        if index_restored
                        else "NOT restored"
                    )
                    + ") - manual recovery is required."
                ),
                country_code,
            ) from index_error

        raise

    # Mission "ORDER 5C", section 38: clearing the section-edit state
    # is still part of the critical, rollback-guarded commit sequence
    # (unlike the best-effort backup finalization just below) - an
    # orphaned edit-state file is a correctness bug (NO_ORPHAN_SECTION
    # _STATE), never a cosmetic cleanup failure.
    try:
        delete_section_edit_state(
            source_directory,
            validated_document_id,
        )

    except OSError as state_error:
        files_restored = True

        try:
            _restore_delete_backups(backups)

        except Exception:
            files_restored = False

        index_restored = True

        try:
            _restore_country_snapshot(
                client=opensearch_client,
                snapshot=chunk_snapshot,
                bulk_chunk_size=DEFAULT_BULK_CHUNK_SIZE,
            )

        except Exception:
            index_restored = False

        if not files_restored or not index_restored:
            raise _tag_country_code(
                AdminDocumentRollbackError(
                    "The document was deleted, but clearing its "
                    "section-edit state afterwards failed, and the "
                    "rollback afterwards was incomplete (source files "
                    + (
                        "restored"
                        if files_restored
                        else "NOT restored"
                    )
                    + ", index "
                    + (
                        "restored"
                        if index_restored
                        else "NOT restored"
                    )
                    + ") - manual recovery is required."
                ),
                country_code,
            ) from state_error

        raise _tag_country_code(
            AdminDocumentLifecycleError(
                "The document could not be deleted: its "
                "section-edit state could not be cleared."
            ),
            country_code,
        ) from state_error

    # Commit point reached - the index delete succeeded. Finalization
    # is best-effort: every backup is still attempted even after an
    # earlier one fails, but a failure here never restores the
    # backups or the index - see the docstring note above.
    failed_backup_paths: list[Path] = []

    for _, backup_path in backups:
        try:
            backup_path.unlink()

        except OSError:
            failed_backup_paths.append(backup_path)

    return AdminDocumentDeleteResponse(
        status="deleted",
        document_id=validated_document_id,
        source_filename=source_filename,
        deleted_chunks=deleted_chunks,
        source_file_deleted=(
            bool(candidate_paths)
            and not failed_backup_paths
        ),
        source_cleanup_deferred=bool(failed_backup_paths),
    )