from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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
from app.services.chat_metrics import LegalChatMetrics
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
