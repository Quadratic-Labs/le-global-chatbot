"""
Tests for LIVE document legal topics (mission "ORDER 8F-A").

Root cause fixed here: RequestUnderstanding's legal_topics extraction was
constrained to the fixed CANONICAL_LEGAL_TOPICS taxonomy, so a question
about an Admin-created custom section either got mapped to the nearest
canonical topic (an incorrectly-narrow hard filter) or - if no canonical
mapping applied - happened to work only by accident (no topic filter at
all). document_legal_topics is a third, distinct concept: the actual,
currently-indexed legal_topic vocabulary for a country (canonical or
custom alike), fed to RequestUnderstanding as context and prioritized
over a canonical guess when building the real retrieval filter (see
_execute_resolved_plan's action_specs construction) - while still
falling back safely to the fixed taxonomy when the LLM call fails
entirely (see _resolve_conservative_fallback).

These six scenarios are mission section 13's own required coverage:
canonical topics are unaffected; an explicit dynamic/document topic is
used as the exact filter; a custom topic whose semantics overlap a
canonical trigger phrase is never collapsed back to that canonical
topic; an LLM failure still resolves via the deterministic exact-title
match; a hallucinated title that is not actually indexed is never used
as a filter; and one country's custom title is never accepted as a
filter for a different country's action.
"""

from __future__ import annotations

import json
import unittest
from typing import Any

from app.clients.openai_responses import GeneratedText, OpenAIResponseError
from app.core.country_registry import COUNTRIES
from app.models.catalog import LegalCatalogCountry, LegalCatalogResponse
from app.models.chat import LegalChatRequest
from app.models.search import LegalSearchHit, LegalSearchResponse
from app.routers.chat import (
    CLARIFICATION_AMBIGUOUS_REQUEST_ANSWER,
    resolve_legal_chat_response,
)


def _catalog_provider() -> LegalCatalogResponse:
    return LegalCatalogResponse(
        countries=[
            LegalCatalogCountry(
                country_code=country.code,
                country=country.display_name,
                chunk_count=42,
            )
            for country in COUNTRIES
        ],
        legal_topics=[],
        subsections=[],
    )


def _document_topic_provider_for(
    topics_by_country: dict[str, list[str]],
):
    """Fake DocumentLegalTopicsProvider returning a fixed, pre-seeded
    live vocabulary per country - never a real OpenSearch call."""

    def provider(country_codes: list[str]) -> dict[str, list[str]]:
        return {
            code: list(topics_by_country[code])
            for code in country_codes
            if code in topics_by_country
        }

    return provider


def _build_legal_hit(
    *,
    country_code: str,
    country: str,
    legal_topic: str,
    content: str = "Legal content.",
) -> LegalSearchHit:
    return LegalSearchHit(
        score=10.0,
        document_id=f"document-{country_code.lower()}",
        chunk_id=f"chunk-{country_code.lower()}",
        country=country,
        country_code=country_code,
        legal_topic=legal_topic,
        document_type="comparator",
        language="en",
        section=legal_topic,
        subsection=None,
        content=content,
        source_filename=(
            f"Labour and Employment Law in {country} 2026.docx"
        ),
        source_format="docx",
        reference_year=2026,
    )


class FakeGenerationClient:
    model = "test-model"

    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.call_count = 0

    def generate(
        self,
        instructions: str,
        input_text: str,
    ) -> GeneratedText:
        self.call_count += 1

        return GeneratedText(text=self.answer, model=self.model)


class _FailingUnderstandingClient:
    """Forces the router's conservative deterministic fallback by
    making the one semantic-understanding call fail outright with a
    non-retryable error."""

    def generate(
        self,
        instructions: str,
        input_text: str,
        text_format: dict[str, Any] | None = None,
    ) -> GeneratedText:
        raise OpenAIResponseError("boom", retryable=False)


def _understanding_action(
    action_type: str,
    *,
    country_codes: list[str] | None = None,
    legal_topics: list[str] | None = None,
    document_legal_topics: list[str] | None = None,
    topic_text: str | None = None,
    resolved_question: str | None = None,
) -> dict[str, Any]:
    return {
        "type": action_type,
        "country_codes": country_codes or [],
        "legal_topics": legal_topics or [],
        "document_legal_topics": document_legal_topics or [],
        "topic_text": topic_text,
        "resolved_question": resolved_question,
    }


def _current_message_delta() -> dict[str, Any]:
    return {
        "explicit_action_types": [],
        "explicit_country_codes": [],
        "explicit_legal_topics": [],
        "explicit_subject_text": None,
        "context_operation": "independent",
    }


def _understanding_result(
    *,
    actions: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "status": "resolved",
        "actions": actions,
        "is_follow_up": False,
        "confidence": 0.9,
        "clarification_reason": None,
        "current_message_delta": _current_message_delta(),
    }


class FakeUnderstandingClient:
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


class CanonicalTopicUnaffectedTests(unittest.TestCase):
    """Scenario 1: an ordinary canonical-topic action, with no document
    topics involved at all, must behave exactly as before this
    mission."""

    def test_canonical_topic_reaches_search_unchanged(self) -> None:
        captured_requests: list[Any] = []

        def fake_search(request: Any) -> LegalSearchResponse:
            captured_requests.append(request)

            return LegalSearchResponse(
                query=request.query,
                total=1,
                limit=request.limit,
                offset=0,
                took_ms=1,
                hits=[
                    _build_legal_hit(
                        country_code="AU",
                        country="Australia",
                        legal_topic="Hiring Practices",
                    )
                ],
            )

        understanding_client = FakeUnderstandingClient(
            payload=_understanding_result(
                actions=[
                    _understanding_action(
                        "legal_information",
                        country_codes=["AU"],
                        legal_topics=["Hiring Practices"],
                    )
                ],
            )
        )

        resolve_legal_chat_response(
            request=LegalChatRequest(
                question="What are the hiring rules in Australia?"
            ),
            catalog_provider=_catalog_provider,
            document_topic_provider=_document_topic_provider_for(
                {"AU": ["Hiring Practices"]}
            ),
            search_function=fake_search,
            generation_client=FakeGenerationClient(
                answer="Australia\n- Legal content. [1]"
            ),
            understanding_client=understanding_client,
        )

        self.assertEqual(len(captured_requests), 1)
        self.assertEqual(
            captured_requests[0].legal_topics, ["Hiring Practices"]
        )


class ExplicitDocumentTopicTests(unittest.TestCase):
    """Scenario 2: an explicit, live, non-canonical document topic must
    become the exact retrieval filter - never the nearest canonical
    guess, never dropped."""

    def test_custom_section_title_becomes_exact_filter(self) -> None:
        captured_requests: list[Any] = []

        def fake_search(request: Any) -> LegalSearchResponse:
            captured_requests.append(request)

            return LegalSearchResponse(
                query=request.query,
                total=1,
                limit=request.limit,
                offset=0,
                took_ms=1,
                hits=[
                    _build_legal_hit(
                        country_code="AU",
                        country="Australia",
                        legal_topic="V060 Temporary Validation Section",
                    )
                ],
            )

        understanding_client = FakeUnderstandingClient(
            payload=_understanding_result(
                actions=[
                    _understanding_action(
                        "legal_information",
                        country_codes=["AU"],
                        document_legal_topics=[
                            "V060 Temporary Validation Section"
                        ],
                    )
                ],
            )
        )

        response = resolve_legal_chat_response(
            request=LegalChatRequest(
                question=(
                    "Tell me about the V060 Temporary Validation "
                    "Section for Australia."
                )
            ),
            catalog_provider=_catalog_provider,
            document_topic_provider=_document_topic_provider_for(
                {
                    "AU": [
                        "Hiring Practices",
                        "V060 Temporary Validation Section",
                    ]
                }
            ),
            search_function=fake_search,
            generation_client=FakeGenerationClient(
                answer=(
                    "Australia\n- Legal content. [1]"
                )
            ),
            understanding_client=understanding_client,
        )

        self.assertEqual(len(captured_requests), 1)
        self.assertEqual(
            captured_requests[0].legal_topics,
            ["V060 Temporary Validation Section"],
        )
        self.assertTrue(response.grounded)


class OverlappingSemanticsNotCollapsedTests(unittest.TestCase):
    """Scenario 3: a custom topic whose semantics overlap a canonical
    trigger phrase (so the model also reports the nearest canonical
    guess) must still win over that canonical guess - the real bug
    reproduced against production: "Foreign Employee Work Eligibility
    Checks" must never be silently narrowed to "Hiring Practices"."""

    def test_document_topic_wins_over_canonical_guess(self) -> None:
        captured_requests: list[Any] = []

        def fake_search(request: Any) -> LegalSearchResponse:
            captured_requests.append(request)

            return LegalSearchResponse(
                query=request.query,
                total=1,
                limit=request.limit,
                offset=0,
                took_ms=1,
                hits=[
                    _build_legal_hit(
                        country_code="AU",
                        country="Australia",
                        legal_topic=(
                            "Foreign Employee Work Eligibility Checks"
                        ),
                    )
                ],
            )

        understanding_client = FakeUnderstandingClient(
            payload=_understanding_result(
                actions=[
                    _understanding_action(
                        "legal_information",
                        country_codes=["AU"],
                        legal_topics=["Hiring Practices"],
                        document_legal_topics=[
                            "Foreign Employee Work Eligibility Checks"
                        ],
                    )
                ],
            )
        )

        response = resolve_legal_chat_response(
            request=LegalChatRequest(
                question=(
                    "What are the foreign employee work eligibility "
                    "checks required in Australia?"
                )
            ),
            catalog_provider=_catalog_provider,
            document_topic_provider=_document_topic_provider_for(
                {
                    "AU": [
                        "Hiring Practices",
                        "Foreign Employee Work Eligibility Checks",
                    ]
                }
            ),
            search_function=fake_search,
            generation_client=FakeGenerationClient(
                answer="Australia\n- Legal content. [1]"
            ),
            understanding_client=understanding_client,
        )

        self.assertEqual(len(captured_requests), 1)
        self.assertEqual(
            captured_requests[0].legal_topics,
            ["Foreign Employee Work Eligibility Checks"],
        )
        self.assertNotIn(
            "Hiring Practices", captured_requests[0].legal_topics
        )
        self.assertTrue(response.grounded)


class FallbackReliabilityTests(unittest.TestCase):
    """Scenario 4: when the understanding call fails entirely, an
    exact, single-country document-topic title match must still
    resolve deterministically - never degrading to the generic
    "please specify country and topic" clarification."""

    def test_llm_failure_still_resolves_exact_document_title(
        self,
    ) -> None:
        captured_requests: list[Any] = []

        def fake_search(request: Any) -> LegalSearchResponse:
            captured_requests.append(request)

            return LegalSearchResponse(
                query=request.query,
                total=1,
                limit=request.limit,
                offset=0,
                took_ms=1,
                hits=[
                    _build_legal_hit(
                        country_code="AU",
                        country="Australia",
                        legal_topic="V060 Temporary Validation Section",
                    )
                ],
            )

        response = resolve_legal_chat_response(
            request=LegalChatRequest(
                question=(
                    "Tell me about the V060 Temporary Validation "
                    "Section for Australia."
                )
            ),
            catalog_provider=_catalog_provider,
            document_topic_provider=_document_topic_provider_for(
                {
                    "AU": [
                        "Hiring Practices",
                        "V060 Temporary Validation Section",
                    ]
                }
            ),
            search_function=fake_search,
            generation_client=FakeGenerationClient(
                answer="Australia\n- Legal content. [1]"
            ),
            understanding_client=_FailingUnderstandingClient(),
        )

        self.assertNotEqual(
            response.answer, CLARIFICATION_AMBIGUOUS_REQUEST_ANSWER
        )
        self.assertTrue(response.grounded)
        self.assertEqual(len(captured_requests), 1)
        self.assertEqual(
            captured_requests[0].legal_topics,
            ["V060 Temporary Validation Section"],
        )


class UnknownTitleInventsNothingTests(unittest.TestCase):
    """Scenario 5: a document_legal_topics value the model reports
    that is not actually part of the live vocabulary (hallucinated,
    stale, or mis-cased) must never reach the retrieval filter - it is
    validated against the real live vocabulary and dropped, falling
    back to canonical/topic_text behavior instead of being trusted
    blindly."""

    def test_hallucinated_title_is_never_used_as_filter(self) -> None:
        captured_requests: list[Any] = []

        def fake_search(request: Any) -> LegalSearchResponse:
            captured_requests.append(request)

            return LegalSearchResponse(
                query=request.query,
                total=1,
                limit=request.limit,
                offset=0,
                took_ms=1,
                hits=[
                    _build_legal_hit(
                        country_code="AU",
                        country="Australia",
                        legal_topic="Hiring Practices",
                    )
                ],
            )

        understanding_client = FakeUnderstandingClient(
            payload=_understanding_result(
                actions=[
                    _understanding_action(
                        "legal_information",
                        country_codes=["AU"],
                        document_legal_topics=[
                            "Some Hallucinated Title Not Really Indexed"
                        ],
                        topic_text="hiring practices",
                    )
                ],
            )
        )

        resolve_legal_chat_response(
            request=LegalChatRequest(
                question="What are the hiring rules in Australia?"
            ),
            catalog_provider=_catalog_provider,
            document_topic_provider=_document_topic_provider_for(
                {"AU": ["Hiring Practices"]}
            ),
            search_function=fake_search,
            generation_client=FakeGenerationClient(
                answer="Australia\n- Legal content. [1]"
            ),
            understanding_client=understanding_client,
        )

        self.assertEqual(len(captured_requests), 1)
        self.assertNotIn(
            "Some Hallucinated Title Not Really Indexed",
            captured_requests[0].legal_topics,
        )
        self.assertEqual(captured_requests[0].legal_topics, [])


class CrossCountryTitleInvalidTests(unittest.TestCase):
    """Scenario 6: one country's own custom section title must never
    be accepted as a retrieval filter for a different country's
    action - validated per-action against that action's own resolved
    country codes only, never a global title vocabulary."""

    def test_australia_title_rejected_for_belgium_action(self) -> None:
        captured_requests: list[Any] = []

        def fake_search(request: Any) -> LegalSearchResponse:
            captured_requests.append(request)

            return LegalSearchResponse(
                query=request.query,
                total=1,
                limit=request.limit,
                offset=0,
                took_ms=1,
                hits=[
                    _build_legal_hit(
                        country_code="BE",
                        country="Belgium",
                        legal_topic="Hiring Practices",
                    )
                ],
            )

        understanding_client = FakeUnderstandingClient(
            payload=_understanding_result(
                actions=[
                    _understanding_action(
                        "legal_information",
                        country_codes=["BE"],
                        document_legal_topics=[
                            "V060 Temporary Validation Section"
                        ],
                        topic_text="hiring practices",
                    )
                ],
            )
        )

        resolve_legal_chat_response(
            request=LegalChatRequest(
                question=(
                    "What are the hiring rules in Belgium under the "
                    "V060 Temporary Validation Section?"
                )
            ),
            catalog_provider=_catalog_provider,
            document_topic_provider=_document_topic_provider_for(
                {
                    "AU": ["V060 Temporary Validation Section"],
                    "BE": ["Hiring Practices"],
                }
            ),
            search_function=fake_search,
            generation_client=FakeGenerationClient(
                answer="Belgium\n- Legal content. [1]"
            ),
            understanding_client=understanding_client,
        )

        self.assertEqual(len(captured_requests), 1)
        self.assertNotIn(
            "V060 Temporary Validation Section",
            captured_requests[0].legal_topics,
        )
        self.assertEqual(captured_requests[0].legal_topics, [])


if __name__ == "__main__":
    unittest.main()
