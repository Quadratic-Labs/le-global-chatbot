"""
Tests for explicit API filter binding behavior.

An explicit country_codes/legal_topics/subsections filter on the
request is a binding retrieval constraint set by the caller (e.g. a
UI-driven filter), never something RequestUnderstanding's own
resolution may silently override:

- explicit legal_topics always wins over whatever legal_topics the
  model merged from its resolved actions (see _execute_resolved_plan's
  effective_legal_topics);
- explicit subsections flow through untouched, since prepared_request
  is built from the original request via model_copy and only ever
  overrides country_codes/legal_topics/question;
- explicit country_codes are enforced as a hard conflict check
  (_check_explicit_filter_conflict) against every resolved action, not
  just the first one - any action naming a country outside the
  explicit set surfaces a distinct clarification rather than silently
  picking a side.
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
    CLARIFICATION_EXPLICIT_FILTER_CONFLICT_ANSWER,
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


class ExplicitLegalTopicsBindingTests(unittest.TestCase):
    def test_explicit_legal_topics_override_resolved_action_topics(
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
                        country_code="PE",
                        country="Peru",
                    )
                ],
            )

        understanding_client = FakeUnderstandingClient(
            payload=_understanding_result(
                actions=[
                    _understanding_action(
                        "legal_information",
                        country_codes=["PE"],
                        legal_topics=["Working Conditions"],
                    )
                ],
            )
        )

        generation_client = FakeGenerationClient(
            answer="Peru\n- Legal content. [1]"
        )

        resolve_legal_chat_response(
            request=LegalChatRequest(
                question="What are the working conditions in Peru?",
                legal_topics=["Termination"],
            ),
            catalog_provider=_catalog_provider,
            search_function=fake_search,
            generation_client=generation_client,
            understanding_client=understanding_client,
        )

        self.assertEqual(len(captured_requests), 1)
        self.assertEqual(
            captured_requests[0].legal_topics, ["Termination"]
        )

    def test_no_explicit_legal_topics_uses_resolved_action_topics(
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
                        country_code="PE",
                        country="Peru",
                    )
                ],
            )

        understanding_client = FakeUnderstandingClient(
            payload=_understanding_result(
                actions=[
                    _understanding_action(
                        "legal_information",
                        country_codes=["PE"],
                        legal_topics=["Working Conditions"],
                    )
                ],
            )
        )

        generation_client = FakeGenerationClient(
            answer="Peru\n- Legal content. [1]"
        )

        resolve_legal_chat_response(
            request=LegalChatRequest(
                question="What are the working conditions in Peru?"
            ),
            catalog_provider=_catalog_provider,
            search_function=fake_search,
            generation_client=generation_client,
            understanding_client=understanding_client,
        )

        self.assertEqual(
            captured_requests[0].legal_topics, ["Working Conditions"]
        )


class ExplicitSubsectionsBindingTests(unittest.TestCase):
    def test_explicit_subsections_flow_through_unchanged(self) -> None:
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
                        country_code="PE",
                        country="Peru",
                    )
                ],
            )

        understanding_client = FakeUnderstandingClient(
            payload=_understanding_result(
                actions=[
                    _understanding_action(
                        "legal_information",
                        country_codes=["PE"],
                        topic_text="overtime",
                    )
                ],
            )
        )

        generation_client = FakeGenerationClient(
            answer="Peru\n- Legal content. [1]"
        )

        resolve_legal_chat_response(
            request=LegalChatRequest(
                question="What are the overtime rules in Peru?",
                subsections=["Overtime"],
            ),
            catalog_provider=_catalog_provider,
            search_function=fake_search,
            generation_client=generation_client,
            understanding_client=understanding_client,
        )

        self.assertEqual(
            captured_requests[0].subsections, ["Overtime"]
        )


class ExplicitCountryFilterConflictTests(unittest.TestCase):
    """
    _check_explicit_filter_conflict runs over every resolved action,
    not just the first - a mixed request where only the second action
    conflicts must still surface the conflict clarification rather
    than executing the first action and silently dropping the second.
    """

    def test_conflict_in_second_of_two_actions_is_still_caught(
        self,
    ) -> None:
        understanding_client = FakeUnderstandingClient(
            payload=_understanding_result(
                actions=[
                    _understanding_action(
                        "legal_information",
                        country_codes=["ES"],
                        topic_text="notice period",
                    ),
                    _understanding_action(
                        "contact",
                        country_codes=["AU"],
                    ),
                ],
            )
        )

        response = resolve_legal_chat_response(
            request=LegalChatRequest(
                question=(
                    "What is the notice period in Spain, and can "
                    "you also give me a contact in Australia?"
                ),
                country_codes=["ES"],
            ),
            catalog_provider=_catalog_provider,
            search_function=_fail_if_called,
            understanding_client=understanding_client,
        )

        self.assertEqual(
            response.answer,
            CLARIFICATION_EXPLICIT_FILTER_CONFLICT_ANSWER,
        )
        self.assertFalse(response.grounded)

    def test_every_action_within_explicit_set_proceeds_normally(
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
                        country_code="ES",
                        country="Spain",
                    )
                ],
            )

        understanding_client = FakeUnderstandingClient(
            payload=_understanding_result(
                actions=[
                    _understanding_action(
                        "legal_information",
                        country_codes=["ES"],
                        topic_text="notice period",
                    )
                ],
            )
        )

        generation_client = FakeGenerationClient(
            answer="Spain\n- Legal content. [1]"
        )

        response = resolve_legal_chat_response(
            request=LegalChatRequest(
                question="What is the notice period in Spain?",
                country_codes=["ES"],
            ),
            catalog_provider=_catalog_provider,
            search_function=fake_search,
            generation_client=generation_client,
            understanding_client=understanding_client,
        )

        self.assertTrue(response.grounded)
        self.assertEqual(len(captured_requests), 1)


if __name__ == "__main__":
    unittest.main()
