from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from app.models.catalog import (
    LegalCatalogCountry,
    LegalCatalogResponse,
)
from app.models.chat import LegalChatRequest
from app.models.conversation_state import (
    ConversationActionState,
    ConversationSearchConcept,
    ConversationState,
)
from app.routers.chat import resolve_legal_chat_response
from app.services.conversation_transition import (
    apply_conversation_transition,
    resolve_contextual_multi_country_contact_codes,
)
from app.services.request_understanding import (
    CurrentMessageDelta,
    DeterministicHints,
    RequestUnderstandingResult,
)


def _comparison_state(
    country_codes: list[str],
) -> ConversationState:
    return ConversationState(
        version=1,
        actions=[
            ConversationActionState(
                type="comparison",
                country_codes=country_codes,
                legal_topics=["Anti-Discrimination Laws"],
                subject_text="anti-discrimination laws",
                search_concepts=[
                    ConversationSearchConcept(
                        terms=["anti-discrimination"]
                    )
                ],
                subject_specificity="specific",
                evidence_mode="direct_topic",
            )
        ],
        focus_action_index=0,
        ordered_country_codes=country_codes,
        pending_clarification=None,
    )


def _semantic_clarification() -> RequestUnderstandingResult:
    return RequestUnderstandingResult(
        status="clarification",
        actions=[],
        is_follow_up=True,
        confidence=0.5,
        clarification_reason="ambiguous_request",
        current_message_delta=CurrentMessageDelta(
            explicit_action_types=[],
            explicit_country_codes=[],
            explicit_legal_topics=[],
            explicit_subject_text=None,
            context_operation="ambiguous",
        ),
    )


class ContextualCountryResolverTests(unittest.TestCase):
    def test_exact_real_user_phrase_resolves_both(self) -> None:
        state = _comparison_state(["FR", "JP"])

        self.assertEqual(
            resolve_contextual_multi_country_contact_codes(
                (
                    "Can you give me contact from both "
                    "country to go further?"
                ),
                state,
            ),
            ["FR", "JP"],
        )

    def test_common_both_variants(self) -> None:
        state = _comparison_state(["FR", "JP"])

        for question in (
            "Can I have the contacts for both?",
            "Give me both contacts.",
            "Give me the contacts for these two countries.",
            "Who can I contact in both countries?",
        ):
            with self.subTest(question=question):
                self.assertEqual(
                    resolve_contextual_multi_country_contact_codes(
                        question,
                        state,
                    ),
                    ["FR", "JP"],
                )

    def test_all_three_uses_all_three(self) -> None:
        state = _comparison_state(["FR", "JP", "IT"])

        self.assertEqual(
            resolve_contextual_multi_country_contact_codes(
                "Give me the contacts for all three.",
                state,
            ),
            ["FR", "JP", "IT"],
        )

    def test_both_never_guesses_inside_three_country_state(self) -> None:
        state = _comparison_state(["FR", "JP", "IT"])

        self.assertEqual(
            resolve_contextual_multi_country_contact_codes(
                "Give me the contacts for both countries.",
                state,
            ),
            [],
        )

    def test_no_state_never_fabricates_countries(self) -> None:
        self.assertIsNone(
            resolve_contextual_multi_country_contact_codes(
                "Give me both contacts.",
                None,
            )
        )

    def test_non_contact_followup_is_not_captured(self) -> None:
        state = _comparison_state(["FR", "JP"])

        self.assertIsNone(
            resolve_contextual_multi_country_contact_codes(
                "Are the penalties the same in both countries?",
                state,
            )
        )


class TransitionTests(unittest.TestCase):
    def test_semantic_clarification_is_corrected_to_contact(self) -> None:
        state = _comparison_state(["FR", "JP"])

        outcome = apply_conversation_transition(
            result=_semantic_clarification(),
            conversation_state=state,
            hints=DeterministicHints(),
            current_question=(
                "Can you give me contact from both "
                "country to go further?"
            ),
        )

        self.assertEqual(outcome.final_status, "resolved")
        self.assertEqual(len(outcome.final_actions), 1)
        self.assertEqual(outcome.final_actions[0].type, "contact")
        self.assertEqual(
            outcome.final_actions[0].country_codes,
            ["FR", "JP"],
        )


class RouterIntegrationTests(unittest.TestCase):
    def test_help_does_not_swallow_contextual_contact_request(
        self,
    ) -> None:
        state = _comparison_state(["FR", "JP"])

        def catalog() -> LegalCatalogResponse:
            return LegalCatalogResponse(
                countries=[
                    LegalCatalogCountry(
                        country_code="FR",
                        country="France",
                        chunk_count=10,
                    ),
                    LegalCatalogCountry(
                        country_code="JP",
                        country="Japan",
                        chunk_count=10,
                    ),
                ],
                legal_topics=[],
                subsections=[],
            )

        semantic_outcome = SimpleNamespace(
            result=_semantic_clarification(),
            elapsed_ms=0.0,
            openai_ms=0.0,
            attempts=1,
            retry_triggered=False,
            retry_reason=None,
            error=None,
        )

        captured: list[list[str]] = []

        def fake_contact_section(
            *,
            country_codes,
            unavailable_country_codes,
            citation_offset,
        ):
            captured.append(list(country_codes))

            return (
                (
                    "France\n"
                    "I could not find a validated L&E Global "
                    "contact for France in the available documents."
                    "\n\n"
                    "Japan\n"
                    "Member firm: Atsumi & Sakai"
                ),
                [],
                0,
                0.0,
            )

        with mock.patch(
            "app.routers.chat.understand_request",
            return_value=semantic_outcome,
        ) as semantic_mock, mock.patch(
            "app.routers.chat._build_contact_section",
            side_effect=fake_contact_section,
        ):
            response = resolve_legal_chat_response(
                LegalChatRequest(
                    question=(
                        "Can you give me contact from both "
                        "country to go further?"
                    ),
                    conversation_state=state,
                ),
                catalog_provider=catalog,
                document_topic_provider=(
                    lambda codes: {
                        code: []
                        for code in codes
                    }
                ),
            )

        semantic_mock.assert_called_once()

        self.assertEqual(captured, [["FR", "JP"]])
        self.assertIn("France", response.answer)
        self.assertIn("Japan", response.answer)
        self.assertNotIn(
            "Please specify the country",
            response.answer,
        )

        self.assertIsNotNone(response.conversation_state)
        self.assertEqual(
            response.conversation_state.actions[0].type,
            "contact",
        )
        self.assertEqual(
            response.conversation_state.actions[0].country_codes,
            ["FR", "JP"],
        )


if __name__ == "__main__":
    unittest.main()
