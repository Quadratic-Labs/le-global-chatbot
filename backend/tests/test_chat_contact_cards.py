from __future__ import annotations

import importlib
import inspect
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

from app.models import chat as chat_models
from app.models.catalog import (
    LegalCatalogCountry,
    LegalCatalogResponse,
)
from app.models.chat import (
    LegalAnswerSource,
    LegalChatRequest,
    LegalChatResponse,
)
from app.routers import chat
from app.services import chat_contact_cards
from app.services.chat_metrics import LegalChatMetrics
from app.services.contact_photo_store import (
    write_contact_photo_atomic,
)
from app.services.contact_state import (
    ContactRecord,
    ContactState,
    write_contact_state_atomic,
)
from app.services.request_understanding import (
    CurrentMessageDelta,
    DeterministicHints,
    RequestUnderstandingAction,
    RequestUnderstandingResult,
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


class MissingEvidenceContactCardTests(unittest.TestCase):

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.source_directory = Path(self.temp.name)

        write_contact_state_atomic(
            self.source_directory,
            ContactState(
                document_id="document-fr",
                country_code="FR",
                contacts=(
                    ContactRecord(
                        contact_id="contact-caroline",
                        member_firm="Flichy Grange Avocats",
                        contact_person="Caroline Scherrmann",
                        email="caroline@example.fr",
                    ),
                    ContactRecord(
                        contact_id="contact-florence",
                        member_firm="Flichy Grange Avocats",
                        contact_person="Florence Bacquet",
                        email="florence@example.fr",
                    ),
                ),
            ),
        )
        write_contact_state_atomic(
            self.source_directory,
            ContactState(
                document_id="document-gb",
                country_code="GB",
                contacts=(
                    ContactRecord(
                        contact_id="contact-robert",
                        member_firm="Clyde & Co",
                        contact_person="Robert Hill",
                        email="robert@example.co.uk",
                    ),
                ),
            ),
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def _catalog() -> LegalCatalogResponse:
        return LegalCatalogResponse(
            countries=[
                LegalCatalogCountry(
                    country_code="FR",
                    country="France",
                    chunk_count=1,
                ),
                LegalCatalogCountry(
                    country_code="GB",
                    country="United Kingdom",
                    chunk_count=1,
                ),
            ],
            legal_topics=[],
            subsections=[],
        )

    @staticmethod
    def _source(
        country_code: str,
        country: str,
        citation: int,
    ) -> LegalAnswerSource:
        return LegalAnswerSource(
            citation=citation,
            document_id=f"document-{country_code.lower()}",
            chunk_id=f"contact-{country_code.lower()}",
            country=country,
            country_code=country_code,
            legal_topic=None,
            section="Contact",
            subsection="Contact",
            source_filename=f"{country}.docx",
            reference_year=2026,
            score=10.0,
        )

    @staticmethod
    def _metrics(question: str) -> LegalChatMetrics:
        return LegalChatMetrics(
            request_id="test-request",
            question_characters=len(question),
            max_sources=6,
            rerank_enabled=False,
        )

    @staticmethod
    def _result(
        action: RequestUnderstandingAction,
    ) -> RequestUnderstandingResult:
        return RequestUnderstandingResult(
            status="resolved",
            actions=[action],
            is_follow_up=False,
            confidence=1.0,
            clarification_reason=None,
            current_message_delta=CurrentMessageDelta(
                explicit_action_types=[action.type],
                explicit_country_codes=action.country_codes,
                explicit_legal_topics=action.legal_topics,
                explicit_subject_text=action.subject_text,
                context_operation="independent",
            ),
        )

    def _execute(
        self,
        *,
        request: LegalChatRequest,
        result: RequestUnderstandingResult,
        contact_answer: str,
        contact_sources: list[LegalAnswerSource],
        legal_answer_generation_fn=None,
    ) -> LegalChatResponse:
        def unexpected_search(_request):
            raise AssertionError(
                "The router must use the injected legal response."
            )

        def fake_contact_section(
            *,
            country_codes,
            unavailable_country_codes,
            citation_offset,
        ):
            del country_codes
            del unavailable_country_codes
            del citation_offset
            return contact_answer, contact_sources, len(contact_sources), 1.0

        with patch.object(
            chat,
            "_optional_contact_source_directory",
            return_value=self.source_directory,
        ), patch.object(
            chat,
            "_build_contact_section",
            side_effect=fake_contact_section,
        ):
            return chat._execute_resolved_plan(
                request=request,
                result=result,
                hints=DeterministicHints(),
                metrics=self._metrics(request.question),
                catalog_provider=self._catalog,
                search_function=unexpected_search,
                generation_client=None,
                rerank_enabled=False,
                rerank_pool_multiplier=1,
                max_context_characters=1000,
                max_source_characters=500,
                legal_answer_generation_fn=(
                    legal_answer_generation_fn
                    if legal_answer_generation_fn is not None
                    else unexpected_search
                ),
            )

    def test_missing_evidence_comparison_returns_all_structured_contacts(
        self,
    ) -> None:
        question = "compare remote work france uk please"
        legal_answer = (
            "I could not find reliable information about remote work "
            "for France or the United Kingdom."
        )
        contact_answer = (
            "France\nCaroline Scherrmann\nFlorence Bacquet\n\n"
            "United Kingdom\nRobert Hill"
        )
        sources = [
            self._source("FR", "France", 1),
            self._source("GB", "United Kingdom", 2),
        ]
        generation_calls = []

        def missing_evidence_response(request, **kwargs):
            generation_calls.append((request, kwargs))
            return LegalChatResponse(
                question=request.question,
                answer=legal_answer,
                grounded=False,
                model=None,
                retrieval_total=7,
                sources=[],
            )

        response = self._execute(
            request=LegalChatRequest(question=question),
            result=self._result(
                RequestUnderstandingAction(
                    type="comparison",
                    country_codes=["FR", "GB"],
                    topic_text="remote work",
                    resolved_question=question,
                    subject_text="remote work",
                    subject_specificity="specific",
                    evidence_mode="direct_topic",
                )
            ),
            contact_answer=contact_answer,
            contact_sources=sources,
            legal_answer_generation_fn=missing_evidence_response,
        )

        self.assertEqual(1, len(generation_calls))
        self.assertTrue(response.grounded)
        self.assertTrue(response.answer.startswith(legal_answer))
        self.assertIn("L&E Global contacts below", response.answer)
        self.assertNotIn(contact_answer, response.answer)
        self.assertNotIn("Caroline Scherrmann", response.answer)
        self.assertNotIn("Robert Hill", response.answer)
        self.assertEqual(
            [
                ("FR", "Caroline Scherrmann"),
                ("FR", "Florence Bacquet"),
                ("GB", "Robert Hill"),
            ],
            [
                (contact.country_code, contact.contact_person)
                for contact in response.contacts
            ],
        )
        self.assertEqual(sources, response.sources)
        self.assertEqual(9, response.retrieval_total)
        self.assertIsNotNone(response.conversation_state)
        self.assertEqual(
            ["comparison"],
            [
                action.type
                for action in response.conversation_state.actions
            ],
        )

    def test_direct_contact_query_keeps_the_same_structured_card_path(
        self,
    ) -> None:
        question = "contact uk"
        contact_answer = "United Kingdom\nRobert Hill"
        source = self._source("GB", "United Kingdom", 1)

        response = self._execute(
            request=LegalChatRequest(question=question),
            result=self._result(
                RequestUnderstandingAction(
                    type="contact",
                    country_codes=["GB"],
                    resolved_question=question,
                )
            ),
            contact_answer=contact_answer,
            contact_sources=[source],
        )

        self.assertTrue(response.grounded)
        self.assertEqual(contact_answer, response.answer)
        self.assertEqual([source], response.sources)
        self.assertEqual(
            [("GB", "Robert Hill")],
            [
                (contact.country_code, contact.contact_person)
                for contact in response.contacts
            ],
        )
        self.assertIsNotNone(response.conversation_state)
        self.assertEqual(
            ["contact"],
            [
                action.type
                for action in response.conversation_state.actions
            ],
        )


if __name__ == "__main__":
    unittest.main()
