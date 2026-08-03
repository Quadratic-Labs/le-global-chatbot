"""
Tests for comparison routing: indirect phrasings, multi-country
comparisons, canonical-topic vs free-text topic representation,
incomplete comparisons, and the Pydantic safety net that keeps an
invalid single-country "resolved" comparison from ever reaching
OpenSearch even if the model's raw output claims otherwise.
"""

from __future__ import annotations

import json
import unittest
from typing import Any

from app.clients.openai_responses import GeneratedText
from app.core.country_registry import COUNTRIES
from app.models.catalog import LegalCatalogCountry, LegalCatalogResponse
from app.models.chat import LegalChatRequest
from app.models.search import LegalSearchHit, LegalSearchResponse
from app.routers.chat import (
    CLARIFICATION_MISSING_COMPARISON_COUNTRIES_ANSWER,
    CLARIFICATION_MISSING_COMPARISON_TOPIC_ANSWER,
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


def _build_legal_hit(
    *,
    country_code: str,
    country: str,
    content: str = "Legal content.",
) -> LegalSearchHit:
    return LegalSearchHit(
        score=10.0,
        document_id=f"document-{country_code.lower()}",
        chunk_id=f"chunk-{country_code.lower()}",
        country=country,
        country_code=country_code,
        legal_topic="Termination",
        document_type="comparator",
        language="en",
        section="02. Termination",
        subsection="Notice",
        content=content,
        source_filename=(
            f"Labour and Employment Law in {country} 2026.docx"
        ),
        source_format="docx",
        reference_year=2026,
    )


class FakeGenerationClient:
    """Test text-generation client (legal generation)."""

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


def _understanding_action(
    action_type: str,
    *,
    country_codes: list[str] | None = None,
    legal_topics: list[str] | None = None,
    topic_text: str | None = None,
    resolved_question: str | None = None,
) -> dict[str, Any]:
    return {
        "type": action_type,
        "country_codes": country_codes or [],
        "legal_topics": legal_topics or [],
        "topic_text": topic_text,
        "resolved_question": resolved_question,
    }


def _current_message_delta(
    *,
    explicit_action_types: list[str] | None = None,
    explicit_country_codes: list[str] | None = None,
    explicit_legal_topics: list[str] | None = None,
    explicit_subject_text: str | None = None,
    context_operation: str = "independent",
) -> dict[str, Any]:
    """Build one CurrentMessageDelta JSON payload."""

    return {
        "explicit_action_types": explicit_action_types or [],
        "explicit_country_codes": explicit_country_codes or [],
        "explicit_legal_topics": explicit_legal_topics or [],
        "explicit_subject_text": explicit_subject_text,
        "context_operation": context_operation,
    }


def _understanding_result(
    *,
    status: str = "resolved",
    actions: list[dict[str, Any]] | None = None,
    is_follow_up: bool = False,
    confidence: float = 0.9,
    clarification_reason: str | None = None,
    current_message_delta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "status": status,
        "actions": actions or [],
        "is_follow_up": is_follow_up,
        "confidence": confidence,
        "clarification_reason": clarification_reason,
        "current_message_delta": (
            current_message_delta
            if current_message_delta is not None
            else _current_message_delta(
                context_operation=(
                    "continue" if is_follow_up else "independent"
                ),
            )
        ),
    }


class FakeUnderstandingClient:
    """Test double for the semantic-understanding OpenAI client."""

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


def _fail_if_called(*args: Any, **kwargs: Any) -> Any:
    raise AssertionError(
        "This function must not be called for this scenario."
    )


def _fake_multi_country_legal_search(
    countries: list[tuple[str, str]],
    content: str,
):
    countries_by_code = dict(countries)

    def fake_search(request: Any) -> LegalSearchResponse:
        country_code = request.country_codes[0]
        country = countries_by_code[country_code]

        return LegalSearchResponse(
            query=request.query,
            total=1,
            limit=request.limit,
            offset=0,
            took_ms=1,
            hits=[
                _build_legal_hit(
                    country_code=country_code,
                    country=country,
                    content=content,
                )
            ],
        )

    return fake_search


def _build_comparison_answer(
    countries: list[tuple[str, str]],
    content: str,
) -> str:
    """
    Build a per-country-sectioned answer matching what
    answer_legal_question's grounding validation requires for a
    multi-country comparison.
    """

    return "\n".join(
        f"{name}\n- {content} [{position}]."
        for position, (_, name) in enumerate(countries, start=1)
    )


class IndirectComparisonRoutingTests(unittest.TestCase):
    """
    Comparisons phrased without the word "compare" must still be
    recognized as a comparison action once RequestUnderstanding
    resolves them - no deterministic comparison-phrase dictionary is
    involved in the decision itself, only in the informational
    comparison_signal hint.
    """

    def test_which_has_the_longer_notice_period(self) -> None:
        countries = [("AU", "Australia"), ("GB", "United Kingdom")]

        understanding_client = FakeUnderstandingClient(
            payload=_understanding_result(
                actions=[
                    _understanding_action(
                        "comparison",
                        country_codes=["AU", "GB"],
                        topic_text="notice period",
                    )
                ],
            )
        )

        generation_client = FakeGenerationClient(
            answer=_build_comparison_answer(
                countries, "Notice period comparison content."
            )
        )

        response = resolve_legal_chat_response(
            request=LegalChatRequest(
                question=(
                    "Which has the longer notice period, "
                    "Australia or the UK?"
                )
            ),
            catalog_provider=_catalog_provider,
            search_function=_fake_multi_country_legal_search(
                countries, "Notice period comparison content."
            ),
            generation_client=generation_client,
            understanding_client=understanding_client,
        )

        self.assertTrue(response.grounded)

    def test_workers_better_protected_than_phrasing(self) -> None:
        countries = [("ES", "Spain"), ("PE", "Peru")]

        understanding_client = FakeUnderstandingClient(
            payload=_understanding_result(
                actions=[
                    _understanding_action(
                        "comparison",
                        country_codes=["ES", "PE"],
                        topic_text="dismissal protection",
                    )
                ],
            )
        )

        generation_client = FakeGenerationClient(
            answer=_build_comparison_answer(
                countries, "Dismissal protection content."
            )
        )

        response = resolve_legal_chat_response(
            request=LegalChatRequest(
                question=(
                    "Are workers better protected against "
                    "dismissal in Spain than in Peru?"
                )
            ),
            catalog_provider=_catalog_provider,
            search_function=_fake_multi_country_legal_search(
                countries, "Dismissal protection content."
            ),
            generation_client=generation_client,
            understanding_client=understanding_client,
        )

        self.assertTrue(response.grounded)

    def test_between_x_and_y_phrasing(self) -> None:
        countries = [("PE", "Peru"), ("AU", "Australia")]

        understanding_client = FakeUnderstandingClient(
            payload=_understanding_result(
                actions=[
                    _understanding_action(
                        "comparison",
                        country_codes=["PE", "AU"],
                        topic_text="overtime rules",
                    )
                ],
            )
        )

        generation_client = FakeGenerationClient(
            answer=_build_comparison_answer(
                countries, "Overtime comparison content."
            )
        )

        response = resolve_legal_chat_response(
            request=LegalChatRequest(
                question=(
                    "Between Peru and Australia, where are "
                    "overtime rules stricter?"
                )
            ),
            catalog_provider=_catalog_provider,
            search_function=_fake_multi_country_legal_search(
                countries, "Overtime comparison content."
            ),
            generation_client=generation_client,
            understanding_client=understanding_client,
        )

        self.assertTrue(response.grounded)


class MultiCountryComparisonTests(unittest.TestCase):
    """A comparison need not stop at two countries."""

    def test_three_country_comparison(self) -> None:
        countries = [
            ("ES", "Spain"),
            ("PE", "Peru"),
            ("AU", "Australia"),
        ]

        understanding_client = FakeUnderstandingClient(
            payload=_understanding_result(
                actions=[
                    _understanding_action(
                        "comparison",
                        country_codes=["ES", "PE", "AU"],
                        topic_text="notice periods",
                    )
                ],
            )
        )

        generation_client = FakeGenerationClient(
            answer=_build_comparison_answer(
                countries, "Notice period comparison content."
            )
        )

        response = resolve_legal_chat_response(
            request=LegalChatRequest(
                question=(
                    "Compare notice periods in Spain, Peru "
                    "and Australia."
                )
            ),
            catalog_provider=_catalog_provider,
            search_function=_fake_multi_country_legal_search(
                countries, "Notice period comparison content."
            ),
            generation_client=generation_client,
            understanding_client=understanding_client,
        )

        self.assertTrue(response.grounded)
        self.assertEqual(len(response.sources), 3)


class ComparisonTopicRepresentationTests(unittest.TestCase):
    """
    A comparison's topic may be represented as a canonical legal_topic
    or as free-text topic_text when no canonical phrase matches -
    either representation must reach search and generation.
    """

    def test_canonical_legal_topic_is_used_when_available(self) -> None:
        countries = [("BE", "Belgium"), ("ES", "Spain")]

        understanding_client = FakeUnderstandingClient(
            payload=_understanding_result(
                actions=[
                    _understanding_action(
                        "comparison",
                        country_codes=["BE", "ES"],
                        legal_topics=["Termination"],
                    )
                ],
            )
        )

        generation_client = FakeGenerationClient(
            answer=_build_comparison_answer(
                countries, "Termination content."
            )
        )

        response = resolve_legal_chat_response(
            request=LegalChatRequest(
                question=(
                    "How do Belgium and Spain differ on "
                    "termination?"
                )
            ),
            catalog_provider=_catalog_provider,
            search_function=_fake_multi_country_legal_search(
                countries, "Termination content."
            ),
            generation_client=generation_client,
            understanding_client=understanding_client,
        )

        self.assertTrue(response.grounded)

    def test_topic_text_used_when_no_canonical_match(self) -> None:
        """
        "maternity rights" does not match the canonical "maternity
        leave" phrase, so the model represents it as free-text
        topic_text - this must still reach search and generation.
        """

        countries = [("BE", "Belgium"), ("ES", "Spain")]

        understanding_client = FakeUnderstandingClient(
            payload=_understanding_result(
                actions=[
                    _understanding_action(
                        "comparison",
                        country_codes=["BE", "ES"],
                        topic_text="maternity rights",
                    )
                ],
            )
        )

        generation_client = FakeGenerationClient(
            answer=_build_comparison_answer(
                countries, "Maternity rights content."
            )
        )

        response = resolve_legal_chat_response(
            request=LegalChatRequest(
                question=(
                    "How different are maternity rights in "
                    "Belgium and Spain?"
                )
            ),
            catalog_provider=_catalog_provider,
            search_function=_fake_multi_country_legal_search(
                countries, "Maternity rights content."
            ),
            generation_client=generation_client,
            understanding_client=understanding_client,
        )

        self.assertTrue(response.grounded)


class ComparisonClarificationTests(unittest.TestCase):
    """
    A comparison request missing a country or a topic must clarify -
    never search, never the documentary-insufficiency message.
    """

    def test_missing_comparison_topic_clarifies(self) -> None:
        understanding_client = FakeUnderstandingClient(
            payload=_understanding_result(
                status="clarification",
                clarification_reason="missing_comparison_topic",
                actions=[
                    _understanding_action(
                        "comparison",
                        country_codes=["ES", "PL"],
                    )
                ],
            )
        )

        response = resolve_legal_chat_response(
            request=LegalChatRequest(
                question=(
                    "Compare employment rules between Spain "
                    "and Poland"
                )
            ),
            catalog_provider=_catalog_provider,
            search_function=_fail_if_called,
            understanding_client=understanding_client,
        )

        self.assertEqual(
            response.answer,
            CLARIFICATION_MISSING_COMPARISON_TOPIC_ANSWER,
        )
        self.assertFalse(response.grounded)
        self.assertEqual(response.sources, [])

    def test_missing_comparison_countries_clarifies(self) -> None:
        understanding_client = FakeUnderstandingClient(
            payload=_understanding_result(
                status="clarification",
                clarification_reason="missing_comparison_countries",
                actions=[
                    _understanding_action(
                        "comparison",
                        country_codes=["PE"],
                        topic_text="annual bonus scheme",
                    )
                ],
            )
        )

        response = resolve_legal_chat_response(
            request=LegalChatRequest(
                question=(
                    "How does the annual bonus scheme compare in "
                    "Peru versus other countries?"
                )
            ),
            catalog_provider=_catalog_provider,
            search_function=_fail_if_called,
            understanding_client=understanding_client,
        )

        self.assertEqual(
            response.answer,
            CLARIFICATION_MISSING_COMPARISON_COUNTRIES_ANSWER,
        )
        self.assertFalse(response.grounded)


class ComparisonSchemaSafetyNetTests(unittest.TestCase):
    """
    Even if the raw model output claims a "resolved" comparison with
    fewer than two countries - a malformed or prompt-injected
    response - the unconditional post-hoc Pydantic validation in
    request_understanding.py must reject it before it ever reaches the
    router, degrading to the conservative fallback/safe clarification
    instead of ever searching with a single-country "comparison".
    """

    def test_single_country_resolved_comparison_never_searches(
        self,
    ) -> None:
        understanding_client = FakeUnderstandingClient(
            payload=_understanding_result(
                status="resolved",
                actions=[
                    _understanding_action(
                        "comparison",
                        country_codes=["PE"],
                        topic_text="annual bonus scheme",
                    )
                ],
            )
        )

        response = resolve_legal_chat_response(
            request=LegalChatRequest(
                question=(
                    "How does the annual bonus scheme compare in "
                    "Peru versus other countries?"
                )
            ),
            catalog_provider=_catalog_provider,
            search_function=_fail_if_called,
            understanding_client=understanding_client,
        )

        self.assertFalse(response.grounded)
        self.assertEqual(response.retrieval_total, 0)
        self.assertEqual(response.sources, [])


if __name__ == "__main__":
    unittest.main()
