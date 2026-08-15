"""
Router-level HTTP contract tests for the two new country-conflict
endpoints (mission "ORDER 8E-A1", sections 22-26, 43).

Reuses the exact same FakeOpenSearch double and test-scaffolding
convention already established in test_admin_documents_router_integration.py
(calling route functions directly - see that file's own docstring for
why) rather than a second, parallel fixture.
"""

from __future__ import annotations

from unittest.mock import patch

from fastapi import HTTPException

from app.routers import admin_document_conflicts as conflicts_router
from tests.test_admin_documents_router_integration import (
    AdminRouterIntegrationTestCase,
    _make_upload_file,
)


class ConflictResolutionRouterTests(AdminRouterIntegrationTestCase):
    def setUp(self) -> None:
        super().setUp()
        self._extra_patches = [
            patch(
                "app.services.admin_document_conflict_resolution."
                "get_opensearch_client",
                return_value=self.fake,
            ),
        ]
        for p in self._extra_patches:
            p.start()

    def tearDown(self) -> None:
        for p in reversed(self._extra_patches):
            p.stop()
        super().tearDown()

    def _seed_conflict(self) -> None:
        self.fake.add(
            document_id="doc_" + "a" * 64,
            country_code="AR",
            source_filename="Argentina-old.docx",
        )
        self.fake.add(
            document_id="doc_" + "b" * 64,
            country_code="AR",
            source_filename="Argentina-new.docx",
            chunk_id="doc_" + "b" * 64 + "-chunk-0",
        )
        (self.source_dir / "Argentina-old.docx").write_bytes(
            b"old content"
        )
        (self.source_dir / "Argentina-new.docx").write_bytes(
            b"new content, different"
        )

    def test_review_returns_candidates_and_auto_dedup_flag(
        self,
    ) -> None:
        self._seed_conflict()

        response = conflicts_router.get_country_conflict_review(
            country_code="AR"
        )

        self.assertEqual(response.country_code, "AR")
        self.assertEqual(len(response.candidates), 2)
        self.assertFalse(response.auto_deduplicate_available)

    def test_review_404s_when_country_is_not_conflicted(self) -> None:
        self.fake.add(
            document_id="doc_" + "a" * 64,
            country_code="AR",
            source_filename="Argentina.docx",
        )

        with self.assertRaises(HTTPException) as context:
            conflicts_router.get_country_conflict_review(
                country_code="AR"
            )

        self.assertEqual(context.exception.status_code, 404)
        self.assertEqual(
            context.exception.detail["code"],
            "country_conflict_not_found",
        )

    def test_choose_document_resolves_via_http_contract(self) -> None:
        self._seed_conflict()

        response = conflicts_router.resolve_country_conflict(
            country_code="AR",
            resolution_mode="CHOOSE_DOCUMENT",
            keep_document_id="doc_" + "b" * 64,
            file=None,
        )

        self.assertEqual(response.kept_document_id, "doc_" + "b" * 64)
        self.assertEqual(
            response.removed_document_ids, ["doc_" + "a" * 64]
        )
        self.assertFalse(
            (self.source_dir / "Argentina-old.docx").exists()
        )
        self.assertTrue(
            (self.source_dir / "Argentina-new.docx").exists()
        )

    def test_choose_document_stale_id_returns_422(self) -> None:
        self._seed_conflict()

        with self.assertRaises(HTTPException) as context:
            conflicts_router.resolve_country_conflict(
                country_code="AR",
                resolution_mode="CHOOSE_DOCUMENT",
                keep_document_id="doc_totally_unrelated",
                file=None,
            )

        self.assertEqual(context.exception.status_code, 422)
        self.assertEqual(
            context.exception.detail["code"],
            "country_conflict_resolution_invalid",
        )

    def test_replace_with_document_requires_a_file(self) -> None:
        self._seed_conflict()

        with self.assertRaises(HTTPException) as context:
            conflicts_router.resolve_country_conflict(
                country_code="AR",
                resolution_mode="REPLACE_WITH_DOCUMENT",
                file=None,
            )

        self.assertEqual(context.exception.status_code, 422)
        self.assertEqual(
            context.exception.detail["code"], "document_required"
        )

    def test_replace_with_document_rejects_a_mismatched_country(
        self,
    ) -> None:
        self._seed_conflict()

        from docx import Document

        document = Document()
        document.add_paragraph(
            "Labour and Employment Law in Chile 2026"
        )
        import io

        buffer = io.BytesIO()
        document.save(buffer)

        with self.assertRaises(HTTPException) as context:
            conflicts_router.resolve_country_conflict(
                country_code="AR",
                resolution_mode="REPLACE_WITH_DOCUMENT",
                file=_make_upload_file(
                    "chile.docx", buffer.getvalue()
                ),
                confirm_warnings=True,
                country_confirmed=True,
                selected_country_code=None,
            )

        self.assertEqual(context.exception.status_code, 422)
        self.assertEqual(
            context.exception.detail["code"],
            "document_unexpected_country",
        )
        # Zero mutation - the AR conflict is untouched.
        self.assertTrue(
            (self.source_dir / "Argentina-old.docx").exists()
        )
        self.assertTrue(
            (self.source_dir / "Argentina-new.docx").exists()
        )


if __name__ == "__main__":
    import unittest

    unittest.main()
