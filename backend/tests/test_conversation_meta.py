from __future__ import annotations

import unittest
from types import SimpleNamespace

from app.models.catalog import (
    LegalCatalogCountry,
    LegalCatalogResponse,
)
from app.models.chat import LegalChatRequest
from app.routers.chat import resolve_legal_chat_response
from app.services.conversation_meta import (
    append_personalised_legal_caution,
    build_comparison_country_limit_answer,
    requires_personalised_legal_caution,
    resolve_conversation_meta,
)


def _catalog() -> LegalCatalogResponse:
    return LegalCatalogResponse(
        countries=[
            LegalCatalogCountry(
                country_code="ES",
                country="Spain",
                chunk_count=50,
            ),
            LegalCatalogCountry(
                country_code="IT",
                country="Italy",
                chunk_count=40,
            ),
            LegalCatalogCountry(
                country_code="GB",
                country="United Kingdom",
                chunk_count=45,
            ),
        ],
        legal_topics=[],
        subsections=[],
    )


def _resolution(
    question: str,
    *,
    history=None,
    conversation_state=None,
):
    return resolve_conversation_meta(
        question=question,
        history=history or [],
        conversation_state=conversation_state,
        catalog_provider=_catalog,
    )


class SmallTalkTests(unittest.TestCase):
    def test_greeting_is_natural(self) -> None:
        result = _resolution("Hello")

        self.assertIsNotNone(result)
        self.assertEqual(result.intent_type, "greeting")
        self.assertIn("Hello", result.answer)
        self.assertNotIn(
            "specify the country",
            result.answer.casefold(),
        )

    def test_wellbeing_is_natural(self) -> None:
        result = _resolution("How are u?")

        self.assertEqual(result.intent_type, "wellbeing")
        self.assertIn(
            "doing well",
            result.answer.casefold(),
        )

    def test_gratitude_is_natural(self) -> None:
        result = _resolution("Thank you")

        self.assertEqual(result.intent_type, "gratitude")
        self.assertIn(
            "welcome",
            result.answer.casefold(),
        )

    def test_smalltalk_does_not_capture_a_legal_question(
        self,
    ) -> None:
        self.assertIsNone(
            _resolution(
                "How are employees paid for overtime in Spain?"
            )
        )


class CapabilityTests(unittest.TestCase):
    def test_explain_role_is_recognised(self) -> None:
        result = _resolution("Explain me your role")

        self.assertEqual(
            result.intent_type,
            "assistant_identity",
        )
        self.assertIn("L&E Global", result.answer)

    def test_can_you_help_me_is_recognised(self) -> None:
        result = _resolution("Can you help me?")

        self.assertEqual(
            result.intent_type,
            "assistant_capabilities",
        )
        self.assertIn(
            "compare",
            result.answer.casefold(),
        )

    def test_what_else_uses_capability_history(self) -> None:
        result = _resolution(
            "What else?",
            history=[
                {
                    "role": "user",
                    "content": "How can u help me?",
                },
                {
                    "role": "assistant",
                    "content": (
                        "I provide employment-law information, "
                        "compare countries and give member-firm "
                        "contact details."
                    ),
                },
            ],
        )

        self.assertEqual(
            result.intent_type,
            "capability_followup",
        )
        self.assertIn(
            "available countries",
            result.answer.casefold(),
        )

    def test_what_else_without_history_is_not_guessed(
        self,
    ) -> None:
        self.assertIsNone(_resolution("What else?"))

    def test_comparison_capability_variant(self) -> None:
        result = _resolution(
            "Can you do comparisons too?"
        )

        self.assertEqual(
            result.intent_type,
            "comparison_capabilities",
        )


class CatalogueTests(unittest.TestCase):
    def test_country_catalogue_is_dynamic(self) -> None:
        result = _resolution(
            "Which countries do you support?"
        )

        self.assertEqual(
            result.intent_type,
            "supported_countries",
        )
        self.assertIn("3 countries", result.answer)
        self.assertIn("Spain", result.answer)
        self.assertNotIn("Australia", result.answer)

    def test_imperfect_country_count_question(self) -> None:
        result = _resolution(
            "How many country do u have?"
        )

        self.assertEqual(
            result.intent_type,
            "supported_countries",
        )
        self.assertIn("3 countries", result.answer)

    def test_imperfect_country_list_question(self) -> None:
        result = _resolution(
            "whitch country u can help for"
        )

        self.assertEqual(
            result.intent_type,
            "supported_countries",
        )

    def test_targeted_unavailable_country(self) -> None:
        result = _resolution(
            "Do you support Tunisia?"
        )

        self.assertEqual(
            result.intent_type,
            "targeted_country_availability",
        )
        self.assertIn("Tunisia", result.answer)
        self.assertIn(
            "do not currently have",
            result.answer,
        )

    def test_mixed_supported_and_unsupported_comparison(
        self,
    ) -> None:
        result = _resolution(
            "Can you compare Italy and Tunisia?"
        )

        self.assertEqual(
            result.intent_type,
            "unsupported_comparison",
        )
        self.assertIn(
            "Italy is currently available",
            result.answer,
        )
        self.assertIn("Tunisia", result.answer)
        self.assertIn(
            "cannot produce a reliable comparison",
            result.answer,
        )

    def test_country_pair_after_compare_history(self) -> None:
        result = _resolution(
            "Italy and Tunisia",
            history=[
                {
                    "role": "user",
                    "content": "Compare",
                },
                {
                    "role": "assistant",
                    "content": (
                        "Which countries would you like to compare?"
                    ),
                },
            ],
        )

        self.assertEqual(
            result.intent_type,
            "unsupported_comparison",
        )

    def test_topic_catalogue(self) -> None:
        result = _resolution(
            "Give me all the topics that you can compare"
        )

        self.assertEqual(
            result.intent_type,
            "supported_legal_topics",
        )
        self.assertIn(
            "Hiring Practices",
            result.answer,
        )
        self.assertIn(
            "Employee Benefits",
            result.answer,
        )

    def test_country_topic_catalogue(self) -> None:
        result = _resolution(
            "Which topics are available for Spain?"
        )

        self.assertEqual(
            result.intent_type,
            "country_legal_topics",
        )
        self.assertIn("For Spain", result.answer)

    def test_contact_catalogue(self) -> None:
        result = _resolution(
            "Show all available contacts."
        )

        self.assertEqual(
            result.intent_type,
            "contact_catalogue",
        )
        self.assertIn(
            "Spain",
            result.answer,
        )


class ConversationRegressionTests(unittest.TestCase):
    def test_topic_catalogue_preserves_existing_state(
        self,
    ) -> None:
        state = SimpleNamespace(
            pending_clarification=None,
        )

        result = _resolution(
            "What topics can you compare?",
            conversation_state=state,
        )

        self.assertIsNotNone(result)
        self.assertEqual(
            result.intent_type,
            "supported_legal_topics",
        )
        self.assertTrue(
            result.preserve_conversation_state
        )

    def test_legal_comparison_with_unavailable_country(
        self,
    ) -> None:
        result = _resolution(
            "Compare overtime rules in Spain and France."
        )

        self.assertIsNone(result)

    def test_supported_country_answer_starts_with_yes(
        self,
    ) -> None:
        result = _resolution(
            "Do you support Spain?"
        )

        self.assertIsNotNone(result)
        self.assertIn("Yes", result.answer)

    def test_unsupported_country_uses_product_wording(
        self,
    ) -> None:
        result = _resolution(
            "Do you support France?"
        )

        self.assertIsNotNone(result)
        self.assertIn(
            "do not currently have",
            result.answer,
        )



class CorrectionAndGuardTests(unittest.TestCase):
    def test_country_typo_suggests_spain(self) -> None:
        result = _resolution("sapin")

        self.assertEqual(
            result.intent_type,
            "country_suggestion",
        )
        self.assertIn(
            "Did you mean Spain",
            result.answer,
        )

    def test_reset_clears_context(self) -> None:
        result = _resolution(
            "Forget my previous question."
        )

        self.assertEqual(result.intent_type, "reset")
        self.assertFalse(
            result.preserve_conversation_state
        )

    def test_personalised_request_needs_caution(
        self,
    ) -> None:
        self.assertTrue(
            requires_personalised_legal_caution(
                "Should I fire this employee in Spain?"
            )
        )

        answer = append_personalised_legal_caution(
            "General dismissal information."
        )

        self.assertIn(
            "not advice for a specific case",
            answer,
        )

    def test_comparison_limit_is_user_friendly(
        self,
    ) -> None:
        action = SimpleNamespace(
            type="comparison",
            country_codes=[
                "ES",
                "IT",
                "GB",
            ],
        )

        answer = build_comparison_country_limit_answer(
            [action],
            2,
        )

        self.assertIsNotNone(answer)
        self.assertIn(
            "up to 2 countries",
            answer,
        )
        self.assertNotIn(
            "max_sources",
            answer,
        )


class RouterIntegrationTests(unittest.TestCase):
    def test_smalltalk_short_circuits_before_catalogue(
        self,
    ) -> None:
        def forbidden_catalog():
            raise AssertionError(
                "The catalogue must not be called."
            )

        response = resolve_legal_chat_response(
            LegalChatRequest(
                question="Hello",
                language="en",
            ),
            catalog_provider=forbidden_catalog,
        )

        self.assertEqual(response.grounded, False)
        self.assertEqual(response.retrieval_total, 0)
        self.assertEqual(response.sources, [])
        self.assertIn("Hello", response.answer)

    def test_country_catalogue_uses_active_catalogue(
        self,
    ) -> None:
        response = resolve_legal_chat_response(
            LegalChatRequest(
                question="Which countries do you support?",
                language="en",
            ),
            catalog_provider=_catalog,
        )

        self.assertEqual(response.grounded, False)
        self.assertIn("3 countries", response.answer)
        self.assertIn("Italy", response.answer)


if __name__ == "__main__":
    unittest.main()
