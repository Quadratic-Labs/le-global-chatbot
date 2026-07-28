"""Tests for legal document administration."""

from __future__ import annotations

import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from typing import Any

from app.models.document import DocumentChunk
from app.security.admin import admin_key_matches
from app.services.admin_documents import (
    InvalidDocumentUploadError,
    list_indexed_documents,
    upload_and_index_document,
)
from app.services.document_indexer import (
    DocumentIndexingError,
    DocumentIndexingResult,
)


def _build_chunk(
    filename: str,
) -> DocumentChunk:
    """Build one valid document chunk."""

    return DocumentChunk(
        document_id="document-1",
        chunk_id="chunk-1",
        country="United Kingdom",
        country_code="GB",
        legal_topic="Employment Contracts",
        document_type="comparator",
        language="en",
        section="Employment Contracts",
        subsection="Notice Period",
        content="One week of notice may apply.",
        source_filename=filename,
        source_format="docx",
        content_hash="content-hash",
        reference_year=2026,
    )


class FakeOpenSearchClient:
    """OpenSearch test double for the admin catalog."""

    def search(
        self,
        index: str,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        del index
        del body

        return {
            "aggregations": {
                "documents": {
                    "buckets": [
                        {
                            "key": "document-1",
                            "doc_count": 41,
                            "metadata": {
                                "hits": {
                                    "hits": [
                                        {
                                            "_source": {
                                                "document_id": (
                                                    "document-1"
                                                ),
                                                "source_filename": (
                                                    "UK 2026.docx"
                                                ),
                                                "country": (
                                                    "United Kingdom"
                                                ),
                                                "country_code": "GB",
                                                "language": "en",
                                                "document_type": (
                                                    "comparator"
                                                ),
                                                "reference_year": 2026,
                                            }
                                        }
                                    ]
                                }
                            },
                        }
                    ]
                }
            }
        }


class AdminDocumentTests(unittest.TestCase):
    """Tests for administration services."""

    def test_admin_key_matching(
        self,
    ) -> None:
        self.assertTrue(
            admin_key_matches(
                "admin-secret",
                "admin-secret",
            )
        )

        self.assertFalse(
            admin_key_matches(
                "wrong",
                "admin-secret",
            )
        )

        self.assertFalse(
            admin_key_matches(
                None,
                "admin-secret",
            )
        )

    def test_non_docx_upload_is_rejected(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(
                InvalidDocumentUploadError
            ):
                upload_and_index_document(
                    filename="document.pdf",
                    file_stream=BytesIO(b"content"),
                    source_directory=(
                        Path(root) / "source"
                    ),
                    processed_directory=(
                        Path(root) / "processed"
                    ),
                    maximum_bytes=1000,
                )

    def test_oversized_upload_is_rejected(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(
                InvalidDocumentUploadError
            ):
                upload_and_index_document(
                    filename="document.docx",
                    file_stream=BytesIO(
                        b"0123456789"
                    ),
                    source_directory=(
                        Path(root) / "source"
                    ),
                    processed_directory=(
                        Path(root) / "processed"
                    ),
                    maximum_bytes=5,
                )

    def test_valid_upload_is_persisted_and_indexed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as root:
            source_directory = (
                Path(root) / "source"
            )

            processed_directory = (
                Path(root) / "processed"
            )

            def chunk_builder(
                path: Path,
            ) -> list[DocumentChunk]:
                return [
                    _build_chunk(
                        path.name
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

            response = upload_and_index_document(
                filename="UK 2026.docx",
                file_stream=BytesIO(
                    b"uploaded-docx"
                ),
                source_directory=source_directory,
                processed_directory=processed_directory,
                maximum_bytes=1000,
                chunk_builder=chunk_builder,
                document_indexer=document_indexer,
            )

            self.assertEqual(
                response.status,
                "indexed",
            )

            self.assertEqual(
                response.indexed_chunks,
                1,
            )

            self.assertFalse(
                response.replaced_source_file
            )

            self.assertEqual(
                (
                    source_directory
                    / "UK 2026.docx"
                ).read_bytes(),
                b"uploaded-docx",
            )

    def test_previous_source_is_restored_on_index_failure(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as root:
            source_directory = (
                Path(root) / "source"
            )

            processed_directory = (
                Path(root) / "processed"
            )

            source_directory.mkdir(
                parents=True
            )

            final_path = (
                source_directory
                / "UK 2026.docx"
            )

            final_path.write_bytes(
                b"previous-version"
            )

            def chunk_builder(
                path: Path,
            ) -> list[DocumentChunk]:
                return [
                    _build_chunk(
                        path.name
                    )
                ]

            def failing_indexer(
                *,
                chunks,
                client=None,
            ):
                del chunks
                del client

                raise DocumentIndexingError(
                    "Indexing failed"
                )

            with self.assertRaises(
                DocumentIndexingError
            ):
                upload_and_index_document(
                    filename="UK 2026.docx",
                    file_stream=BytesIO(
                        b"new-version"
                    ),
                    source_directory=source_directory,
                    processed_directory=processed_directory,
                    maximum_bytes=1000,
                    chunk_builder=chunk_builder,
                    document_indexer=failing_indexer,
                )

            self.assertEqual(
                final_path.read_bytes(),
                b"previous-version",
            )

    def test_indexed_documents_are_listed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root)

            (
                source_directory
                / "UK 2026.docx"
            ).write_bytes(
                b"document"
            )

            response = list_indexed_documents(
                source_directory=source_directory,
                client=FakeOpenSearchClient(),
            )

            self.assertEqual(
                response.total,
                1,
            )

            self.assertEqual(
                response.documents[0].country_code,
                "GB",
            )

            self.assertEqual(
                response.documents[0].chunk_count,
                41,
            )

            self.assertEqual(
                response.documents[0].status,
                "indexed",
            )


if __name__ == "__main__":
    unittest.main()