"""
End-to-end router tests for the RequestUnderstanding-driven router.

RequestUnderstanding (see app/services/request_understanding.py) is now
the sole primary router for every free-text /api/v1/chat request: the
deterministic STRONG_CONTACT_INTENT / COUNTRY_SCOPED_REACH_INTENT
regexes and country/topic detection only ever feed it hints - they
never again decide, on their own, that a request is fully understood,
block a second action, or block a demonym/city resolution. These tests
exercise resolve_legal_chat_response() end-to-end with a mocked
understanding client standing in for the real OpenAI call, covering:
basic single-action routing, the exact mixed-request phrasings that a
prior, now-deleted connector-word gate
(`_has_coordinated_second_clause`) silently lost their Contact half
for, every clarification wording, the explicit-filter-conflict path,
and the deterministic "unavailable country" refinement layered on top
of a semantic clarification. The phrasings below are test fixtures
only - they are never copied into any production lookup table.
"""

from __future__ import annotations

import json
import unittest
from typing import Any
from unittest import mock

from app.clients.openai_responses import GeneratedText, OpenAIResponseError
from app.core.country_registry import COUNTRIES
from app.models.catalog import LegalCatalogCountry, LegalCatalogResponse
from app.models.chat import LegalChatRequest
from app.models.search import LegalSearchHit, LegalSearchResponse
from app.routers.chat import (
    CLARIFICATION_AMBIGUOUS_REQUEST_ANSWER,
    CLARIFICATION_AMBIGUOUS_WITH_COUNTRY_TEMPLATE,
    CLARIFICATION_EXPLICIT_FILTER_CONFLICT_ANSWER,
    CLARIFICATION_LEGAL_MISSING_COUNTRY_ANSWER,
    CLARIFICATION_MISSING_COMPARISON_COUNTRIES_ANSWER,
    CLARIFICATION_MISSING_COMPARISON_TOPIC_ANSWER,
    CLARIFICATION_UNSUPPORTED_REQUEST_ANSWER,
    CONTACT_CLARIFICATION_ANSWER,
    resolve_legal_chat_response,
)


def _document_topic_provider(
    country_codes: list[str],
) -> dict[str, list[str]]:
    """
    Fake DocumentLegalTopicsProvider - mission "ORDER 8F-A" - no live
    document legal topics for any country, matching every test in this
    file written before that mission (none of them concern the new
    document_legal_topics concept).
    """

    return {}


# See test_chat.py's _NOT_YET_INDEXED_CODES for the full rationale:
# country_registry.COUNTRIES now includes several countries (France,
# Germany among them) registered purely for detection/admin-allowlist
# purposes, with no real indexed content yet - mirroring it 1:1 into
# this fake catalog would silently claim otherwise.
_NOT_YET_INDEXED_CODES: frozenset[str] = frozenset({"FR", "DE"})


def _catalog_provider() -> LegalCatalogResponse:
    """Return a catalog covering every actually-indexed real country."""

    return LegalCatalogResponse(
        countries=[
            LegalCatalogCountry(
                country_code=country.code,
                country=country.display_name,
                chunk_count=42,
            )
            for country in COUNTRIES
            if country.code not in _NOT_YET_INDEXED_CODES
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
        legal_topic="Hiring Practices",
        document_type="comparator",
        language="en",
        section="01. Hiring Practices",
        subsection="Seasonal Workers",
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

        return GeneratedText(
            text=self.answer,
            model=self.model,
        )


def _understanding_action(
    action_type: str,
    *,
    country_codes: list[str] | None = None,
    legal_topics: list[str] | None = None,
    topic_text: str | None = None,
    resolved_question: str | None = None,
) -> dict[str, Any]:
    """
    Build one RequestUnderstandingAction JSON payload.

    Mirrors app.services.request_understanding.RequestUnderstandingAction
    exactly - see that module's model_validator for which fields are
    required for which type/status combination.
    """

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
    """
    Build one RequestUnderstandingResult JSON payload.

    Mirrors app.services.request_understanding.RequestUnderstandingResult
    exactly, so every fake understanding response used below is a
    genuinely valid payload the real model_validator would accept.
    """

    return {
        "status": status,
        "actions": actions or [],
        "is_follow_up": is_follow_up,
        "confidence": confidence,
        "current_message_delta": (
            current_message_delta
            if current_message_delta is not None
            else _current_message_delta(
                context_operation=(
                    "continue" if is_follow_up else "independent"
                ),
            )
        ),
        "clarification_reason": clarification_reason,
    }


class FakeUnderstandingClient:
    """Test double for the semantic-understanding OpenAI client."""

    def __init__(
        self,
        payload: dict[str, Any] | None = None,
        raise_error: Exception | None = None,
    ) -> None:
        self.payload = payload
        self.raise_error = raise_error
        self.call_count = 0

    def generate(
        self,
        instructions: str,
        input_text: str,
        text_format: dict[str, Any] | None = None,
    ) -> GeneratedText:
        self.call_count += 1

        if self.raise_error is not None:
            raise self.raise_error

        return GeneratedText(
            text=json.dumps(self.payload),
            model="test-model",
        )


def _fail_if_called(*args: Any, **kwargs: Any) -> Any:
    raise AssertionError(
        "This function must not be called for this scenario."
    )


def _fake_legal_search(
    country_code: str,
    country: str,
    content: str,
):
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
    """
    Build a per-country-sectioned answer matching what
    answer_legal_question's grounding validation requires for a
    multi-country comparison: each requested country's display name as
    its own section, each citing the one source position that country
    contributed, in retrieval order.
    """

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
            ) == sorted(expected_codes), (
                f"expected contact search for {expected_codes}, "
                f"got {country_codes}"
            )

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


class BasicSingleActionRoutingTests(unittest.TestCase):
    """
    One resolved action of each type must reach its own dedicated
    path - contact never touches search_function/generation_client,
    legal_information and comparison never touch search_contact_chunks.
    """

    def test_contact_only_action_reaches_contact_path(self) -> None:
        understanding_client = FakeUnderstandingClient(
            payload=_understanding_result(
                actions=[
                    _understanding_action(
                        "contact",
                        country_codes=["PE"],
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
                    question="I need someone in Peru."
                ),
                catalog_provider=_catalog_provider,
                document_topic_provider=_document_topic_provider,
                search_function=_fail_if_called,
                understanding_client=understanding_client,
            )

        self.assertTrue(response.grounded)
        self.assertIsNone(response.model)
        self.assertIn("Test Firm PE", response.answer)

    def test_legal_information_only_action_reaches_legal_path(
        self,
    ) -> None:
        understanding_client = FakeUnderstandingClient(
            payload=_understanding_result(
                actions=[
                    _understanding_action(
                        "legal_information",
                        country_codes=["PE"],
                        topic_text="notice period",
                    )
                ],
            )
        )

        generation_client = FakeGenerationClient(
            answer="Peru\n- Notice period content. [1]"
        )

        with mock.patch(
            "app.routers.chat.search_contact_chunks",
            side_effect=_fail_if_called,
        ):
            response = resolve_legal_chat_response(
                request=LegalChatRequest(
                    question="What is the notice period in Peru?"
                ),
                catalog_provider=_catalog_provider,
                document_topic_provider=_document_topic_provider,
                search_function=_fake_legal_search(
                    "PE", "Peru", "Notice period content."
                ),
                generation_client=generation_client,
                understanding_client=understanding_client,
            )

        self.assertTrue(response.grounded)
        self.assertEqual(generation_client.call_count, 1)
        self.assertNotIn("Test Firm", response.answer)

    def test_comparison_only_action_reaches_comparison_path(
        self,
    ) -> None:
        countries = [("ES", "Spain"), ("AU", "Australia")]

        understanding_client = FakeUnderstandingClient(
            payload=_understanding_result(
                actions=[
                    _understanding_action(
                        "comparison",
                        country_codes=["ES", "AU"],
                        topic_text="dismissal rules",
                    )
                ],
            )
        )

        generation_client = FakeGenerationClient(
            answer=_build_comparison_answer(
                countries, "Dismissal comparison content."
            )
        )

        with mock.patch(
            "app.routers.chat.search_contact_chunks",
            side_effect=_fail_if_called,
        ):
            response = resolve_legal_chat_response(
                request=LegalChatRequest(
                    question=(
                        "Compare dismissal rules in Spain "
                        "and Australia."
                    )
                ),
                catalog_provider=_catalog_provider,
                document_topic_provider=_document_topic_provider,
                search_function=_fake_multi_country_legal_search(
                    countries, "Dismissal comparison content."
                ),
                generation_client=generation_client,
                understanding_client=understanding_client,
            )

        self.assertTrue(response.grounded)
        self.assertIn("Dismissal comparison content.", response.answer)


class CriticalMixedRequestRegressionTests(unittest.TestCase):
    """
    The three exact phrasings that the deleted, closed-connector-word
    gate (`_has_coordinated_second_clause`) silently lost their Contact
    half for, as confirmed by the real-world OpenAI/OpenSearch
    pre-deployment validation of commit 51cec1f. These phrasings are
    test fixtures only - never copied into any production lookup
    table. Since RequestUnderstanding now resolves the full action
    list every time (no gate deciding whether a second action can even
    be looked for), each of these must return both halves.
    """

    def test_compare_spain_and_australia_with_spanish_adviser(
        self,
    ) -> None:
        countries = [("ES", "Spain"), ("AU", "Australia")]

        understanding_client = FakeUnderstandingClient(
            payload=_understanding_result(
                actions=[
                    _understanding_action(
                        "comparison",
                        country_codes=["ES", "AU"],
                        topic_text="dismissal rules",
                    ),
                    _understanding_action(
                        "contact",
                        country_codes=["ES"],
                    ),
                ],
            )
        )

        generation_client = FakeGenerationClient(
            answer=_build_comparison_answer(
                countries, "Dismissal comparison content."
            )
        )

        with mock.patch(
            "app.routers.chat.search_contact_chunks",
            side_effect=_fake_contact_search(expected_codes=["ES"]),
        ):
            response = resolve_legal_chat_response(
                request=LegalChatRequest(
                    question=(
                        "Compare Spain and Australia on dismissal. "
                        "Please point us to the Spanish adviser "
                        "as well."
                    )
                ),
                catalog_provider=_catalog_provider,
                document_topic_provider=_document_topic_provider,
                search_function=_fake_multi_country_legal_search(
                    countries, "Dismissal comparison content."
                ),
                generation_client=generation_client,
                understanding_client=understanding_client,
            )

        self.assertIn("Dismissal comparison content.", response.answer)
        self.assertIn("Test Firm ES", response.answer)
        self.assertEqual(understanding_client.call_count, 1)
        self.assertEqual(generation_client.call_count, 1)

    def test_uk_fixed_term_contract_with_local_counsel(self) -> None:
        understanding_client = FakeUnderstandingClient(
            payload=_understanding_result(
                actions=[
                    _understanding_action(
                        "legal_information",
                        country_codes=["GB"],
                        topic_text="fixed-term contract rules",
                    ),
                    _understanding_action(
                        "contact",
                        country_codes=["GB"],
                    ),
                ],
            )
        )

        generation_client = FakeGenerationClient(
            answer="United Kingdom\n- Fixed-term contract content. [1]"
        )

        with mock.patch(
            "app.routers.chat.search_contact_chunks",
            side_effect=_fake_contact_search(expected_codes=["GB"]),
        ):
            response = resolve_legal_chat_response(
                request=LegalChatRequest(
                    question=(
                        "Explain UK fixed-term contract rules "
                        "— local counsel details would be "
                        "useful too."
                    )
                ),
                catalog_provider=_catalog_provider,
                document_topic_provider=_document_topic_provider,
                search_function=_fake_legal_search(
                    "GB",
                    "United Kingdom",
                    "Fixed-term contract content.",
                ),
                generation_client=generation_client,
                understanding_client=understanding_client,
            )

        self.assertIn(
            "Fixed-term contract content.", response.answer
        )
        self.assertIn("Test Firm GB", response.answer)
        self.assertEqual(generation_client.call_count, 1)

    def test_peru_australia_notice_periods_with_lima_contact(
        self,
    ) -> None:
        countries = [("PE", "Peru"), ("AU", "Australia")]

        understanding_client = FakeUnderstandingClient(
            payload=_understanding_result(
                actions=[
                    _understanding_action(
                        "comparison",
                        country_codes=["PE", "AU"],
                        topic_text="notice periods",
                    ),
                    _understanding_action(
                        "contact",
                        country_codes=["PE"],
                    ),
                ],
            )
        )

        generation_client = FakeGenerationClient(
            answer=_build_comparison_answer(
                countries, "Notice period comparison content."
            )
        )

        with mock.patch(
            "app.routers.chat.search_contact_chunks",
            side_effect=_fake_contact_search(expected_codes=["PE"]),
        ):
            response = resolve_legal_chat_response(
                request=LegalChatRequest(
                    question=(
                        "How do notice periods differ in Peru and "
                        "Australia? Our team also needs a person "
                        "in Lima to discuss the result."
                    )
                ),
                catalog_provider=_catalog_provider,
                document_topic_provider=_document_topic_provider,
                search_function=_fake_multi_country_legal_search(
                    countries, "Notice period comparison content."
                ),
                generation_client=generation_client,
                understanding_client=understanding_client,
            )

        self.assertIn(
            "Notice period comparison content.", response.answer
        )
        self.assertIn("Test Firm PE", response.answer)
        self.assertNotIn("Test Firm AU", response.answer)

        citations = [source.citation for source in response.sources]
        self.assertEqual(citations, sorted(citations))
        self.assertEqual(len(citations), len(set(citations)))


class ClarificationWordingTests(unittest.TestCase):
    """
    Every clarification_reason value must map to its correct
    user-facing wording, and the one reason with two possible wordings
    (missing_country) must pick the Contact phrasing exactly when the
    (possibly partial) hint action is itself type="contact".
    """

    def test_missing_country_without_contact_hint_uses_legal_wording(
        self,
    ) -> None:
        understanding_client = FakeUnderstandingClient(
            payload=_understanding_result(
                status="clarification",
                clarification_reason="missing_country",
                actions=[
                    _understanding_action(
                        "legal_information",
                        topic_text="termination rules",
                    )
                ],
            )
        )

        response = resolve_legal_chat_response(
            request=LegalChatRequest(
                question="What are the termination rules?"
            ),
            catalog_provider=_catalog_provider,
            document_topic_provider=_document_topic_provider,
            search_function=_fail_if_called,
            understanding_client=understanding_client,
        )

        self.assertEqual(
            response.answer,
            CLARIFICATION_LEGAL_MISSING_COUNTRY_ANSWER,
        )
        self.assertFalse(response.grounded)
        self.assertEqual(response.sources, [])

    def test_missing_country_with_contact_hint_uses_contact_wording(
        self,
    ) -> None:
        understanding_client = FakeUnderstandingClient(
            payload=_understanding_result(
                status="clarification",
                clarification_reason="missing_country",
                actions=[_understanding_action("contact")],
            )
        )

        response = resolve_legal_chat_response(
            request=LegalChatRequest(
                question="Can you give me a lawyer contact?"
            ),
            catalog_provider=_catalog_provider,
            document_topic_provider=_document_topic_provider,
            search_function=_fail_if_called,
            understanding_client=understanding_client,
        )

        self.assertEqual(response.answer, CONTACT_CLARIFICATION_ANSWER)
        self.assertFalse(response.grounded)

    def test_missing_comparison_countries_reason(self) -> None:
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
            document_topic_provider=_document_topic_provider,
            search_function=_fail_if_called,
            understanding_client=understanding_client,
        )

        self.assertEqual(
            response.answer,
            CLARIFICATION_MISSING_COMPARISON_COUNTRIES_ANSWER,
        )
        self.assertFalse(response.grounded)

    def test_missing_comparison_topic_reason(self) -> None:
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
            document_topic_provider=_document_topic_provider,
            search_function=_fail_if_called,
            understanding_client=understanding_client,
        )

        self.assertEqual(
            response.answer,
            CLARIFICATION_MISSING_COMPARISON_TOPIC_ANSWER,
        )
        self.assertFalse(response.grounded)

    def test_ambiguous_request_without_country_hint(self) -> None:
        understanding_client = FakeUnderstandingClient(
            payload=_understanding_result(
                status="clarification",
                clarification_reason="ambiguous_request",
                actions=[],
            )
        )

        response = resolve_legal_chat_response(
            request=LegalChatRequest(question="Which country is better?"),
            catalog_provider=_catalog_provider,
            document_topic_provider=_document_topic_provider,
            search_function=_fail_if_called,
            understanding_client=understanding_client,
        )

        self.assertEqual(
            response.answer, CLARIFICATION_AMBIGUOUS_REQUEST_ANSWER
        )
        self.assertFalse(response.grounded)
        self.assertEqual(response.sources, [])

    def test_ambiguous_request_with_country_hint_names_the_country(
        self,
    ) -> None:
        understanding_client = FakeUnderstandingClient(
            payload=_understanding_result(
                status="clarification",
                clarification_reason="ambiguous_request",
                actions=[
                    _understanding_action(
                        "legal_information",
                        country_codes=["PE"],
                    )
                ],
            )
        )

        response = resolve_legal_chat_response(
            request=LegalChatRequest(question="I need help in Peru."),
            catalog_provider=_catalog_provider,
            document_topic_provider=_document_topic_provider,
            search_function=_fail_if_called,
            understanding_client=understanding_client,
        )

        self.assertEqual(
            response.answer,
            CLARIFICATION_AMBIGUOUS_WITH_COUNTRY_TEMPLATE.format(
                country="Peru"
            ),
        )
        self.assertFalse(response.grounded)

    def test_unsupported_request_reason(self) -> None:
        understanding_client = FakeUnderstandingClient(
            payload=_understanding_result(
                status="unsupported",
                clarification_reason="unsupported_request",
                actions=[],
            )
        )

        response = resolve_legal_chat_response(
            request=LegalChatRequest(
                question="Tell me about taxes in Peru."
            ),
            catalog_provider=_catalog_provider,
            document_topic_provider=_document_topic_provider,
            search_function=_fail_if_called,
            understanding_client=understanding_client,
        )

        self.assertEqual(
            response.answer, CLARIFICATION_UNSUPPORTED_REQUEST_ANSWER
        )
        self.assertFalse(response.grounded)
        self.assertEqual(response.retrieval_total, 0)


class ExplicitFilterConflictTests(unittest.TestCase):
    """
    An explicit country_codes filter on the request is binding: when
    RequestUnderstanding resolves a country outside that explicit set,
    the router must surface a distinct clarification rather than
    silently pick either side.
    """

    def test_resolved_action_outside_explicit_filter_conflicts(
        self,
    ) -> None:
        understanding_client = FakeUnderstandingClient(
            payload=_understanding_result(
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
                question="What is the notice period in Australia?",
                country_codes=["ES"],
            ),
            catalog_provider=_catalog_provider,
            document_topic_provider=_document_topic_provider,
            search_function=_fail_if_called,
            understanding_client=understanding_client,
        )

        self.assertEqual(
            response.answer,
            CLARIFICATION_EXPLICIT_FILTER_CONFLICT_ANSWER,
        )
        self.assertFalse(response.grounded)

    def test_resolved_action_within_explicit_filter_is_not_a_conflict(
        self,
    ) -> None:
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
            answer="Spain\n- Notice period content. [1]"
        )

        response = resolve_legal_chat_response(
            request=LegalChatRequest(
                question="What is the notice period there?",
                country_codes=["ES"],
            ),
            catalog_provider=_catalog_provider,
            document_topic_provider=_document_topic_provider,
            search_function=_fake_legal_search(
                "ES", "Spain", "Notice period content."
            ),
            generation_client=generation_client,
            understanding_client=understanding_client,
        )

        self.assertTrue(response.grounded)
        self.assertEqual(generation_client.call_count, 1)


class DeterministicUnavailableCountryRefinementTests(unittest.TestCase):
    """
    A country the model correctly leaves out of a resolved action
    (since it is instructed to only ever return a country from the
    supported list) can still be named by the deterministic
    "unavailable country" refinement layered on top of a semantic
    missing_country clarification - preserving the pre-existing,
    helpful "we don't have documents for X" UX without making that
    fact a routing gate.
    """

    def test_france_mentioned_in_current_question_is_named(
        self,
    ) -> None:
        understanding_client = FakeUnderstandingClient(
            payload=_understanding_result(
                status="clarification",
                clarification_reason="missing_country",
                actions=[
                    _understanding_action(
                        "legal_information",
                        topic_text="overtime rules",
                    )
                ],
            )
        )

        response = resolve_legal_chat_response(
            request=LegalChatRequest(
                question="What are the overtime rules in France?"
            ),
            catalog_provider=_catalog_provider,
            document_topic_provider=_document_topic_provider,
            search_function=_fail_if_called,
            understanding_client=understanding_client,
        )

        self.assertFalse(response.grounded)
        self.assertEqual(response.retrieval_total, 0)
        self.assertEqual(response.sources, [])
        self.assertIn("France", response.answer)

    def test_germany_mentioned_in_current_question_is_named(
        self,
    ) -> None:
        understanding_client = FakeUnderstandingClient(
            payload=_understanding_result(
                status="clarification",
                clarification_reason="missing_country",
                actions=[
                    _understanding_action(
                        "legal_information",
                        topic_text="tax rules",
                    )
                ],
            )
        )

        response = resolve_legal_chat_response(
            request=LegalChatRequest(
                question="What are the tax rules in Germany?"
            ),
            catalog_provider=_catalog_provider,
            document_topic_provider=_document_topic_provider,
            search_function=_fail_if_called,
            understanding_client=understanding_client,
        )

        self.assertFalse(response.grounded)
        self.assertEqual(response.sources, [])
        self.assertIn("Germany", response.answer)

    def test_unavailable_country_named_from_history_when_absent_now(
        self,
    ) -> None:
        """
        The unavailable country was named two turns back, not in the
        current follow-up - the refinement must still surface it via
        hints.history_unavailable_country_codes, never silently drop
        it just because the current turn alone carries no country.
        """

        understanding_client = FakeUnderstandingClient(
            payload=_understanding_result(
                status="clarification",
                clarification_reason="missing_country",
                is_follow_up=True,
                actions=[
                    _understanding_action(
                        "legal_information",
                        topic_text="tax rules",
                    )
                ],
            )
        )

        response = resolve_legal_chat_response(
            request=LegalChatRequest(
                question="What about that?",
                history=[
                    {
                        "role": "user",
                        "content": (
                            "What are the tax rules in Germany?"
                        ),
                    },
                    {
                        "role": "assistant",
                        "content": "Answer.",
                    },
                ],
            ),
            catalog_provider=_catalog_provider,
            document_topic_provider=_document_topic_provider,
            search_function=_fail_if_called,
            understanding_client=understanding_client,
        )

        self.assertFalse(response.grounded)
        self.assertIn("Germany", response.answer)


class UnderstandingResilienceTests(unittest.TestCase):
    """
    The understanding call itself must never crash the request and
    never degrade to the documentary-insufficiency message - only to
    the conservative fallback route or a safe, generic clarification.
    """

    def test_understanding_call_failure_yields_safe_clarification(
        self,
    ) -> None:
        understanding_client = FakeUnderstandingClient(
            raise_error=OpenAIResponseError("boom", retryable=False)
        )

        response = resolve_legal_chat_response(
            request=LegalChatRequest(
                question=(
                    "Tell me something about the weather in Peru"
                )
            ),
            catalog_provider=_catalog_provider,
            document_topic_provider=_document_topic_provider,
            search_function=_fail_if_called,
            understanding_client=understanding_client,
        )

        self.assertEqual(
            response.answer, CLARIFICATION_AMBIGUOUS_REQUEST_ANSWER
        )
        self.assertFalse(response.grounded)
        self.assertIsNone(response.model)

    def test_understanding_unparsable_response_yields_safe_clarification(
        self,
    ) -> None:
        class GarbageClient:
            def generate(
                self,
                instructions: str,
                input_text: str,
                text_format: dict[str, Any] | None = None,
            ) -> GeneratedText:
                return GeneratedText(
                    text="not JSON at all",
                    model="test-model",
                )

        response = resolve_legal_chat_response(
            request=LegalChatRequest(
                question=(
                    "Tell me something about the weather in Peru"
                )
            ),
            catalog_provider=_catalog_provider,
            document_topic_provider=_document_topic_provider,
            search_function=_fail_if_called,
            understanding_client=GarbageClient(),
        )

        self.assertEqual(
            response.answer, CLARIFICATION_AMBIGUOUS_REQUEST_ANSWER
        )
        self.assertFalse(response.grounded)


if __name__ == "__main__":
    unittest.main()
