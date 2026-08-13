from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from typing import Any
from unittest import mock

from app.clients.openai_responses import GeneratedText
from app.models.catalog import (
    LegalCatalogCountry,
    LegalCatalogResponse,
)
from app.models.chat import LegalChatRequest
from app.models.search import LegalSearchResponse
from app.routers.chat import resolve_legal_chat_response
from app.services.conversation_meta import (
    append_personalised_legal_caution,
    build_comparison_country_limit_answer,
    requires_personalised_legal_caution,
    resolve_ambiguous_city_followup_question,
    resolve_conversation_meta,
)


class _FakeUnderstandingClient:
    """Minimal test double for the semantic-understanding client -
    only what AmbiguousCityFollowupResumeTests' own end-to-end test
    needs (see test_chat.py's own, fuller FakeUnderstandingClient for
    the canonical version)."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.call_count = 0

    def generate(
        self,
        instructions: str,
        input_text: str,
        text_format: dict[str, Any] | None = None,
    ) -> GeneratedText:
        self.call_count += 1

        return GeneratedText(
            text=json.dumps(self.payload), model="test-model"
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

    def test_extended_gratitude_is_natural(self) -> None:
        for question in (
            "Thank you, that was helpful.",
            "Thank you im fine",
        ):
            with self.subTest(question=question):
                result = _resolution(question)

                self.assertEqual(result.intent_type, "gratitude")
                self.assertIn("welcome", result.answer.casefold())

    def test_acknowledgements_are_natural(self) -> None:
        for question in ("aaa ok", "no problem"):
            with self.subTest(question=question):
                result = _resolution(question)

                self.assertEqual(
                    result.intent_type,
                    "acknowledgement",
                )
                self.assertNotIn(
                    "clarify",
                    result.answer.casefold(),
                )

    def test_gratitude_and_farewell_is_natural(self) -> None:
        result = _resolution("Thanks, that's all for now.")

        self.assertEqual(result.intent_type, "farewell")
        self.assertIn("nice day", result.answer.casefold())

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

    def test_what_else_can_you_help_me_with_is_recognised(
        self,
    ) -> None:
        result = _resolution(
            "What else can you help me with?"
        )

        self.assertEqual(
            result.intent_type,
            "assistant_capabilities",
        )
        self.assertIn("compare", result.answer.casefold())

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

    def test_concise_topic_catalogue_question(self) -> None:
        result = _resolution("whitch topics")

        self.assertEqual(
            result.intent_type,
            "supported_legal_topics",
        )

    def test_concise_country_catalogue_followup(self) -> None:
        result = _resolution(
            "And countries?",
            history=[
                {
                    "role": "assistant",
                    "content": (
                        "You can ask about the following canonical "
                        "employment-law topics."
                    ),
                }
            ],
        )

        self.assertEqual(
            result.intent_type,
            "supported_countries",
        )
        self.assertIn("3 countries", result.answer)

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
        self.assertIn(
            "Tunisia is a valid country",
            result.answer,
        )
        self.assertIn(
            "Would you like to see the countries currently covered?",
            result.answer,
        )

    def test_targeted_availability_matches_country_between_verb_and_adjective(
        self,
    ) -> None:
        # Real regression found by adversarial review: a plain
        # substring check for "is supported"/"is available" missed
        # the natural word order "is COUNTRY supported/available"
        # (the country name sits between the verb and the adjective),
        # falling through to assistant_help.py's older, registry-only
        # answer instead of this module's catalogue-based one.
        for question in (
            "Is Tunisia supported?",
            "Is Tunisia available?",
            "Are Tunisia and Algeria supported?",
        ):
            with self.subTest(question=question):
                result = _resolution(question)

                self.assertEqual(
                    result.intent_type,
                    "targeted_country_availability",
                )
                self.assertIn(
                    "do not currently have",
                    result.answer,
                )

        available = _resolution("Is Spain supported?")

        self.assertEqual(
            available.intent_type,
            "targeted_country_availability",
        )
        self.assertIn(
            "Spain is currently available",
            available.answer,
        )

    def test_yes_after_unavailable_country_shows_dynamic_coverage_list(
        self,
    ) -> None:
        # Mission "ORDER 5C-GEO", sections 3/18: the offer above must
        # be followable by a bare "Yes", answered from the real
        # catalogue - never a hardcoded count.
        offer = _resolution("Do you support Algeria?")

        result = _resolution(
            "Yes",
            history=[
                {"role": "user", "content": "Do you support Algeria?"},
                {"role": "assistant", "content": offer.answer},
            ],
        )

        self.assertEqual(
            result.intent_type,
            "coverage_list_followup",
        )
        self.assertIn("3 countries", result.answer)
        self.assertIn("Spain", result.answer)
        self.assertIn("Italy", result.answer)
        self.assertIn("United Kingdom", result.answer)

    def test_coverage_list_followup_state_is_not_stuck(
        self,
    ) -> None:
        # A later, unrelated "Yes" (the offer is no longer the last
        # assistant turn) must never be misread as asking for the
        # coverage list again.
        offer = _resolution("Do you support Algeria?")
        shown = _resolution(
            "Yes",
            history=[
                {"role": "user", "content": "Do you support Algeria?"},
                {"role": "assistant", "content": offer.answer},
            ],
        )

        result = _resolution(
            "Yes",
            history=[
                {"role": "user", "content": "Do you support Algeria?"},
                {"role": "assistant", "content": offer.answer},
                {"role": "user", "content": "Yes"},
                {"role": "assistant", "content": shown.answer},
                {"role": "user", "content": "Do you have topics too?"},
                {
                    "role": "assistant",
                    "content": (
                        "You can ask about the following canonical "
                        "employment-law topics."
                    ),
                },
            ],
        )

        self.assertNotEqual(
            result.intent_type if result else None,
            "coverage_list_followup",
        )

    def test_coverage_list_reflects_catalog_changes(self) -> None:
        # Mission section 3's own worked example - remove a country
        # from the catalog and the very same flow must name fewer
        # countries, with zero hardcoded count anywhere in the code
        # path.
        def catalog_without_spain() -> LegalCatalogResponse:
            return LegalCatalogResponse(
                countries=[
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

        offer = resolve_conversation_meta(
            question="Do you support Algeria?",
            history=[],
            conversation_state=None,
            catalog_provider=catalog_without_spain,
        )

        result = resolve_conversation_meta(
            question="Yes",
            history=[
                {"role": "user", "content": "Do you support Algeria?"},
                {"role": "assistant", "content": offer.answer},
            ],
            conversation_state=None,
            catalog_provider=catalog_without_spain,
        )

        self.assertIn("2 countries", result.answer)
        self.assertNotIn("Spain", result.answer)

    def test_embedded_country_typo_is_suggested(self) -> None:
        result = _resolution(
            "Do you have data for Egypte?",
            history=[
                {"role": "user", "content": "What about France?"},
                {
                    "role": "assistant",
                    "content": "France is not currently available.",
                },
                {"role": "user", "content": "What about Tunisia?"},
            ],
            conversation_state=SimpleNamespace(
                pending_clarification=None,
            ),
        )

        self.assertEqual(
            result.intent_type,
            "country_suggestion",
        )
        self.assertIn("Did you mean Egypt", result.answer)
        self.assertNotIn("France", result.answer)
        self.assertNotIn("Tunisia", result.answer)
        self.assertFalse(result.preserve_conversation_state)

    def test_country_correction_inside_natural_phrase(self) -> None:
        result = _resolution("No, I ask about tunsie")

        self.assertEqual(
            result.intent_type,
            "country_suggestion",
        )
        self.assertIn("Did you mean Tunisia", result.answer)

    def test_yes_confirms_the_latest_country_suggestion(self) -> None:
        result = _resolution(
            "yes",
            history=[
                {
                    "role": "assistant",
                    "content": (
                        "Did you mean Tunisia? Tunisia is not currently "
                        "available in the validated corpus."
                    ),
                }
            ],
        )

        self.assertEqual(
            result.intent_type,
            "targeted_country_availability",
        )
        self.assertIn("Tunisia is a valid country", result.answer)
        self.assertFalse(result.preserve_conversation_state)

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


class AmbiguousCityClarificationTests(unittest.TestCase):
    """
    Corrective gate, section 9 - a genuinely ambiguous city name (real
    geonamescache data: Barcelona, Spain / Barcelona, Venezuela at a
    ~2x population ratio) asks a specific clarifying question rather
    than falling through to the generic "specify a country" prompt,
    and never hijacks a real multi-country comparison (which is also
    AMBIGUOUS from resolve_jurisdiction's own point of view, but for
    an entirely different, explicit-countries reason).
    """

    def test_ambiguous_city_alone_asks_which_country(self) -> None:
        result = _resolution(
            "social media rules at work in Barcelona"
        )

        self.assertEqual(
            result.intent_type, "ambiguous_city_clarification"
        )
        self.assertIn("Barcelona", result.answer)
        self.assertIn("Spain", result.answer)
        self.assertIn("Venezuela", result.answer)

    def test_explicit_country_is_never_hijacked_as_ambiguous(
        self,
    ) -> None:
        result = _resolution("Barcelona, Spain")

        self.assertIsNone(result)

    def test_two_country_comparison_is_never_hijacked(self) -> None:
        # AMBIGUOUS for resolve_jurisdiction too, but matched_location
        # is unset for this case - must fall through to normal
        # comparison routing untouched (both countries are in the
        # shared test catalogue, so this exercises exactly that path,
        # not the separate, pre-existing "unsupported_comparison"
        # branch for a country the catalogue does not have).
        result = _resolution("Compare Spain and Italy")

        self.assertIsNone(result)


class NoRedundantCountryDetectionCallTests(unittest.TestCase):
    """
    Adversarial-review finding, corrective gate: the ambiguous-city/
    unknown-locality check used to call the full resolve_jurisdiction,
    which re-derives explicit countries from scratch even though
    resolve_conversation_meta's own country_codes (already confirmed
    empty at that point) came from the exact same ~400-precompiled-
    regex scan moments earlier - a real, measured ~2x-per-call
    overhead. Going straight to the city-only primitives instead must
    call the explicit-country scan exactly once per request.
    """

    def test_detect_mentioned_country_codes_called_once(self) -> None:
        import app.services.conversation_meta as conversation_meta_module
        import app.services.country_detection as country_detection_module

        call_count = 0
        original = (
            country_detection_module.detect_mentioned_country_codes
        )

        def counting(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return original(*args, **kwargs)

        with mock.patch.object(
            country_detection_module,
            "detect_mentioned_country_codes",
            side_effect=counting,
        ), mock.patch.object(
            conversation_meta_module,
            "detect_mentioned_country_codes",
            side_effect=counting,
        ):
            resolve_conversation_meta(
                question=(
                    "What are the general employment law "
                    "considerations for probation, notice, and "
                    "severance, without naming any specific place?"
                ),
                history=[],
                conversation_state=None,
                catalog_provider=_catalog,
            )

        self.assertEqual(call_count, 1)


class AmbiguousCityFollowupResumeTests(unittest.TestCase):
    """
    Corrective gate, section 9 - "User: Spain" after the clarification
    above resumes the ORIGINAL question with Spain substituted for the
    ambiguous city, entirely as plain text rewriting (this module has
    no RAG access of its own).
    """

    def test_naming_an_offered_country_rebuilds_the_question(
        self,
    ) -> None:
        offer = _resolution(
            "social media rules at work in Barcelona"
        )

        resumed = resolve_ambiguous_city_followup_question(
            question="Spain",
            history=[
                {
                    "role": "user",
                    "content": (
                        "social media rules at work in Barcelona"
                    ),
                },
                {"role": "assistant", "content": offer.answer},
            ],
        )

        self.assertIsNotNone(resumed)
        self.assertIn("Spain", resumed)
        self.assertNotIn("Barcelona", resumed)
        self.assertIn("social media rules at work", resumed)

    def test_naming_an_unoffered_country_does_not_resume(
        self,
    ) -> None:
        offer = _resolution(
            "social media rules at work in Barcelona"
        )

        resumed = resolve_ambiguous_city_followup_question(
            question="Italy",
            history=[
                {
                    "role": "user",
                    "content": (
                        "social media rules at work in Barcelona"
                    ),
                },
                {"role": "assistant", "content": offer.answer},
            ],
        )

        self.assertIsNone(resumed)

    def test_unrelated_later_turn_never_resumes(self) -> None:
        # The offer is no longer the last assistant turn - this must
        # never fire for an unrelated later "Spain" reply.
        offer = _resolution(
            "social media rules at work in Barcelona"
        )

        resumed = resolve_ambiguous_city_followup_question(
            question="Spain",
            history=[
                {
                    "role": "user",
                    "content": (
                        "social media rules at work in Barcelona"
                    ),
                },
                {"role": "assistant", "content": offer.answer},
                {"role": "user", "content": "Thanks, one more thing"},
                {
                    "role": "assistant",
                    "content": "Sure, go ahead.",
                },
            ],
        )

        self.assertIsNone(resumed)

    def test_no_prior_offer_never_resumes(self) -> None:
        resumed = resolve_ambiguous_city_followup_question(
            question="Spain",
            history=[],
        )

        self.assertIsNone(resumed)

    def test_end_to_end_through_the_router_resolves_spain(
        self,
    ) -> None:
        # Full round trip: the ambiguous question, then the
        # clarification's own answer, then "Spain" as the new
        # question - resolve_legal_chat_response rewrites request.
        # question BEFORE routing (chat.py), so the rewritten text is
        # what every downstream step, including this response's own
        # echoed `question` field, actually sees - proven here with a
        # deterministic contact action (never needs a real OpenAI
        # understanding/generation call).
        offer_response = resolve_legal_chat_response(
            LegalChatRequest(
                question="Who can help me in Barcelona?"
            ),
            catalog_provider=_catalog,
        )

        self.assertIn("Barcelona", offer_response.answer)

        understanding_client = _FakeUnderstandingClient(
            payload={
                "status": "resolved",
                "actions": [
                    {
                        "type": "contact",
                        "country_codes": ["ES"],
                        "legal_topics": [],
                        "topic_text": None,
                        "resolved_question": None,
                    }
                ],
                "is_follow_up": False,
                "confidence": 0.9,
                "clarification_reason": None,
                "current_message_delta": {
                    "explicit_action_types": ["contact"],
                    "explicit_country_codes": ["ES"],
                    "explicit_legal_topics": [],
                    "explicit_subject_text": None,
                    "context_operation": "independent",
                },
            }
        )

        with mock.patch(
            "app.routers.chat.search_contact_chunks",
            return_value=LegalSearchResponse(
                query="",
                total=0,
                limit=20,
                offset=0,
                took_ms=1,
                hits=[],
            ),
        ):
            resumed_response = resolve_legal_chat_response(
                LegalChatRequest(
                    question="Spain",
                    history=[
                        {
                            "role": "user",
                            "content": "Who can help me in Barcelona?",
                        },
                        {
                            "role": "assistant",
                            "content": offer_response.answer,
                        },
                    ],
                ),
                catalog_provider=_catalog,
                understanding_client=understanding_client,
            )

        self.assertIn("Spain", resumed_response.question)
        self.assertNotIn("Barcelona", resumed_response.question)
        self.assertEqual(understanding_client.call_count, 1)


class UnknownLocalityClarificationTests(unittest.TestCase):
    """
    Corrective gate, section 11 - a question that clearly names a
    place the dataset does not recognize must ask which country it is
    in, never fabricate a country, and never call the internet or an
    LLM to resolve it.
    """

    def test_unrecognized_place_asks_which_country(self) -> None:
        result = _resolution("employment law in Ruritania")

        self.assertEqual(
            result.intent_type, "unknown_locality_clarification"
        )
        self.assertIn("Ruritania", result.answer)
        self.assertIn("Which country", result.answer)

    def test_no_location_at_all_is_not_hijacked(self) -> None:
        result = _resolution("What are the rules on termination?")

        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
