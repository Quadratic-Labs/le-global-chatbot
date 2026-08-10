"""
Router-level integration tests for the admin document HTTP contract
(mission "HOTFIX 0.4.9" review, sections 5/6/9).

These call the FastAPI route functions directly (upload_admin_document,
delete_admin_document) rather than through an ASGI TestClient - this
project has never used TestClient (see git history), and a plain
function call already exercises 100% of the route function's own
code, including every exception -> HTTPException mapping; only the
ASGI/HTTP transport itself is skipped, which is not what this suite
verifies. get_settings() and get_opensearch_client() are both
@lru_cache-decorated and read real environment variables / construct
a real OpenSearch client when uncached, so every test here clears
both caches before and after itself and points settings at a fresh
temp directory - never /data/documents/source, never a real cluster.
"""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from docx import Document
from fastapi import HTTPException, UploadFile

from app.core.config import get_settings
from app.routers import admin_document_lifecycle as lifecycle_router
from app.routers import admin_documents as documents_router


_SETTINGS_ENV_KEYS = (
    "OPENSEARCH_URL",
    "OPENSEARCH_PASSWORD",
    "REDIS_URL",
    "DOCUMENT_SOURCE_DIR",
    "DOCUMENT_PROCESSED_DIR",
    "ADMIN_API_KEY",
    "API_ACCESS_KEY",
)


def _build_real_docx_bytes(country_line: str) -> bytes:
    document = Document()
    document.add_paragraph(country_line)
    document.add_paragraph("I. GENERAL OVERVIEW")
    document.add_paragraph("1. Introduction")
    document.add_paragraph("Overview content.")
    document.add_paragraph("II. Hiring Practices")
    document.add_paragraph("Hiring content.")
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


_AR_BYTES = _build_real_docx_bytes(
    "Labour and Employment Law in Argentina 2026"
)


class FakeOpenSearch:
    """
    In-memory OpenSearch double at chunk granularity - matches the
    real index's own granularity (one document has 1+ chunks, each a
    separate hit), correctly handling every query shape the admin
    upload/replace/delete/reindex code paths issue: term.document_id,
    term.country_code, and the bool/filter+must_not "keep these chunk
    ids" shape _delete_country_chunks uses.
    """

    def __init__(self) -> None:
        self.chunks: dict[str, dict[str, Any]] = {}

    def add(
        self,
        *,
        document_id: str,
        country_code: str,
        source_filename: str,
        chunk_id: str | None = None,
    ) -> None:
        resolved_chunk_id = chunk_id or f"{document_id}-chunk-0"
        self.chunks[resolved_chunk_id] = {
            "document_id": document_id,
            "chunk_id": resolved_chunk_id,
            "country_code": country_code,
            "source_filename": source_filename,
            "country": "Argentina" if country_code == "AR" else "Other",
            "reference_year": 2026,
        }

    def document_ids_for_country(self, country_code: str) -> set[str]:
        return {
            c["document_id"]
            for c in self.chunks.values()
            if c["country_code"] == country_code
        }

    def search(self, *, index: str, body: dict[str, Any]) -> dict[str, Any]:
        del index
        term = body["query"].get("term") or {}

        if "country_code" in term:
            # One hit per actual chunk, matching the real index's own
            # granularity - callers that want distinct documents
            # (e.g. lookup_existing_country_documents) already
            # deduplicate by document_id client-side; callers that
            # need the true per-document chunk count (e.g. mission
            # "HOTFIX 0.4.9" review 3's _snapshot_country_chunks-based
            # expected_chunks validation) require every chunk hit,
            # never one representative hit per document.
            hits = [
                c for c in self.chunks.values()
                if c["country_code"] == term["country_code"]
            ]
        elif "document_id" in term:
            hits = [
                c for c in self.chunks.values()
                if c["document_id"] == term["document_id"]
            ]
        else:
            hits = list(self.chunks.values())

        return {
            "hits": {
                "total": {"value": len(hits)},
                "hits": [
                    {"_id": h["chunk_id"], "_source": h} for h in hits
                ],
            }
        }

    def delete_by_query(self, **kwargs: Any) -> dict[str, Any]:
        query = kwargs["body"]["query"]
        term = query.get("term")
        deleted = 0

        if term and "document_id" in term:
            target = term["document_id"]
            for chunk_id in [
                cid for cid, c in self.chunks.items()
                if c["document_id"] == target
            ]:
                del self.chunks[chunk_id]
                deleted += 1
        elif term and "country_code" in term:
            country = term["country_code"]
            for chunk_id in [
                cid for cid, c in self.chunks.items()
                if c["country_code"] == country
            ]:
                del self.chunks[chunk_id]
                deleted += 1
        elif "bool" in query:
            country = query["bool"]["filter"][0]["term"]["country_code"]
            keep_ids = set(
                query["bool"]["must_not"][0]["terms"]["chunk_id"]
            )
            for chunk_id in [
                cid for cid, c in self.chunks.items()
                if c["country_code"] == country and cid not in keep_ids
            ]:
                del self.chunks[chunk_id]
                deleted += 1

        return {"deleted": deleted}


def _bulk_fake(client: FakeOpenSearch, actions, **kwargs):
    action_list = list(actions)
    for action in action_list:
        doc = action["_source"]
        client.add(
            document_id=doc["document_id"],
            chunk_id=doc["chunk_id"],
            country_code=doc["country_code"],
            source_filename=doc["source_filename"],
        )
    return (len(action_list), [])


def _make_upload_file(filename: str, content: bytes) -> UploadFile:
    return UploadFile(file=io.BytesIO(content), filename=filename)


class AdminRouterIntegrationTestCase(unittest.TestCase):
    """
    Base class: fresh temp source/processed dirs and a fresh
    FakeOpenSearch per test, get_settings()/get_opensearch_client()
    patched at every call site the admin code uses them from.
    """

    def setUp(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory()
        root = Path(self._tempdir.name)
        self.source_dir = root / "source"
        self.processed_dir = root / "processed"
        self.source_dir.mkdir(parents=True)
        self.processed_dir.mkdir(parents=True)

        self._original_env = {
            key: os.environ.get(key) for key in _SETTINGS_ENV_KEYS
        }
        os.environ["OPENSEARCH_URL"] = "http://unused-in-this-test:9200"
        os.environ["OPENSEARCH_PASSWORD"] = "unused"
        os.environ["REDIS_URL"] = "redis://unused-in-this-test:6379/0"
        os.environ["DOCUMENT_SOURCE_DIR"] = str(self.source_dir)
        os.environ["DOCUMENT_PROCESSED_DIR"] = str(self.processed_dir)
        os.environ["ADMIN_API_KEY"] = "unused-admin-key"
        os.environ["API_ACCESS_KEY"] = "unused-api-key"
        get_settings.cache_clear()

        self.fake = FakeOpenSearch()

        self._patches = [
            patch(
                "app.services.admin_document_replacement."
                "get_opensearch_client",
                return_value=self.fake,
            ),
            patch(
                "app.services.admin_document_lifecycle."
                "get_opensearch_client",
                return_value=self.fake,
            ),
            patch(
                "app.services.admin_documents.get_opensearch_client",
                return_value=self.fake,
            ),
            patch(
                "app.services.document_indexer.get_opensearch_client",
                return_value=self.fake,
            ),
            patch(
                "app.services.document_indexer.bulk",
                side_effect=lambda client, actions, **kw: _bulk_fake(
                    self.fake, actions, **kw
                ),
            ),
            patch(
                "app.services.document_indexer."
                "ensure_legal_documents_index"
            ),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self) -> None:
        for p in reversed(self._patches):
            p.stop()

        for key, value in self._original_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        get_settings.cache_clear()

        self._tempdir.cleanup()


class FreshUploadHttpContractTests(AdminRouterIntegrationTestCase):
    """Mission section 5 - fresh upload -> HTTP 201, then delete, then
    reupload under a different filename."""

    def test_fresh_upload_returns_201_uploaded_one_document_indexed(
        self,
    ) -> None:
        upload = _make_upload_file("Argentina.docx", _AR_BYTES)

        response = documents_router.upload_admin_document(
            file=upload, replace_existing=False
        )

        self.assertEqual(response.status, "uploaded")
        self.assertEqual(response.country_code, "AR")
        self.assertGreater(response.indexed_chunks, 0)
        self.assertTrue((self.source_dir / "AR.docx").exists())
        self.assertEqual(
            len(self.fake.document_ids_for_country("AR")), 1
        )

    def test_upload_then_delete_then_reupload_different_filename(
        self,
    ) -> None:
        first = documents_router.upload_admin_document(
            file=_make_upload_file("Argentina.docx", _AR_BYTES),
            replace_existing=False,
        )
        self.assertEqual(first.status, "uploaded")

        delete_response = lifecycle_router.delete_admin_document(
            document_id=first.document_id
        )
        self.assertEqual(delete_response.status, "deleted")
        self.assertEqual(
            len(self.fake.document_ids_for_country("AR")), 0
        )
        self.assertFalse((self.source_dir / "AR.docx").exists())

        second = documents_router.upload_admin_document(
            file=_make_upload_file(
                "random-file-name.docx", _AR_BYTES
            ),
            replace_existing=False,
        )

        self.assertEqual(second.status, "uploaded")
        self.assertEqual(
            len(self.fake.document_ids_for_country("AR")), 1
        )
        self.assertTrue((self.source_dir / "AR.docx").exists())


class ExistingCountryHttpContractTests(AdminRouterIntegrationTestCase):
    """Mission section 6 - existing country always yields 409 with the
    structured detail, regardless of the uploaded filename."""

    def setUp(self) -> None:
        super().setUp()
        self.fake.add(
            document_id="doc_" + "a" * 64,
            country_code="AR",
            source_filename="Argentina.docx",
        )
        (self.source_dir / "AR.docx").write_bytes(b"existing-ar-bytes")

    def test_same_filename_returns_409_with_structured_detail(
        self,
    ) -> None:
        with self.assertRaises(HTTPException) as context:
            documents_router.upload_admin_document(
                file=_make_upload_file("Argentina.docx", _AR_BYTES),
                replace_existing=False,
            )

        error = context.exception
        self.assertEqual(error.status_code, 409)
        self.assertEqual(
            error.detail["code"], "document_replacement_required"
        )
        self.assertEqual(error.detail["country_code"], "AR")
        # No mutation.
        self.assertEqual(
            (self.source_dir / "AR.docx").read_bytes(),
            b"existing-ar-bytes",
        )
        self.assertEqual(
            len(self.fake.document_ids_for_country("AR")), 1
        )

    def test_completely_unrelated_filename_still_returns_409(
        self,
    ) -> None:
        # Same content (AR), a filename with no relation whatsoever
        # to the country or to the existing source's own name.
        with self.assertRaises(HTTPException) as context:
            documents_router.upload_admin_document(
                file=_make_upload_file(
                    "Legal-update-final-v7.docx", _AR_BYTES
                ),
                replace_existing=False,
            )

        error = context.exception
        self.assertEqual(error.status_code, 409)
        self.assertEqual(
            error.detail["code"], "document_replacement_required"
        )
        self.assertEqual(
            (self.source_dir / "AR.docx").read_bytes(),
            b"existing-ar-bytes",
        )


class DeleteHttpContractTests(AdminRouterIntegrationTestCase):
    """Mission section 9 - DELETE HTTP contract: duplicates (2 and 3
    ids), the last document, source missing, and an unknown id."""

    def test_delete_one_of_two_duplicates_defers_and_keeps_the_other(
        self,
    ) -> None:
        first_id = "doc_" + "a" * 64
        second_id = "doc_" + "b" * 64
        self.fake.add(
            document_id=first_id,
            country_code="AR",
            source_filename="Argentina.docx",
        )
        self.fake.add(
            document_id=second_id,
            country_code="AR",
            source_filename="Argentina.docx",
            chunk_id=f"{second_id}-chunk-0",
        )
        (self.source_dir / "AR.docx").write_bytes(b"canonical")
        (self.source_dir / "Argentina.docx").write_bytes(b"legacy")

        response = lifecycle_router.delete_admin_document(
            document_id=first_id
        )

        self.assertEqual(response.status, "deleted")
        self.assertTrue(response.source_cleanup_deferred)
        self.assertFalse(response.source_file_deleted)
        self.assertEqual(
            self.fake.document_ids_for_country("AR"), {second_id}
        )
        self.assertTrue((self.source_dir / "AR.docx").exists())
        self.assertTrue((self.source_dir / "Argentina.docx").exists())

    def test_delete_one_of_three_duplicates_defers_and_keeps_the_others(
        self,
    ) -> None:
        ids = ["doc_" + letter * 64 for letter in ("a", "b", "c")]
        for document_id in ids:
            self.fake.add(
                document_id=document_id,
                country_code="AR",
                source_filename="Argentina.docx",
                chunk_id=f"{document_id}-chunk-0",
            )
        (self.source_dir / "AR.docx").write_bytes(b"canonical")

        response = lifecycle_router.delete_admin_document(
            document_id=ids[0]
        )

        self.assertEqual(response.status, "deleted")
        self.assertTrue(response.source_cleanup_deferred)
        self.assertEqual(
            self.fake.document_ids_for_country("AR"), set(ids[1:])
        )
        self.assertTrue((self.source_dir / "AR.docx").exists())

    def test_delete_of_the_last_document_cleans_up_candidate_files(
        self,
    ) -> None:
        only_id = "doc_" + "a" * 64
        self.fake.add(
            document_id=only_id,
            country_code="AR",
            source_filename="Argentina.docx",
        )
        (self.source_dir / "AR.docx").write_bytes(b"canonical")
        (self.source_dir / "Argentina.docx").write_bytes(b"legacy")

        response = lifecycle_router.delete_admin_document(
            document_id=only_id
        )

        self.assertEqual(response.status, "deleted")
        self.assertFalse(response.source_cleanup_deferred)
        self.assertTrue(response.source_file_deleted)
        self.assertEqual(list(self.source_dir.iterdir()), [])
        self.assertEqual(
            self.fake.document_ids_for_country("AR"), set()
        )

    def test_delete_with_source_missing_still_succeeds(self) -> None:
        only_id = "doc_" + "a" * 64
        self.fake.add(
            document_id=only_id,
            country_code="AR",
            source_filename="Argentina.docx",
        )
        # No physical file at all - the source is already missing.

        response = lifecycle_router.delete_admin_document(
            document_id=only_id
        )

        self.assertEqual(response.status, "deleted")
        self.assertFalse(response.source_file_deleted)
        self.assertFalse(response.source_cleanup_deferred)

    def test_delete_unknown_document_id_returns_404(self) -> None:
        with self.assertRaises(HTTPException) as context:
            lifecycle_router.delete_admin_document(
                document_id="doc_" + "f" * 64
            )

        self.assertEqual(context.exception.status_code, 404)
