"""Tests for indexed document reindexing and deletion."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from app.models.document import DocumentChunk
from app.services.admin_document_lifecycle import (
    AdminDocumentNotFoundError,
    AdminDocumentSourceMissingError,
    delete_indexed_document,
    reindex_indexed_document,
)
from app.services.document_indexer import (
    DocumentIndexingResult,
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


class FakeOpenSearchClient:
    """OpenSearch test double for lifecycle operations."""

    def __init__(
        self,
        *,
        document_exists: bool = True,
        source_filename: str = "UK 2026.docx",
    ) -> None:
        self.document_exists = document_exists
        self.source_filename = source_filename
        self.deleted_document_ids: list[str] = []

    def search(
        self,
        index: str,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        del index

        requested_document_id = (
            body["query"]["term"]["document_id"]
        )

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

            (
                source_directory
                / source_filename
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
                / source_filename
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
                / source_filename
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


if __name__ == "__main__":
    unittest.main()