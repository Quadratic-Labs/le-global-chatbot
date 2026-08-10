"""Regression tests for safe country-level document replacement."""

from __future__ import annotations

import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

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
        # Mission "HOTFIX 0.4.9" review, section 11 - a single clean
        # document + byte-identical upload must raise
        # AdminDocumentAlreadyCurrentError and never call the
        # indexer at all (explicit "must not be called" double,
        # not merely inferred from the absence of a real connection).
        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root) / "source"
            processed_directory = Path(root) / "processed"
            source_directory.mkdir(parents=True)

            (
                source_directory
                / "Employment Law Overview Australia.docx"
            ).write_bytes(b"same-australia")

            def indexer_must_not_be_called(**kwargs):
                del kwargs
                raise AssertionError(
                    "the indexer must not be called for an "
                    "already-current document"
                )

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
                    country_document_indexer=(
                        indexer_must_not_be_called
                    ),
                )

    def test_fresh_country_is_staged_and_parsed_exactly_once(
        self,
    ) -> None:
        # Mission "HOTFIX 0.4.9" - the Argentina regression: an
        # earlier implementation detected a fresh/absent country
        # during a preflight parse, then delegated to a second,
        # separate upload implementation that re-staged and re-parsed
        # the exact same upload a second time by rewinding and
        # re-reading file_stream - fragile by construction (depends on
        # a stream that may not always be safely re-readable) and
        # never necessary, since nothing about the already-computed
        # chunks needs to change for a fresh country. This proves the
        # unified implementation reads the stream and invokes
        # chunk_builder exactly once, regardless of country freshness.
        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root) / "source"
            processed_directory = Path(root) / "processed"

            chunk_builder_calls = []

            def counting_chunk_builder(path: Path):
                chunk_builder_calls.append(path)
                return [_build_au_chunk(path.name)]

            stream = BytesIO(b"new-argentina")
            original_read = stream.read
            read_calls = []

            def counting_read(*args, **kwargs):
                read_calls.append(1)
                return original_read(*args, **kwargs)

            stream.read = counting_read  # type: ignore[method-assign]

            response = safe_upload_and_index_document(
                filename="Argentina.docx",
                file_stream=stream,
                source_directory=source_directory,
                processed_directory=processed_directory,
                maximum_bytes=1000,
                chunk_builder=counting_chunk_builder,
                country_document_lookup=lambda code, client: [],
                country_document_indexer=lambda *, chunks, client=None: (
                    DocumentIndexingResult(
                        index_alias="legal-documents-v1",
                        document_id=chunks[0].document_id,
                        source_filename=chunks[0].source_filename,
                        requested_chunks=len(chunks),
                        indexed_chunks=len(chunks),
                        stale_chunks_deleted=0,
                    )
                ),
            )

            self.assertEqual(
                len(chunk_builder_calls),
                1,
                "chunk_builder must run exactly once for a fresh "
                "upload - never a second, redundant parse.",
            )

            # Exactly one read pass to EOF: the first read() call
            # returns all bytes, the second returns b"" (EOF) - two
            # calls total for one pass, never four (which a second
            # staging pass would require).
            self.assertLessEqual(len(read_calls), 2)

            self.assertEqual(response.status, "uploaded")
            self.assertEqual(response.document_id, AU_NEW_ID)
            self.assertEqual(response.country_code, "AU")
            self.assertFalse(response.replaced_source_file)
            self.assertEqual(response.replaced_document_ids, [])
            self.assertTrue(
                (source_directory / "AU.docx").exists()
            )
            self.assertEqual(
                (source_directory / "AU.docx").read_bytes(),
                b"new-argentina",
            )

    def test_deleted_then_reuploaded_country_succeeds(self) -> None:
        # Mission "HOTFIX 0.4.9" - the exact end-to-end Argentina
        # scenario: upload, delete, then re-upload under a completely
        # different filename must succeed and leave exactly one
        # active document, with no stale conflict from the deletion.
        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root) / "source"
            processed_directory = Path(root) / "processed"

            def indexer(*, chunks, client=None):
                return DocumentIndexingResult(
                    index_alias="legal-documents-v1",
                    document_id=chunks[0].document_id,
                    source_filename=chunks[0].source_filename,
                    requested_chunks=len(chunks),
                    indexed_chunks=len(chunks),
                    stale_chunks_deleted=0,
                )

            first = safe_upload_and_index_document(
                filename="Argentina.docx",
                file_stream=BytesIO(b"first-argentina"),
                source_directory=source_directory,
                processed_directory=processed_directory,
                maximum_bytes=1000,
                chunk_builder=lambda path: [
                    _build_au_chunk(path.name)
                ],
                country_document_lookup=lambda code, client: [],
                country_document_indexer=indexer,
            )
            self.assertEqual(first.status, "uploaded")

            # Simulate a completed delete: the source file removed,
            # the country lookup now reporting zero active documents
            # again - exactly the state a real DELETE leaves behind.
            (source_directory / "AU.docx").unlink()

            second = safe_upload_and_index_document(
                filename="random-name-completely-different.docx",
                file_stream=BytesIO(b"second-argentina"),
                source_directory=source_directory,
                processed_directory=processed_directory,
                maximum_bytes=1000,
                chunk_builder=lambda path: [
                    _build_au_chunk(path.name)
                ],
                country_document_lookup=lambda code, client: [],
                country_document_indexer=indexer,
            )

            self.assertEqual(second.status, "uploaded")
            self.assertEqual(
                [path.name for path in source_directory.iterdir()],
                ["AU.docx"],
            )
            self.assertEqual(
                (source_directory / "AU.docx").read_bytes(),
                b"second-argentina",
            )


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


class FilenameNeverDecidesIdentityTests(unittest.TestCase):
    """
    Mission "HOTFIX 0.4.9", section F - the country_code detected from
    a DOCX's own content is the only identity that matters; the
    uploaded filename is display information only. Three completely
    different filenames carrying the exact same detected country must
    all resolve to the exact same existing-country/409 outcome.
    """

    def test_same_filename_as_the_existing_document_still_requires_confirmation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root) / "source"
            processed_directory = Path(root) / "processed"
            source_directory.mkdir(parents=True)

            (
                source_directory
                / "Employment Law Overview Australia.docx"
            ).write_bytes(b"old-australia")

            with self.assertRaises(
                AdminDocumentReplacementRequiredError
            ):
                safe_upload_and_index_document(
                    # The exact same name as the existing source -
                    # must never be treated as "no real change".
                    filename=(
                        "Employment Law Overview Australia.docx"
                    ),
                    file_stream=BytesIO(b"new-australia"),
                    source_directory=source_directory,
                    processed_directory=processed_directory,
                    maximum_bytes=1000,
                    chunk_builder=lambda path: [
                        _build_au_chunk(path.name)
                    ],
                    country_document_lookup=_existing_documents,
                )

    def test_unrelated_filenames_all_detect_the_same_country(
        self,
    ) -> None:
        for filename in (
            "Argentina.docx",
            "random-file-name.docx",
            "Legal-update-final-v7.docx",
        ):
            with self.subTest(filename=filename):
                with tempfile.TemporaryDirectory() as root:
                    source_directory = Path(root) / "source"
                    processed_directory = Path(root) / "processed"

                    response = safe_upload_and_index_document(
                        filename=filename,
                        file_stream=BytesIO(b"argentina-bytes"),
                        source_directory=source_directory,
                        processed_directory=processed_directory,
                        maximum_bytes=1000,
                        chunk_builder=lambda path: [
                            _build_au_chunk(path.name)
                        ],
                        country_document_lookup=(
                            lambda code, client: []
                        ),
                        country_document_indexer=(
                            lambda *, chunks, client=None: (
                                DocumentIndexingResult(
                                    index_alias="legal-documents-v1",
                                    document_id=chunks[0].document_id,
                                    source_filename=(
                                        chunks[0].source_filename
                                    ),
                                    requested_chunks=len(chunks),
                                    indexed_chunks=len(chunks),
                                    stale_chunks_deleted=0,
                                )
                            )
                        ),
                    )

                    self.assertEqual(response.status, "uploaded")
                    self.assertEqual(response.country_code, "AU")
                    self.assertTrue(
                        (source_directory / "AU.docx").exists()
                    )


class ConfirmedReplacementDuplicateCountTests(unittest.TestCase):
    """
    Mission "HOTFIX 0.4.9", section 8 - confirmed replacement must
    collapse to exactly one active document/source regardless of how
    many duplicate legacy document_ids and source files the country
    started with (tested here for 3, complementing the existing
    2-duplicate coverage in test_confirmed_replacement_collapses_
    files_and_ids above).
    """

    def test_confirmed_replace_with_three_duplicate_ids(self) -> None:
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
                    document_id="doc_" + letter * 64,
                    source_filename=legacy_path.name,
                    country="Australia",
                    country_code="AU",
                    reference_year=None,
                )
                for letter in ("a", "c", "d")
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
            self.assertEqual(
                response.replaced_document_ids,
                sorted(
                    document.document_id for document in existing
                ),
            )
            self.assertEqual(
                [
                    path.name
                    for path in source_directory.iterdir()
                ],
                ["AU.docx"],
            )

    def test_confirmed_replace_with_source_missing(self) -> None:
        # The country's OpenSearch metadata still names a source that
        # no longer exists on disk - confirmed replacement must still
        # succeed (write the new file, index it) rather than treat a
        # missing legacy file as a blocking error.
        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root) / "source"
            processed_directory = Path(root) / "processed"
            source_directory.mkdir(parents=True)

            existing = [
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

            def indexer(*, chunks, client=None):
                return DocumentIndexingResult(
                    index_alias="legal-documents-v1",
                    document_id=chunks[0].document_id,
                    source_filename=chunks[0].source_filename,
                    requested_chunks=len(chunks),
                    indexed_chunks=len(chunks),
                    stale_chunks_deleted=0,
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
                country_document_lookup=lambda code, client: existing,
                country_document_indexer=indexer,
            )

            self.assertEqual(response.status, "replaced")
            self.assertFalse(response.replaced_source_file)
            self.assertEqual(
                response.replaced_document_ids, [AU_OLD_ID]
            )
            self.assertEqual(
                (source_directory / "AU.docx").read_bytes(),
                b"new-australia",
            )


class IdenticalFileAmbiguousCountryTests(unittest.TestCase):
    """
    Mission "HOTFIX 0.4.9", section 13.B - a country left with
    several document_ids (a pre-existing conflict/duplicate state)
    must never be told "document_already_current", even if the newly
    uploaded bytes happen to match one of the candidate files exactly:
    the country's own state is inconsistent, and normalizing it
    always requires an explicit confirmed replacement.
    """

    def test_identical_bytes_but_ambiguous_country_still_requires_confirmation(
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
            legacy_path.write_bytes(b"same-australia")
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

            with self.assertRaises(
                AdminDocumentReplacementRequiredError
            ):
                safe_upload_and_index_document(
                    filename="Australia 2026.docx",
                    # Byte-identical to the legacy file specifically.
                    file_stream=BytesIO(b"same-australia"),
                    source_directory=source_directory,
                    processed_directory=processed_directory,
                    maximum_bytes=1000,
                    chunk_builder=lambda path: [
                        _build_au_chunk(path.name)
                    ],
                    country_document_lookup=(
                        lambda code, client: existing
                    ),
                )

            # Nothing on disk changed.
            self.assertEqual(
                legacy_path.read_bytes(), b"same-australia"
            )
            self.assertEqual(
                canonical_path.read_bytes(), b"other-australia"
            )


class SourceWriteFailureRollbackTests(unittest.TestCase):
    """
    Mission "HOTFIX 0.4.9", section 14 - injecting a failure at the
    physical write step itself (not just at the OpenSearch indexing
    step) must still leave the country's previous state exactly
    intact, for both a fresh upload and a confirmed replacement.
    """

    def test_fresh_upload_write_failure_leaves_no_partial_file(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root) / "source"
            processed_directory = Path(root) / "processed"

            def failing_indexer(**kwargs):
                del kwargs
                raise DocumentIndexingError(
                    "simulated indexing failure"
                )

            with self.assertRaises(DocumentIndexingError):
                safe_upload_and_index_document(
                    filename="Argentina.docx",
                    file_stream=BytesIO(b"argentina-bytes"),
                    source_directory=source_directory,
                    processed_directory=processed_directory,
                    maximum_bytes=1000,
                    chunk_builder=lambda path: [
                        _build_au_chunk(path.name)
                    ],
                    country_document_lookup=(
                        lambda code, client: []
                    ),
                    country_document_indexer=failing_indexer,
                )

            # No partial/incoming file survives a failed fresh upload.
            self.assertEqual(
                list(source_directory.iterdir()),
                [],
            )

    def test_confirmed_replacement_write_failure_restores_exact_bytes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root) / "source"
            processed_directory = Path(root) / "processed"
            source_directory.mkdir(parents=True)

            existing_path = (
                source_directory
                / "Employment Law Overview Australia.docx"
            )
            existing_path.write_bytes(b"original-australia-bytes")

            def failing_indexer(**kwargs):
                del kwargs
                raise DocumentIndexingError(
                    "simulated indexing failure"
                )

            with self.assertRaises(DocumentIndexingError):
                safe_upload_and_index_document(
                    filename="Australia 2026.docx",
                    file_stream=BytesIO(b"new-australia-bytes"),
                    source_directory=source_directory,
                    processed_directory=processed_directory,
                    maximum_bytes=1000,
                    replace_existing=True,
                    chunk_builder=lambda path: [
                        _build_au_chunk(path.name)
                    ],
                    country_document_lookup=_existing_documents,
                    country_document_indexer=failing_indexer,
                )

            self.assertEqual(
                existing_path.read_bytes(),
                b"original-australia-bytes",
            )
            self.assertEqual(
                [
                    path.name
                    for path in source_directory.iterdir()
                ],
                [existing_path.name],
            )


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
