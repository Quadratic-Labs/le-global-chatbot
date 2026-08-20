from __future__ import annotations

import inspect
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

from app.models.chat import LegalChatResponse
from app.routers import chat
from app.services import chat_contact_cards
from app.services.contact_photo_store import (
    write_contact_photo_atomic,
)
from app.services.contact_state import (
    ContactRecord,
    ContactState,
    write_contact_state_atomic,
)


class ChatContactHttpContractTests(unittest.TestCase):

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.source_directory = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _settings(self):
        return SimpleNamespace(
            document_source_dir=self.source_directory,
        )

    def _seed_photo_contact(self):
        stored = write_contact_photo_atomic(
            self.source_directory,
            "contact-public",
            data=b"public-photo-bytes",
            content_type="image/jpeg",
        )

        write_contact_state_atomic(
            self.source_directory,
            ContactState(
                document_id="doc-public",
                country_code="BE",
                contacts=(
                    ContactRecord(
                        contact_id="contact-public",
                        contact_person="Public Person",
                        email="public@example.com",
                        photo_filename=stored.filename,
                        photo_content_type=stored.content_type,
                        photo_sha256=stored.sha256,
                    ),
                ),
            ),
        )

        return stored

    def test_public_contact_photo_route_is_registered(self) -> None:
        routes = {
            route.path: getattr(route, "methods", set())
            for route in chat.router.routes
        }

        path = (
            "/api/v1/contact-photos/"
            "{contact_id}/{sha256}"
        )

        self.assertIn(path, routes)
        self.assertIn("GET", routes[path])

    def test_public_contact_photo_returns_bytes_mime_etag_and_cache(
        self,
    ) -> None:
        stored = self._seed_photo_contact()

        handler = getattr(
            chat,
            "get_public_contact_photo",
        )

        with patch.object(
            chat,
            "get_settings",
            return_value=self._settings(),
        ):
            response = handler(
                contact_id="contact-public",
                sha256=stored.sha256,
            )

        self.assertEqual(200, response.status_code)
        self.assertEqual(
            b"public-photo-bytes",
            response.body,
        )
        self.assertEqual(
            "image/jpeg",
            response.headers["content-type"],
        )
        self.assertEqual(
            f'"{stored.sha256}"',
            response.headers["etag"],
        )

        cache_control = response.headers["cache-control"]

        self.assertIn(
            "max-age=31536000",
            cache_control,
        )
        self.assertIn(
            "immutable",
            cache_control,
        )

        self.assertEqual(
            "nosniff",
            response.headers["x-content-type-options"],
        )

    def test_wrong_sha_returns_404(self) -> None:
        self._seed_photo_contact()

        handler = getattr(
            chat,
            "get_public_contact_photo",
        )

        with patch.object(
            chat,
            "get_settings",
            return_value=self._settings(),
        ):
            with self.assertRaises(HTTPException) as caught:
                handler(
                    contact_id="contact-public",
                    sha256="0" * 64,
                )

        self.assertEqual(
            404,
            caught.exception.status_code,
        )

    def test_unknown_contact_returns_404(self) -> None:
        handler = getattr(
            chat,
            "get_public_contact_photo",
        )

        with patch.object(
            chat,
            "get_settings",
            return_value=self._settings(),
        ):
            with self.assertRaises(HTTPException) as caught:
                handler(
                    contact_id="unknown",
                    sha256="0" * 64,
                )

        self.assertEqual(
            404,
            caught.exception.status_code,
        )

    def test_chat_uses_shared_contact_fallback_mapping(self) -> None:
        self.assertIs(
            chat.CONTACT_COUNTRY_FALLBACK_CODES,
            chat_contact_cards.CONTACT_COUNTRY_FALLBACK_CODES,
        )

    def test_contact_paths_are_wired_to_structured_card_builder(
        self,
    ) -> None:
        source = inspect.getsource(chat)

        self.assertGreaterEqual(
            source.count(
                "build_legal_chat_contacts("
            ),
            2,
        )

        self.assertIn(
            "contacts=contacts",
            source,
        )

    def test_non_contact_response_remains_backward_compatible(
        self,
    ) -> None:
        response = LegalChatResponse(
            question="What is the notice period in Spain?",
            answer="Existing legal answer",
            grounded=True,
            model="test-model",
            retrieval_total=1,
            sources=[],
        )

        self.assertEqual([], response.contacts)


if __name__ == "__main__":
    unittest.main()
