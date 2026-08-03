"""
Tests for demonym/city resolution and Contact false-positives.

Mission A's real-world validation (commit 51cec1f) found that the
demonym "Spanish" was never resolved to Spain: country detection only
ever recognized country/city names, never nationality adjectives, so a
question naming only a demonym silently fell through to the
documentary-insufficiency message. The fix is architectural, not a
bigger dictionary: RequestUnderstanding (the model) is now the only
thing that ever resolves a demonym or a city to a country code -
app/services/country_detection.py deliberately still has no demonym
table, and these tests confirm that directly (see
DemonymIsNeverDeterministicallyDetectedTests) before confirming the
router fully trusts and executes on whatever the model resolves.

The second class of regression this file guards is the reverse
direction: a genuine legal question that merely uses "contact" as a
verb between other parties (e.g. an employer contacting an employee)
must never be misrouted to the Contact action, even though the word
"contact" appears in the text.
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
    _build_deterministic_hints,
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
        subsection="Dismissal",
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


class DemonymIsNeverDeterministicallyDetectedTests(unittest.TestCase):
    """
    Confirms the architectural fix directly: a demonym-only phrasing
    must never populate the deterministic country hints - proving the
    router has no demonym dictionary of its own and depends entirely
    on RequestUnderstanding to resolve it.
    """

    def test_spanish_demonym_yields_no_deterministic_country_hint(
        self,
    ) -> None:
        hints, current_country_scope, _ = _build_deterministic_hints(
            request=LegalChatRequest(
                question=(
                    "Explain the dismissal procedure for a "
                    "Spanish employee."
                )
            ),
            catalog_provider=_catalog_provider,
        )

        self.assertEqual(hints.current_country_codes, [])
        self.assertEqual(current_country_scope.available_codes, [])
        self.assertEqual(current_country_scope.unavailable_codes, [])

    def test_peruvian_demonym_yields_no_deterministic_country_hint(
        self,
    ) -> None:
        hints, _, _ = _build_deterministic_hints(
            request=LegalChatRequest(
                question="What is the notice period for a Peruvian worker?"
            ),
            catalog_provider=_catalog_provider,
        )

        self.assertEqual(hints.current_country_codes, [])


class DemonymRoutingTests(unittest.TestCase):
    """
    Once RequestUnderstanding resolves a demonym to its country code,
    the router must trust it fully and execute the ordinary
    legal_information plan - exactly the M-mission's L2 regression.
    """

    def test_spanish_demonym_resolves_and_executes(self) -> None:
        understanding_client = FakeUnderstandingClient(
            payload=_understanding_result(
                actions=[
                    _understanding_action(
                        "legal_information",
                        country_codes=["ES"],
                        topic_text="dismissal procedure",
                    )
                ],
            )
        )

        generation_client = FakeGenerationClient(
            answer="Spain\n- Dismissal procedure content. [1]"
        )

        with mock.patch(
            "app.routers.chat.search_contact_chunks",
            side_effect=_fail_if_called,
        ):
            response = resolve_legal_chat_response(
                request=LegalChatRequest(
                    question=(
                        "Explain the dismissal procedure for a "
                        "Spanish employee."
                    )
                ),
                catalog_provider=_catalog_provider,
                search_function=_fake_legal_search(
                    "ES", "Spain", "Dismissal procedure content."
                ),
                generation_client=generation_client,
                understanding_client=understanding_client,
            )

        self.assertTrue(response.grounded)
        self.assertEqual(understanding_client.call_count, 1)
        self.assertEqual(generation_client.call_count, 1)

    def test_peruvian_demonym_resolves_and_executes(self) -> None:
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

        response = resolve_legal_chat_response(
            request=LegalChatRequest(
                question="What is the notice period for a Peruvian worker?"
            ),
            catalog_provider=_catalog_provider,
            search_function=_fake_legal_search(
                "PE", "Peru", "Notice period content."
            ),
            generation_client=generation_client,
            understanding_client=understanding_client,
        )

        self.assertTrue(response.grounded)

    def test_australian_demonym_contact_request(self) -> None:
        understanding_client = FakeUnderstandingClient(
            payload=_understanding_result(
                actions=[
                    _understanding_action(
                        "contact", country_codes=["AU"]
                    )
                ],
            )
        )

        with mock.patch(
            "app.routers.chat.search_contact_chunks",
            side_effect=_fake_contact_search(expected_codes=["AU"]),
        ):
            response = resolve_legal_chat_response(
                request=LegalChatRequest(
                    question="I need to speak with an Australian adviser."
                ),
                catalog_provider=_catalog_provider,
                search_function=_fail_if_called,
                understanding_client=understanding_client,
            )

        self.assertTrue(response.grounded)
        self.assertIn("Test Firm AU", response.answer)


class CityResolutionTests(unittest.TestCase):
    """
    An unambiguous city standing in for a supported country - the
    router trusts a validated country code the model resolves from a
    city name, with no deterministic city dictionary of its own.
    """

    def test_lima_resolves_to_peru_contact(self) -> None:
        understanding_client = FakeUnderstandingClient(
            payload=_understanding_result(
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
                    question="Can you connect me with your team in Lima?"
                ),
                catalog_provider=_catalog_provider,
                search_function=_fail_if_called,
                understanding_client=understanding_client,
            )

        self.assertTrue(response.grounded)

    def test_barcelona_resolves_to_spain_contact(self) -> None:
        understanding_client = FakeUnderstandingClient(
            payload=_understanding_result(
                actions=[
                    _understanding_action(
                        "contact", country_codes=["ES"]
                    )
                ],
            )
        )

        with mock.patch(
            "app.routers.chat.search_contact_chunks",
            side_effect=_fake_contact_search(expected_codes=["ES"]),
        ):
            response = resolve_legal_chat_response(
                request=LegalChatRequest(
                    question="Who can help me in Barcelona?"
                ),
                catalog_provider=_catalog_provider,
                search_function=_fail_if_called,
                understanding_client=understanding_client,
            )

        self.assertTrue(response.grounded)

    def test_london_resolves_to_uk_legal_information(self) -> None:
        understanding_client = FakeUnderstandingClient(
            payload=_understanding_result(
                actions=[
                    _understanding_action(
                        "legal_information",
                        country_codes=["GB"],
                        topic_text="redundancy rules",
                    )
                ],
            )
        )

        generation_client = FakeGenerationClient(
            answer="United Kingdom\n- Redundancy content. [1]"
        )

        response = resolve_legal_chat_response(
            request=LegalChatRequest(
                question="What are the redundancy rules for our London office?"
            ),
            catalog_provider=_catalog_provider,
            search_function=_fake_legal_search(
                "GB", "United Kingdom", "Redundancy content."
            ),
            generation_client=generation_client,
            understanding_client=understanding_client,
        )

        self.assertTrue(response.grounded)

    def test_sydney_resolves_to_australia_contact(self) -> None:
        understanding_client = FakeUnderstandingClient(
            payload=_understanding_result(
                actions=[
                    _understanding_action(
                        "contact", country_codes=["AU"]
                    )
                ],
            )
        )

        with mock.patch(
            "app.routers.chat.search_contact_chunks",
            side_effect=_fake_contact_search(expected_codes=["AU"]),
        ):
            response = resolve_legal_chat_response(
                request=LegalChatRequest(
                    question="Is there somebody from your network in Sydney?"
                ),
                catalog_provider=_catalog_provider,
                search_function=_fail_if_called,
                understanding_client=understanding_client,
            )

        self.assertTrue(response.grounded)


class ContactVerbFalsePositiveTests(unittest.TestCase):
    """
    A legal question that merely discusses contact/communication
    between other parties (an employer, a lawyer, a manager) must
    never be misrouted to Contact, even when the word "contact"
    literally appears in the text.
    """

    def test_employer_contact_during_sick_leave_stays_legal(
        self,
    ) -> None:
        understanding_client = FakeUnderstandingClient(
            payload=_understanding_result(
                actions=[
                    _understanding_action(
                        "legal_information",
                        country_codes=["PE"],
                        topic_text="sick leave",
                    )
                ],
            )
        )

        generation_client = FakeGenerationClient(
            answer="Peru\n- Sick leave rules content. [1]"
        )

        with mock.patch(
            "app.routers.chat.search_contact_chunks",
            side_effect=_fail_if_called,
        ):
            response = resolve_legal_chat_response(
                request=LegalChatRequest(
                    question=(
                        "Can my employer contact me during sick "
                        "leave in Peru?"
                    )
                ),
                catalog_provider=_catalog_provider,
                search_function=_fake_legal_search(
                    "PE", "Peru", "Sick leave rules content."
                ),
                generation_client=generation_client,
                understanding_client=understanding_client,
            )

        self.assertTrue(response.grounded)
        self.assertEqual(generation_client.call_count, 1)

    def test_lawyer_contact_employee_question_stays_legal(
        self,
    ) -> None:
        understanding_client = FakeUnderstandingClient(
            payload=_understanding_result(
                actions=[
                    _understanding_action(
                        "legal_information",
                        country_codes=["BE"],
                        topic_text="permissible workplace communication",
                    )
                ],
            )
        )

        generation_client = FakeGenerationClient(
            answer="Belgium\n- Communication rules content. [1]"
        )

        with mock.patch(
            "app.routers.chat.search_contact_chunks",
            side_effect=_fail_if_called,
        ):
            response = resolve_legal_chat_response(
                request=LegalChatRequest(
                    question=(
                        "Can a lawyer contact an employee directly "
                        "in Belgium?"
                    )
                ),
                catalog_provider=_catalog_provider,
                search_function=_fake_legal_search(
                    "BE", "Belgium", "Communication rules content."
                ),
                generation_client=generation_client,
                understanding_client=understanding_client,
            )

        self.assertTrue(response.grounded)

    def test_workplace_accident_contact_question_stays_legal(
        self,
    ) -> None:
        """
        "Who must an employer contact after a workplace accident" is
        about the employer's own notification duty, never the user's
        request for their own L&E Global contact.
        """

        understanding_client = FakeUnderstandingClient(
            payload=_understanding_result(
                actions=[
                    _understanding_action(
                        "legal_information",
                        country_codes=["ES"],
                        topic_text="workplace accident reporting",
                    )
                ],
            )
        )

        generation_client = FakeGenerationClient(
            answer="Spain\n- Accident reporting content. [1]"
        )

        with mock.patch(
            "app.routers.chat.search_contact_chunks",
            side_effect=_fail_if_called,
        ):
            response = resolve_legal_chat_response(
                request=LegalChatRequest(
                    question=(
                        "Who must an employer contact after a "
                        "workplace accident in Spain?"
                    )
                ),
                catalog_provider=_catalog_provider,
                search_function=_fake_legal_search(
                    "ES", "Spain", "Accident reporting content."
                ),
                generation_client=generation_client,
                understanding_client=understanding_client,
            )

        self.assertTrue(response.grounded)
        self.assertEqual(generation_client.call_count, 1)

    def test_contact_details_in_employment_contract_stays_legal(
        self,
    ) -> None:
        understanding_client = FakeUnderstandingClient(
            payload=_understanding_result(
                actions=[
                    _understanding_action(
                        "legal_information",
                        country_codes=["BE"],
                        topic_text="employment contract requirements",
                    )
                ],
            )
        )

        generation_client = FakeGenerationClient(
            answer="Belgium\n- Employment contract content. [1]"
        )

        with mock.patch(
            "app.routers.chat.search_contact_chunks",
            side_effect=_fail_if_called,
        ):
            response = resolve_legal_chat_response(
                request=LegalChatRequest(
                    question=(
                        "What contact details must appear in an "
                        "employment contract in Belgium?"
                    )
                ),
                catalog_provider=_catalog_provider,
                search_function=_fake_legal_search(
                    "BE", "Belgium", "Employment contract content."
                ),
                generation_client=generation_client,
                understanding_client=understanding_client,
            )

        self.assertTrue(response.grounded)


if __name__ == "__main__":
    unittest.main()
