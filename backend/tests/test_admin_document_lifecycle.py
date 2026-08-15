"""Tests for indexed document reindexing and deletion."""

from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from pathlib import Path

from tests.admin_invariants import real_source_entries
from typing import Any
from unittest.mock import patch

from opensearchpy.exceptions import OpenSearchException

from app.models.document import DocumentChunk
from app.services.admin_document_lifecycle import (
    AdminDocumentLifecycleError,
    AdminDocumentNotFoundError,
    AdminDocumentRollbackError,
    AdminDocumentSourceConflictError,
    AdminDocumentSourceMissingError,
    DeleteBackupRestoreError,
    _delete_document_chunks,
    delete_indexed_document,
    reindex_indexed_document,
)
from app.services.admin_document_replacement import (
    ExistingCountryDocument,
)
from app.services.document_indexer import (
    DocumentIndexingError,
    DocumentIndexingResult,
    replace_document_chunks,
)
from app.services.document_section_state import (
    SectionEdit,
    SectionEditState,
    read_section_edit_state,
    section_id_for_legal_topic,
    write_section_edit_state_atomic,
)


OLD_DOCUMENT_ID = (
    "doc_"
    + "a" * 64
)

NEW_DOCUMENT_ID = (
    "doc_"
    + "b" * 64
)


def _build_chunk(
    *,
    document_id: str,
    source_filename: str,
) -> DocumentChunk:
    """Build one valid test chunk."""

    return DocumentChunk(
        document_id=document_id,
        chunk_id=(
            "chunk_"
            + "c" * 64
        ),
        country="United Kingdom",
        country_code="GB",
        legal_topic="Employment Contracts",
        document_type="comparator",
        language="en",
        section="Employment Contracts",
        subsection="Notice Period",
        content="One week of notice may apply.",
        source_filename=source_filename,
        source_format="docx",
        content_hash="content-hash",
        reference_year=2026,
    )


def _build_reindex_chunk(
    *,
    document_id: str,
    chunk_id: str,
    source_filename: str = "GB.docx",
    country_code: str = "GB",
    country: str = "United Kingdom",
) -> DocumentChunk:
    """
    One valid chunk with an explicit, distinct chunk_id - needed for
    multi-chunk REINDEX transaction tests (mission "HOTFIX 0.4.9"
    review 4), where _build_chunk's single hardcoded chunk_id would
    collide across every chunk of a multi-chunk document.
    """

    return DocumentChunk(
        document_id=document_id,
        chunk_id=chunk_id,
        country=country,
        country_code=country_code,
        legal_topic="Employment Contracts",
        document_type="comparator",
        language="en",
        section="Employment Contracts",
        subsection="Notice Period",
        content="One week of notice may apply.",
        source_filename=source_filename,
        source_format="docx",
        content_hash="content-hash",
        reference_year=2026,
    )


class FakeOpenSearchClient:
    """
    OpenSearch test double for lifecycle operations.

    country_document_ids models every document_id currently active
    for this country, as delete_indexed_document's own country-level
    lookup would see it - defaulting to [OLD_DOCUMENT_ID] alone, which
    is exactly the single-document assumption every pre-existing test
    in this file already made before that lookup existed (mission
    "HOTFIX 0.4.9"). A test that needs to exercise several documents
    sharing one country (a real Australia-shaped duplicate) passes
    its own explicit list instead.
    """

    def __init__(
        self,
        *,
        document_exists: bool = True,
        source_filename: str = "UK 2026.docx",
        country_document_ids: list[str] | None = None,
        country_source_filenames: dict[str, str] | None = None,
    ) -> None:
        self.document_exists = document_exists
        self.source_filename = source_filename
        self.country_document_ids = (
            country_document_ids
            if country_document_ids is not None
            else [OLD_DOCUMENT_ID]
        )
        self.country_source_filenames = (
            country_source_filenames or {}
        )
        self.deleted_document_ids: list[str] = []

    def search(
        self,
        index: str,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        del index

        term = body["query"]["term"]

        if "country_code" in term:
            # Real OpenSearch 3.7 shape (mission "ORDER 3B"): a sorted
            # search_after page reports an exact total and includes a
            # "sort" array per hit - both required by the real,
            # centralized _fetch_all_chunks() this fake now stands in
            # for.
            sorted_document_ids = sorted(
                self.country_document_ids
            )

            return {
                "hits": {
                    "total": {
                        "value": len(sorted_document_ids),
                    },
                    "hits": [
                        {
                            "_id": f"{document_id}-snapshot-chunk",
                            "_source": {
                                "document_id": document_id,
                                "chunk_id": (
                                    f"{document_id}-snapshot-chunk"
                                ),
                                "source_filename": (
                                    self.country_source_filenames.get(
                                        document_id,
                                        self.source_filename,
                                    )
                                ),
                                "country": "United Kingdom",
                                "country_code": "GB",
                                "reference_year": 2026,
                            },
                            "sort": [
                                f"{document_id}-snapshot-chunk"
                            ],
                        }
                        for document_id in sorted_document_ids
                    ]
                }
            }

        requested_document_id = term["document_id"]

        if not self.document_exists:
            return {
                "hits": {
                    "hits": [],
                }
            }

        return {
            "hits": {
                "hits": [
                    {
                        "_source": {
                            "document_id": (
                                requested_document_id
                            ),
                            "source_filename": (
                                self.source_filename
                            ),
                            "country": (
                                "United Kingdom"
                            ),
                            "country_code": "GB",
                            "reference_year": 2026,
                        }
                    }
                ]
            }
        }

    def delete_by_query(
        self,
        *,
        index: str,
        body: dict[str, Any],
        conflicts: str,
        refresh: bool,
    ) -> dict[str, Any]:
        del index
        del conflicts
        del refresh

        document_id = (
            body["query"]["term"]["document_id"]
        )

        self.deleted_document_ids.append(
            document_id
        )

        return {
            "deleted": 1,
        }


class BackupInspectingOpenSearchClient(FakeOpenSearchClient):
    """
    Records on-disk backup state at the moment chunks are deleted.

    This is the only point in delete_indexed_document where the
    source DOCX has already been moved but the operation has not
    yet completed - the right moment to observe where the backup
    was actually created.
    """

    def __init__(
        self,
        *,
        source_directory: Path,
        processed_directory: Path,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.source_directory = source_directory
        self.processed_directory = processed_directory
        self.backup_path_at_delete_time: Path | None = None
        self.processed_directory_entries_at_delete_time: list[str] = []

    def delete_by_query(
        self,
        *,
        index: str,
        body: dict[str, Any],
        conflicts: str,
        refresh: bool,
    ) -> dict[str, Any]:
        backups = [
            path
            for path in real_source_entries(self.source_directory)
            if path.name.startswith(".delete-backup-")
        ]

        if backups:
            self.backup_path_at_delete_time = backups[0]

        if self.processed_directory.exists():
            self.processed_directory_entries_at_delete_time = [
                path.name
                for path in self.processed_directory.iterdir()
            ]

        return super().delete_by_query(
            index=index,
            body=body,
            conflicts=conflicts,
            refresh=refresh,
        )


class FailingDeleteOpenSearchClient(FakeOpenSearchClient):
    """Simulates an OpenSearch failure during chunk deletion."""

    def delete_by_query(
        self,
        *,
        index: str,
        body: dict[str, Any],
        conflicts: str,
        refresh: bool,
    ) -> dict[str, Any]:
        del index
        del body
        del conflicts
        del refresh

        raise RuntimeError(
            "Simulated OpenSearch deletion failure."
        )


class SnapshotFailingOpenSearchClient(FakeOpenSearchClient):
    """
    Fails only the country-level snapshot search - the metadata
    lookup (by document_id) still succeeds normally. Paired with a
    country_document_lookup override that never touches the client
    at all, the snapshot's own search() call is the ONLY country_code
    -shaped query this client will ever see, so failing every such
    query unconditionally isolates the snapshot step precisely.
    """

    def search(
        self,
        index: str,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        term = body["query"].get("term", {})

        if "country_code" in term:
            raise OpenSearchException(
                "simulated snapshot search failure"
            )

        return super().search(index, body)


class PartialIndexDeleteFailureClient(FakeOpenSearchClient):
    """
    Simulates OpenSearch genuinely removing the document server-side
    before the client-visible delete_by_query call itself errors out
    (e.g. the response is lost after the operation already committed
    server-side) - a stronger simulation than "raises before any
    mutation", per mission "HOTFIX 0.4.9" review 2, section 6.
    """

    def delete_by_query(
        self,
        *,
        index: str,
        body: dict[str, Any],
        conflicts: str,
        refresh: bool,
    ) -> dict[str, Any]:
        del index
        del conflicts
        del refresh

        document_id = body["query"]["term"]["document_id"]
        self.deleted_document_ids.append(document_id)

        raise OpenSearchException(
            "simulated partial index deletion failure"
        )


class ConfigurableDeleteResponseClient(FakeOpenSearchClient):
    """
    Returns a fully caller-controlled delete_by_query response dict,
    to exercise _delete_document_chunks' own response-integrity
    validation - directly and end-to-end through
    delete_indexed_document/reindex_indexed_document (mission
    "HOTFIX 0.4.9" review 3, sections 1/5-7/9-10/12-13). total,
    deleted, version_conflicts, timed_out, and failures are exactly
    whatever the test configures, independent of the fake's own
    country/chunk model - conflicts="proceed" means OpenSearch can
    report any combination of these fields regardless of how many
    chunks a query's term would otherwise match.
    """

    def __init__(
        self,
        *,
        delete_response: dict[str, Any],
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.delete_response = delete_response

    def delete_by_query(
        self,
        *,
        index: str,
        body: dict[str, Any],
        conflicts: str,
        refresh: bool,
    ) -> dict[str, Any]:
        del index
        del conflicts
        del refresh

        document_id = body["query"]["term"]["document_id"]
        self.deleted_document_ids.append(document_id)

        return self.delete_response


class MultiChunkConfigurableDeleteResponseClient(FakeOpenSearchClient):
    """
    Models a country snapshot where any document may have more than
    one chunk (chunk_counts maps document_id -> chunk count) while
    also returning a fully caller-controlled delete_by_query response
    (mission "HOTFIX 0.4.9" review 3, sections 9/10) - needed to
    exercise a deleted count that is genuinely partial (greater than
    zero, less than expected) against a target whose true expected
    count is greater than one.
    """

    def __init__(
        self,
        *,
        delete_response: dict[str, Any],
        chunk_counts: dict[str, int],
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.delete_response = delete_response
        self.chunk_counts = chunk_counts

    def search(
        self,
        index: str,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        term = body["query"].get("term", {})

        if "country_code" not in term:
            return super().search(index, body)

        hits = [
            {
                "_id": f"{document_id}-chunk-{position}",
                "_source": {
                    "document_id": document_id,
                    "chunk_id": f"{document_id}-chunk-{position}",
                    "source_filename": (
                        self.country_source_filenames.get(
                            document_id,
                            self.source_filename,
                        )
                    ),
                    "country": "United Kingdom",
                    "country_code": "GB",
                    "reference_year": 2026,
                },
                "sort": [f"{document_id}-chunk-{position}"],
            }
            for document_id, count in self.chunk_counts.items()
            for position in range(count)
        ]

        return {
            "hits": {
                "total": {"value": len(hits)},
                "hits": hits,
            }
        }

    def delete_by_query(
        self,
        *,
        index: str,
        body: dict[str, Any],
        conflicts: str,
        refresh: bool,
    ) -> dict[str, Any]:
        del index
        del conflicts
        del refresh

        document_id = body["query"]["term"]["document_id"]
        self.deleted_document_ids.append(document_id)

        return self.delete_response


class ReindexTransactionOpenSearchClient:
    """
    Stateful, chunk-granular OpenSearch double for REINDEX
    transaction tests (mission "HOTFIX 0.4.9" review 4).

    Unlike the lighter fakes above, delete_by_query here genuinely
    mutates self.chunks - removing exactly as many chunks as
    configured before responding or raising - and is paired with a
    document_indexer test fixture and a bulk() patch that also write
    into this SAME instance's self.chunks. This lets a test assert on
    the real final chunk state after a rollback, not merely on which
    mock was called.

    The special (possibly partial/failing) delete behavior is scoped
    to target_document_id only - any other document_id (in practice,
    the rollback's own best-effort cleanup of a newly-indexed
    document) is always deleted cleanly in full, so a test configured
    to make the OLD delete fail never accidentally also breaks the
    NEW-cleanup step it did not intend to exercise.
    """

    def __init__(
        self,
        *,
        chunks: dict[str, dict[str, Any]],
        document_metadata: dict[str, dict[str, Any]],
        target_document_id: str,
    ) -> None:
        self.chunks = dict(chunks)
        self.document_metadata = dict(document_metadata)
        self.target_document_id = target_document_id
        self.deleted_document_ids: list[str] = []
        self.remove_count_before_response: int | None = None
        self.raise_exception_after: Exception | None = None
        self.response_overrides: dict[str, Any] = {}
        self.stray_cleanup_calls: int = 0
        self.fail_stray_cleanup: Exception | None = None

    def search(
        self,
        index: str,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        del index

        term = body["query"].get("term", {})

        if "country_code" in term:
            hits = [
                {
                    "_id": chunk_id,
                    "_source": source,
                    "sort": [chunk_id],
                }
                for chunk_id, source in sorted(
                    self.chunks.items()
                )
                if source["country_code"] == term["country_code"]
            ]

            return {
                "hits": {
                    "total": {"value": len(hits)},
                    "hits": hits,
                }
            }

        if "document_id" in term:
            source = self.document_metadata.get(
                term["document_id"]
            )

            if source is None:
                return {"hits": {"hits": []}}

            return {"hits": {"hits": [{"_source": source}]}}

        return {"hits": {"hits": []}}

    def delete_by_query(
        self,
        *,
        index: str,
        body: dict[str, Any],
        conflicts: str,
        refresh: bool,
    ) -> dict[str, Any]:
        del index
        del conflicts
        del refresh

        query = body["query"]
        term = query.get("term")

        if term is not None and "document_id" in term:
            return self._delete_by_document_id(term["document_id"])

        return self._delete_stray_chunks(query)

    def _delete_by_document_id(
        self,
        document_id: str,
    ) -> dict[str, Any]:
        self.deleted_document_ids.append(document_id)

        matching_chunk_ids = [
            chunk_id
            for chunk_id, source in self.chunks.items()
            if source["document_id"] == document_id
        ]

        total = len(matching_chunk_ids)

        if document_id != self.target_document_id:
            # Not the document under special test configuration
            # (typically the rollback's own NEW-cleanup call) -
            # always a clean, complete deletion.
            for chunk_id in matching_chunk_ids:
                del self.chunks[chunk_id]

            return {
                "total": total,
                "deleted": total,
                "version_conflicts": 0,
                "timed_out": False,
                "failures": [],
            }

        remove_count = (
            total
            if self.remove_count_before_response is None
            else self.remove_count_before_response
        )

        for chunk_id in matching_chunk_ids[:remove_count]:
            del self.chunks[chunk_id]

        if self.raise_exception_after is not None:
            raise self.raise_exception_after

        response = {
            "total": total,
            "deleted": remove_count,
            "version_conflicts": 0,
            "timed_out": False,
            "failures": [],
        }
        response.update(self.response_overrides)

        return response

    def _delete_stray_chunks(
        self,
        query: dict[str, Any],
    ) -> dict[str, Any]:
        # The country-wide stray-chunk cleanup shape
        # (_delete_stray_country_chunks): bool/filter/country_code +
        # must_not/terms/chunk_id - delete every chunk NOT in the
        # must_not list.
        self.stray_cleanup_calls += 1

        if self.fail_stray_cleanup is not None:
            raise self.fail_stray_cleanup

        keep_ids: set[str] = set()

        for clause in query.get("bool", {}).get("must_not", []):
            keep_ids.update(
                clause.get("terms", {}).get("chunk_id", [])
            )

        stray_chunk_ids = [
            chunk_id
            for chunk_id in self.chunks
            if chunk_id not in keep_ids
        ]

        for chunk_id in stray_chunk_ids:
            del self.chunks[chunk_id]

        return {
            "total": len(stray_chunk_ids),
            "deleted": len(stray_chunk_ids),
            "version_conflicts": 0,
            "timed_out": False,
            "failures": [],
        }


def _reindex_bulk_fake(fake_client: "ReindexTransactionOpenSearchClient"):
    """
    A bulk() side_effect that writes every restored action back into
    fake_client's own self.chunks - so _restore_country_snapshot's
    effect on the fake's state is real, not merely a recorded call.
    """

    def fake_bulk(client, actions, **kwargs):
        del client
        del kwargs

        action_list = list(actions)

        for action in action_list:
            fake_client.chunks[action["_id"]] = action["_source"]

        return (len(action_list), [])

    return fake_bulk


def _reindex_document_indexer_fixture(
    fake_client: "ReindexTransactionOpenSearchClient",
):
    """
    A document_indexer test fixture that genuinely writes the new
    chunks into fake_client's own self.chunks - mission "HOTFIX
    0.4.9" review 4, section 7's "document_indexer ajoute réellement
    NEW dans le fake store."
    """

    def document_indexer(
        *,
        chunks,
        client=None,
    ) -> DocumentIndexingResult:
        del client

        for chunk in chunks:
            fake_client.chunks[chunk.chunk_id] = {
                "document_id": chunk.document_id,
                "chunk_id": chunk.chunk_id,
                "country_code": chunk.country_code,
                "source_filename": chunk.source_filename,
                "country": chunk.country,
                "reference_year": chunk.reference_year,
            }

        return DocumentIndexingResult(
            index_alias="legal-documents",
            document_id=chunks[0].document_id,
            source_filename=chunks[0].source_filename,
            requested_chunks=len(chunks),
            indexed_chunks=len(chunks),
            stale_chunks_deleted=0,
        )

    return document_indexer


class AdminDocumentLifecycleTests(
    unittest.TestCase
):
    """Tests for reindex and delete operations."""

    def test_reindex_existing_document(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root)

            source_filename = "UK 2026.docx"

            # Stored on disk under the country-derived name ("GB",
            # matching FakeOpenSearchClient's own country_code),
            # never source_filename itself - mission "CONTINUATION
            # PATCH 0.4.3", section 10.
            (
                source_directory
                / "GB.docx"
            ).write_bytes(
                b"docx"
            )

            client = FakeOpenSearchClient(
                source_filename=source_filename
            )

            def chunk_builder(
                path: Path,
            ) -> list[DocumentChunk]:
                return [
                    _build_chunk(
                        document_id=OLD_DOCUMENT_ID,
                        source_filename=path.name,
                    )
                ]

            def document_indexer(
                *,
                chunks,
                client=None,
            ) -> DocumentIndexingResult:
                del client

                return DocumentIndexingResult(
                    index_alias="legal-documents",
                    document_id=(
                        chunks[0].document_id
                    ),
                    source_filename=(
                        chunks[0].source_filename
                    ),
                    requested_chunks=1,
                    indexed_chunks=1,
                    stale_chunks_deleted=0,
                )

            response = reindex_indexed_document(
                document_id=OLD_DOCUMENT_ID,
                source_directory=source_directory,
                client=client,
                chunk_builder=chunk_builder,
                document_indexer=document_indexer,
            )

            self.assertEqual(
                response.status,
                "reindexed",
            )

            self.assertFalse(
                response.document_id_changed
            )

            self.assertEqual(
                response.indexed_chunks,
                1,
            )

            self.assertEqual(
                client.deleted_document_ids,
                [],
            )

    def test_changed_document_id_removes_previous_version(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root)

            source_filename = "UK 2026.docx"

            (
                source_directory
                / "GB.docx"
            ).write_bytes(
                b"docx"
            )

            client = FakeOpenSearchClient(
                source_filename=source_filename
            )

            def chunk_builder(
                path: Path,
            ) -> list[DocumentChunk]:
                return [
                    _build_chunk(
                        document_id=NEW_DOCUMENT_ID,
                        source_filename=path.name,
                    )
                ]

            def document_indexer(
                *,
                chunks,
                client=None,
            ) -> DocumentIndexingResult:
                del client

                return DocumentIndexingResult(
                    index_alias="legal-documents",
                    document_id=(
                        chunks[0].document_id
                    ),
                    source_filename=(
                        chunks[0].source_filename
                    ),
                    requested_chunks=1,
                    indexed_chunks=1,
                    stale_chunks_deleted=0,
                )

            response = reindex_indexed_document(
                document_id=OLD_DOCUMENT_ID,
                source_directory=source_directory,
                client=client,
                chunk_builder=chunk_builder,
                document_indexer=document_indexer,
            )

            self.assertTrue(
                response.document_id_changed
            )

            self.assertEqual(
                response.document_id,
                NEW_DOCUMENT_ID,
            )

            self.assertEqual(
                response.previous_chunks_deleted,
                1,
            )

            self.assertEqual(
                client.deleted_document_ids,
                [
                    OLD_DOCUMENT_ID,
                ],
            )

    def test_reindex_rejects_missing_source(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(
                AdminDocumentSourceMissingError
            ):
                reindex_indexed_document(
                    document_id=OLD_DOCUMENT_ID,
                    source_directory=Path(root),
                    client=FakeOpenSearchClient(),
                )

    def test_delete_removes_chunks_and_source(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as root:
            source_directory = (
                Path(root)
                / "source"
            )

            processed_directory = (
                Path(root)
                / "processed"
            )

            source_directory.mkdir()

            source_filename = "UK 2026.docx"

            source_path = (
                source_directory
                / "GB.docx"
            )

            source_path.write_bytes(
                b"docx"
            )

            client = FakeOpenSearchClient(
                source_filename=source_filename
            )

            response = delete_indexed_document(
                document_id=OLD_DOCUMENT_ID,
                source_directory=source_directory,
                processed_directory=processed_directory,
                client=client,
            )

            self.assertEqual(
                response.status,
                "deleted",
            )

            self.assertEqual(
                response.deleted_chunks,
                1,
            )

            self.assertTrue(
                response.source_file_deleted
            )

            self.assertFalse(
                source_path.exists()
            )

            self.assertEqual(
                client.deleted_document_ids,
                [
                    OLD_DOCUMENT_ID,
                ],
            )

            # No leftover backup file: source_directory must end up
            # completely empty, not just missing the original name.
            self.assertEqual(
                list(
                    real_source_entries(source_directory)
                ),
                [],
            )

    def test_delete_backup_is_created_next_to_source_not_processed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as root:
            source_directory = (
                Path(root)
                / "source"
            )

            processed_directory = (
                Path(root)
                / "processed"
            )

            source_directory.mkdir()

            source_filename = "UK 2026.docx"

            source_path = (
                source_directory
                / "GB.docx"
            )

            source_path.write_bytes(
                b"docx"
            )

            client = BackupInspectingOpenSearchClient(
                source_directory=source_directory,
                processed_directory=processed_directory,
                source_filename=source_filename,
            )

            delete_indexed_document(
                document_id=OLD_DOCUMENT_ID,
                source_directory=source_directory,
                processed_directory=processed_directory,
                client=client,
            )

            self.assertIsNotNone(
                client.backup_path_at_delete_time
            )

            self.assertEqual(
                client.backup_path_at_delete_time.parent,
                source_directory,
            )

            self.assertEqual(
                client.processed_directory_entries_at_delete_time,
                [],
            )

    def test_delete_backup_path_does_not_end_with_docx(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as root:
            source_directory = (
                Path(root)
                / "source"
            )

            processed_directory = (
                Path(root)
                / "processed"
            )

            source_directory.mkdir()

            source_filename = "UK 2026.docx"

            source_path = (
                source_directory
                / "GB.docx"
            )

            source_path.write_bytes(
                b"docx"
            )

            client = BackupInspectingOpenSearchClient(
                source_directory=source_directory,
                processed_directory=processed_directory,
                source_filename=source_filename,
            )

            delete_indexed_document(
                document_id=OLD_DOCUMENT_ID,
                source_directory=source_directory,
                processed_directory=processed_directory,
                client=client,
            )

            self.assertIsNotNone(
                client.backup_path_at_delete_time
            )

            self.assertFalse(
                client.backup_path_at_delete_time.name.endswith(
                    ".docx"
                )
            )

    def test_delete_restores_source_file_exactly_on_opensearch_failure(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as root:
            source_directory = (
                Path(root)
                / "source"
            )

            processed_directory = (
                Path(root)
                / "processed"
            )

            source_directory.mkdir()

            source_filename = "UK 2026.docx"

            source_path = (
                source_directory
                / "GB.docx"
            )

            original_bytes = b"original-docx-bytes"

            source_path.write_bytes(
                original_bytes
            )

            client = FailingDeleteOpenSearchClient(
                source_filename=source_filename
            )

            with self.assertRaises(
                RuntimeError
            ):
                delete_indexed_document(
                    document_id=OLD_DOCUMENT_ID,
                    source_directory=source_directory,
                    processed_directory=processed_directory,
                    client=client,
                )

            self.assertTrue(
                source_path.exists()
            )

            self.assertEqual(
                source_path.read_bytes(),
                original_bytes,
            )

            self.assertEqual(
                list(
                    real_source_entries(source_directory)
                ),
                [
                    source_path,
                ],
            )

    def test_unknown_document_is_rejected(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(
                AdminDocumentNotFoundError
            ):
                delete_indexed_document(
                    document_id=OLD_DOCUMENT_ID,
                    source_directory=(
                        Path(root) / "source"
                    ),
                    processed_directory=(
                        Path(root) / "processed"
                    ),
                    client=FakeOpenSearchClient(
                        document_exists=False
                    ),
                )

    def test_reindex_previous_document_partial_deletion_is_not_silently_accepted(
        self,
    ) -> None:
        # Mission "HOTFIX 0.4.9" review 3, section 13, kept accurate
        # after review 4's rewrite - the hardened
        # _delete_document_chunks integrity checks apply to every
        # caller, including reindex's own removal of the previous
        # document_id once a metadata change produces a new one.
        # Review 4 gave reindex its own pre-reindex snapshot and
        # expected_chunks (ReindexTransactionalIntegrityTests exercises
        # that mechanism directly with a genuinely stateful fake); this
        # test instead proves the fake's default single-chunk model
        # still gets rejected by _delete_document_chunks' OWN response-
        # integrity checks - the version_conflicts check AND the
        # total-vs-deleted self-consistency check (added during review
        # 3's independent verification) - independently of
        # expected_chunks, confirming neither check was weakened by
        # review 4's changes.
        bad_responses = {
            "version_conflicts": {
                "total": 1,
                "deleted": 1,
                "version_conflicts": 1,
                "timed_out": False,
                "failures": [],
            },
            "total_deleted_mismatch": {
                "total": 5,
                "deleted": 3,
                "version_conflicts": 0,
                "timed_out": False,
                "failures": [],
            },
        }

        for kind, delete_response in bad_responses.items():
            with self.subTest(kind=kind):
                with tempfile.TemporaryDirectory() as root:
                    source_directory = Path(root)

                    (
                        source_directory / "GB.docx"
                    ).write_bytes(b"docx-bytes")

                    client = ConfigurableDeleteResponseClient(
                        delete_response=delete_response,
                        source_filename="GB.docx",
                    )

                    def chunk_builder(
                        path: Path,
                    ) -> list[DocumentChunk]:
                        del path
                        return [
                            _build_chunk(
                                document_id=NEW_DOCUMENT_ID,
                                source_filename="GB.docx",
                            )
                        ]

                    def document_indexer(
                        *,
                        chunks,
                        client=None,
                    ) -> DocumentIndexingResult:
                        del client
                        return DocumentIndexingResult(
                            index_alias="legal-documents",
                            document_id=chunks[0].document_id,
                            source_filename=(
                                chunks[0].source_filename
                            ),
                            requested_chunks=1,
                            indexed_chunks=1,
                            stale_chunks_deleted=0,
                        )

                    with self.assertRaises(
                        AdminDocumentLifecycleError
                    ):
                        reindex_indexed_document(
                            document_id=OLD_DOCUMENT_ID,
                            source_directory=source_directory,
                            client=client,
                            chunk_builder=chunk_builder,
                            document_indexer=document_indexer,
                        )


class LegacySourceResolutionTests(unittest.TestCase):
    """
    Mission "HOTFIX 0.4.4", section 7.B/E/F - Reindex and Delete for a
    document indexed before country-keyed storage existed, still
    physically stored under its own historical filename.
    """

    LEGACY_FILENAME = "Labour and Employment Law in UK 2026.docx"

    def test_reindex_opens_the_exact_historical_file(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root)

            legacy_path = source_directory / self.LEGACY_FILENAME
            legacy_path.write_bytes(b"legacy-docx-bytes")

            client = FakeOpenSearchClient(
                source_filename=self.LEGACY_FILENAME
            )

            opened_paths: list[Path] = []

            def chunk_builder(path: Path) -> list[DocumentChunk]:
                opened_paths.append(path)
                return [
                    _build_chunk(
                        document_id=OLD_DOCUMENT_ID,
                        source_filename=path.name,
                    )
                ]

            def document_indexer(
                *,
                chunks,
                client=None,
            ) -> DocumentIndexingResult:
                del client
                return DocumentIndexingResult(
                    index_alias="legal-documents",
                    document_id=chunks[0].document_id,
                    source_filename=chunks[0].source_filename,
                    requested_chunks=1,
                    indexed_chunks=1,
                    stale_chunks_deleted=0,
                )

            response = reindex_indexed_document(
                document_id=OLD_DOCUMENT_ID,
                source_directory=source_directory,
                client=client,
                chunk_builder=chunk_builder,
                document_indexer=document_indexer,
            )

            self.assertEqual(response.status, "reindexed")
            self.assertEqual(opened_paths, [legacy_path])

            # No CODE.docx was ever created or expected - the
            # historical file was never renamed.
            self.assertFalse(
                (source_directory / "GB.docx").exists()
            )

    def test_reindex_refuses_on_source_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root)

            (
                source_directory / self.LEGACY_FILENAME
            ).write_bytes(b"legacy-bytes")

            (
                source_directory / "GB.docx"
            ).write_bytes(b"canonical-bytes")

            client = FakeOpenSearchClient(
                source_filename=self.LEGACY_FILENAME
            )

            with self.assertRaises(
                AdminDocumentSourceConflictError
            ):
                reindex_indexed_document(
                    document_id=OLD_DOCUMENT_ID,
                    source_directory=source_directory,
                    client=client,
                )

            # Nothing on disk changed.
            self.assertEqual(
                (
                    source_directory / self.LEGACY_FILENAME
                ).read_bytes(),
                b"legacy-bytes",
            )
            self.assertEqual(
                (source_directory / "GB.docx").read_bytes(),
                b"canonical-bytes",
            )

    def test_delete_targets_only_the_historical_file(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root) / "source"
            processed_directory = Path(root) / "processed"
            source_directory.mkdir()

            legacy_path = source_directory / self.LEGACY_FILENAME
            legacy_path.write_bytes(b"legacy-docx-bytes")

            # An unrelated file that must never be touched.
            decoy_path = (
                source_directory
                / "Labour and Employment Law in Spain 2026.docx"
            )
            decoy_path.write_bytes(b"unrelated-spain-bytes")

            client = FakeOpenSearchClient(
                source_filename=self.LEGACY_FILENAME
            )

            response = delete_indexed_document(
                document_id=OLD_DOCUMENT_ID,
                source_directory=source_directory,
                processed_directory=processed_directory,
                client=client,
            )

            self.assertEqual(response.status, "deleted")
            self.assertTrue(response.source_file_deleted)
            self.assertFalse(legacy_path.exists())

            # The decoy file survives untouched.
            self.assertTrue(decoy_path.exists())
            self.assertEqual(
                decoy_path.read_bytes(),
                b"unrelated-spain-bytes",
            )

    def test_delete_of_the_last_document_retires_every_conflicting_file(
        self,
    ) -> None:
        # Mission "HOTFIX 0.4.9" - the exact Australia-shaped bug this
        # mission opened with: a country with two on-disk source
        # files (a legacy name and the canonical {CODE}.docx) for what
        # is otherwise its only active document must no longer fail
        # DELETE with "Multiple distinct source files resolve..." -
        # since nothing else depends on either file once this, the
        # country's last document, is removed, both are safely
        # retired together.
        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root) / "source"
            processed_directory = Path(root) / "processed"
            source_directory.mkdir()

            (
                source_directory / self.LEGACY_FILENAME
            ).write_bytes(b"legacy-bytes")

            (
                source_directory / "GB.docx"
            ).write_bytes(b"canonical-bytes")

            client = FakeOpenSearchClient(
                source_filename=self.LEGACY_FILENAME
            )

            response = delete_indexed_document(
                document_id=OLD_DOCUMENT_ID,
                source_directory=source_directory,
                processed_directory=processed_directory,
                client=client,
            )

            self.assertEqual(response.status, "deleted")
            self.assertTrue(response.source_file_deleted)
            self.assertFalse(response.source_cleanup_deferred)
            self.assertEqual(
                client.deleted_document_ids,
                [OLD_DOCUMENT_ID],
            )

            # Both candidate files are gone, with no leftover backup.
            self.assertEqual(
                list(real_source_entries(source_directory)),
                [],
            )

    def test_delete_of_one_duplicate_defers_file_cleanup(self) -> None:
        # The other half of the same bug: when a second document_id
        # still shares this country (Australia's real, currently-live
        # state - two indexed documents, both status=indexed_source_
        # conflict), deleting one of them must still succeed - but
        # must not guess which physical file belongs to which
        # remaining document, so neither file is touched.
        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root) / "source"
            processed_directory = Path(root) / "processed"
            source_directory.mkdir()

            (
                source_directory / self.LEGACY_FILENAME
            ).write_bytes(b"legacy-bytes")

            (
                source_directory / "GB.docx"
            ).write_bytes(b"canonical-bytes")

            sibling_document_id = "doc_" + "e" * 64

            client = FakeOpenSearchClient(
                source_filename=self.LEGACY_FILENAME,
                country_document_ids=[
                    OLD_DOCUMENT_ID,
                    sibling_document_id,
                ],
            )

            response = delete_indexed_document(
                document_id=OLD_DOCUMENT_ID,
                source_directory=source_directory,
                processed_directory=processed_directory,
                client=client,
            )

            self.assertEqual(response.status, "deleted")
            self.assertFalse(response.source_file_deleted)
            self.assertTrue(response.source_cleanup_deferred)
            self.assertEqual(
                client.deleted_document_ids,
                [OLD_DOCUMENT_ID],
            )

            # Neither file was touched - the sibling document_id may
            # still depend on either of them.
            self.assertEqual(
                (
                    source_directory / self.LEGACY_FILENAME
                ).read_bytes(),
                b"legacy-bytes",
            )
            self.assertEqual(
                (source_directory / "GB.docx").read_bytes(),
                b"canonical-bytes",
            )

    def test_delete_of_one_of_three_duplicates_defers_file_cleanup(
        self,
    ) -> None:
        # Three legacy document_ids sharing one country - deleting
        # any single one must still leave the other two exploitable
        # and must not touch any file while they remain.
        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root) / "source"
            processed_directory = Path(root) / "processed"
            source_directory.mkdir()

            (
                source_directory / self.LEGACY_FILENAME
            ).write_bytes(b"legacy-bytes")

            (
                source_directory / "GB.docx"
            ).write_bytes(b"canonical-bytes")

            sibling_ids = [
                "doc_" + "e" * 64,
                "doc_" + "f" * 64,
            ]

            client = FakeOpenSearchClient(
                source_filename=self.LEGACY_FILENAME,
                country_document_ids=[
                    OLD_DOCUMENT_ID,
                    *sibling_ids,
                ],
            )

            response = delete_indexed_document(
                document_id=OLD_DOCUMENT_ID,
                source_directory=source_directory,
                processed_directory=processed_directory,
                client=client,
            )

            self.assertEqual(response.status, "deleted")
            self.assertFalse(response.source_file_deleted)
            self.assertTrue(response.source_cleanup_deferred)
            self.assertEqual(
                client.deleted_document_ids,
                [OLD_DOCUMENT_ID],
            )
            self.assertEqual(
                (
                    source_directory / self.LEGACY_FILENAME
                ).read_bytes(),
                b"legacy-bytes",
            )
            self.assertEqual(
                (source_directory / "GB.docx").read_bytes(),
                b"canonical-bytes",
            )

    def test_deleting_down_to_the_last_of_three_then_retires_files(
        self,
    ) -> None:
        # Deleting the third and final remaining document_id (the
        # other two already gone) is exactly the "last document"
        # case - both files may now be safely retired together.
        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root) / "source"
            processed_directory = Path(root) / "processed"
            source_directory.mkdir()

            (
                source_directory / self.LEGACY_FILENAME
            ).write_bytes(b"legacy-bytes")

            (
                source_directory / "GB.docx"
            ).write_bytes(b"canonical-bytes")

            client = FakeOpenSearchClient(
                source_filename=self.LEGACY_FILENAME,
                country_document_ids=[OLD_DOCUMENT_ID],
            )

            response = delete_indexed_document(
                document_id=OLD_DOCUMENT_ID,
                source_directory=source_directory,
                processed_directory=processed_directory,
                client=client,
            )

            self.assertEqual(response.status, "deleted")
            self.assertTrue(response.source_file_deleted)
            self.assertFalse(response.source_cleanup_deferred)
            self.assertEqual(
                list(real_source_entries(source_directory)),
                [],
            )


class DeleteTransactionalIntegrityTests(unittest.TestCase):
    """
    Mission "HOTFIX 0.4.9" review 2 - the delete transaction's exact
    ordering and rollback semantics: a snapshot must exist before any
    filesystem mutation; once the index delete has committed,
    finalization (removing .bak files) is best-effort and never
    triggers an OpenSearch/file rollback; a rollback that is itself
    only partially successful must say so explicitly, never claim a
    silent, complete recovery.
    """

    LEGACY_FILENAME = "Labour and Employment Law in UK 2026.docx"

    @staticmethod
    def _only_this_document(document_id: str):
        """A country_document_lookup override that never touches the
        OpenSearch client at all - isolates whichever client method
        a test wants to fail without that method also being reached
        by this earlier lookup step."""

        def lookup(country_code: str, client=None):
            del country_code
            del client

            return [
                ExistingCountryDocument(
                    document_id=document_id,
                    source_filename=(
                        DeleteTransactionalIntegrityTests
                        .LEGACY_FILENAME
                    ),
                    country="United Kingdom",
                    country_code="GB",
                    reference_year=2026,
                )
            ]

        return lookup

    def test_last_document_snapshot_failure_leaves_sources_untouched(
        self,
    ) -> None:
        # SNAPSHOT_FAILURE - the snapshot is acquired before any
        # filesystem mutation; if it fails, nothing has been touched.
        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root) / "source"
            processed_directory = Path(root) / "processed"
            source_directory.mkdir()

            legacy_path = source_directory / self.LEGACY_FILENAME
            canonical_path = source_directory / "GB.docx"
            legacy_path.write_bytes(b"legacy-bytes")
            canonical_path.write_bytes(b"canonical-bytes")

            client = SnapshotFailingOpenSearchClient(
                source_filename=self.LEGACY_FILENAME,
                country_document_ids=[OLD_DOCUMENT_ID],
            )

            with self.assertRaises(Exception):
                delete_indexed_document(
                    document_id=OLD_DOCUMENT_ID,
                    source_directory=source_directory,
                    processed_directory=processed_directory,
                    client=client,
                    country_document_lookup=(
                        self._only_this_document(OLD_DOCUMENT_ID)
                    ),
                )

            # Nothing was touched at all: no .bak/.incoming residue,
            # both original files present with their original bytes.
            self.assertEqual(
                sorted(p.name for p in real_source_entries(source_directory)),
                sorted([self.LEGACY_FILENAME, "GB.docx"]),
            )
            self.assertEqual(
                legacy_path.read_bytes(), b"legacy-bytes"
            )
            self.assertEqual(
                canonical_path.read_bytes(), b"canonical-bytes"
            )
            self.assertEqual(client.deleted_document_ids, [])

    def test_second_source_backup_failure_restores_first_source(
        self,
    ) -> None:
        # SECOND_BACKUP_MOVE_FAILURE
        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root) / "source"
            processed_directory = Path(root) / "processed"
            source_directory.mkdir()

            legacy_path = source_directory / self.LEGACY_FILENAME
            canonical_path = source_directory / "GB.docx"
            legacy_path.write_bytes(b"legacy-bytes")
            canonical_path.write_bytes(b"canonical-bytes")

            legacy_hash = hashlib.sha256(
                legacy_path.read_bytes()
            ).hexdigest()
            canonical_hash = hashlib.sha256(
                canonical_path.read_bytes()
            ).hexdigest()

            client = FakeOpenSearchClient(
                source_filename=self.LEGACY_FILENAME,
                country_document_ids=[OLD_DOCUMENT_ID],
            )

            real_replace = os.replace
            call_count = {"n": 0}

            def replace_second_call_fails(src, dst, *a, **kw):
                call_count["n"] += 1

                if call_count["n"] == 2:
                    raise OSError(
                        "simulated second backup move failure"
                    )

                return real_replace(src, dst, *a, **kw)

            with patch(
                "app.services.admin_document_lifecycle.os.replace",
                side_effect=replace_second_call_fails,
            ):
                with self.assertRaises(AdminDocumentLifecycleError):
                    delete_indexed_document(
                        document_id=OLD_DOCUMENT_ID,
                        source_directory=source_directory,
                        processed_directory=processed_directory,
                        client=client,
                        country_document_lookup=(
                            self._only_this_document(OLD_DOCUMENT_ID)
                        ),
                    )

            # Both files back at their original paths, byte-identical
            # - no backup residue, no OpenSearch mutation attempted.
            self.assertEqual(
                sorted(p.name for p in real_source_entries(source_directory)),
                sorted([self.LEGACY_FILENAME, "GB.docx"]),
            )
            self.assertEqual(
                hashlib.sha256(
                    legacy_path.read_bytes()
                ).hexdigest(),
                legacy_hash,
            )
            self.assertEqual(
                hashlib.sha256(
                    canonical_path.read_bytes()
                ).hexdigest(),
                canonical_hash,
            )
            self.assertEqual(client.deleted_document_ids, [])

    def test_two_source_index_delete_failure_restores_files_and_index(
        self,
    ) -> None:
        # TWO_SOURCE_INDEX_FAILURE - a stronger simulation: OpenSearch
        # genuinely registers the deletion server-side before the
        # client call itself errors out, never merely "raises before
        # any mutation".
        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root) / "source"
            processed_directory = Path(root) / "processed"
            source_directory.mkdir()

            legacy_path = source_directory / self.LEGACY_FILENAME
            canonical_path = source_directory / "GB.docx"
            legacy_path.write_bytes(b"legacy-bytes")
            canonical_path.write_bytes(b"canonical-bytes")

            legacy_hash = hashlib.sha256(
                legacy_path.read_bytes()
            ).hexdigest()
            canonical_hash = hashlib.sha256(
                canonical_path.read_bytes()
            ).hexdigest()

            client = PartialIndexDeleteFailureClient(
                source_filename=self.LEGACY_FILENAME,
                country_document_ids=[OLD_DOCUMENT_ID],
            )

            restore_bulk_calls: list[Any] = []

            def fake_bulk(client, actions, **kwargs):
                del client
                action_list = list(actions)
                restore_bulk_calls.append(action_list)
                return (len(action_list), [])

            with patch(
                "app.services.document_indexer.bulk",
                side_effect=fake_bulk,
            ), patch(
                "app.services.document_indexer."
                "ensure_legal_documents_index"
            ):
                with self.assertRaises(AdminDocumentLifecycleError):
                    delete_indexed_document(
                        document_id=OLD_DOCUMENT_ID,
                        source_directory=source_directory,
                        processed_directory=processed_directory,
                        client=client,
                        country_document_lookup=(
                            self._only_this_document(OLD_DOCUMENT_ID)
                        ),
                    )

            # Files restored exactly.
            self.assertEqual(
                sorted(p.name for p in real_source_entries(source_directory)),
                sorted([self.LEGACY_FILENAME, "GB.docx"]),
            )
            self.assertEqual(
                hashlib.sha256(
                    legacy_path.read_bytes()
                ).hexdigest(),
                legacy_hash,
            )
            self.assertEqual(
                hashlib.sha256(
                    canonical_path.read_bytes()
                ).hexdigest(),
                canonical_hash,
            )

            # Index restored: the snapshot (taken before any mutation,
            # containing the document's own original chunk) was
            # re-indexed exactly once.
            self.assertEqual(len(restore_bulk_calls), 1)
            restored_source = restore_bulk_calls[0][0]["_source"]
            self.assertEqual(
                restored_source["document_id"], OLD_DOCUMENT_ID
            )

    def test_partial_multi_source_finalization_is_deferred_not_rolled_back(
        self,
    ) -> None:
        # MULTI_SOURCE_FINALIZATION_FAILURE
        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root) / "source"
            processed_directory = Path(root) / "processed"
            source_directory.mkdir()

            legacy_path = source_directory / self.LEGACY_FILENAME
            canonical_path = source_directory / "GB.docx"
            legacy_path.write_bytes(b"legacy-bytes")
            canonical_path.write_bytes(b"canonical-bytes")

            client = FakeOpenSearchClient(
                source_filename=self.LEGACY_FILENAME,
                country_document_ids=[OLD_DOCUMENT_ID],
            )

            real_unlink = Path.unlink
            call_count = {"n": 0}

            def unlink_second_backup_fails(self_path, *a, **kw):
                if self_path.name.endswith(".bak"):
                    call_count["n"] += 1

                    if call_count["n"] == 2:
                        raise OSError(
                            "simulated second finalization failure"
                        )

                return real_unlink(self_path, *a, **kw)

            with patch.object(
                Path, "unlink", unlink_second_backup_fails
            ):
                response = delete_indexed_document(
                    document_id=OLD_DOCUMENT_ID,
                    source_directory=source_directory,
                    processed_directory=processed_directory,
                    client=client,
                    country_document_lookup=(
                        self._only_this_document(OLD_DOCUMENT_ID)
                    ),
                )

            # DELETE is still a success - the index delete already
            # committed before finalization ever started.
            self.assertEqual(response.status, "deleted")
            self.assertEqual(
                client.deleted_document_ids, [OLD_DOCUMENT_ID]
            )
            self.assertTrue(response.source_cleanup_deferred)
            self.assertFalse(response.source_file_deleted)

            # Neither active path reappears...
            self.assertFalse(legacy_path.exists())
            self.assertFalse(canonical_path.exists())

            # ...one backup was fully finalized (gone), the other
            # remains under its hidden name - never restored, never
            # re-presented as active, and the response never claims
            # every file was cleaned up.
            remaining = list(real_source_entries(source_directory))
            self.assertEqual(len(remaining), 1)
            self.assertTrue(remaining[0].name.endswith(".bak"))

    def test_single_source_finalization_failure_is_deferred_not_rolled_back(
        self,
    ) -> None:
        # SINGLE_SOURCE_FINALIZATION_FAILURE - replaces the previous
        # test_filesystem_cleanup_failure_on_last_document_restores_
        # index_and_files contract, which promised a full rollback on
        # any finalization failure - architecturally impossible to
        # guarantee once a backup may have already been permanently
        # unlinked (mission "HOTFIX 0.4.9" review 2, sections 2/8).
        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root) / "source"
            processed_directory = Path(root) / "processed"
            source_directory.mkdir()

            source_path = source_directory / self.LEGACY_FILENAME
            source_path.write_bytes(b"legacy-docx-bytes")

            client = FakeOpenSearchClient(
                source_filename=self.LEGACY_FILENAME,
                country_document_ids=[OLD_DOCUMENT_ID],
            )

            real_unlink = Path.unlink

            def failing_unlink(self_path, *args, **kwargs):
                if self_path.name.endswith(".bak"):
                    raise OSError(
                        "simulated filesystem cleanup failure"
                    )

                return real_unlink(self_path, *args, **kwargs)

            with patch.object(Path, "unlink", failing_unlink):
                response = delete_indexed_document(
                    document_id=OLD_DOCUMENT_ID,
                    source_directory=source_directory,
                    processed_directory=processed_directory,
                    client=client,
                    country_document_lookup=(
                        self._only_this_document(OLD_DOCUMENT_ID)
                    ),
                )

            # DELETE is a success - the index delete already
            # committed. The active source is gone; its backup
            # remains under a hidden name (never restored, never
            # re-deleted); the response reflects this honestly.
            self.assertEqual(response.status, "deleted")
            self.assertEqual(
                client.deleted_document_ids, [OLD_DOCUMENT_ID]
            )
            self.assertTrue(response.source_cleanup_deferred)
            self.assertFalse(response.source_file_deleted)
            self.assertFalse(source_path.exists())

            remaining = list(real_source_entries(source_directory))
            self.assertEqual(len(remaining), 1)
            self.assertTrue(remaining[0].name.endswith(".bak"))

    def test_backup_restore_failure_is_reported_explicitly(
        self,
    ) -> None:
        # RESTORE_FAILURE - a reversible step fails (source B's
        # backup move), triggering a rollback attempt of source A's
        # already-created backup - and that rollback ALSO fails.
        # Never a silent pass; the error must say the rollback itself
        # was incomplete.
        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root) / "source"
            processed_directory = Path(root) / "processed"
            source_directory.mkdir()

            legacy_path = source_directory / self.LEGACY_FILENAME
            canonical_path = source_directory / "GB.docx"
            legacy_path.write_bytes(b"legacy-bytes")
            canonical_path.write_bytes(b"canonical-bytes")

            client = FakeOpenSearchClient(
                source_filename=self.LEGACY_FILENAME,
                country_document_ids=[OLD_DOCUMENT_ID],
            )

            real_replace = os.replace
            call_count = {"n": 0}

            def replace_fails_on_second_and_third_call(
                src, dst, *a, **kw
            ):
                call_count["n"] += 1

                # Call 1: legacy -> backup (succeeds).
                # Call 2: canonical -> backup (fails - triggers
                #   rollback of call 1's backup).
                # Call 3: the rollback itself (backup -> legacy),
                #   which also fails.
                if call_count["n"] in (2, 3):
                    raise OSError(
                        f"simulated failure on call {call_count['n']}"
                    )

                return real_replace(src, dst, *a, **kw)

            with patch(
                "app.services.admin_document_lifecycle.os.replace",
                side_effect=replace_fails_on_second_and_third_call,
            ):
                with self.assertRaises(
                    AdminDocumentLifecycleError
                ) as context:
                    delete_indexed_document(
                        document_id=OLD_DOCUMENT_ID,
                        source_directory=source_directory,
                        processed_directory=processed_directory,
                        client=client,
                        country_document_lookup=(
                            self._only_this_document(OLD_DOCUMENT_ID)
                        ),
                    )

            # The error explicitly names the rollback itself as
            # incomplete - never a generic message indistinguishable
            # from a fully-successful rollback.
            self.assertIn(
                "manual recovery",
                str(context.exception).casefold(),
            )
            self.assertIsInstance(
                context.exception.__cause__,
                DeleteBackupRestoreError,
            )
            self.assertEqual(client.deleted_document_ids, [])

    def test_other_documents_remain_never_touches_any_source_file(
        self,
    ) -> None:
        # Section 10 (N=2 and N=3) - strengthens the existing
        # test_delete_of_one_duplicate_defers_file_cleanup /
        # test_delete_of_one_of_three_duplicates_defers_file_cleanup
        # (kept, unchanged) with an explicit proof that this branch
        # never even calls os.replace or Path.unlink for a source
        # file - not merely that file contents happen to be
        # unchanged afterward.
        for sibling_ids in (
            ["doc_" + "e" * 64],
            ["doc_" + "e" * 64, "doc_" + "f" * 64],
        ):
            with self.subTest(sibling_count=len(sibling_ids)):
                with tempfile.TemporaryDirectory() as root:
                    source_directory = Path(root) / "source"
                    processed_directory = Path(root) / "processed"
                    source_directory.mkdir()

                    (
                        source_directory / self.LEGACY_FILENAME
                    ).write_bytes(b"legacy-bytes")
                    (
                        source_directory / "GB.docx"
                    ).write_bytes(b"canonical-bytes")

                    client = FakeOpenSearchClient(
                        source_filename=self.LEGACY_FILENAME,
                        country_document_ids=[
                            OLD_DOCUMENT_ID,
                            *sibling_ids,
                        ],
                    )

                    def replace_must_not_be_called(*a, **kw):
                        raise AssertionError(
                            "os.replace must never be called when "
                            "other documents remain for the country"
                        )

                    # Mission "ORDER 5C": deleting the target
                    # document_id's own section-edit state file (an
                    # unambiguous, per-document_id path nested inside
                    # source_directory - never one of the ambiguous
                    # candidate source files this test guards) is a
                    # legitimate unlink even in this branch. Only a
                    # real candidate source file may never be
                    # unlinked here.
                    guarded_source_names = {
                        self.LEGACY_FILENAME,
                        "GB.docx",
                    }
                    original_unlink = Path.unlink

                    def unlink_must_not_be_called(self_path, *a, **kw):
                        if self_path.name in guarded_source_names:
                            raise AssertionError(
                                "Path.unlink must never be called on "
                                "a source file when other documents "
                                "remain for the country"
                            )

                        return original_unlink(self_path, *a, **kw)

                    with patch(
                        "app.services.admin_document_lifecycle."
                        "os.replace",
                        side_effect=replace_must_not_be_called,
                    ), patch.object(
                        Path,
                        "unlink",
                        unlink_must_not_be_called,
                    ):
                        response = delete_indexed_document(
                            document_id=OLD_DOCUMENT_ID,
                            source_directory=source_directory,
                            processed_directory=processed_directory,
                            client=client,
                        )

                    self.assertEqual(response.status, "deleted")
                    self.assertTrue(response.source_cleanup_deferred)
                    self.assertFalse(response.source_file_deleted)
                    self.assertEqual(
                        client.deleted_document_ids,
                        [OLD_DOCUMENT_ID],
                    )
                    for sibling_id in sibling_ids:
                        self.assertNotIn(
                            sibling_id, client.deleted_document_ids
                        )

    def test_last_document_delete_by_query_integrity_failures_restore_files_and_index(
        self,
    ) -> None:
        # DELETE_BY_QUERY_FAILURES_VALIDATED / VERSION_CONFLICTS_
        # VALIDATED / TIMEOUT_VALIDATED, end-to-end (mission "HOTFIX
        # 0.4.9" review 3, sections 5-7): none of these responses may
        # ever be accepted as a successful delete, and each must
        # still trigger the exact same full files+index rollback
        # already proven by
        # test_two_source_index_delete_failure_restores_files_and_index
        # - the rollback path does not care which specific integrity
        # check inside _delete_document_chunks raised.
        bad_responses = {
            "failures": {
                "total": 1,
                "deleted": 1,
                "version_conflicts": 0,
                "timed_out": False,
                "failures": [
                    {"cause": {"reason": "simulated failure"}}
                ],
            },
            "version_conflicts": {
                "total": 1,
                "deleted": 1,
                "version_conflicts": 1,
                "timed_out": False,
                "failures": [],
            },
            "timed_out": {
                "total": 0,
                "deleted": 0,
                "version_conflicts": 0,
                "timed_out": True,
                "failures": [],
            },
        }

        for kind, delete_response in bad_responses.items():
            with self.subTest(kind=kind):
                with tempfile.TemporaryDirectory() as root:
                    source_directory = Path(root) / "source"
                    processed_directory = Path(root) / "processed"
                    source_directory.mkdir()

                    legacy_path = (
                        source_directory / self.LEGACY_FILENAME
                    )
                    canonical_path = source_directory / "GB.docx"
                    legacy_path.write_bytes(b"legacy-bytes")
                    canonical_path.write_bytes(b"canonical-bytes")

                    legacy_hash = hashlib.sha256(
                        legacy_path.read_bytes()
                    ).hexdigest()
                    canonical_hash = hashlib.sha256(
                        canonical_path.read_bytes()
                    ).hexdigest()

                    client = ConfigurableDeleteResponseClient(
                        delete_response=delete_response,
                        source_filename=self.LEGACY_FILENAME,
                        country_document_ids=[OLD_DOCUMENT_ID],
                    )

                    restore_bulk_calls: list[Any] = []

                    def fake_bulk(client, actions, **kwargs):
                        del client
                        action_list = list(actions)
                        restore_bulk_calls.append(action_list)
                        return (len(action_list), [])

                    with patch(
                        "app.services.document_indexer.bulk",
                        side_effect=fake_bulk,
                    ), patch(
                        "app.services.document_indexer."
                        "ensure_legal_documents_index"
                    ):
                        with self.assertRaises(
                            AdminDocumentLifecycleError
                        ):
                            delete_indexed_document(
                                document_id=OLD_DOCUMENT_ID,
                                source_directory=source_directory,
                                processed_directory=(
                                    processed_directory
                                ),
                                client=client,
                                country_document_lookup=(
                                    self._only_this_document(
                                        OLD_DOCUMENT_ID
                                    )
                                ),
                            )

                    self.assertEqual(
                        sorted(
                            p.name
                            for p in real_source_entries(source_directory)
                        ),
                        sorted(
                            [self.LEGACY_FILENAME, "GB.docx"]
                        ),
                    )
                    self.assertEqual(
                        hashlib.sha256(
                            legacy_path.read_bytes()
                        ).hexdigest(),
                        legacy_hash,
                    )
                    self.assertEqual(
                        hashlib.sha256(
                            canonical_path.read_bytes()
                        ).hexdigest(),
                        canonical_hash,
                    )
                    self.assertEqual(len(restore_bulk_calls), 1)

    def test_duplicate_partial_delete_failure_restores_country_snapshot(
        self,
    ) -> None:
        # DUPLICATE_DELETE_ROLLBACK_TESTED (exception path, mission
        # "HOTFIX 0.4.9" review 3, section 8) - another document
        # remains for the country; the delete_by_query call for the
        # target genuinely registers some deletion server-side before
        # erroring out. The country snapshot (target + sibling) must
        # be restored exactly; the sibling's own chunk is never lost;
        # no file is ever touched in this branch either way.
        sibling_document_id = "doc_" + "e" * 64

        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root) / "source"
            processed_directory = Path(root) / "processed"
            source_directory.mkdir()

            legacy_path = source_directory / self.LEGACY_FILENAME
            canonical_path = source_directory / "GB.docx"
            legacy_path.write_bytes(b"legacy-bytes")
            canonical_path.write_bytes(b"canonical-bytes")

            client = PartialIndexDeleteFailureClient(
                source_filename=self.LEGACY_FILENAME,
                country_document_ids=[
                    OLD_DOCUMENT_ID,
                    sibling_document_id,
                ],
            )

            restore_bulk_calls: list[Any] = []

            def fake_bulk(client, actions, **kwargs):
                del client
                action_list = list(actions)
                restore_bulk_calls.append(action_list)
                return (len(action_list), [])

            with patch(
                "app.services.document_indexer.bulk",
                side_effect=fake_bulk,
            ), patch(
                "app.services.document_indexer."
                "ensure_legal_documents_index"
            ):
                with self.assertRaises(AdminDocumentLifecycleError):
                    delete_indexed_document(
                        document_id=OLD_DOCUMENT_ID,
                        source_directory=source_directory,
                        processed_directory=processed_directory,
                        client=client,
                    )

            self.assertEqual(
                legacy_path.read_bytes(), b"legacy-bytes"
            )
            self.assertEqual(
                canonical_path.read_bytes(), b"canonical-bytes"
            )

            self.assertEqual(len(restore_bulk_calls), 1)
            restored_ids = {
                action["_source"]["document_id"]
                for action in restore_bulk_calls[0]
            }
            self.assertEqual(
                restored_ids,
                {OLD_DOCUMENT_ID, sibling_document_id},
            )

    def test_duplicate_partial_delete_count_is_rolled_back(
        self,
    ) -> None:
        # DUPLICATE_PARTIAL_COUNT_ROLLBACK (mission "HOTFIX 0.4.9"
        # review 3, section 9) - no exception at all, just a
        # delete_by_query response whose deleted count is genuinely
        # less than the target's own expected chunk count taken from
        # the same snapshot. Must still be rejected and rolled back.
        sibling_document_id = "doc_" + "e" * 64

        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root) / "source"
            processed_directory = Path(root) / "processed"
            source_directory.mkdir()

            legacy_path = source_directory / self.LEGACY_FILENAME
            canonical_path = source_directory / "GB.docx"
            legacy_path.write_bytes(b"legacy-bytes")
            canonical_path.write_bytes(b"canonical-bytes")

            client = MultiChunkConfigurableDeleteResponseClient(
                delete_response={
                    "total": 3,
                    "deleted": 2,
                    "version_conflicts": 0,
                    "timed_out": False,
                    "failures": [],
                },
                chunk_counts={
                    OLD_DOCUMENT_ID: 3,
                    sibling_document_id: 1,
                },
                source_filename=self.LEGACY_FILENAME,
                country_document_ids=[
                    OLD_DOCUMENT_ID,
                    sibling_document_id,
                ],
            )

            restore_bulk_calls: list[Any] = []

            def fake_bulk(client, actions, **kwargs):
                del client
                action_list = list(actions)
                restore_bulk_calls.append(action_list)
                return (len(action_list), [])

            with patch(
                "app.services.document_indexer.bulk",
                side_effect=fake_bulk,
            ), patch(
                "app.services.document_indexer."
                "ensure_legal_documents_index"
            ):
                with self.assertRaises(AdminDocumentLifecycleError):
                    delete_indexed_document(
                        document_id=OLD_DOCUMENT_ID,
                        source_directory=source_directory,
                        processed_directory=processed_directory,
                        client=client,
                    )

            self.assertEqual(
                legacy_path.read_bytes(), b"legacy-bytes"
            )
            self.assertEqual(
                canonical_path.read_bytes(), b"canonical-bytes"
            )

            self.assertEqual(len(restore_bulk_calls), 1)
            restored_target_chunks = [
                action
                for action in restore_bulk_calls[0]
                if (
                    action["_source"]["document_id"]
                    == OLD_DOCUMENT_ID
                )
            ]
            self.assertEqual(len(restored_target_chunks), 3)
            restored_sibling_chunks = [
                action
                for action in restore_bulk_calls[0]
                if (
                    action["_source"]["document_id"]
                    == sibling_document_id
                )
            ]
            self.assertEqual(len(restored_sibling_chunks), 1)

    def test_last_document_partial_delete_count_restores_index_and_sources(
        self,
    ) -> None:
        # LAST_PARTIAL_COUNT_ROLLBACK (mission "HOTFIX 0.4.9" review
        # 3, section 10) - same rule for the last-document path, with
        # two physical source files: a partial deleted count (no
        # exception) must restore both the index snapshot and every
        # source file, leaving no .bak residue.
        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root) / "source"
            processed_directory = Path(root) / "processed"
            source_directory.mkdir()

            legacy_path = source_directory / self.LEGACY_FILENAME
            canonical_path = source_directory / "GB.docx"
            legacy_path.write_bytes(b"legacy-bytes")
            canonical_path.write_bytes(b"canonical-bytes")

            legacy_hash = hashlib.sha256(
                legacy_path.read_bytes()
            ).hexdigest()
            canonical_hash = hashlib.sha256(
                canonical_path.read_bytes()
            ).hexdigest()

            client = MultiChunkConfigurableDeleteResponseClient(
                delete_response={
                    "total": 2,
                    "deleted": 1,
                    "version_conflicts": 0,
                    "timed_out": False,
                    "failures": [],
                },
                chunk_counts={OLD_DOCUMENT_ID: 2},
                source_filename=self.LEGACY_FILENAME,
                country_document_ids=[OLD_DOCUMENT_ID],
            )

            restore_bulk_calls: list[Any] = []

            def fake_bulk(client, actions, **kwargs):
                del client
                action_list = list(actions)
                restore_bulk_calls.append(action_list)
                return (len(action_list), [])

            with patch(
                "app.services.document_indexer.bulk",
                side_effect=fake_bulk,
            ), patch(
                "app.services.document_indexer."
                "ensure_legal_documents_index"
            ):
                with self.assertRaises(AdminDocumentLifecycleError):
                    delete_indexed_document(
                        document_id=OLD_DOCUMENT_ID,
                        source_directory=source_directory,
                        processed_directory=processed_directory,
                        client=client,
                        country_document_lookup=(
                            self._only_this_document(
                                OLD_DOCUMENT_ID
                            )
                        ),
                    )

            self.assertEqual(
                sorted(
                    p.name for p in real_source_entries(source_directory)
                ),
                sorted([self.LEGACY_FILENAME, "GB.docx"]),
            )
            self.assertEqual(
                hashlib.sha256(
                    legacy_path.read_bytes()
                ).hexdigest(),
                legacy_hash,
            )
            self.assertEqual(
                hashlib.sha256(
                    canonical_path.read_bytes()
                ).hexdigest(),
                canonical_hash,
            )
            self.assertEqual(len(restore_bulk_calls), 1)
            self.assertEqual(len(restore_bulk_calls[0]), 2)

    def test_last_document_snapshot_with_sibling_aborts_before_file_mutation(
        self,
    ) -> None:
        # LATE_SIBLING_GUARD_TESTED (mission "HOTFIX 0.4.9" review 3,
        # section 11) - country_document_lookup (an earlier read)
        # reports only the target as active; the OpenSearch snapshot
        # (a later, more authoritative read taken immediately before
        # any mutation) reveals a sibling that lookup did not report.
        # This is a concurrent-change/stale-state signal and must
        # abort before any file is touched or any chunk is deleted -
        # never guess that it is still safe to retire every source.
        sibling_document_id = "doc_" + "e" * 64

        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root) / "source"
            processed_directory = Path(root) / "processed"
            source_directory.mkdir()

            legacy_path = source_directory / self.LEGACY_FILENAME
            canonical_path = source_directory / "GB.docx"
            legacy_path.write_bytes(b"legacy-bytes")
            canonical_path.write_bytes(b"canonical-bytes")

            client = FakeOpenSearchClient(
                source_filename=self.LEGACY_FILENAME,
                country_document_ids=[
                    OLD_DOCUMENT_ID,
                    sibling_document_id,
                ],
            )

            def replace_must_not_be_called(*a, **kw):
                raise AssertionError(
                    "os.replace must never be called when the "
                    "snapshot reveals a late sibling"
                )

            with patch(
                "app.services.admin_document_lifecycle.os.replace",
                side_effect=replace_must_not_be_called,
            ):
                with self.assertRaises(AdminDocumentLifecycleError):
                    delete_indexed_document(
                        document_id=OLD_DOCUMENT_ID,
                        source_directory=source_directory,
                        processed_directory=processed_directory,
                        client=client,
                        country_document_lookup=(
                            self._only_this_document(
                                OLD_DOCUMENT_ID
                            )
                        ),
                    )

            self.assertEqual(
                legacy_path.read_bytes(), b"legacy-bytes"
            )
            self.assertEqual(
                canonical_path.read_bytes(), b"canonical-bytes"
            )
            self.assertEqual(client.deleted_document_ids, [])

    def test_duplicate_delete_failure_with_snapshot_restore_failure_is_reported_explicitly(
        self,
    ) -> None:
        # Mission "HOTFIX 0.4.9" review 3, independent verification
        # round - the duplicate-path index-delete-failure handler
        # must report index_restored=False (never a silent complete
        # rollback) when _restore_country_snapshot ITSELF also fails.
        # No existing test made this inner restore call fail before
        # this one - every fake_bulk in the suite always succeeded.
        sibling_document_id = "doc_" + "e" * 64

        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root) / "source"
            processed_directory = Path(root) / "processed"
            source_directory.mkdir()

            client = FailingDeleteOpenSearchClient(
                source_filename=self.LEGACY_FILENAME,
                country_document_ids=[
                    OLD_DOCUMENT_ID,
                    sibling_document_id,
                ],
            )

            def failing_bulk(client, actions, **kwargs):
                del client
                del actions
                del kwargs
                raise RuntimeError(
                    "simulated snapshot restore failure"
                )

            with patch(
                "app.services.document_indexer.bulk",
                side_effect=failing_bulk,
            ):
                with self.assertRaises(
                    AdminDocumentLifecycleError
                ) as context:
                    delete_indexed_document(
                        document_id=OLD_DOCUMENT_ID,
                        source_directory=source_directory,
                        processed_directory=processed_directory,
                        client=client,
                    )

            message = str(context.exception).casefold()
            self.assertIn("manual recovery", message)

    def test_last_document_delete_failure_with_snapshot_restore_failure_is_reported_explicitly(
        self,
    ) -> None:
        # Mission "HOTFIX 0.4.9" review 3, independent verification
        # round - the last-document path's index-delete-failure
        # handler must report BOTH flags honestly: files ARE
        # restored (backup staging succeeds and is rolled back
        # normally), but the index is NOT restored because
        # _restore_country_snapshot itself returns a genuinely
        # partial bulk result (not an exception - a tuple with
        # errors) rather than a silent complete rollback.
        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root) / "source"
            processed_directory = Path(root) / "processed"
            source_directory.mkdir()

            source_path = source_directory / self.LEGACY_FILENAME
            source_path.write_bytes(b"legacy-docx-bytes")

            client = FailingDeleteOpenSearchClient(
                source_filename=self.LEGACY_FILENAME,
                country_document_ids=[OLD_DOCUMENT_ID],
            )

            def partial_bulk(client, actions, **kwargs):
                del client
                del kwargs
                list(actions)
                return (0, [{"error": "simulated restore failure"}])

            with patch(
                "app.services.document_indexer.bulk",
                side_effect=partial_bulk,
            ):
                with self.assertRaises(
                    AdminDocumentLifecycleError
                ) as context:
                    delete_indexed_document(
                        document_id=OLD_DOCUMENT_ID,
                        source_directory=source_directory,
                        processed_directory=processed_directory,
                        client=client,
                        country_document_lookup=(
                            self._only_this_document(
                                OLD_DOCUMENT_ID
                            )
                        ),
                    )

            message = str(context.exception).casefold()
            self.assertIn("manual recovery", message)
            self.assertIn("source files restored", message)
            self.assertIn("index not restored", message)

            # The file rollback itself genuinely succeeded - the
            # active source is back, byte-identical.
            self.assertTrue(source_path.exists())
            self.assertEqual(
                source_path.read_bytes(), b"legacy-docx-bytes"
            )


class DeleteResponseIntegrityTests(unittest.TestCase):
    """
    Mission "HOTFIX 0.4.9" review 3, section 12 - _delete_document_
    chunks' own response-integrity validation, tested directly
    against the function itself rather than only observed indirectly
    through delete_indexed_document/reindex_indexed_document.
    """

    def test_delete_chunks_rejects_nonempty_failures(self) -> None:
        client = ConfigurableDeleteResponseClient(
            delete_response={
                "total": 59,
                "deleted": 30,
                "version_conflicts": 0,
                "timed_out": False,
                "failures": [
                    {"cause": {"reason": "simulated failure"}}
                ],
            }
        )

        with self.assertRaises(AdminDocumentLifecycleError):
            _delete_document_chunks(
                document_id=OLD_DOCUMENT_ID,
                client=client,
            )

    def test_delete_chunks_rejects_version_conflicts(self) -> None:
        client = ConfigurableDeleteResponseClient(
            delete_response={
                "total": 59,
                "deleted": 58,
                "version_conflicts": 1,
                "timed_out": False,
                "failures": [],
            }
        )

        with self.assertRaises(AdminDocumentLifecycleError):
            _delete_document_chunks(
                document_id=OLD_DOCUMENT_ID,
                client=client,
            )

    def test_delete_chunks_rejects_timeout(self) -> None:
        client = ConfigurableDeleteResponseClient(
            delete_response={
                "total": 59,
                "deleted": 30,
                "version_conflicts": 0,
                "timed_out": True,
                "failures": [],
            }
        )

        with self.assertRaises(AdminDocumentLifecycleError):
            _delete_document_chunks(
                document_id=OLD_DOCUMENT_ID,
                client=client,
            )

    def test_delete_chunks_rejects_incomplete_deleted_count(
        self,
    ) -> None:
        client = ConfigurableDeleteResponseClient(
            delete_response={
                "total": 3,
                "deleted": 2,
                "version_conflicts": 0,
                "timed_out": False,
                "failures": [],
            }
        )

        with self.assertRaises(AdminDocumentLifecycleError):
            _delete_document_chunks(
                document_id=OLD_DOCUMENT_ID,
                client=client,
                expected_chunks=3,
            )

    def test_delete_chunks_accepts_exact_complete_response(
        self,
    ) -> None:
        client = ConfigurableDeleteResponseClient(
            delete_response={
                "total": 3,
                "deleted": 3,
                "version_conflicts": 0,
                "timed_out": False,
                "failures": [],
            }
        )

        deleted = _delete_document_chunks(
            document_id=OLD_DOCUMENT_ID,
            client=client,
            expected_chunks=3,
        )

        self.assertEqual(deleted, 3)

    def test_delete_chunks_rejects_total_deleted_mismatch(
        self,
    ) -> None:
        # Mission "HOTFIX 0.4.9" review 3, independent verification
        # round - total is OpenSearch's own count of how many
        # documents delete_by_query matched and processed; deleted is
        # how many it actually deleted. Cross-checking the two must
        # catch a partial deletion even with no expected_chunks at
        # all - the exact situation reindex_indexed_document's own
        # previous-document cleanup is in, since it has no country
        # snapshot to derive an expected count from.
        client = ConfigurableDeleteResponseClient(
            delete_response={
                "total": 59,
                "deleted": 30,
                "version_conflicts": 0,
                "timed_out": False,
                "failures": [],
            }
        )

        with self.assertRaises(AdminDocumentLifecycleError):
            _delete_document_chunks(
                document_id=OLD_DOCUMENT_ID,
                client=client,
            )

    def test_delete_chunks_rejects_negative_version_conflicts(
        self,
    ) -> None:
        # A negative version_conflicts count is corrupt/invalid data,
        # not a legitimate "no conflicts" signal - never silently
        # accepted just because it fails an isolated `> 0` check.
        client = ConfigurableDeleteResponseClient(
            delete_response={
                "total": 1,
                "deleted": 1,
                "version_conflicts": -1,
                "timed_out": False,
                "failures": [],
            }
        )

        with self.assertRaises(AdminDocumentLifecycleError):
            _delete_document_chunks(
                document_id=OLD_DOCUMENT_ID,
                client=client,
            )

    def test_delete_chunks_rejects_negative_deleted_count(
        self,
    ) -> None:
        # A negative deleted count is corrupt/invalid data - the same
        # >= 0 bound already enforced on total must also apply here.
        client = ConfigurableDeleteResponseClient(
            delete_response={
                "total": 0,
                "deleted": -1,
                "version_conflicts": 0,
                "timed_out": False,
                "failures": [],
            }
        )

        with self.assertRaises(AdminDocumentLifecycleError):
            _delete_document_chunks(
                document_id=OLD_DOCUMENT_ID,
                client=client,
            )


class ReindexTransactionalIntegrityTests(unittest.TestCase):
    """
    Mission "HOTFIX 0.4.9" review 4 - REINDEX's own transactional
    guarantee: either it ends in the new state (new document fully
    indexed, old document fully removed when the ID changed) or it
    ends in exactly the pre-reindex state - never an intermediate
    state with the old document partially destroyed and the new
    document also gone.
    """

    @staticmethod
    def _old_chunks(count: int) -> dict[str, dict[str, Any]]:
        return {
            f"{OLD_DOCUMENT_ID}-chunk-{i}": {
                "document_id": OLD_DOCUMENT_ID,
                "chunk_id": f"{OLD_DOCUMENT_ID}-chunk-{i}",
                "country_code": "GB",
                "source_filename": "GB.docx",
                "country": "United Kingdom",
                "reference_year": 2026,
            }
            for i in range(count)
        }

    @staticmethod
    def _old_metadata() -> dict[str, dict[str, Any]]:
        return {
            OLD_DOCUMENT_ID: {
                "document_id": OLD_DOCUMENT_ID,
                "source_filename": "GB.docx",
                "country": "United Kingdom",
                "country_code": "GB",
                "reference_year": 2026,
            }
        }

    @staticmethod
    def _new_chunk_builder(count: int):
        def chunk_builder(path: Path) -> list[DocumentChunk]:
            del path
            return [
                _build_reindex_chunk(
                    document_id=NEW_DOCUMENT_ID,
                    chunk_id=f"{NEW_DOCUMENT_ID}-chunk-{i}",
                )
                for i in range(count)
            ]

        return chunk_builder

    def test_reindex_partial_old_delete_restores_exact_pre_reindex_snapshot(
        self,
    ) -> None:
        # OLD=3 chunks, NEW=2 chunks; delete OLD genuinely removes
        # 2/3 then returns a partial (no-exception) response.
        # Expected: OLD fully restored to exactly 3 chunks, NEW=0,
        # no orphan of either.
        fake_client = ReindexTransactionOpenSearchClient(
            chunks=self._old_chunks(3),
            document_metadata=self._old_metadata(),
            target_document_id=OLD_DOCUMENT_ID,
        )
        fake_client.remove_count_before_response = 2

        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root)
            (source_directory / "GB.docx").write_bytes(b"docx-bytes")

            with patch(
                "app.services.document_indexer.bulk",
                side_effect=_reindex_bulk_fake(fake_client),
            ):
                with self.assertRaises(AdminDocumentLifecycleError):
                    reindex_indexed_document(
                        document_id=OLD_DOCUMENT_ID,
                        source_directory=source_directory,
                        client=fake_client,
                        chunk_builder=self._new_chunk_builder(2),
                        document_indexer=(
                            _reindex_document_indexer_fixture(
                                fake_client
                            )
                        ),
                    )

        remaining_ids = {
            source["document_id"]
            for source in fake_client.chunks.values()
        }
        self.assertEqual(remaining_ids, {OLD_DOCUMENT_ID})
        self.assertEqual(len(fake_client.chunks), 3)

    def test_reindex_partial_old_delete_exception_restores_exact_snapshot(
        self,
    ) -> None:
        # Same as above, but delete OLD genuinely removes 2/3 chunks
        # and THEN raises OpenSearchException, rather than returning
        # a partial dict - a stronger simulation of a genuine
        # server-side partial deletion before the client call itself
        # errors out.
        fake_client = ReindexTransactionOpenSearchClient(
            chunks=self._old_chunks(3),
            document_metadata=self._old_metadata(),
            target_document_id=OLD_DOCUMENT_ID,
        )
        fake_client.remove_count_before_response = 2
        fake_client.raise_exception_after = OpenSearchException(
            "simulated partial old-document deletion failure"
        )

        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root)
            (source_directory / "GB.docx").write_bytes(b"docx-bytes")

            with patch(
                "app.services.document_indexer.bulk",
                side_effect=_reindex_bulk_fake(fake_client),
            ):
                with self.assertRaises(AdminDocumentLifecycleError):
                    reindex_indexed_document(
                        document_id=OLD_DOCUMENT_ID,
                        source_directory=source_directory,
                        client=fake_client,
                        chunk_builder=self._new_chunk_builder(2),
                        document_indexer=(
                            _reindex_document_indexer_fixture(
                                fake_client
                            )
                        ),
                    )

        remaining_ids = {
            source["document_id"]
            for source in fake_client.chunks.values()
        }
        self.assertEqual(remaining_ids, {OLD_DOCUMENT_ID})
        self.assertEqual(len(fake_client.chunks), 3)

    def test_reindex_old_delete_integrity_failure_restores_snapshot(
        self,
    ) -> None:
        # DELETE_BY_QUERY_FAILURES_VALIDATED / VERSION_CONFLICTS /
        # TIMEOUT, end-to-end through reindex: OLD's 3 chunks are
        # genuinely, fully removed (deleted == total == expected),
        # but the response still reports a problem - even a
        # deleted-count-complete response must be rejected, and
        # rollback must still restore what was genuinely removed.
        bad_overrides = {
            "version_conflicts": {"version_conflicts": 1},
            "failures": {
                "failures": [
                    {"cause": {"reason": "simulated failure"}}
                ]
            },
            "timed_out": {"timed_out": True},
        }

        for kind, overrides in bad_overrides.items():
            with self.subTest(kind=kind):
                fake_client = ReindexTransactionOpenSearchClient(
                    chunks=self._old_chunks(3),
                    document_metadata=self._old_metadata(),
                    target_document_id=OLD_DOCUMENT_ID,
                )
                fake_client.response_overrides = overrides

                with tempfile.TemporaryDirectory() as root:
                    source_directory = Path(root)
                    (
                        source_directory / "GB.docx"
                    ).write_bytes(b"docx-bytes")

                    with patch(
                        "app.services.document_indexer.bulk",
                        side_effect=_reindex_bulk_fake(fake_client),
                    ):
                        with self.assertRaises(
                            AdminDocumentLifecycleError
                        ):
                            reindex_indexed_document(
                                document_id=OLD_DOCUMENT_ID,
                                source_directory=source_directory,
                                client=fake_client,
                                chunk_builder=(
                                    self._new_chunk_builder(2)
                                ),
                                document_indexer=(
                                    _reindex_document_indexer_fixture(
                                        fake_client
                                    )
                                ),
                            )

                remaining_ids = {
                    source["document_id"]
                    for source in fake_client.chunks.values()
                }
                self.assertEqual(remaining_ids, {OLD_DOCUMENT_ID})
                self.assertEqual(len(fake_client.chunks), 3)

    def test_reindex_snapshot_restore_failure_is_reported_explicitly(
        self,
    ) -> None:
        # NEW is indexed, OLD delete fails (a plain partial count is
        # enough here), AND the snapshot restore itself fails too -
        # must never be masked behind the original error alone.
        fake_client = ReindexTransactionOpenSearchClient(
            chunks=self._old_chunks(3),
            document_metadata=self._old_metadata(),
            target_document_id=OLD_DOCUMENT_ID,
        )
        fake_client.remove_count_before_response = 2

        def failing_bulk(client, actions, **kwargs):
            del client
            del actions
            del kwargs
            raise RuntimeError(
                "simulated snapshot restore failure"
            )

        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root)
            (source_directory / "GB.docx").write_bytes(b"docx-bytes")

            with patch(
                "app.services.document_indexer.bulk",
                side_effect=failing_bulk,
            ):
                with self.assertRaises(
                    AdminDocumentLifecycleError
                ) as context:
                    reindex_indexed_document(
                        document_id=OLD_DOCUMENT_ID,
                        source_directory=source_directory,
                        client=fake_client,
                        chunk_builder=self._new_chunk_builder(2),
                        document_indexer=(
                            _reindex_document_indexer_fixture(
                                fake_client
                            )
                        ),
                    )

        message = str(context.exception).casefold()
        self.assertIn("manual recovery", message)

        # Specific attribution, not just the presence of a
        # manual-recovery phrase - a swapped/mislabeled ternary
        # (index_restored <-> extra_chunks_removed) would leave this
        # substring intact but the attribution backwards; mirrors the
        # precision already required of the DELETE-flow analogs.
        self.assertIn("previous index state not restored", message)
        self.assertIn("extra chunks removed", message)

    def test_reindex_stray_cleanup_failure_is_reported_explicitly(
        self,
    ) -> None:
        # The rollback's OTHER half can also fail on its own: the
        # snapshot restore succeeds, but the stray-chunk cleanup
        # itself fails - must be reported with its own explicit
        # attribution, never silently treated as a complete rollback
        # just because the snapshot restore succeeded.
        fake_client = ReindexTransactionOpenSearchClient(
            chunks=self._old_chunks(3),
            document_metadata=self._old_metadata(),
            target_document_id=OLD_DOCUMENT_ID,
        )
        fake_client.remove_count_before_response = 2
        fake_client.fail_stray_cleanup = RuntimeError(
            "simulated stray-chunk cleanup failure"
        )

        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root)
            (source_directory / "GB.docx").write_bytes(b"docx-bytes")

            with patch(
                "app.services.document_indexer.bulk",
                side_effect=_reindex_bulk_fake(fake_client),
            ):
                with self.assertRaises(
                    AdminDocumentLifecycleError
                ) as context:
                    reindex_indexed_document(
                        document_id=OLD_DOCUMENT_ID,
                        source_directory=source_directory,
                        client=fake_client,
                        chunk_builder=self._new_chunk_builder(2),
                        document_indexer=(
                            _reindex_document_indexer_fixture(
                                fake_client
                            )
                        ),
                    )

        message = str(context.exception).casefold()
        self.assertIn("manual recovery", message)
        self.assertIn("previous index state restored", message)
        self.assertIn("extra chunks not removed", message)

    def test_reindex_old_delete_self_consistent_undercount_is_still_rejected(
        self,
    ) -> None:
        # Mission "HOTFIX 0.4.9" review 4, independent verification -
        # the ONE scenario only the round-4 expected_chunks mechanism
        # (derived from the pre-reindex snapshot) catches: a
        # delete_by_query response that is fully self-consistent
        # (deleted == total, no version_conflicts/failures/timed_out)
        # yet reports fewer chunks than the snapshot recorded for the
        # old document. total is overridden to match the genuinely
        # removed count exactly, so the pre-existing (round 3)
        # total-vs-deleted check cannot be what catches this - only
        # expected_chunks can.
        fake_client = ReindexTransactionOpenSearchClient(
            chunks=self._old_chunks(3),
            document_metadata=self._old_metadata(),
            target_document_id=OLD_DOCUMENT_ID,
        )
        fake_client.remove_count_before_response = 2
        fake_client.response_overrides = {"total": 2}

        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root)
            (source_directory / "GB.docx").write_bytes(b"docx-bytes")

            with patch(
                "app.services.document_indexer.bulk",
                side_effect=_reindex_bulk_fake(fake_client),
            ):
                with self.assertRaises(AdminDocumentLifecycleError):
                    reindex_indexed_document(
                        document_id=OLD_DOCUMENT_ID,
                        source_directory=source_directory,
                        client=fake_client,
                        chunk_builder=self._new_chunk_builder(2),
                        document_indexer=(
                            _reindex_document_indexer_fixture(
                                fake_client
                            )
                        ),
                    )

        remaining_ids = {
            source["document_id"]
            for source in fake_client.chunks.values()
        }
        self.assertEqual(remaining_ids, {OLD_DOCUMENT_ID})
        self.assertEqual(len(fake_client.chunks), 3)

    def test_reindex_document_indexer_partial_failure_with_unchanged_document_id_restores_exact_snapshot(
        self,
    ) -> None:
        # The actually-reachable production scenario found by the
        # independent adversarial review of round 4: document_id
        # never changes in practice (it depends only on a fixed
        # family constant, country_code - which Reindex already
        # refuses to let change - and a language that no caller ever
        # overrides). A content edit keeps the SAME document_id but
        # produces different chunk_ids; if document_indexer's own
        # internal work fails partway through (its bulk-index or its
        # stale-chunk cleanup), a snapshot restore ALONE would never
        # remove a stray new chunk_id under that same document_id,
        # since a country snapshot restore only re-indexes what it
        # captured. The stray-chunk cleanup added during independent
        # verification closes exactly this gap.
        fake_client = ReindexTransactionOpenSearchClient(
            chunks=self._old_chunks(2),
            document_metadata=self._old_metadata(),
            target_document_id=OLD_DOCUMENT_ID,
        )

        def chunk_builder(path: Path) -> list[DocumentChunk]:
            del path
            return [
                _build_reindex_chunk(
                    document_id=OLD_DOCUMENT_ID,
                    chunk_id=f"{OLD_DOCUMENT_ID}-chunk-X",
                ),
                _build_reindex_chunk(
                    document_id=OLD_DOCUMENT_ID,
                    chunk_id=f"{OLD_DOCUMENT_ID}-chunk-Y",
                ),
            ]

        def failing_document_indexer(
            *,
            chunks,
            client=None,
        ) -> DocumentIndexingResult:
            del client

            # document_indexer (replace_document_chunks) partially
            # succeeds: the first new chunk is genuinely written...
            fake_client.chunks[chunks[0].chunk_id] = {
                "document_id": chunks[0].document_id,
                "chunk_id": chunks[0].chunk_id,
                "country_code": chunks[0].country_code,
                "source_filename": chunks[0].source_filename,
                "country": chunks[0].country,
                "reference_year": chunks[0].reference_year,
            }

            # ...then its own internal indexing/stale-chunk-cleanup
            # step fails, before the second chunk is written and
            # before the old chunk_ids are cleaned up.
            raise DocumentIndexingError(
                "simulated partial internal indexing failure"
            )

        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root)
            (source_directory / "GB.docx").write_bytes(b"docx-bytes")

            with patch(
                "app.services.document_indexer.bulk",
                side_effect=_reindex_bulk_fake(fake_client),
            ):
                with self.assertRaises(DocumentIndexingError):
                    reindex_indexed_document(
                        document_id=OLD_DOCUMENT_ID,
                        source_directory=source_directory,
                        client=fake_client,
                        chunk_builder=chunk_builder,
                        document_indexer=failing_document_indexer,
                    )

        remaining_ids = {
            source["document_id"]
            for source in fake_client.chunks.values()
        }
        self.assertEqual(remaining_ids, {OLD_DOCUMENT_ID})

        remaining_chunk_ids = set(fake_client.chunks)
        self.assertEqual(
            remaining_chunk_ids,
            {f"{OLD_DOCUMENT_ID}-chunk-{i}" for i in range(2)},
        )

    def test_reindex_success_path_fully_deletes_old_and_keeps_new(
        self,
    ) -> None:
        # REINDEX_SUCCESS_PATH - OLD=3 chunks, NEW=2 chunks, OLD
        # delete genuinely and fully succeeds (deleted == total ==
        # expected). status=reindexed, previous_chunks_deleted equals
        # the exact pre-reindex OLD chunk count, and only NEW's
        # chunks remain afterward.
        fake_client = ReindexTransactionOpenSearchClient(
            chunks=self._old_chunks(3),
            document_metadata=self._old_metadata(),
            target_document_id=OLD_DOCUMENT_ID,
        )

        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root)
            (source_directory / "GB.docx").write_bytes(b"docx-bytes")

            response = reindex_indexed_document(
                document_id=OLD_DOCUMENT_ID,
                source_directory=source_directory,
                client=fake_client,
                chunk_builder=self._new_chunk_builder(2),
                document_indexer=(
                    _reindex_document_indexer_fixture(fake_client)
                ),
            )

        self.assertEqual(response.status, "reindexed")
        self.assertTrue(response.document_id_changed)
        self.assertEqual(response.document_id, NEW_DOCUMENT_ID)
        self.assertEqual(response.previous_chunks_deleted, 3)

        remaining_ids = {
            source["document_id"]
            for source in fake_client.chunks.values()
        }
        self.assertEqual(remaining_ids, {NEW_DOCUMENT_ID})
        self.assertEqual(len(fake_client.chunks), 2)

    def test_reindex_refuses_country_change_before_any_mutation(
        self,
    ) -> None:
        # Mission "HOTFIX 0.4.9" review 4, section 3, option B - a
        # country change during Reindex is not a supported product
        # behavior; refusing before any mutation is simpler and
        # safer than a two-country snapshot/rollback scheme for a
        # scenario no legitimate caller relies on.
        fake_client = ReindexTransactionOpenSearchClient(
            chunks=self._old_chunks(1),
            document_metadata=self._old_metadata(),
            target_document_id=OLD_DOCUMENT_ID,
        )

        def chunk_builder(path: Path) -> list[DocumentChunk]:
            del path
            return [
                _build_reindex_chunk(
                    document_id=NEW_DOCUMENT_ID,
                    chunk_id=f"{NEW_DOCUMENT_ID}-chunk-0",
                    country_code="FR",
                    country="France",
                )
            ]

        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root)
            (source_directory / "GB.docx").write_bytes(b"docx-bytes")

            with self.assertRaises(AdminDocumentLifecycleError):
                reindex_indexed_document(
                    document_id=OLD_DOCUMENT_ID,
                    source_directory=source_directory,
                    client=fake_client,
                    chunk_builder=chunk_builder,
                    document_indexer=(
                        _reindex_document_indexer_fixture(
                            fake_client
                        )
                    ),
                )

        # Nothing was ever touched: still exactly OLD's original
        # chunk, no delete_by_query attempted.
        self.assertEqual(len(fake_client.chunks), 1)
        self.assertEqual(fake_client.deleted_document_ids, [])


class SectionEditLifecycleIntegrationTests(unittest.TestCase):
    """
    Mission "ORDER 5C" - wiring the persisted section-edit state
    (document_section_state.py) into the pre-existing Reindex/Delete
    lifecycle: a Reindex must never silently revert an Edit (section
    2), and a successful Delete must never leave an orphaned
    section-edit state file behind (sections 34/38, NO_ORPHAN_SECTION_
    STATE).
    """

    def test_reindex_ignores_legacy_persisted_edit_state(
        self,
    ) -> None:
        """
        ORDER 8A, sections 5/19: the current DOCX is the unique source
        of truth - Reindex must NEVER apply a legacy .admin-state
        override anymore, even if one is still present on disk from
        before this architecture change. Every topic's fresh,
        DOCX-derived content wins, including the one a stale override
        file claims to have edited.
        """

        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root)

            (source_directory / "GB.docx").write_bytes(b"docx-bytes")

            document_id = OLD_DOCUMENT_ID

            def fresh_chunk_builder(
                path: Path,
            ) -> list[DocumentChunk]:
                del path

                return [
                    DocumentChunk(
                        document_id=document_id,
                        chunk_id="chunk-fresh-ec",
                        country="United Kingdom",
                        country_code="GB",
                        legal_topic="Employment Contracts",
                        document_type="comparator",
                        language="en",
                        section="Employment Contracts",
                        subsection=None,
                        content=(
                            "FRESH DOCX content - must be replaced "
                            "by the persisted edit."
                        ),
                        source_filename="UK 2026.docx",
                        source_format="docx",
                        content_hash="fresh-ec-hash",
                        reference_year=2026,
                    ),
                    DocumentChunk(
                        document_id=document_id,
                        chunk_id="chunk-fresh-hp",
                        country="United Kingdom",
                        country_code="GB",
                        legal_topic="Hiring Practices",
                        document_type="comparator",
                        language="en",
                        section="Hiring Practices",
                        subsection=None,
                        content=(
                            "FRESH DOCX content - unaffected, no "
                            "persisted edit exists for this topic."
                        ),
                        source_filename="UK 2026.docx",
                        source_format="docx",
                        content_hash="fresh-hp-hash",
                        reference_year=2026,
                    ),
                ]

            write_section_edit_state_atomic(
                source_directory,
                SectionEditState(
                    document_id=document_id,
                    country_code="GB",
                    sections={
                        section_id_for_legal_topic(
                            "Employment Contracts"
                        ): SectionEdit(
                            legal_topic="Employment Contracts",
                            section="Employment Contracts",
                            subsection=None,
                            content=(
                                "EDITED content - must survive "
                                "Reindex."
                            ),
                        ),
                    },
                ),
            )

            captured_chunks: list[list[DocumentChunk]] = []

            def spy_document_indexer(
                *,
                chunks,
                client=None,
            ) -> DocumentIndexingResult:
                del client
                captured_chunks.append(chunks)

                return DocumentIndexingResult(
                    index_alias="legal-documents",
                    document_id=chunks[0].document_id,
                    source_filename=chunks[0].source_filename,
                    requested_chunks=len(chunks),
                    indexed_chunks=len(chunks),
                    stale_chunks_deleted=0,
                )

            client = FakeOpenSearchClient(
                source_filename="UK 2026.docx",
                country_document_ids=[document_id],
            )

            response = reindex_indexed_document(
                document_id=document_id,
                source_directory=source_directory,
                client=client,
                chunk_builder=fresh_chunk_builder,
                document_indexer=spy_document_indexer,
            )

            self.assertEqual(response.status, "reindexed")
            self.assertFalse(response.document_id_changed)

            self.assertEqual(len(captured_chunks), 1)

            by_topic = {
                chunk.legal_topic: chunk
                for chunk in captured_chunks[0]
            }

            self.assertEqual(
                by_topic["Employment Contracts"].content,
                (
                    "FRESH DOCX content - must be replaced "
                    "by the persisted edit."
                ),
            )
            self.assertEqual(
                by_topic["Hiring Practices"].content,
                (
                    "FRESH DOCX content - unaffected, no persisted "
                    "edit exists for this topic."
                ),
            )

            # The legacy override file itself is left physically
            # untouched by Reindex (no auto-migration/deletion) - only
            # no longer READ.
            legacy_state = read_section_edit_state(
                source_directory,
                document_id,
            )
            self.assertIsNotNone(legacy_state)
            self.assertIn(
                section_id_for_legal_topic("Employment Contracts"),
                legacy_state.sections,
            )

    def test_delete_last_document_clears_section_edit_state(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root) / "source"
            processed_directory = Path(root) / "processed"
            source_directory.mkdir()

            (source_directory / "GB.docx").write_bytes(b"docx-bytes")

            client = FakeOpenSearchClient(
                source_filename="UK 2026.docx"
            )

            write_section_edit_state_atomic(
                source_directory,
                SectionEditState(
                    document_id=OLD_DOCUMENT_ID,
                    country_code="GB",
                    sections={
                        section_id_for_legal_topic(
                            "Employment Contracts"
                        ): SectionEdit(
                            legal_topic="Employment Contracts",
                            section="Employment Contracts",
                            subsection=None,
                            content=(
                                "An edit that must not outlive its "
                                "own document."
                            ),
                        ),
                    },
                ),
            )

            self.assertIsNotNone(
                read_section_edit_state(
                    source_directory, OLD_DOCUMENT_ID
                )
            )

            response = delete_indexed_document(
                document_id=OLD_DOCUMENT_ID,
                source_directory=source_directory,
                processed_directory=processed_directory,
                client=client,
            )

            self.assertEqual(response.status, "deleted")
            self.assertIsNone(
                read_section_edit_state(
                    source_directory, OLD_DOCUMENT_ID
                )
            )

    def test_delete_other_documents_remain_clears_section_edit_state(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root) / "source"
            processed_directory = Path(root) / "processed"
            source_directory.mkdir()

            (source_directory / "GB.docx").write_bytes(b"docx-bytes")

            sibling_id = "doc_" + "e" * 64

            client = FakeOpenSearchClient(
                source_filename="UK 2026.docx",
                country_document_ids=[OLD_DOCUMENT_ID, sibling_id],
            )

            write_section_edit_state_atomic(
                source_directory,
                SectionEditState(
                    document_id=OLD_DOCUMENT_ID,
                    country_code="GB",
                    sections={
                        section_id_for_legal_topic(
                            "Hiring Practices"
                        ): SectionEdit(
                            legal_topic="Hiring Practices",
                            section="Hiring Practices",
                            subsection=None,
                            content=(
                                "An edit for the deleted document only."
                            ),
                        ),
                    },
                ),
            )

            # A sibling document's own edit state must never be
            # touched by deleting a DIFFERENT document_id.
            write_section_edit_state_atomic(
                source_directory,
                SectionEditState(
                    document_id=sibling_id,
                    country_code="GB",
                    sections={
                        section_id_for_legal_topic(
                            "Employee Benefits"
                        ): SectionEdit(
                            legal_topic="Employee Benefits",
                            section="Employee Benefits",
                            subsection=None,
                            content="Sibling's own edit - must survive.",
                        ),
                    },
                ),
            )

            response = delete_indexed_document(
                document_id=OLD_DOCUMENT_ID,
                source_directory=source_directory,
                processed_directory=processed_directory,
                client=client,
            )

            self.assertEqual(response.status, "deleted")
            self.assertTrue(response.source_cleanup_deferred)
            self.assertIsNone(
                read_section_edit_state(
                    source_directory, OLD_DOCUMENT_ID
                )
            )
            self.assertIsNotNone(
                read_section_edit_state(source_directory, sibling_id)
            )

    def test_delete_state_clear_failure_with_successful_restore_raises_plain_error(
        self,
    ) -> None:
        # The OpenSearch delete had already fully succeeded before the
        # state-clear step ran; when the rollback triggered by that
        # state-clear failure itself fully succeeds (files and index
        # both restored), the operation is fully recovered - this
        # must raise the plain AdminDocumentLifecycleError, never the
        # more urgent AdminDocumentRollbackError (mission "ORDER 5C",
        # section 38).
        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root) / "source"
            processed_directory = Path(root) / "processed"
            source_directory.mkdir()

            source_path = source_directory / "GB.docx"
            source_path.write_bytes(b"docx-bytes")

            client = FakeOpenSearchClient(
                source_filename="UK 2026.docx"
            )

            write_section_edit_state_atomic(
                source_directory,
                SectionEditState(
                    document_id=OLD_DOCUMENT_ID,
                    country_code="GB",
                    sections={
                        section_id_for_legal_topic(
                            "Employment Contracts"
                        ): SectionEdit(
                            legal_topic="Employment Contracts",
                            section="Employment Contracts",
                            subsection=None,
                            content="Some edit.",
                        ),
                    },
                ),
            )

            restore_bulk_calls: list[Any] = []

            def fake_bulk(client, actions, **kwargs):
                del client
                action_list = list(actions)
                restore_bulk_calls.append(action_list)
                return (len(action_list), [])

            with patch(
                "app.services.admin_document_lifecycle."
                "delete_section_edit_state",
                side_effect=OSError("simulated disk failure"),
            ), patch(
                "app.services.document_indexer.bulk",
                side_effect=fake_bulk,
            ), patch(
                "app.services.document_indexer."
                "ensure_legal_documents_index"
            ):
                with self.assertRaises(
                    AdminDocumentLifecycleError
                ) as context:
                    delete_indexed_document(
                        document_id=OLD_DOCUMENT_ID,
                        source_directory=source_directory,
                        processed_directory=processed_directory,
                        client=client,
                    )

            self.assertEqual(
                client.deleted_document_ids, [OLD_DOCUMENT_ID]
            )
            self.assertEqual(len(restore_bulk_calls), 1)
            self.assertNotIsInstance(
                context.exception, AdminDocumentRollbackError
            )
            self.assertIn(
                "section-edit state could not be cleared",
                str(context.exception),
            )

            # The source file was moved to a backup during deletion
            # staging, but the failed state-clear's own rollback
            # restored it byte-for-byte, exactly like a fully
            # successful delete's own rollback would.
            self.assertEqual(
                source_path.read_bytes(), b"docx-bytes"
            )

    def test_delete_state_clear_failure_with_failed_restore_raises_rollback_error(
        self,
    ) -> None:
        # Both the state-clear AND its own attempted rollback fail -
        # the indexed/filesystem state may now differ from both the
        # pre- and post-operation state, so this must raise the more
        # urgent AdminDocumentRollbackError, never the plain
        # AdminDocumentLifecycleError a fully-recovered failure gets.
        sibling_id = "doc_" + "e" * 64

        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root) / "source"
            processed_directory = Path(root) / "processed"
            source_directory.mkdir()

            (source_directory / "GB.docx").write_bytes(b"docx-bytes")

            client = FakeOpenSearchClient(
                source_filename="UK 2026.docx",
                country_document_ids=[OLD_DOCUMENT_ID, sibling_id],
            )

            write_section_edit_state_atomic(
                source_directory,
                SectionEditState(
                    document_id=OLD_DOCUMENT_ID,
                    country_code="GB",
                    sections={
                        section_id_for_legal_topic(
                            "Employment Contracts"
                        ): SectionEdit(
                            legal_topic="Employment Contracts",
                            section="Employment Contracts",
                            subsection=None,
                            content="Some edit.",
                        ),
                    },
                ),
            )

            with patch(
                "app.services.admin_document_lifecycle."
                "delete_section_edit_state",
                side_effect=OSError("simulated disk failure"),
            ), patch(
                "app.services.admin_document_lifecycle."
                "_restore_country_snapshot",
                side_effect=RuntimeError("simulated restore failure"),
            ):
                with self.assertRaises(
                    AdminDocumentRollbackError
                ) as context:
                    delete_indexed_document(
                        document_id=OLD_DOCUMENT_ID,
                        source_directory=source_directory,
                        processed_directory=processed_directory,
                        client=client,
                    )

            self.assertIn(
                "manual recovery",
                str(context.exception).casefold(),
            )
            self.assertEqual(
                client.deleted_document_ids, [OLD_DOCUMENT_ID]
            )


AU_DOCUMENT_ID = "doc_" + "a" * 64


class FakeWholeDocumentOpenSearch:
    """
    Minimal OpenSearch test double for replace_document_chunks itself
    (not the outer reindex/lifecycle plumbing) - mirrors
    test_admin_document_replacement.py's own FakeCountryOpenSearch
    exactly, scoped to one document_id instead of one country_code.

    Mission "ORDER 5C" corrective gate, section 4 (WHOLE_DOCUMENT_
    STALE_DELETE_GAP): proves replace_document_chunks - the reindex
    path's own indexer, previously the one sibling of
    replace_country_document_chunks with no internal snapshot/
    rollback of its own - now restores the exact pre-call chunk set
    when the stale-chunk cleanup step fails after a successful bulk.
    """

    def __init__(self, *, fail_cleanup: bool = False) -> None:
        self.fail_cleanup = fail_cleanup
        self.delete_calls = 0

    def search(self, *, index, body):
        del index
        del body

        return {
            "hits": {
                "total": {
                    "value": 1,
                },
                "hits": [
                    {
                        "_id": "chunk-old-1",
                        "_source": {
                            "document_id": AU_DOCUMENT_ID,
                            "chunk_id": "chunk-old-1",
                            "country": "Australia",
                            "country_code": "AU",
                            "legal_topic": "Employment Contracts",
                        },
                        "sort": ["chunk-old-1"],
                    }
                ],
            }
        }

    def delete_by_query(self, **kwargs):
        del kwargs
        self.delete_calls += 1

        if self.fail_cleanup and self.delete_calls == 1:
            raise RuntimeError("simulated stale-chunk cleanup failure")

        return {
            "deleted": 1,
        }


class WholeDocumentIndexerAtomicityTests(unittest.TestCase):
    @patch(
        "app.services.document_indexer.ensure_legal_documents_index"
    )
    @patch("app.services.document_indexer.bulk")
    def test_document_indexer_removes_stale_chunk_on_success(
        self,
        bulk_mock,
        ensure_mock,
    ) -> None:
        del ensure_mock
        bulk_mock.return_value = (1, [])

        result = replace_document_chunks(
            chunks=[
                DocumentChunk(
                    document_id=AU_DOCUMENT_ID,
                    chunk_id="chunk-new-1",
                    country="Australia",
                    country_code="AU",
                    legal_topic="Employment Contracts",
                    document_type="comparator",
                    language="en",
                    section="Employment Contracts",
                    subsection="Trial Period",
                    content="New content.",
                    source_filename="Australia 2026.docx",
                    source_format="docx",
                    content_hash="new-content-hash",
                    reference_year=2026,
                )
            ],
            client=FakeWholeDocumentOpenSearch(),
        )

        self.assertEqual(result.indexed_chunks, 1)
        self.assertEqual(result.stale_chunks_deleted, 1)
        self.assertEqual(bulk_mock.call_count, 1)

    @patch(
        "app.services.document_indexer.ensure_legal_documents_index"
    )
    @patch("app.services.document_indexer.bulk")
    def test_document_indexer_restores_snapshot_on_stale_delete_failure(
        self,
        bulk_mock,
        ensure_mock,
    ) -> None:
        del ensure_mock
        # Call #1: the edit's own bulk write, succeeds. Call #2: the
        # internal rollback's own reindex-snapshot bulk call, also
        # succeeds - exactly like
        # CountryIndexerTests.test_country_indexer_restores_
        # snapshot_on_cleanup_failure's own proven shape.
        bulk_mock.side_effect = [
            (1, []),
            (1, []),
        ]

        client = FakeWholeDocumentOpenSearch(fail_cleanup=True)

        with self.assertRaises(DocumentIndexingError):
            replace_document_chunks(
                chunks=[
                    DocumentChunk(
                        document_id=AU_DOCUMENT_ID,
                        chunk_id="chunk-new-1",
                        country="Australia",
                        country_code="AU",
                        legal_topic="Employment Contracts",
                        document_type="comparator",
                        language="en",
                        section="Employment Contracts",
                        subsection="Trial Period",
                        content="New content that never sticks.",
                        source_filename="Australia 2026.docx",
                        source_format="docx",
                        content_hash="new-content-hash",
                        reference_year=2026,
                    )
                ],
                client=client,
            )

        # The stale-cleanup call (#1, fails) and the internal
        # rollback's own wipe-then-restore delete_by_query call (#2,
        # succeeds) both ran.
        self.assertEqual(client.delete_calls, 2)
        self.assertEqual(bulk_mock.call_count, 2)


if __name__ == "__main__":
    unittest.main()