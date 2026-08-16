"""
End-to-end regression sequences for the 0.4.2 mission's named defects,
each reproduced as a literal multi-turn conversation through
resolve_legal_chat_response - turn 1's response.conversation_state is
threaded into turn 2's request exactly as the real client would.

Covers:
  A - the last active conversational state is lost across turns
  B - a precise legal sub-topic is lost on a follow-up
  D - Contact follow-ups are unstable (sometimes resolving the country,
      sometimes falling back to a generic clarification)
  E - an old turn's topic contaminates the latest turn's answer
  F - clarifications are too generic when context could support a
      specific one
  G - the disclaimer-relevant signal (conversation_state.actions) must
      be empty after a pure clarification, and conversation_state must
      be None entirely after an out-of-scope response

Defect C (retrieval surfacing only topically-adjacent sources) is
covered exhaustively in test_evidence_coverage.py and
test_rag_answer_evidence_gating.py; H (citation dedup) in
test_rag_answer_evidence_gating.py; I (UK contact address/phone) in
test_chat.py's ContactContentSanitizationTests. The conversation-
transition engine's own decision matrix is unit-tested directly in
test_conversation_transition.py - these tests instead prove the full
pipeline wiring: that resolve_legal_chat_response actually threads
conversation_state through understand_request, apply_conversation_
transition, and build_next_conversation_state correctly, turn over
turn.

Also covers Phase 27's explicit performance ceilings (reusing the same
fixtures above, so kept in this file rather than duplicated in a new
one): conversation_state reconciliation never costs a second
RequestUnderstanding call, evidence-gating never adds a third
generation call beyond the existing one-generation-plus-one-repair
budget, and evidence_coverage.py's own local engine has no network
dependency at all.
"""

from __future__ import annotations

import ast
import inspect
import unittest
from typing import Any
from unittest import mock

from app.clients.openai_responses import GeneratedText
from app.core.country_registry import COUNTRIES
from app.models.catalog import LegalCatalogCountry, LegalCatalogResponse
from app.models.chat import LegalChatRequest
from app.models.search import LegalSearchHit, LegalSearchResponse
from app.routers.chat import (
    CONTACT_CLARIFICATION_ANSWER,
    CLARIFICATION_LEGAL_MISSING_COUNTRY_ANSWER,
    CLARIFICATION_UNSUPPORTED_REQUEST_ANSWER,
    resolve_legal_chat_response,
)
from app.models.conversation_state import (
    ConversationActionState,
    ConversationSearchConcept,
    ConversationState,
)
from app.services.country_detection import resolve_country_display_name
from app.services.rag_answer import answer_legal_question


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


def _understanding_action(
    action_type: str,
    *,
    country_codes: list[str] | None = None,
    legal_topics: list[str] | None = None,
    topic_text: str | None = None,
    resolved_question: str | None = None,
    subject_text: str | None = None,
    search_concepts: list[dict[str, Any]] | None = None,
    subject_specificity: str | None = None,
    evidence_mode: str | None = None,
) -> dict[str, Any]:
    return {
        "type": action_type,
        "country_codes": country_codes or [],
        "legal_topics": legal_topics or [],
        "topic_text": topic_text,
        "resolved_question": resolved_question,
        "subject_text": subject_text,
        "search_concepts": search_concepts or [],
        "subject_specificity": subject_specificity,
        "evidence_mode": evidence_mode,
    }


def _delta(
    *,
    context_operation: str = "independent",
    explicit_action_types: list[str] | None = None,
    explicit_country_codes: list[str] | None = None,
    explicit_legal_topics: list[str] | None = None,
    explicit_subject_text: str | None = None,
) -> dict[str, Any]:
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
    delta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "status": status,
        "actions": actions or [],
        "is_follow_up": is_follow_up,
        "confidence": confidence,
        "current_message_delta": (
            delta or _delta(context_operation="independent")
        ),
        "clarification_reason": clarification_reason,
    }


class FakeUnderstandingClient:
    def __init__(self, payload: dict[str, Any]) -> None:
        import json

        self._text = json.dumps(payload)
        self.call_count = 0

    def generate(
        self,
        instructions: str,
        input_text: str,
        text_format: dict[str, Any] | None = None,
    ) -> GeneratedText:
        self.call_count += 1

        return GeneratedText(text=self._text, model="test-model")


class CapturingGenerationClient:
    """Records every (instructions, input_text) pair it was called with."""

    model = "test-model"

    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.calls: list[tuple[str, str]] = []

    def generate(self, instructions: str, input_text: str) -> GeneratedText:
        self.calls.append((instructions, input_text))

        return GeneratedText(text=self.answer, model=self.model)

    @property
    def called(self) -> bool:
        return bool(self.calls)


class NoCallGenerationClient:
    """Fails the test if generate() is ever called."""

    model = "test-model"

    def generate(self, instructions: str, input_text: str) -> GeneratedText:
        raise AssertionError(
            "OpenAI must not be called for a deterministic contact "
            "response."
        )


def _dismissal_sick_leave_search_function():
    """
    Returns one on-subject hit for whichever single country is
    requested - realistic enough to classify as "direct" evidence
    under evidence_mode="relation_required" for the dismissal/sick-
    leave concepts used throughout sequences A/B/D/E.
    """

    def fake_search(request: Any) -> LegalSearchResponse:
        country_code = (
            request.country_codes[0]
            if request.country_codes
            else "PE"
        )
        country_name = resolve_country_display_name(country_code)

        hit = LegalSearchHit(
            score=10.0,
            document_id=f"document-{country_code.lower()}",
            chunk_id=f"chunk-{country_code.lower()}",
            country=country_name,
            country_code=country_code,
            legal_topic="Termination of Employment Contracts",
            document_type="comparator",
            language="en",
            section="Termination of Employment Contracts",
            subsection="Dismissal During Sick Leave",
            content=(
                "An employee dismissed while on sick leave retains "
                "additional termination protections and continues to "
                "receive sick leave benefits during the notice "
                "period."
            ),
            source_filename=(
                f"Labour and Employment Law in {country_name} "
                "2026.docx"
            ),
            source_format="docx",
            reference_year=2026,
        )

        return LegalSearchResponse(
            query=request.query,
            total=1,
            limit=request.limit,
            offset=0,
            took_ms=1,
            hits=[hit],
        )

    return fake_search


def _build_contact_hit(
    *, country_code: str, country: str
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


def _fake_contact_search(country_codes: list[str], client: Any = None):
    return LegalSearchResponse(
        query="",
        total=len(country_codes),
        limit=20,
        offset=0,
        took_ms=1,
        hits=[
            _build_contact_hit(
                country_code=code,
                country=resolve_country_display_name(code),
            )
            for code in country_codes
        ],
    )


_DISMISSAL_SEARCH_CONCEPTS = [
    {"terms": ["dismissal", "termination"]},
    {"terms": ["sick leave", "medical leave"]},
]


def _turn_one_dismissal_understanding_payload() -> dict[str, Any]:
    return _understanding_result(
        status="resolved",
        actions=[
            _understanding_action(
                "legal_information",
                country_codes=["PE"],
                legal_topics=["Termination of Employment Contracts"],
                subject_text="dismissal while on sick leave",
                resolved_question=(
                    "For Peru, answer this employment law question: "
                    "dismissal while on sick leave."
                ),
                search_concepts=_DISMISSAL_SEARCH_CONCEPTS,
                subject_specificity="specific",
                evidence_mode="relation_required",
            )
        ],
        is_follow_up=False,
        delta=_delta(
            context_operation="independent",
            explicit_action_types=["legal_information"],
            explicit_country_codes=["PE"],
            explicit_legal_topics=["Termination of Employment Contracts"],
            explicit_subject_text="dismissal while on sick leave",
        ),
    )


class SequenceABLastStateAndPreciseSubjectTests(unittest.TestCase):
    """
    Sequence A+B: the last active conversational state (here, a
    precise sub-topic - "dismissal while on sick leave" - not just the
    broad "termination" topic) must survive a bare country follow-up,
    even when the classifier's own re-derivation for turn 2 only gets
    the broad topic right.
    """

    def test_precise_subject_and_country_both_survive_the_follow_up(
        self,
    ) -> None:
        turn_one_client = FakeUnderstandingClient(
            payload=_turn_one_dismissal_understanding_payload()
        )
        turn_one_generation = CapturingGenerationClient(
            answer=(
                "Peru\n"
                "- Dismissal while on sick leave triggers additional "
                "termination protections. [1]"
            )
        )

        turn_one_response = resolve_legal_chat_response(
            request=LegalChatRequest(
                question=(
                    "Can an employee be dismissed while on sick "
                    "leave in Peru?"
                )
            ),
            catalog_provider=_catalog_provider,
            document_topic_provider=_document_topic_provider,
            search_function=_dismissal_sick_leave_search_function(),
            generation_client=turn_one_generation,
            understanding_client=turn_one_client,
        )

        self.assertTrue(turn_one_response.grounded)
        self.assertIsNotNone(turn_one_response.conversation_state)

        state = turn_one_response.conversation_state
        self.assertEqual(len(state.actions), 1)
        self.assertEqual(state.actions[0].type, "legal_information")
        self.assertEqual(state.actions[0].country_codes, ["PE"])
        self.assertEqual(
            state.actions[0].subject_text,
            "dismissal while on sick leave",
        )
        self.assertEqual(state.focus_action_index, 0)

        # Turn 2's own classifier output is deliberately imprecise -
        # it gets the country and the broad topic right, but loses the
        # exact sub-topic (the real-world failure mode this engine
        # exists to correct - see conversation_transition.py).
        turn_two_client = FakeUnderstandingClient(
            payload=_understanding_result(
                status="resolved",
                actions=[
                    _understanding_action(
                        "legal_information",
                        country_codes=["ES"],
                        legal_topics=[
                            "Termination of Employment Contracts"
                        ],
                        resolved_question=(
                            "For Spain, answer this employment law "
                            "question about termination."
                        ),
                    )
                ],
                is_follow_up=True,
                delta=_delta(
                    context_operation="replace_country",
                    explicit_country_codes=["ES"],
                ),
            )
        )
        turn_two_generation = CapturingGenerationClient(
            answer=(
                "Spain\n"
                "- Dismissal while on sick leave triggers additional "
                "termination protections. [1]"
            )
        )

        turn_two_response = resolve_legal_chat_response(
            request=LegalChatRequest(
                question="What about in Spain?",
                conversation_state=turn_one_response.conversation_state,
            ),
            catalog_provider=_catalog_provider,
            document_topic_provider=_document_topic_provider,
            search_function=_dismissal_sick_leave_search_function(),
            generation_client=turn_two_generation,
            understanding_client=turn_two_client,
        )

        self.assertTrue(turn_two_response.grounded)

        # The engine, not the classifier, decided the final subject -
        # verify the actual generation input carried the precise
        # sub-topic and the new country, never just the broad topic.
        self.assertEqual(len(turn_two_generation.calls), 1)
        generation_input = turn_two_generation.calls[0][1]

        self.assertIn("Spain", generation_input)
        self.assertIn("dismissal", generation_input)
        self.assertIn("sick leave", generation_input)

        # And the state persisted for a hypothetical turn 3 still
        # carries the precise sub-topic forward, for Spain now.
        next_state = turn_two_response.conversation_state
        self.assertIsNotNone(next_state)
        self.assertEqual(next_state.actions[0].country_codes, ["ES"])
        self.assertEqual(
            next_state.actions[0].subject_text,
            "dismissal while on sick leave",
        )


class SameInformationCountryFollowUpTests(unittest.TestCase):
    """A natural one-country "same information" follow-up reuses the subject."""

    def test_same_information_for_new_country_reuses_previous_subject(
        self,
    ) -> None:
        state = ConversationState(
            actions=[
                ConversationActionState(
                    type="legal_information",
                    country_codes=["PE"],
                    legal_topics=[
                        "Termination of Employment Contracts"
                    ],
                    subject_text="dismissal while on sick leave",
                    search_concepts=[
                        {
                            "terms": [
                                "dismissal",
                                "termination",
                            ]
                        },
                        {
                            "terms": [
                                "sick leave",
                                "medical leave",
                            ]
                        },
                    ],
                    subject_specificity="specific",
                    evidence_mode="relation_required",
                )
            ],
            focus_action_index=0,
        )

        understanding_client = FakeUnderstandingClient(
            payload=_understanding_result(
                status="clarification",
                actions=[
                    _understanding_action(
                        "legal_information",
                        country_codes=["ES"],
                    )
                ],
                is_follow_up=True,
                clarification_reason="ambiguous_request",
                delta=_delta(context_operation="ambiguous"),
            )
        )
        generation_client = CapturingGenerationClient(
            answer=(
                "Spain\n"
                "- Dismissal while on sick leave triggers additional "
                "termination protections. [1]"
            )
        )

        response = resolve_legal_chat_response(
            request=LegalChatRequest(
                question=(
                    "Now give me the same information for Spain."
                ),
                conversation_state=state,
            ),
            catalog_provider=_catalog_provider,
            document_topic_provider=_document_topic_provider,
            search_function=_dismissal_sick_leave_search_function(),
            generation_client=generation_client,
            understanding_client=understanding_client,
        )

        self.assertTrue(response.grounded)
        self.assertEqual(understanding_client.call_count, 1)
        self.assertEqual(len(generation_client.calls), 1)

        generation_input = generation_client.calls[0][1]
        self.assertIn("Spain", generation_input)
        self.assertIn("dismissal", generation_input)
        self.assertIn("sick leave", generation_input)

        next_state = response.conversation_state
        self.assertIsNotNone(next_state)
        self.assertEqual(next_state.actions[0].country_codes, ["ES"])
        self.assertEqual(
            next_state.actions[0].subject_text,
            "dismissal while on sick leave",
        )


class SequenceDContactFollowUpStabilityTests(unittest.TestCase):
    """
    Sequence D: a Contact follow-up must resolve the same way every
    time, even when the classifier's own per-call result is unstable
    (here: it falls back to a generic "missing_country" clarification
    despite the message plainly naming a new country).
    """

    def test_an_unstable_clarification_is_overridden_to_the_new_country(
        self,
    ) -> None:
        turn_one_client = FakeUnderstandingClient(
            payload=_understanding_result(
                status="resolved",
                actions=[
                    _understanding_action(
                        "contact", country_codes=["PE"]
                    )
                ],
                delta=_delta(
                    context_operation="independent",
                    explicit_action_types=["contact"],
                    explicit_country_codes=["PE"],
                ),
            )
        )

        with mock.patch(
            "app.routers.chat.search_contact_chunks",
            side_effect=_fake_contact_search,
        ):
            turn_one_response = resolve_legal_chat_response(
                request=LegalChatRequest(
                    question=(
                        "Who is the L&E Global contact in Peru?"
                    )
                ),
                catalog_provider=_catalog_provider,
                document_topic_provider=_document_topic_provider,
                search_function=(
                    lambda request: (_ for _ in ()).throw(
                        AssertionError(
                            "Legal search must not be called for a "
                            "pure contact request."
                        )
                    )
                ),
                generation_client=NoCallGenerationClient(),
                understanding_client=turn_one_client,
            )

        self.assertTrue(turn_one_response.grounded)
        self.assertEqual(
            turn_one_response.conversation_state.actions[0].type,
            "contact",
        )

        # Turn 2's classifier unhelpfully falls back to a generic
        # clarification, even though the delta plainly captured the
        # new country - the engine must override this regardless.
        turn_two_client = FakeUnderstandingClient(
            payload=_understanding_result(
                status="clarification",
                clarification_reason="missing_country",
                actions=[],
                delta=_delta(
                    context_operation="replace_country",
                    explicit_country_codes=["ES"],
                ),
            )
        )

        with mock.patch(
            "app.routers.chat.search_contact_chunks",
            side_effect=_fake_contact_search,
        ):
            turn_two_response = resolve_legal_chat_response(
                request=LegalChatRequest(
                    question="And in Spain?",
                    conversation_state=(
                        turn_one_response.conversation_state
                    ),
                ),
                catalog_provider=_catalog_provider,
                document_topic_provider=_document_topic_provider,
                search_function=(
                    lambda request: (_ for _ in ()).throw(
                        AssertionError(
                            "Legal search must not be called for a "
                            "pure contact request."
                        )
                    )
                ),
                generation_client=NoCallGenerationClient(),
                understanding_client=turn_two_client,
            )

        self.assertTrue(turn_two_response.grounded)
        self.assertNotEqual(
            turn_two_response.answer,
            CONTACT_CLARIFICATION_ANSWER,
        )
        self.assertNotEqual(
            turn_two_response.answer,
            CLARIFICATION_LEGAL_MISSING_COUNTRY_ANSWER,
        )
        self.assertIn("Test Firm Spain", turn_two_response.answer)
        self.assertEqual(
            turn_two_response.conversation_state.actions[0]
            .country_codes,
            ["ES"],
        )


class SequenceEStateReplacementNotAccumulationTests(unittest.TestCase):
    """
    Sequence E: a genuinely new action must never be contaminated by
    the previous turn's topic, and the persisted state must be
    replaced outright, never accumulated (rectificatif A).
    """

    def test_a_new_contact_request_drops_the_old_legal_topic_entirely(
        self,
    ) -> None:
        turn_one_client = FakeUnderstandingClient(
            payload=_turn_one_dismissal_understanding_payload()
        )
        turn_one_generation = CapturingGenerationClient(
            answer=(
                "Peru\n"
                "- Dismissal while on sick leave triggers additional "
                "termination protections. [1]"
            )
        )

        turn_one_response = resolve_legal_chat_response(
            request=LegalChatRequest(
                question=(
                    "Can an employee be dismissed while on sick "
                    "leave in Peru?"
                )
            ),
            catalog_provider=_catalog_provider,
            document_topic_provider=_document_topic_provider,
            search_function=_dismissal_sick_leave_search_function(),
            generation_client=turn_one_generation,
            understanding_client=turn_one_client,
        )

        self.assertTrue(turn_one_response.grounded)

        # The classifier correctly identifies this as a brand new,
        # independent contact request - has_new_action=True, so the
        # engine must defer to it entirely rather than inherit.
        turn_two_client = FakeUnderstandingClient(
            payload=_understanding_result(
                status="resolved",
                actions=[
                    _understanding_action(
                        "contact", country_codes=["PE"]
                    )
                ],
                is_follow_up=True,
                delta=_delta(
                    context_operation="select_action",
                    explicit_action_types=["contact"],
                    explicit_country_codes=["PE"],
                ),
            )
        )

        with mock.patch(
            "app.routers.chat.search_contact_chunks",
            side_effect=_fake_contact_search,
        ):
            turn_two_response = resolve_legal_chat_response(
                request=LegalChatRequest(
                    question="Who is the contact in Peru?",
                    conversation_state=(
                        turn_one_response.conversation_state
                    ),
                ),
                catalog_provider=_catalog_provider,
                document_topic_provider=_document_topic_provider,
                search_function=(
                    lambda request: (_ for _ in ()).throw(
                        AssertionError(
                            "Legal search must not run for a pure "
                            "contact follow-up."
                        )
                    )
                ),
                generation_client=NoCallGenerationClient(),
                understanding_client=turn_two_client,
            )

        self.assertTrue(turn_two_response.grounded)

        lowered_answer = turn_two_response.answer.lower()
        self.assertNotIn("dismissal", lowered_answer)
        self.assertNotIn("sick leave", lowered_answer)
        self.assertNotIn("termination", lowered_answer)

        next_state = turn_two_response.conversation_state
        self.assertEqual(len(next_state.actions), 1)
        self.assertEqual(next_state.actions[0].type, "contact")


class SequenceFContextualClarificationTests(unittest.TestCase):
    """
    Sequence F: after a multi-country comparison, naming a new action
    with no country must ask specifically about those countries -
    never a generic "please specify a country" clarification.
    """

    def test_contact_after_a_comparison_asks_about_its_two_countries(
        self,
    ) -> None:
        turn_one_client = FakeUnderstandingClient(
            payload=_understanding_result(
                status="resolved",
                actions=[
                    _understanding_action(
                        "comparison",
                        country_codes=["PE", "ES"],
                        legal_topics=[
                            "Termination of Employment Contracts"
                        ],
                        resolved_question=(
                            "Compare Peru and Spain regarding this "
                            "employment law issue: termination."
                        ),
                    )
                ],
                delta=_delta(
                    context_operation="independent",
                    explicit_action_types=["comparison"],
                    explicit_country_codes=["PE", "ES"],
                    explicit_legal_topics=[
                        "Termination of Employment Contracts"
                    ],
                ),
            )
        )
        turn_one_generation = CapturingGenerationClient(
            answer=(
                "Peru\n"
                "- Termination content. [1]\n\n"
                "Spain\n"
                "- Termination content. [2]"
            )
        )

        def two_country_search(request: Any) -> LegalSearchResponse:
            country_code = request.country_codes[0]
            country_name = resolve_country_display_name(country_code)

            return LegalSearchResponse(
                query=request.query,
                total=1,
                limit=request.limit,
                offset=0,
                took_ms=1,
                hits=[
                    LegalSearchHit(
                        score=10.0,
                        document_id=f"document-{country_code.lower()}",
                        chunk_id=f"chunk-{country_code.lower()}",
                        country=country_name,
                        country_code=country_code,
                        legal_topic=(
                            "Termination of Employment Contracts"
                        ),
                        document_type="comparator",
                        language="en",
                        section="Termination of Employment Contracts",
                        subsection="Notice",
                        content="Termination content.",
                        source_filename=(
                            "Labour and Employment Law in "
                            f"{country_name} 2026.docx"
                        ),
                        source_format="docx",
                        reference_year=2026,
                    )
                ],
            )

        turn_one_response = resolve_legal_chat_response(
            request=LegalChatRequest(
                question=(
                    "Compare termination rules in Peru and Spain."
                )
            ),
            catalog_provider=_catalog_provider,
            document_topic_provider=_document_topic_provider,
            search_function=two_country_search,
            generation_client=turn_one_generation,
            understanding_client=turn_one_client,
        )

        self.assertTrue(turn_one_response.grounded)

        state = turn_one_response.conversation_state
        self.assertEqual(state.actions[0].type, "comparison")
        self.assertEqual(state.ordered_country_codes, ["PE", "ES"])

        # Turn 2's classifier is generic/unhelpful again - it flags a
        # clarification for a missing country without naming one -
        # but the delta at least captures that "contact" was named.
        turn_two_client = FakeUnderstandingClient(
            payload=_understanding_result(
                status="clarification",
                clarification_reason="missing_country",
                actions=[],
                delta=_delta(
                    context_operation="select_action",
                    explicit_action_types=["contact"],
                ),
            )
        )

        turn_two_response = resolve_legal_chat_response(
            request=LegalChatRequest(
                question="give me the local contact",
                conversation_state=state,
            ),
            catalog_provider=_catalog_provider,
            document_topic_provider=_document_topic_provider,
            search_function=(
                lambda request: (_ for _ in ()).throw(
                    AssertionError(
                        "Legal search must not run for a "
                        "clarification."
                    )
                )
            ),
            generation_client=NoCallGenerationClient(),
            understanding_client=turn_two_client,
        )

        self.assertFalse(turn_two_response.grounded)
        self.assertEqual(
            turn_two_response.answer,
            "Do you mean the contact in Peru or in Spain?",
        )

        # Sequence G, contextual-clarification half: conversation_state
        # is not None (a pending_clarification is tracked so the next
        # short answer - "Peru" - can resolve it) but carries zero
        # actions, which is exactly the signal the frontend uses to
        # suppress the legal disclaimer after a pure clarification.
        clarification_state = turn_two_response.conversation_state
        self.assertIsNotNone(clarification_state)
        self.assertEqual(clarification_state.actions, [])
        self.assertIsNotNone(clarification_state.pending_clarification)
        self.assertEqual(
            clarification_state.pending_clarification.candidate_country_codes,
            ["PE", "ES"],
        )


class SequenceGDisclaimerSignalAfterOutOfScopeTests(unittest.TestCase):
    """
    Sequence G, out-of-scope half: conversation_state must be None
    entirely (not merely empty-actions) after an out-of-scope answer,
    matching the other three non-resolved response paths.
    """

    def test_an_unsupported_request_carries_no_conversation_state(
        self,
    ) -> None:
        understanding_client = FakeUnderstandingClient(
            payload=_understanding_result(
                status="unsupported",
                clarification_reason="unsupported_request",
                actions=[],
                delta=_delta(context_operation="independent"),
            )
        )

        response = resolve_legal_chat_response(
            request=LegalChatRequest(
                question="What is the weather like today?"
            ),
            catalog_provider=_catalog_provider,
            document_topic_provider=_document_topic_provider,
            search_function=(
                lambda request: (_ for _ in ()).throw(
                    AssertionError(
                        "Legal search must not run for an "
                        "out-of-scope request."
                    )
                )
            ),
            generation_client=NoCallGenerationClient(),
            understanding_client=understanding_client,
        )

        self.assertFalse(response.grounded)
        self.assertEqual(
            response.answer,
            CLARIFICATION_UNSUPPORTED_REQUEST_ANSWER,
        )
        self.assertIsNone(response.conversation_state)


class RequestUnderstandingCallBudgetTests(unittest.TestCase):
    """
    Phase 27: conversation_state reconciliation is entirely local
    (conversation_transition.py never calls OpenAI) - a turn carrying
    conversation_state must cost exactly the same one
    RequestUnderstanding call as a turn without it, never a second
    "contextualization" call.
    """

    def test_a_turn_with_conversation_state_still_makes_one_call(
        self,
    ) -> None:
        turn_one_client = FakeUnderstandingClient(
            payload=_turn_one_dismissal_understanding_payload()
        )

        turn_one_response = resolve_legal_chat_response(
            request=LegalChatRequest(
                question=(
                    "Can an employee be dismissed while on sick "
                    "leave in Peru?"
                )
            ),
            catalog_provider=_catalog_provider,
            document_topic_provider=_document_topic_provider,
            search_function=_dismissal_sick_leave_search_function(),
            generation_client=CapturingGenerationClient(
                answer=(
                    "Peru\n"
                    "- Dismissal while on sick leave triggers "
                    "additional termination protections. [1]"
                )
            ),
            understanding_client=turn_one_client,
        )

        self.assertEqual(turn_one_client.call_count, 1)

        turn_two_client = FakeUnderstandingClient(
            payload=_understanding_result(
                status="resolved",
                actions=[
                    _understanding_action(
                        "legal_information",
                        country_codes=["ES"],
                        legal_topics=[
                            "Termination of Employment Contracts"
                        ],
                    )
                ],
                is_follow_up=True,
                delta=_delta(
                    context_operation="replace_country",
                    explicit_country_codes=["ES"],
                ),
            )
        )

        resolve_legal_chat_response(
            request=LegalChatRequest(
                question="What about in Spain?",
                conversation_state=turn_one_response.conversation_state,
            ),
            catalog_provider=_catalog_provider,
            document_topic_provider=_document_topic_provider,
            search_function=_dismissal_sick_leave_search_function(),
            generation_client=CapturingGenerationClient(
                answer=(
                    "Spain\n"
                    "- Dismissal while on sick leave triggers "
                    "additional termination protections. [1]"
                )
            ),
            understanding_client=turn_two_client,
        )

        # Exactly one RequestUnderstanding call for turn 2 too - the
        # engine reconciles conversation_state purely locally.
        self.assertEqual(turn_two_client.call_count, 1)


class EvidenceGatingGenerationCallBudgetTests(unittest.TestCase):
    """
    Phase 27: evidence-gating must never add a third generation call -
    the existing one-generation-plus-at-most-one-repair budget is
    unchanged, even when a partial-evidence instruction is injected
    and the first attempt triggers a repair (here, via subject_drift).
    """

    def test_partial_evidence_plus_a_triggered_repair_stays_at_two_calls(
        self,
    ) -> None:
        hits = [
            LegalSearchHit(
                score=10.0,
                document_id="document-gb-1",
                chunk_id="chunk-gb-1",
                country="United Kingdom",
                country_code="GB",
                legal_topic="Working Conditions",
                document_type="comparator",
                language="en",
                section="Working Conditions",
                subsection="Remote Work",
                content="Teleworking is permitted for eligible roles.",
                source_filename=(
                    "Labour and Employment Law in United Kingdom "
                    "2026.docx"
                ),
                source_format="docx",
                reference_year=2026,
            ),
            LegalSearchHit(
                score=9.0,
                document_id="document-gb-2",
                chunk_id="chunk-gb-2",
                country="United Kingdom",
                country_code="GB",
                legal_topic="Working Conditions",
                document_type="comparator",
                language="en",
                section="Working Conditions",
                subsection="Equipment",
                content=(
                    "Equipment costs are reimbursed by the employer."
                ),
                source_filename=(
                    "Labour and Employment Law in United Kingdom "
                    "2026.docx"
                ),
                source_format="docx",
                reference_year=2026,
            ),
        ]

        class _RepairCountingGenerationClient:
            model = "test-model"

            def __init__(self) -> None:
                self.calls: list[tuple[str, str]] = []

            def generate(
                self, instructions: str, input_text: str
            ) -> GeneratedText:
                self.calls.append((instructions, input_text))

                if len(self.calls) == 1:
                    # Mentions neither concept - triggers subject_drift.
                    return GeneratedText(
                        text=(
                            "United Kingdom\n"
                            "- General workplace policies apply. [1]"
                        ),
                        model=self.model,
                    )

                if len(self.calls) == 2:
                    return GeneratedText(
                        text=(
                            "United Kingdom\n"
                            "- Teleworking arrangements affect the "
                            "equipment allowance provided. [1]"
                        ),
                        model=self.model,
                    )

                raise AssertionError(
                    "Generation must never be called a third time - "
                    "one generation plus at most one repair is the "
                    "whole budget."
                )

        client = _RepairCountingGenerationClient()

        def fake_search(request: Any) -> LegalSearchResponse:
            return LegalSearchResponse(
                query=request.query,
                total=len(hits),
                limit=request.limit,
                offset=0,
                took_ms=1,
                hits=hits,
            )

        from app.services.chat_metrics import LegalChatMetrics

        metrics = LegalChatMetrics(
            request_id="performance-budget",
            question_characters=10,
            max_sources=6,
            rerank_enabled=False,
        )

        response = answer_legal_question(
            request=LegalChatRequest(
                question="What are the remote work equipment rules?",
                country_codes=["GB"],
            ),
            search_function=fake_search,
            generation_client=client,
            metrics=metrics,
            subject_text="remote work equipment allowance",
            search_concepts=[
                ConversationSearchConcept(terms=["teleworking"]),
                ConversationSearchConcept(
                    terms=["equipment allowance"]
                ),
            ],
            evidence_mode="relation_required",
        )

        self.assertEqual(len(client.calls), 2)
        self.assertTrue(response.grounded)
        self.assertEqual(metrics.generation_attempts, 2)
        self.assertTrue(metrics.repair_triggered)


class EvidenceCoverageHasNoNetworkDependencyTests(unittest.TestCase):
    """
    Phase 27: the local concept-coverage engine must never make its
    own network/OpenAI call - it is pure, deterministic text matching,
    always available even when reranking is disabled (the production
    default).
    """

    def test_evidence_coverage_module_imports_nothing_network_related(
        self,
    ) -> None:
        # Parses actual import statements only (via ast) - a plain
        # substring search would false-positive on this very module's
        # own docstrings, which explain in prose that no such call is
        # ever made.
        import app.services.evidence_coverage as module

        tree = ast.parse(inspect.getsource(module))
        imported_top_level_modules: set[str] = set()

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported_top_level_modules.add(
                        alias.name.split(".")[0]
                    )
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_top_level_modules.add(
                    node.module.split(".")[0]
                )

        forbidden_modules = {
            "openai",
            "httpx",
            "requests",
            "urllib3",
            "urllib",
        }

        self.assertEqual(
            imported_top_level_modules & forbidden_modules,
            set(),
        )


class JurisdictionNeutralSubjectRegressionTests(unittest.TestCase):
    """
    Mission "DÉCOUPLAGE COMPLET DU SUJET JURIDIQUE ET DE LA JURIDICTION",
    Phase 2: reproduces, end to end through resolve_legal_chat_response,
    the exact reported defect - RequestUnderstanding sometimes bakes the
    jurisdiction into subject_text itself (e.g. "rules on remote work
    (telework) in Spain" instead of "rules on remote work (telework)"),
    and a bare country follow-up ("Peru?") only ever replaces
    country_codes (see conversation_transition._inherit_action) - so
    the OLD country silently survives inside the inherited subject_text,
    the retrieval query built from it, and the insufficient-evidence
    message shown for the NEW country.

    Zero hits for every country throughout, so both turns land on the
    insufficient-evidence path (matching the exact bug report) without
    needing a generation client at all.
    """

    def _off_topic_search_function(self):
        """
        One real hit per requested country, on an unrelated
        subsection - forces a genuine content-mismatch "insufficient"
        verdict (the per-subject/per-country message template) rather
        than the unrelated all-countries-zero-hits NO_INFORMATION_
        ANSWER short-circuit.
        """

        captured: list[Any] = []

        def fake_search(request: Any) -> LegalSearchResponse:
            captured.append(request)

            code = (
                request.country_codes[0]
                if request.country_codes
                else "XX"
            )
            hit = LegalSearchHit(
                score=5.0,
                document_id=f"document-{code.lower()}",
                chunk_id=f"chunk-{code.lower()}",
                country=code,
                country_code=code,
                legal_topic="Working Conditions",
                document_type="comparator",
                language="en",
                section="Working Conditions",
                subsection="Meal Breaks",
                content=(
                    "Employees are entitled to a 30-minute meal "
                    "break after six hours of work."
                ),
                source_filename=f"Labour Law {code} 2026.docx",
                source_format="docx",
                reference_year=2026,
            )

            return LegalSearchResponse(
                query=request.query,
                total=1,
                limit=request.limit,
                offset=0,
                took_ms=1,
                hits=[hit],
            )

        return fake_search, captured

    def test_old_jurisdiction_never_survives_a_bare_country_follow_up(
        self,
    ) -> None:
        turn_one_search, turn_one_requests = (
            self._off_topic_search_function()
        )

        turn_one_client = FakeUnderstandingClient(
            payload=_understanding_result(
                status="resolved",
                actions=[
                    _understanding_action(
                        "legal_information",
                        country_codes=["ES"],
                        legal_topics=["Working Conditions"],
                        # The exact contaminated shape from the bug
                        # report - the jurisdiction is duplicated
                        # inside subject_text, not just in
                        # country_codes.
                        subject_text=(
                            "rules on remote work (telework) in Spain"
                        ),
                        search_concepts=[
                            {
                                "terms": [
                                    "remote work",
                                    "telework",
                                    "telecommuting",
                                    "working from home",
                                ]
                            }
                        ],
                        subject_specificity="specific",
                        evidence_mode="direct_topic",
                    )
                ],
                is_follow_up=False,
                delta=_delta(
                    context_operation="independent",
                    explicit_action_types=["legal_information"],
                    explicit_country_codes=["ES"],
                    explicit_legal_topics=["Working Conditions"],
                    explicit_subject_text=(
                        "rules on remote work (telework) in Spain"
                    ),
                ),
            )
        )

        turn_one_response = resolve_legal_chat_response(
            request=LegalChatRequest(
                question="What are the rules on remote work in Spain?"
            ),
            catalog_provider=_catalog_provider,
            document_topic_provider=_document_topic_provider,
            search_function=turn_one_search,
            understanding_client=turn_one_client,
        )

        self.assertFalse(turn_one_response.grounded)

        state = turn_one_response.conversation_state
        self.assertIsNotNone(state)
        self.assertEqual(state.actions[0].country_codes, ["ES"])

        # Turn 2: a bare country follow-up - the classifier's own
        # per-call action guess is deliberately imprecise (no subject
        # of its own, matching how a bare "Peru?" naturally under-
        # specifies) - context_operation="replace_country" is what
        # makes conversation_transition override it with the
        # deterministically inherited action, never the classifier's
        # own re-derivation.
        turn_two_search, turn_two_requests = (
            self._off_topic_search_function()
        )

        turn_two_client = FakeUnderstandingClient(
            payload=_understanding_result(
                status="resolved",
                actions=[
                    _understanding_action(
                        "legal_information",
                        country_codes=["PE"],
                        legal_topics=["Working Conditions"],
                        resolved_question=(
                            "For Peru, answer this employment law "
                            "question."
                        ),
                    )
                ],
                is_follow_up=True,
                delta=_delta(
                    context_operation="replace_country",
                    explicit_country_codes=["PE"],
                ),
            )
        )

        turn_two_response = resolve_legal_chat_response(
            request=LegalChatRequest(
                question="Peru?",
                conversation_state=turn_one_response.conversation_state,
            ),
            catalog_provider=_catalog_provider,
            document_topic_provider=_document_topic_provider,
            search_function=turn_two_search,
            understanding_client=turn_two_client,
        )

        self.assertFalse(turn_two_response.grounded)

        next_state = turn_two_response.conversation_state
        self.assertIsNotNone(next_state)
        self.assertEqual(next_state.actions[0].country_codes, ["PE"])

        # 1. Spain absent from the second turn's stored subject_text.
        self.assertNotIn(
            "Spain",
            next_state.actions[0].subject_text or "",
        )

        # 2/3. Spain absent from the second turn's own retrieval
        # query (the backend-reconstructed, self-contained question
        # for this turn) - never leaked in from the inherited subject.
        # Peru itself is correctly absent too: _build_retrieval_query
        # always strips whichever country is currently being searched
        # from the query text by design (it carries no BM25 signal),
        # old or new - Peru's own scope is instead correctly carried
        # as this request's country_codes filter, checked separately.
        self.assertEqual(len(turn_two_requests), 1)
        retrieval_query = turn_two_requests[0].query
        self.assertNotIn("Spain", retrieval_query)
        self.assertEqual(
            turn_two_requests[0].country_codes, ["PE"]
        )
        self.assertTrue(
            "remote work" in retrieval_query
            or "telework" in retrieval_query
        )

        # 4. Spain absent from the second turn's insufficient-evidence
        # answer.
        self.assertNotIn("Spain", turn_two_response.answer)

        # 5. Peru named exactly once in that answer - never duplicated
        # (e.g. "for Peru for Peru") by a defensive fix gone wrong.
        self.assertEqual(
            turn_two_response.answer.count("Peru"),
            1,
        )


if __name__ == "__main__":
    unittest.main()
