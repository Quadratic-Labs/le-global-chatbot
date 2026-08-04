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
    _sanitize_filename,
    list_indexed_documents,
    upload_and_index_document,
)
from app.services.document_chunk_builder import DOCUMENT_FAMILY
from app.services.document_indexer import (
    DocumentIndexingError,
    DocumentIndexingResult,
)


def _no_existing_source(
    document_id: str,
    client: Any,
) -> None:
    """
    Stub for upload_and_index_document's existing_source_lookup: no
    prior document shares this document_id - the ordinary case for a
    fresh temporary source_directory in these tests. Tests that need
    to simulate a real pre-existing legacy document override this
    explicitly instead.
    """

    del document_id
    del client

    return None


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

    def test_invalid_docx_content_is_rejected_before_any_storage(
        self,
    ) -> None:
        # Mission "CONTINUATION PATCH 0.4.3", section 16/19 - the real
        # upload path (default chunk_builder, i.e. the actual DOCX
        # format validation), never a fake/stubbed one. A .docx
        # extension with non-DOCX bytes must be refused, and nothing
        # may be left behind in source_directory.
        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root) / "source"

            with self.assertRaises(InvalidDocumentUploadError):
                upload_and_index_document(
                    filename="renamed.docx",
                    file_stream=BytesIO(
                        b"This is plain text, not a real DOCX."
                    ),
                    source_directory=source_directory,
                    processed_directory=Path(root) / "processed",
                    maximum_bytes=1000,
                )

            self.assertEqual(
                list(source_directory.iterdir())
                if source_directory.exists()
                else [],
                [],
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
                existing_source_lookup=_no_existing_source,
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

            # Stored under a country-derived name, never the
            # original filename (mission "CONTINUATION PATCH 0.4.3",
            # section 10) - the original name survives only as
            # response.source_filename, checked separately below.
            self.assertEqual(
                (
                    source_directory
                    / "GB.docx"
                ).read_bytes(),
                b"uploaded-docx",
            )

            self.assertEqual(
                response.source_filename,
                "UK 2026.docx",
            )

            self.assertEqual(
                response.document_family,
                DOCUMENT_FAMILY,
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

            # Storage is keyed by the detected country_code ("GB",
            # from _build_chunk), never by the original filename.
            final_path = (
                source_directory
                / "GB.docx"
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
                    existing_source_lookup=_no_existing_source,
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

            # The stored OpenSearch metadata's source_filename is
            # display-only ("UK 2026.docx") and deliberately does NOT
            # match the on-disk storage filename ("GB.docx", keyed by
            # country_code) - proving presence detection uses the
            # country-derived storage path, never source_filename.
            (
                source_directory
                / "GB.docx"
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
                response.documents[0].source_filename,
                "UK 2026.docx",
            )

            self.assertEqual(
                response.documents[0].chunk_count,
                41,
            )

            self.assertEqual(
                response.documents[0].status,
                "indexed",
            )

    def test_indexed_document_missing_from_disk_is_flagged(
        self,
    ) -> None:
        # No file at all is written to source_directory here - proves
        # the presence check is genuinely tied to storage_filename_for_
        # country, not merely always True.
        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root)

            response = list_indexed_documents(
                source_directory=source_directory,
                client=FakeOpenSearchClient(),
            )

            self.assertEqual(
                response.documents[0].status,
                "indexed_source_missing",
            )

    def test_legacy_document_with_historical_filename_shows_source_available(
        self,
    ) -> None:
        # Mission "HOTFIX 0.4.4", section 7.A - a document indexed
        # before country-keyed storage existed: its historical
        # source_filename is the exact, only physical file on disk;
        # GB.docx (the canonical name) is deliberately absent. Must
        # resolve as available, never "indexed_source_missing".
        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root)

            (
                source_directory / "UK 2026.docx"
            ).write_bytes(b"legacy-document-bytes")

            response = list_indexed_documents(
                source_directory=source_directory,
                client=FakeOpenSearchClient(),
            )

            self.assertEqual(
                response.documents[0].status,
                "indexed",
            )

            self.assertTrue(
                response.documents[0].source_file_present
            )

            self.assertFalse(
                (source_directory / "GB.docx").exists()
            )

    def test_conflicting_sources_are_flagged_not_guessed(
        self,
    ) -> None:
        # Both the historical filename and the canonical name exist as
        # two distinct real files - never silently pick one.
        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root)

            (
                source_directory / "UK 2026.docx"
            ).write_bytes(b"legacy-bytes")

            (
                source_directory / "GB.docx"
            ).write_bytes(b"canonical-bytes")

            response = list_indexed_documents(
                source_directory=source_directory,
                client=FakeOpenSearchClient(),
            )

            self.assertEqual(
                response.documents[0].status,
                "indexed_source_conflict",
            )

            self.assertFalse(
                response.documents[0].source_file_present
            )


class FilenameAcceptanceTests(unittest.TestCase):
    """
    Mission "CONTINUATION PATCH 0.4.3", section 14 - arbitrary safe
    DOCX filenames are accepted verbatim: no business naming format
    is required at all, only the safety checks listed in section 4.
    """

    ACCEPTED_FILENAMES = (
        "Canada_2026-04-15-Employment-Law-Overview-EDITED.docx",
        "final.docx",
        "Canada final version.docx",
        "document_received_from_client.docx",
        "Version corrigée (3).DOCX",
        "fichier client été 2026.docx",
        "Spain-template-used-for-Canada.docx",
    )

    def test_all_example_filenames_are_accepted_verbatim(self) -> None:
        for filename in self.ACCEPTED_FILENAMES:
            with self.subTest(filename=filename):
                self.assertEqual(
                    _sanitize_filename(filename),
                    filename,
                )

    def test_upload_preserves_the_arbitrary_filename_exactly(self) -> None:
        # End-to-end proof through the real upload path: the response's
        # source_filename is exactly the uploaded name, never rewritten,
        # truncated, or accent/space/parenthesis-stripped.
        for filename in self.ACCEPTED_FILENAMES:
            with self.subTest(filename=filename):
                with tempfile.TemporaryDirectory() as root:

                    def chunk_builder(
                        path: Path,
                    ) -> list[DocumentChunk]:
                        return [_build_chunk(path.name)]

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

                    response = upload_and_index_document(
                        filename=filename,
                        file_stream=BytesIO(b"uploaded-docx"),
                        source_directory=Path(root) / "source",
                        processed_directory=Path(root) / "processed",
                        maximum_bytes=1000,
                        chunk_builder=chunk_builder,
                        document_indexer=document_indexer,
                        existing_source_lookup=_no_existing_source,
                    )

                    self.assertEqual(
                        response.source_filename,
                        filename,
                    )


class FilenameRejectionTests(unittest.TestCase):
    """
    Mission "CONTINUATION PATCH 0.4.3", section 14 - only safety
    properties are checked, never a business naming format: these
    filenames must still be rejected for the reasons in section 4
    (path traversal, wrong extension, null byte, empty name).
    """

    REJECTED_FILENAMES = (
        "../../document.docx",
        "../document.docx",
        "folder/document.docx",
        "folder\\document.docx",
        "document.pdf",
        "document.docx.exe",
        "document.docm",
        "",
        "document\x00.docx",
    )

    def test_all_example_filenames_are_rejected(self) -> None:
        for filename in self.REJECTED_FILENAMES:
            with self.subTest(filename=filename):
                with self.assertRaises(InvalidDocumentUploadError):
                    _sanitize_filename(filename)

    def test_case_insensitive_docx_extension_is_still_accepted(
        self,
    ) -> None:
        # Not a rejection case - proves the extension check is
        # genuinely case-insensitive (section 4), independent from
        # the rejection cases above.
        self.assertEqual(
            _sanitize_filename("Report.DOCX"),
            "Report.DOCX",
        )


CANADA_DOCUMENT_ID = "doc_" + "d" * 64


def _build_canada_chunk(
    *,
    filename: str,
    reference_year: int,
) -> DocumentChunk:
    """
    One Canada/employment-law-overview chunk carrying the same fixed
    document_id regardless of year or filename - exactly how the real
    country_code+family identity scheme (document_chunk_builder.
    _build_document_id) behaves once a country is already active.
    """

    return DocumentChunk(
        document_id=CANADA_DOCUMENT_ID,
        chunk_id="chunk-ca-1",
        country="Canada",
        country_code="CA",
        legal_topic="Employment Contracts",
        document_type="comparator",
        language="en",
        section="Employment Contracts",
        subsection="Notice Period",
        content="Notice content.",
        source_filename=filename,
        source_format="docx",
        content_hash="content-hash",
        reference_year=reference_year,
    )


class ReplacingDocumentIndexer:
    """
    A stateful document_indexer double simulating OpenSearch's own
    replace-by-document_id behaviour: the first call for a document_id
    indexes fresh chunks, every later call for the same document_id
    reports the previous chunks as stale/deleted - exactly the
    signal upload_and_index_document's own response surfaces to the
    admin API (mission "CONTINUATION PATCH 0.4.3", section 17).
    """

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.indexed_document_ids: set[str] = set()
        self.call_count = 0

    def __call__(
        self,
        *,
        chunks: list[DocumentChunk],
        client: Any = None,
    ) -> DocumentIndexingResult:
        del client
        self.call_count += 1

        if self.fail:
            raise DocumentIndexingError(
                "Simulated indexing failure."
            )

        document_id = chunks[0].document_id
        stale_chunks_deleted = (
            1 if document_id in self.indexed_document_ids else 0
        )
        self.indexed_document_ids.add(document_id)

        return DocumentIndexingResult(
            index_alias="legal-documents",
            document_id=document_id,
            source_filename=chunks[0].source_filename,
            requested_chunks=len(chunks),
            indexed_chunks=len(chunks),
            stale_chunks_deleted=stale_chunks_deleted,
        )


class ReplacementAndRollbackTests(unittest.TestCase):
    """
    Mission "CONTINUATION PATCH 0.4.3", section 17 - the mandatory
    Canada 2025 -> 2026 replacement scenario, plus its mid-indexing
    failure/rollback variant.
    """

    def test_canada_2026_upload_replaces_the_2025_version(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root) / "source"
            processed_directory = Path(root) / "processed"
            indexer = ReplacingDocumentIndexer()

            first_response = upload_and_index_document(
                filename="Employment Law Overview - Canada 2025.docx",
                file_stream=BytesIO(b"canada-2025-bytes"),
                source_directory=source_directory,
                processed_directory=processed_directory,
                maximum_bytes=1000,
                chunk_builder=lambda path: [
                    _build_canada_chunk(
                        filename=path.name,
                        reference_year=2025,
                    )
                ],
                document_indexer=indexer,
                existing_source_lookup=_no_existing_source,
            )

            self.assertEqual(first_response.status, "indexed")
            self.assertFalse(first_response.replaced_source_file)
            self.assertEqual(first_response.reference_year, 2025)

            second_response = upload_and_index_document(
                filename=(
                    "Canada_2026-04-15-Employment-Law-"
                    "Overview-EDITED.docx"
                ),
                file_stream=BytesIO(b"canada-2026-bytes"),
                source_directory=source_directory,
                processed_directory=processed_directory,
                maximum_bytes=1000,
                chunk_builder=lambda path: [
                    _build_canada_chunk(
                        filename=path.name,
                        reference_year=2026,
                    )
                ],
                document_indexer=indexer,
                existing_source_lookup=_no_existing_source,
            )

            # Same country -> same document_id -> the second upload
            # replaces the first, never creates a second document.
            self.assertEqual(
                second_response.document_id,
                first_response.document_id,
            )
            self.assertEqual(second_response.country_code, "CA")
            self.assertEqual(second_response.country, "Canada")
            self.assertEqual(second_response.reference_year, 2026)
            self.assertTrue(second_response.replaced_source_file)
            self.assertEqual(
                second_response.source_filename,
                (
                    "Canada_2026-04-15-Employment-Law-"
                    "Overview-EDITED.docx"
                ),
            )

            # The OpenSearch double reports the old chunks as stale/
            # deleted on this second call for the same document_id.
            self.assertEqual(indexer.call_count, 2)

            # Exactly one physical file for Canada, holding the NEW
            # content, no leftover backup/incoming temp files.
            entries = list(source_directory.iterdir())
            self.assertEqual(
                [entry.name for entry in entries],
                ["CA.docx"],
            )
            self.assertEqual(
                (source_directory / "CA.docx").read_bytes(),
                b"canada-2026-bytes",
            )

    def test_failed_2026_reindex_restores_the_2025_version(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root) / "source"
            processed_directory = Path(root) / "processed"
            indexer = ReplacingDocumentIndexer()

            upload_and_index_document(
                filename="Employment Law Overview - Canada 2025.docx",
                file_stream=BytesIO(b"canada-2025-bytes"),
                source_directory=source_directory,
                processed_directory=processed_directory,
                maximum_bytes=1000,
                chunk_builder=lambda path: [
                    _build_canada_chunk(
                        filename=path.name,
                        reference_year=2025,
                    )
                ],
                document_indexer=indexer,
                existing_source_lookup=_no_existing_source,
            )

            failing_indexer = ReplacingDocumentIndexer(fail=True)
            failing_indexer.indexed_document_ids = set(
                indexer.indexed_document_ids
            )

            with self.assertRaises(DocumentIndexingError):
                upload_and_index_document(
                    filename=(
                        "Canada_2026-04-15-Employment-Law-"
                        "Overview-EDITED.docx"
                    ),
                    file_stream=BytesIO(b"canada-2026-bytes"),
                    source_directory=source_directory,
                    processed_directory=processed_directory,
                    maximum_bytes=1000,
                    chunk_builder=lambda path: [
                        _build_canada_chunk(
                            filename=path.name,
                            reference_year=2026,
                        )
                    ],
                    document_indexer=failing_indexer,
                    existing_source_lookup=_no_existing_source,
                )

            # The 2025 version is restored exactly - no partial 2026
            # file, no leftover backup/incoming temp files.
            entries = list(source_directory.iterdir())
            self.assertEqual(
                [entry.name for entry in entries],
                ["CA.docx"],
            )
            self.assertEqual(
                (source_directory / "CA.docx").read_bytes(),
                b"canada-2025-bytes",
            )


SPAIN_DOCUMENT_ID = "doc_" + "e" * 64


def _build_spain_chunk(
    *,
    filename: str,
) -> DocumentChunk:
    """One Spain/employment-law-overview chunk, fixed document_id."""

    return DocumentChunk(
        document_id=SPAIN_DOCUMENT_ID,
        chunk_id="chunk-es-1",
        country="Spain",
        country_code="ES",
        legal_topic="Employment Contracts",
        document_type="comparator",
        language="en",
        section="Employment Contracts",
        subsection="Notice Period",
        content="Notice content.",
        source_filename=filename,
        source_format="docx",
        content_hash="content-hash",
        reference_year=2027,
    )


class LegacyReplacementAndRollbackTests(unittest.TestCase):
    """
    Mission "HOTFIX 0.4.4", section 7.C/D - Replace and Rollback for a
    country whose currently active document is still stored under its
    historical filename, never {COUNTRY_CODE}.docx.
    """

    LEGACY_FILENAME = "Labour and Employment Law in Spain 2026.docx"

    def test_replace_upload_finds_and_retires_the_historical_file(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root) / "source"
            processed_directory = Path(root) / "processed"
            source_directory.mkdir(parents=True)

            legacy_path = source_directory / self.LEGACY_FILENAME
            legacy_path.write_bytes(b"legacy-spain-bytes")

            indexer = ReplacingDocumentIndexer()
            indexer.indexed_document_ids.add(SPAIN_DOCUMENT_ID)

            response = upload_and_index_document(
                filename="Spain-2027-update.docx",
                file_stream=BytesIO(b"new-spain-bytes"),
                source_directory=source_directory,
                processed_directory=processed_directory,
                maximum_bytes=1000,
                chunk_builder=lambda path: [
                    _build_spain_chunk(filename=path.name)
                ],
                document_indexer=indexer,
                existing_source_lookup=(
                    lambda document_id, client: self.LEGACY_FILENAME
                ),
            )

            self.assertTrue(response.replaced_source_file)
            self.assertEqual(response.country_code, "ES")

            # The historical file is retired - exactly one physical
            # file remains, under the canonical name, with the new
            # content. No renaming of the historical file occurred:
            # it was replaced, not moved to a new name.
            entries = list(source_directory.iterdir())
            self.assertEqual(
                [entry.name for entry in entries],
                ["ES.docx"],
            )
            self.assertEqual(
                (source_directory / "ES.docx").read_bytes(),
                b"new-spain-bytes",
            )

    def test_failed_replace_upload_restores_the_historical_file_exactly(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root) / "source"
            processed_directory = Path(root) / "processed"
            source_directory.mkdir(parents=True)

            legacy_path = source_directory / self.LEGACY_FILENAME
            original_bytes = b"legacy-spain-bytes"
            legacy_path.write_bytes(original_bytes)

            failing_indexer = ReplacingDocumentIndexer(fail=True)
            failing_indexer.indexed_document_ids.add(
                SPAIN_DOCUMENT_ID
            )

            with self.assertRaises(DocumentIndexingError):
                upload_and_index_document(
                    filename="Spain-2027-update.docx",
                    file_stream=BytesIO(b"new-spain-bytes"),
                    source_directory=source_directory,
                    processed_directory=processed_directory,
                    maximum_bytes=1000,
                    chunk_builder=lambda path: [
                        _build_spain_chunk(filename=path.name)
                    ],
                    document_indexer=failing_indexer,
                    existing_source_lookup=(
                        lambda document_id, client: self.LEGACY_FILENAME
                    ),
                )

            # The historical file is restored exactly, at its own
            # original name and path - never renamed, never left as
            # a partial ES.docx.
            entries = list(source_directory.iterdir())
            self.assertEqual(
                [entry.name for entry in entries],
                [self.LEGACY_FILENAME],
            )
            self.assertEqual(
                legacy_path.read_bytes(),
                original_bytes,
            )

    def test_upload_refuses_when_source_conflict_exists(
        self,
    ) -> None:
        # Both the historical file and a canonical ES.docx already
        # exist - refuse the upload entirely rather than guess which
        # one to replace; nothing on disk may change.
        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root) / "source"
            processed_directory = Path(root) / "processed"
            source_directory.mkdir(parents=True)

            legacy_path = source_directory / self.LEGACY_FILENAME
            legacy_path.write_bytes(b"legacy-spain-bytes")

            canonical_path = source_directory / "ES.docx"
            canonical_path.write_bytes(b"canonical-spain-bytes")

            with self.assertRaises(InvalidDocumentUploadError):
                upload_and_index_document(
                    filename="Spain-2027-update.docx",
                    file_stream=BytesIO(b"new-spain-bytes"),
                    source_directory=source_directory,
                    processed_directory=processed_directory,
                    maximum_bytes=1000,
                    chunk_builder=lambda path: [
                        _build_spain_chunk(filename=path.name)
                    ],
                    document_indexer=ReplacingDocumentIndexer(),
                    existing_source_lookup=(
                        lambda document_id, client: self.LEGACY_FILENAME
                    ),
                )

            self.assertEqual(
                legacy_path.read_bytes(),
                b"legacy-spain-bytes",
            )
            self.assertEqual(
                canonical_path.read_bytes(),
                b"canonical-spain-bytes",
            )


if __name__ == "__main__":
    unittest.main()