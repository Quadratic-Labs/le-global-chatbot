from __future__ import annotations

import importlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.models import chat as chat_models
from app.services.contact_photo_store import (
    write_contact_photo_atomic,
)
from app.services.contact_state import (
    ContactRecord,
    ContactState,
    write_contact_state_atomic,
)


class LegalChatContactModelTests(unittest.TestCase):

    def test_legal_chat_response_defaults_contacts_to_empty_list(
        self,
    ) -> None:
        response = chat_models.LegalChatResponse(
            question="Question",
            answer="Answer",
            grounded=True,
            model=None,
            retrieval_total=0,
            sources=[],
        )

        self.assertEqual([], response.contacts)

    def test_legal_chat_contact_has_public_card_shape(
        self,
    ) -> None:
        model = getattr(
            chat_models,
            "LegalChatContact",
        )

        contact = model(
            contact_id="contact-1",
            country_code="BE",
            member_firm="Firm",
            contact_person="Jane Doe",
            email="jane@example.com",
            phone="+32 1",
            address="Address",
            website="example.com",
            photo_url=(
                "/api/v1/contact-photos/"
                "contact-1/"
                + ("a" * 64)
            ),
        )

        self.assertEqual("contact-1", contact.contact_id)
        self.assertEqual("BE", contact.country_code)
        self.assertEqual("Jane Doe", contact.contact_person)
        self.assertTrue(
            contact.photo_url.startswith(
                "/api/v1/contact-photos/contact-1/"
            )
        )


class StructuredContactCardServiceTests(unittest.TestCase):

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.source_directory = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _service(self):
        return importlib.import_module(
            "app.services.chat_contact_cards"
        )

    def _write_state(
        self,
        *,
        document_id: str,
        country_code: str,
        contacts: tuple[ContactRecord, ...],
    ) -> None:
        write_contact_state_atomic(
            self.source_directory,
            ContactState(
                document_id=document_id,
                country_code=country_code,
                contacts=contacts,
            ),
        )

    def test_belgium_returns_two_structured_cards(
        self,
    ) -> None:
        service = self._service()

        first_photo = write_contact_photo_atomic(
            self.source_directory,
            "contact-chris",
            data=b"chris-photo",
            content_type="image/jpeg",
        )

        second_photo = write_contact_photo_atomic(
            self.source_directory,
            "contact-nicolas",
            data=b"nicolas-photo",
            content_type="image/png",
        )

        self._write_state(
            document_id="doc-belgium",
            country_code="BE",
            contacts=(
                ContactRecord(
                    contact_id="contact-chris",
                    member_firm="Van Olmen & Wynant",
                    contact_person="Chris van Olmen",
                    email="chris.van.olmen@vow.be",
                    phone="+32 264 405 11",
                    address="Brussels",
                    website="www.vow.be",
                    photo_filename=first_photo.filename,
                    photo_content_type=first_photo.content_type,
                    photo_sha256=first_photo.sha256,
                ),
                ContactRecord(
                    contact_id="contact-nicolas",
                    member_firm="Van Olmen & Wynant",
                    contact_person="Nicolas Simon",
                    email="nicolas.simon@vow.be",
                    phone="+32 264 405 11",
                    address="Brussels",
                    website="www.vow.be",
                    photo_filename=second_photo.filename,
                    photo_content_type=second_photo.content_type,
                    photo_sha256=second_photo.sha256,
                ),
            ),
        )

        sources = [
            SimpleNamespace(
                document_id="doc-belgium",
                country_code="BE",
            )
        ]

        contacts = service.build_legal_chat_contacts(
            source_directory=self.source_directory,
            requested_country_codes=["BE"],
            unavailable_country_codes=[],
            sources=sources,
        )

        self.assertEqual(2, len(contacts))

        self.assertEqual(
            [
                "Chris van Olmen",
                "Nicolas Simon",
            ],
            [
                item.contact_person
                for item in contacts
            ],
        )

        self.assertEqual(
            [
                "BE",
                "BE",
            ],
            [
                item.country_code
                for item in contacts
            ],
        )

        self.assertEqual(
            (
                "/api/v1/contact-photos/"
                f"contact-chris/{first_photo.sha256}"
            ),
            contacts[0].photo_url,
        )

        self.assertEqual(
            (
                "/api/v1/contact-photos/"
                f"contact-nicolas/{second_photo.sha256}"
            ),
            contacts[1].photo_url,
        )

    def test_contact_without_photo_remains_a_valid_card(
        self,
    ) -> None:
        service = self._service()

        self._write_state(
            document_id="doc-france",
            country_code="FR",
            contacts=(
                ContactRecord(
                    contact_id="contact-france",
                    member_firm="Flichy Grangé Avocats",
                    contact_person=(
                        "Caroline Scherrmann and Florence Bacquet"
                    ),
                    email=(
                        "scherrmann@flichy.com, "
                        "bacquet@flichy.com"
                    ),
                ),
            ),
        )

        contacts = service.build_legal_chat_contacts(
            source_directory=self.source_directory,
            requested_country_codes=["FR"],
            unavailable_country_codes=[],
            sources=[
                SimpleNamespace(
                    document_id="doc-france",
                    country_code="FR",
                )
            ],
        )

        self.assertEqual(1, len(contacts))
        self.assertEqual(
            "Caroline Scherrmann and Florence Bacquet",
            contacts[0].contact_person,
        )
        self.assertIsNone(contacts[0].photo_url)

    def test_missing_source_directory_returns_no_cards(
        self,
    ) -> None:
        service = self._service()

        contacts = service.build_legal_chat_contacts(
            source_directory=None,
            requested_country_codes=["BE"],
            unavailable_country_codes=[],
            sources=[
                SimpleNamespace(
                    document_id="doc-belgium",
                    country_code="BE",
                )
            ],
        )

        self.assertEqual([], contacts)

    def test_missing_structured_state_returns_no_cards(
        self,
    ) -> None:
        service = self._service()

        contacts = service.build_legal_chat_contacts(
            source_directory=self.source_directory,
            requested_country_codes=["BE"],
            unavailable_country_codes=[],
            sources=[
                SimpleNamespace(
                    document_id="missing-doc",
                    country_code="BE",
                )
            ],
        )

        self.assertEqual([], contacts)

    def test_fallback_contact_is_labelled_with_requested_country(
        self,
    ) -> None:
        service = self._service()

        self._write_state(
            document_id="doc-czech",
            country_code="CZ",
            contacts=(
                ContactRecord(
                    contact_id="contact-cz",
                    member_firm="Czech Firm",
                    contact_person="Czech Contact",
                    email="contact@example.cz",
                ),
            ),
        )

        contacts = service.build_legal_chat_contacts(
            source_directory=self.source_directory,
            requested_country_codes=["SK"],
            unavailable_country_codes=[],
            sources=[
                SimpleNamespace(
                    document_id="doc-czech",
                    country_code="CZ",
                )
            ],
        )

        self.assertEqual(1, len(contacts))

        # Same deterministic contact-routing semantics as the text
        # response: Czech data serving a Slovakia enquiry is labelled
        # as the requested jurisdiction in the card payload.
        self.assertEqual(
            "SK",
            contacts[0].country_code,
        )
        self.assertEqual(
            "Czech Contact",
            contacts[0].contact_person,
        )

    def test_same_source_is_not_duplicated_for_same_requested_country(
        self,
    ) -> None:
        service = self._service()

        self._write_state(
            document_id="doc-be",
            country_code="BE",
            contacts=(
                ContactRecord(
                    contact_id="contact-1",
                    contact_person="Person",
                    email="person@example.com",
                ),
            ),
        )

        contacts = service.build_legal_chat_contacts(
            source_directory=self.source_directory,
            requested_country_codes=["BE"],
            unavailable_country_codes=[],
            sources=[
                SimpleNamespace(
                    document_id="doc-be",
                    country_code="BE",
                ),
                SimpleNamespace(
                    document_id="doc-be",
                    country_code="BE",
                ),
            ],
        )

        self.assertEqual(1, len(contacts))


class ContactPhotoResolutionServiceTests(unittest.TestCase):

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.source_directory = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _service(self):
        return importlib.import_module(
            "app.services.chat_contact_cards"
        )

    def test_photo_is_resolved_by_contact_id_and_sha_only(
        self,
    ) -> None:
        service = self._service()

        stored = write_contact_photo_atomic(
            self.source_directory,
            "contact-photo",
            data=b"real-photo-bytes",
            content_type="image/jpeg",
        )

        write_contact_state_atomic(
            self.source_directory,
            ContactState(
                document_id="doc-photo",
                country_code="BE",
                contacts=(
                    ContactRecord(
                        contact_id="contact-photo",
                        contact_person="Person",
                        photo_filename=stored.filename,
                        photo_content_type=stored.content_type,
                        photo_sha256=stored.sha256,
                    ),
                ),
            ),
        )

        resolved = service.resolve_public_contact_photo(
            source_directory=self.source_directory,
            contact_id="contact-photo",
            sha256=stored.sha256,
        )

        self.assertIsNotNone(resolved)
        self.assertEqual(
            b"real-photo-bytes",
            resolved.data,
        )
        self.assertEqual(
            "image/jpeg",
            resolved.content_type,
        )
        self.assertEqual(
            stored.sha256,
            resolved.sha256,
        )

    def test_wrong_sha_cannot_read_current_photo(
        self,
    ) -> None:
        service = self._service()

        stored = write_contact_photo_atomic(
            self.source_directory,
            "contact-photo",
            data=b"photo",
            content_type="image/jpeg",
        )

        write_contact_state_atomic(
            self.source_directory,
            ContactState(
                document_id="doc-photo",
                country_code="BE",
                contacts=(
                    ContactRecord(
                        contact_id="contact-photo",
                        photo_filename=stored.filename,
                        photo_content_type=stored.content_type,
                        photo_sha256=stored.sha256,
                    ),
                ),
            ),
        )

        resolved = service.resolve_public_contact_photo(
            source_directory=self.source_directory,
            contact_id="contact-photo",
            sha256="0" * 64,
        )

        self.assertIsNone(resolved)

    def test_unknown_contact_returns_none(
        self,
    ) -> None:
        service = self._service()

        resolved = service.resolve_public_contact_photo(
            source_directory=self.source_directory,
            contact_id="unknown",
            sha256="0" * 64,
        )

        self.assertIsNone(resolved)


if __name__ == "__main__":
    unittest.main()
