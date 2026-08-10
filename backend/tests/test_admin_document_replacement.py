"""Regression tests for safe country-level document replacement."""

from __future__ import annotations

import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from app.models.admin_documents import (
    AdminDocumentUploadResponse,
)
from app.models.document import DocumentChunk
from app.services.admin_document_replacement import (
    AdminDocumentAlreadyCurrentError,
    AdminDocumentReplacementRequiredError,
    ExistingCountryDocument,
    safe_upload_and_index_document,
)
from app.services.document_indexer import (
    DocumentIndexingError,
    DocumentIndexingResult,
    replace_country_document_chunks,
)


AU_OLD_ID = "doc_" + "a" * 64
AU_NEW_ID = "doc_" + "b" * 64


def _build_au_chunk(filename: str) -> DocumentChunk:
    return DocumentChunk(
        document_id=AU_NEW_ID,
        chunk_id="chunk-au-new-1",
        country="Australia",
        country_code="AU",
        legal_topic="Employment Contracts",
        document_type="comparator",
        language="en",
        section="Employment Contracts",
        subsection="Trial Period",
        content="Australian probation content.",
        source_filename=filename,
        source_format="docx",
        content_hash="new-content-hash",
        reference_year=2026,
    )


def _existing_documents(
    country_code: str,
    client=None,
) -> list[ExistingCountryDocument]:
    del client
    assert country_code == "AU"

    return [
        ExistingCountryDocument(
            document_id=AU_OLD_ID,
            source_filename=(
                "Employment Law Overview Australia.docx"
            ),
            country="Australia",
            country_code="AU",
            reference_year=None,
        )
    ]


class SafeCountryReplacementTests(unittest.TestCase):
    def test_existing_country_requires_confirmation_without_changes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root) / "source"
            processed_directory = Path(root) / "processed"
            source_directory.mkdir(parents=True)

            source_path = (
                source_directory
                / "Employment Law Overview Australia.docx"
            )
            source_path.write_bytes(b"old-australia")

            indexer_called = False

            def country_indexer(**kwargs):
                nonlocal indexer_called
                del kwargs
                indexer_called = True
                raise AssertionError("indexer must not run")

            with self.assertRaises(
                AdminDocumentReplacementRequiredError
            ) as context:
                safe_upload_and_index_document(
                    filename="Australia 2026.docx",
                    file_stream=BytesIO(b"new-australia"),
                    source_directory=source_directory,
                    processed_directory=processed_directory,
                    maximum_bytes=1000,
                    chunk_builder=lambda path: [
                        _build_au_chunk(path.name)
                    ],
                    country_document_lookup=(
                        _existing_documents
                    ),
                    country_document_indexer=(
                        country_indexer
                    ),
                )

            self.assertEqual(
                context.exception.country_code,
                "AU",
            )
            self.assertFalse(indexer_called)
            self.assertEqual(
                source_path.read_bytes(),
                b"old-australia",
            )
            self.assertFalse(
                (source_directory / "AU.docx").exists()
            )

    def test_confirmed_replacement_collapses_files_and_ids(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root) / "source"
            processed_directory = Path(root) / "processed"
            source_directory.mkdir(parents=True)

            legacy_path = (
                source_directory
                / "Employment Law Overview Australia.docx"
            )
            canonical_path = source_directory / "AU.docx"

            legacy_path.write_bytes(b"legacy-australia")
            canonical_path.write_bytes(b"other-australia")

            existing = [
                ExistingCountryDocument(
                    document_id=AU_OLD_ID,
                    source_filename=legacy_path.name,
                    country="Australia",
                    country_code="AU",
                    reference_year=None,
                ),
                ExistingCountryDocument(
                    document_id="doc_" + "c" * 64,
                    source_filename=legacy_path.name,
                    country="Australia",
                    country_code="AU",
                    reference_year=None,
                ),
            ]

            def lookup(country_code: str, client=None):
                del client
                self.assertEqual(country_code, "AU")
                return existing

            def indexer(*, chunks, client=None):
                del client
                return DocumentIndexingResult(
                    index_alias="legal-documents-v1",
                    document_id=chunks[0].document_id,
                    source_filename=chunks[0].source_filename,
                    requested_chunks=len(chunks),
                    indexed_chunks=len(chunks),
                    stale_chunks_deleted=118,
                )

            response = safe_upload_and_index_document(
                filename="Australia 2026.docx",
                file_stream=BytesIO(b"new-australia"),
                source_directory=source_directory,
                processed_directory=processed_directory,
                maximum_bytes=1000,
                replace_existing=True,
                chunk_builder=lambda path: [
                    _build_au_chunk(path.name)
                ],
                country_document_lookup=lookup,
                country_document_indexer=indexer,
            )

            self.assertEqual(response.status, "replaced")
            self.assertEqual(response.country_code, "AU")
            self.assertEqual(
                response.replaced_document_ids,
                sorted(
                    [
                        AU_OLD_ID,
                        "doc_" + "c" * 64,
                    ]
                ),
            )
            self.assertFalse(legacy_path.exists())
            self.assertEqual(
                canonical_path.read_bytes(),
                b"new-australia",
            )
            self.assertEqual(
                [path.name for path in source_directory.iterdir()],
                ["AU.docx"],
            )

    def test_failed_confirmed_replacement_restores_all_sources(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root) / "source"
            processed_directory = Path(root) / "processed"
            source_directory.mkdir(parents=True)

            legacy_path = (
                source_directory
                / "Employment Law Overview Australia.docx"
            )
            canonical_path = source_directory / "AU.docx"

            legacy_path.write_bytes(b"legacy-australia")
            canonical_path.write_bytes(b"canonical-australia")

            def failing_indexer(**kwargs):
                del kwargs
                raise DocumentIndexingError(
                    "simulated indexing failure"
                )

            with self.assertRaises(DocumentIndexingError):
                safe_upload_and_index_document(
                    filename="Australia 2026.docx",
                    file_stream=BytesIO(b"new-australia"),
                    source_directory=source_directory,
                    processed_directory=processed_directory,
                    maximum_bytes=1000,
                    replace_existing=True,
                    chunk_builder=lambda path: [
                        _build_au_chunk(path.name)
                    ],
                    country_document_lookup=(
                        _existing_documents
                    ),
                    country_document_indexer=(
                        failing_indexer
                    ),
                )

            self.assertEqual(
                legacy_path.read_bytes(),
                b"legacy-australia",
            )
            self.assertEqual(
                canonical_path.read_bytes(),
                b"canonical-australia",
            )
            self.assertEqual(
                sorted(
                    path.name
                    for path in source_directory.iterdir()
                ),
                [
                    "AU.docx",
                    "Employment Law Overview Australia.docx",
                ],
            )

    def test_identical_single_document_is_not_reindexed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root) / "source"
            processed_directory = Path(root) / "processed"
            source_directory.mkdir(parents=True)

            (
                source_directory
                / "Employment Law Overview Australia.docx"
            ).write_bytes(b"same-australia")

            with self.assertRaises(
                AdminDocumentAlreadyCurrentError
            ):
                safe_upload_and_index_document(
                    filename="Australia 2026.docx",
                    file_stream=BytesIO(b"same-australia"),
                    source_directory=source_directory,
                    processed_directory=processed_directory,
                    maximum_bytes=1000,
                    chunk_builder=lambda path: [
                        _build_au_chunk(path.name)
                    ],
                    country_document_lookup=(
                        _existing_documents
                    ),
                )

    def test_fresh_country_delegates_to_established_upload(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as root:
            expected = AdminDocumentUploadResponse(
                status="indexed",
                document_id=AU_NEW_ID,
                source_filename="Australia 2026.docx",
                country="Australia",
                country_code="AU",
                reference_year=2026,
                document_family="employment_law_overview",
                uploaded_bytes=13,
                indexed_chunks=1,
                stale_chunks_deleted=0,
                replaced_source_file=False,
            )

            calls = []

            def fresh_uploader(**kwargs):
                calls.append(kwargs)
                return expected

            response = safe_upload_and_index_document(
                filename="Australia 2026.docx",
                file_stream=BytesIO(b"new-australia"),
                source_directory=Path(root) / "source",
                processed_directory=Path(root) / "processed",
                maximum_bytes=1000,
                chunk_builder=lambda path: [
                    _build_au_chunk(path.name)
                ],
                country_document_lookup=lambda code, client: [],
                fresh_document_uploader=fresh_uploader,
            )

            self.assertIs(response, expected)
            self.assertEqual(len(calls), 1)


class FakeCountryOpenSearch:
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
                            "document_id": AU_OLD_ID,
                            "chunk_id": "chunk-old-1",
                            "country": "Australia",
                            "country_code": "AU",
                        },
                    }
                ],
            }
        }

    def delete_by_query(self, **kwargs):
        del kwargs
        self.delete_calls += 1

        if self.fail_cleanup and self.delete_calls == 1:
            raise RuntimeError("cleanup failed")

        return {
            "deleted": 1,
        }


class CountryIndexerTests(unittest.TestCase):
    @patch(
        "app.services.document_indexer."
        "ensure_legal_documents_index"
    )
    @patch("app.services.document_indexer.bulk")
    def test_country_indexer_removes_every_stale_country_chunk(
        self,
        bulk_mock,
        ensure_mock,
    ) -> None:
        del ensure_mock
        bulk_mock.return_value = (1, [])

        result = replace_country_document_chunks(
            chunks=[_build_au_chunk("Australia 2026.docx")],
            client=FakeCountryOpenSearch(),
        )

        self.assertEqual(result.indexed_chunks, 1)
        self.assertEqual(result.stale_chunks_deleted, 1)
        self.assertEqual(bulk_mock.call_count, 1)

    @patch(
        "app.services.document_indexer."
        "ensure_legal_documents_index"
    )
    @patch("app.services.document_indexer.bulk")
    def test_country_indexer_restores_snapshot_on_cleanup_failure(
        self,
        bulk_mock,
        ensure_mock,
    ) -> None:
        del ensure_mock
        bulk_mock.side_effect = [
            (1, []),
            (1, []),
        ]

        client = FakeCountryOpenSearch(
            fail_cleanup=True
        )

        with self.assertRaises(DocumentIndexingError):
            replace_country_document_chunks(
                chunks=[
                    _build_au_chunk(
                        "Australia 2026.docx"
                    )
                ],
                client=client,
            )

        self.assertEqual(client.delete_calls, 2)
        self.assertEqual(bulk_mock.call_count, 2)


if __name__ == "__main__":
    unittest.main()
