"""
Tests for conversational follow-ups: contact/legal/comparison
follow-ups resolved from history, switching the objective mid-
conversation (e.g. a legal question followed by "compare that with
X"), ordinal references ("the first country") that are either
confidently resolved or must clarify rather than guess, and the
performance guarantee that resolving a follow-up never costs a second
OpenAI round trip beyond the one RequestUnderstanding call and the one
legal-generation call.
"""

from __future__ import annotations

import json
import unittest
from typing import Any
from unittest import mock

from app.clients.openai_responses import GeneratedText
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


def _build_contact_hit(
    *,
    country_code: str,
    country: str,
) -> LegalSearchHit:
    return LegalSearchHit(
        score=10.0,
        document_id=f"document-{country_code.lower()}",
        chunk_id=f"chunk-{country_code.lower()}-contact",
        country=country,
        country_code=country_code,
        legal_topic=None,
        document_type="overview",
        language="en",
        section=f"Employment Law Overview {country}",
        subsection="Contact",
        content=(
            f"Member firm: Test Firm {country}\n"
            "Email: contact@test-firm.example"
        ),
        source_filename=(
            f"Labour and Employment Law in {country} 2026.docx"
        ),
        source_format="docx",
        reference_year=2026,
    )


class CountingGenerationClient:
    """Counts every legal-generation call, to prove there is only one."""

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


def _understanding_result(
    *,
    status: str = "resolved",
    actions: list[dict[str, Any]] | None = None,
    is_follow_up: bool = False,
    confidence: float = 0.9,
    clarification_reason: str | None = None,
) -> dict[str, Any]:
    return {
        "status": status,
        "actions": actions or [],
        "is_follow_up": is_follow_up,
        "confidence": confidence,
        "clarification_reason": clarification_reason,
    }


class CountingUnderstandingClient:
    """Counts every semantic-understanding call, to prove there is
    only one, and captures what it received."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.call_count = 0
        self.captured_input_texts: list[str] = []

    def generate(
        self,
        instructions: str,
        input_text: str,
        text_format: dict[str, Any] | None = None,
    ) -> GeneratedText:
        self.call_count += 1
        self.captured_input_texts.append(input_text)

        return GeneratedText(
            text=json.dumps(self.payload), model="test-model"
        )


def _fail_if_called(*args: Any, **kwargs: Any) -> Any:
    raise AssertionError(
        "This function must not be called for this scenario."
    )


def _fake_legal_search(country_code: str, country: str, content: str):
    def fake_search(request: Any) -> LegalSearchResponse:
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
    return "\n".join(
        f"{name}\n- {content} [{position}]."
        for position, (_, name) in enumerate(countries, start=1)
    )


def _fake_contact_search(expected_codes: list[str] | None = None):
    def fake_search(
        country_codes: list[str],
        client: Any = None,
    ) -> LegalSearchResponse:
        if expected_codes is not None:
            assert sorted(
                code.upper() for code in country_codes
            ) == sorted(expected_codes)

        return LegalSearchResponse(
            query="",
            total=1,
            limit=20,
            offset=0,
            took_ms=1,
            hits=[
                _build_contact_hit(country_code=code, country=code)
                for code in country_codes
            ],
        )

    return fake_search


class ContactFollowUpTests(unittest.TestCase):
    def test_contact_follow_up_resolves_country_from_history(
        self,
    ) -> None:
        understanding_client = CountingUnderstandingClient(
            payload=_understanding_result(
                is_follow_up=True,
                actions=[
                    _understanding_action(
                        "contact", country_codes=["PE"]
                    )
                ],
            )
        )

        with mock.patch(
            "app.routers.chat.search_contact_chunks",
            side_effect=_fake_contact_search(expected_codes=["PE"]),
        ):
            response = resolve_legal_chat_response(
                request=LegalChatRequest(
                    question="Can you give me a lawyer contact there?",
                    history=[
                        {
                            "role": "user",
                            "content": (
                                "What are the working time rules "
                                "in Peru?"
                            ),
                        },
                        {"role": "assistant", "content": "Answer."},
                    ],
                ),
                catalog_provider=_catalog_provider,
                search_function=_fail_if_called,
                understanding_client=understanding_client,
            )

        self.assertTrue(response.grounded)
        self.assertEqual(understanding_client.call_count, 1)


class LegalFollowUpTests(unittest.TestCase):
    def test_legal_follow_up_resolves_country_and_topic_from_history(
        self,
    ) -> None:
        generation_client = CountingGenerationClient(
            answer="Australia\n- Notice period content. [1]"
        )
        understanding_client = CountingUnderstandingClient(
            payload=_understanding_result(
                is_follow_up=True,
                actions=[
                    _understanding_action(
                        "legal_information",
                        country_codes=["AU"],
                        topic_text="notice period",
                    )
                ],
            )
        )

        response = resolve_legal_chat_response(
            request=LegalChatRequest(
                question="What about Australia?",
                history=[
                    {
                        "role": "user",
                        "content": "What is the notice period in Peru?",
                    },
                    {
                        "role": "assistant",
                        "content": "In Peru, notice periods depend on seniority.",
                    },
                ],
            ),
            catalog_provider=_catalog_provider,
            search_function=_fake_legal_search(
                "AU", "Australia", "Notice period content."
            ),
            generation_client=generation_client,
            understanding_client=understanding_client,
        )

        self.assertTrue(response.grounded)
        self.assertEqual(generation_client.call_count, 1)
        self.assertEqual(understanding_client.call_count, 1)


class ObjectiveSwitchTests(unittest.TestCase):
    """
    A conversation may switch its objective entirely mid-thread - e.g.
    from a plain legal question to a comparison, or from a comparison
    to a contact request - and RequestUnderstanding must follow the
    switch rather than staying anchored to the previous turn's action
    type.
    """

    def test_legal_question_then_switch_to_comparison(self) -> None:
        countries = [("PE", "Peru"), ("ES", "Spain")]

        understanding_client = CountingUnderstandingClient(
            payload=_understanding_result(
                is_follow_up=True,
                actions=[
                    _understanding_action(
                        "comparison",
                        country_codes=["PE", "ES"],
                        topic_text="notice period",
                    )
                ],
            )
        )

        generation_client = CountingGenerationClient(
            answer=_build_comparison_answer(
                countries, "Notice period comparison content."
            )
        )

        response = resolve_legal_chat_response(
            request=LegalChatRequest(
                question="Actually, how does that compare with Spain?",
                history=[
                    {
                        "role": "user",
                        "content": "What is the notice period in Peru?",
                    },
                    {
                        "role": "assistant",
                        "content": "In Peru, notice periods depend on seniority.",
                    },
                ],
            ),
            catalog_provider=_catalog_provider,
            search_function=_fake_multi_country_legal_search(
                countries, "Notice period comparison content."
            ),
            generation_client=generation_client,
            understanding_client=understanding_client,
        )

        self.assertTrue(response.grounded)
        self.assertEqual(understanding_client.call_count, 1)

    def test_comparison_then_switch_to_contact(self) -> None:
        understanding_client = CountingUnderstandingClient(
            payload=_understanding_result(
                is_follow_up=True,
                actions=[
                    _understanding_action(
                        "contact", country_codes=["PE"]
                    )
                ],
            )
        )

        with mock.patch(
            "app.routers.chat.search_contact_chunks",
            side_effect=_fake_contact_search(expected_codes=["PE"]),
        ):
            response = resolve_legal_chat_response(
                request=LegalChatRequest(
                    question=(
                        "Never mind the comparison, just give me "
                        "the Peruvian contact."
                    ),
                    history=[
                        {
                            "role": "user",
                            "content": (
                                "Compare notice periods in Peru "
                                "and Spain."
                            ),
                        },
                        {
                            "role": "assistant",
                            "content": "Comparison answer.",
                        },
                    ],
                ),
                catalog_provider=_catalog_provider,
                search_function=_fail_if_called,
                understanding_client=understanding_client,
            )

        self.assertTrue(response.grounded)


class OrdinalReferenceTests(unittest.TestCase):
    """
    An ordinal follow-up reference ("the first country") must never be
    resolved arbitrarily - either the model reliably recovers it from
    the previous question's own wording, or the router must clarify
    rather than guess.
    """

    def test_confidently_resolved_ordinal_reaches_contact(
        self,
    ) -> None:
        understanding_client = CountingUnderstandingClient(
            payload=_understanding_result(
                is_follow_up=True,
                actions=[
                    _understanding_action(
                        "contact", country_codes=["PE"]
                    )
                ],
            )
        )

        with mock.patch(
            "app.routers.chat.search_contact_chunks",
            side_effect=_fake_contact_search(expected_codes=["PE"]),
        ):
            response = resolve_legal_chat_response(
                request=LegalChatRequest(
                    question="Give me someone to contact in the first country.",
                    history=[
                        {
                            "role": "user",
                            "content": (
                                "Compare notice periods in Peru "
                                "and Spain."
                            ),
                        },
                        {
                            "role": "assistant",
                            "content": "Comparison answer.",
                        },
                    ],
                ),
                catalog_provider=_catalog_provider,
                search_function=_fail_if_called,
                understanding_client=understanding_client,
            )

        self.assertTrue(response.grounded)

    def test_unresolvable_ordinal_asks_for_clarification(self) -> None:
        understanding_client = CountingUnderstandingClient(
            payload=_understanding_result(
                status="clarification",
                is_follow_up=True,
                clarification_reason="ambiguous_request",
                actions=[],
            )
        )

        response = resolve_legal_chat_response(
            request=LegalChatRequest(
                question="Give me someone to contact in the first country.",
                history=[
                    {
                        "role": "user",
                        "content": (
                            "Compare notice periods in Peru "
                            "and Spain."
                        ),
                    },
                    {
                        "role": "assistant",
                        "content": "Comparison answer.",
                    },
                ],
            ),
            catalog_provider=_catalog_provider,
            search_function=_fail_if_called,
            understanding_client=understanding_client,
        )

        self.assertEqual(
            response.answer, CLARIFICATION_AMBIGUOUS_REQUEST_ANSWER
        )
        self.assertFalse(response.grounded)


class NoExtraOpenAICallForFollowUpsTests(unittest.TestCase):
    """
    Resolving a follow-up's country/topic from history is entirely
    RequestUnderstanding's job in its one semantic call - legal-answer
    generation itself must never need a second round trip to "figure
    out" what the follow-up meant.
    """

    def test_follow_up_costs_exactly_one_generation_call(self) -> None:
        understanding_client = CountingUnderstandingClient(
            payload=_understanding_result(
                is_follow_up=True,
                actions=[
                    _understanding_action(
                        "legal_information",
                        country_codes=["AU"],
                        topic_text="notice period",
                    )
                ],
            )
        )

        generation_client = CountingGenerationClient(
            answer="Australia\n- Notice period content. [1]"
        )

        resolve_legal_chat_response(
            request=LegalChatRequest(
                question="What about Australia?",
                history=[
                    {
                        "role": "user",
                        "content": "What is the notice period in Peru?",
                    },
                    {"role": "assistant", "content": "Answer."},
                ],
            ),
            catalog_provider=_catalog_provider,
            search_function=_fake_legal_search(
                "AU", "Australia", "Notice period content."
            ),
            generation_client=generation_client,
            understanding_client=understanding_client,
        )

        self.assertEqual(generation_client.call_count, 1)
        self.assertEqual(understanding_client.call_count, 1)


if __name__ == "__main__":
    unittest.main()
