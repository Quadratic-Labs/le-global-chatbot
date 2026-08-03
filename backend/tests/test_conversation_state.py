"""
Tests for the client-facing conversation_state models (0.4.2).

ConversationState is untrusted input on the way in (round-tripped
through sessionStorage and the WordPress proxy) and is validated
strictly server-side (Pydantic, extra="forbid") as the final defense
- see app/models/conversation_state.py's module docstring and
app/services/conversation_transition.py for how a validated instance
is then reconciled with the semantic classifier's own output.
"""

from __future__ import annotations

import json
import unittest

from pydantic import ValidationError

from app.core.country_registry import COUNTRIES
from app.models.conversation_state import (
    MAX_ACTIONS,
    MAX_CONCEPT_TERM_CHARACTERS,
    MAX_CONCEPT_TERMS,
    MAX_CONVERSATION_STATE_JSON_CHARACTERS,
    MAX_COUNTRY_CODES_PER_ACTION,
    MAX_RESOLVED_QUESTION_CHARACTERS,
    MAX_SEARCH_CONCEPT_GROUPS,
    MAX_SUBJECT_TEXT_CHARACTERS,
    MIN_CONCEPT_TERM_CHARACTERS,
    ConversationActionState,
    ConversationPendingClarification,
    ConversationSearchConcept,
    ConversationState,
)


class ConversationSearchConceptTests(unittest.TestCase):
    def test_accepts_a_normal_synonym_group(self) -> None:
        concept = ConversationSearchConcept(
            terms=["remote work", "telework", "teleworking"]
        )

        self.assertEqual(
            concept.terms,
            ["remote work", "telework", "teleworking"],
        )

    def test_strips_surrounding_whitespace_from_each_term(self) -> None:
        concept = ConversationSearchConcept(
            terms=["  remote work  ", "telework"]
        )

        self.assertEqual(
            concept.terms,
            ["remote work", "telework"],
        )

    def test_rejects_an_empty_terms_list(self) -> None:
        with self.assertRaises(ValidationError):
            ConversationSearchConcept(terms=[])

    def test_rejects_more_than_the_maximum_number_of_terms(self) -> None:
        with self.assertRaises(ValidationError):
            ConversationSearchConcept(
                terms=[
                    f"term-{index}"
                    for index in range(MAX_CONCEPT_TERMS + 1)
                ]
            )

    def test_rejects_a_term_shorter_than_the_minimum_length(self) -> None:
        with self.assertRaises(ValidationError):
            ConversationSearchConcept(
                terms=["a" * (MIN_CONCEPT_TERM_CHARACTERS - 1)]
            )

    def test_rejects_a_term_longer_than_the_maximum_length(self) -> None:
        with self.assertRaises(ValidationError):
            ConversationSearchConcept(
                terms=["a" * (MAX_CONCEPT_TERM_CHARACTERS + 1)]
            )

    def test_rejects_case_insensitive_duplicate_terms(self) -> None:
        with self.assertRaises(ValidationError):
            ConversationSearchConcept(
                terms=["Remote Work", "remote work"]
            )

    def test_rejects_unknown_fields(self) -> None:
        with self.assertRaises(ValidationError):
            ConversationSearchConcept(
                terms=["remote work"],
                extra_field="not allowed",
            )


class ConversationActionStateTests(unittest.TestCase):
    def test_accepts_a_minimal_contact_action(self) -> None:
        action = ConversationActionState(
            type="contact",
            country_codes=["ES"],
        )

        self.assertEqual(action.country_codes, ["ES"])
        self.assertEqual(action.legal_topics, [])

    def test_contact_action_rejects_legal_topics(self) -> None:
        with self.assertRaises(ValidationError):
            ConversationActionState(
                type="contact",
                country_codes=["ES"],
                legal_topics=["Employment Contracts"],
            )

    def test_contact_action_rejects_subject_text(self) -> None:
        with self.assertRaises(ValidationError):
            ConversationActionState(
                type="contact",
                country_codes=["ES"],
                subject_text="dismissal while on sick leave",
            )

    def test_contact_action_rejects_search_concepts(self) -> None:
        with self.assertRaises(ValidationError):
            ConversationActionState(
                type="contact",
                country_codes=["ES"],
                search_concepts=[
                    ConversationSearchConcept(terms=["dismissal"])
                ],
            )

    def test_contact_action_rejects_subject_specificity(self) -> None:
        with self.assertRaises(ValidationError):
            ConversationActionState(
                type="contact",
                country_codes=["ES"],
                subject_specificity="specific",
            )

    def test_contact_action_rejects_evidence_mode(self) -> None:
        with self.assertRaises(ValidationError):
            ConversationActionState(
                type="contact",
                country_codes=["ES"],
                evidence_mode="broad_topic",
            )

    def test_legal_information_action_accepts_legal_topics_alone(
        self,
    ) -> None:
        action = ConversationActionState(
            type="legal_information",
            country_codes=["ES"],
            legal_topics=["Termination of Employment Contracts"],
        )

        self.assertEqual(
            action.legal_topics,
            ["Termination of Employment Contracts"],
        )

    def test_legal_information_action_accepts_subject_text_alone(
        self,
    ) -> None:
        action = ConversationActionState(
            type="legal_information",
            country_codes=["ES"],
            subject_text="dismissal while on sick leave",
        )

        self.assertEqual(
            action.subject_text,
            "dismissal while on sick leave",
        )

    def test_legal_information_action_requires_topics_or_subject(
        self,
    ) -> None:
        with self.assertRaises(ValidationError):
            ConversationActionState(
                type="legal_information",
                country_codes=["ES"],
            )

    def test_comparison_action_accepts_two_countries(self) -> None:
        action = ConversationActionState(
            type="comparison",
            country_codes=["ES", "IT"],
            legal_topics=["Termination of Employment Contracts"],
        )

        self.assertEqual(action.country_codes, ["ES", "IT"])

    def test_comparison_action_rejects_a_single_country(self) -> None:
        with self.assertRaises(ValidationError):
            ConversationActionState(
                type="comparison",
                country_codes=["ES"],
                legal_topics=["Termination of Employment Contracts"],
            )

    def test_country_codes_are_uppercased_and_deduplicated(self) -> None:
        action = ConversationActionState(
            type="contact",
            country_codes=["es", "ES", "it"],
        )

        self.assertEqual(action.country_codes, ["ES", "IT"])

    def test_rejects_an_unsupported_country_code(self) -> None:
        with self.assertRaises(ValidationError):
            ConversationActionState(
                type="contact",
                country_codes=["ZZ"],
            )

    def test_rejects_more_country_codes_than_exist(self) -> None:
        with self.assertRaises(ValidationError):
            ConversationActionState(
                type="contact",
                country_codes=(
                    [COUNTRIES[0].code]
                    * (MAX_COUNTRY_CODES_PER_ACTION + 1)
                ),
            )

    def test_rejects_a_non_canonical_legal_topic(self) -> None:
        with self.assertRaises(ValidationError):
            ConversationActionState(
                type="legal_information",
                country_codes=["ES"],
                legal_topics=["Termination"],
            )

    def test_rejects_subject_text_longer_than_the_maximum(self) -> None:
        with self.assertRaises(ValidationError):
            ConversationActionState(
                type="legal_information",
                country_codes=["ES"],
                subject_text=(
                    "a" * (MAX_SUBJECT_TEXT_CHARACTERS + 1)
                ),
            )

    def test_rejects_resolved_question_longer_than_the_maximum(
        self,
    ) -> None:
        with self.assertRaises(ValidationError):
            ConversationActionState(
                type="legal_information",
                country_codes=["ES"],
                subject_text="dismissal",
                resolved_question=(
                    "a" * (MAX_RESOLVED_QUESTION_CHARACTERS + 1)
                ),
            )

    def test_rejects_more_search_concept_groups_than_the_maximum(
        self,
    ) -> None:
        with self.assertRaises(ValidationError):
            ConversationActionState(
                type="legal_information",
                country_codes=["ES"],
                subject_text="dismissal",
                search_concepts=[
                    ConversationSearchConcept(terms=[f"term-{index}"])
                    for index in range(MAX_SEARCH_CONCEPT_GROUPS + 1)
                ],
            )

    def test_accepts_each_evidence_mode(self) -> None:
        for evidence_mode in (
            "broad_topic",
            "direct_topic",
            "relation_required",
            None,
        ):
            action = ConversationActionState(
                type="legal_information",
                country_codes=["ES"],
                subject_text="dismissal",
                evidence_mode=evidence_mode,
            )

            self.assertEqual(action.evidence_mode, evidence_mode)

    def test_rejects_an_unsupported_evidence_mode(self) -> None:
        with self.assertRaises(ValidationError):
            ConversationActionState(
                type="legal_information",
                country_codes=["ES"],
                subject_text="dismissal",
                evidence_mode="vector_search",
            )

    def test_rejects_an_unsupported_action_type(self) -> None:
        with self.assertRaises(ValidationError):
            ConversationActionState(
                type="unknown_action",
                country_codes=["ES"],
            )

    def test_rejects_unknown_fields(self) -> None:
        with self.assertRaises(ValidationError):
            ConversationActionState(
                type="contact",
                country_codes=["ES"],
                extra_field="not allowed",
            )


class ConversationPendingClarificationTests(unittest.TestCase):
    def test_accepts_a_minimal_clarification(self) -> None:
        clarification = ConversationPendingClarification(
            reason="select_country",
            candidate_country_codes=["ES", "IT"],
        )

        self.assertEqual(
            clarification.candidate_country_codes,
            ["ES", "IT"],
        )

    def test_accepts_a_clarification_with_no_candidates_yet(
        self,
    ) -> None:
        clarification = ConversationPendingClarification(
            reason="ambiguous_reference",
        )

        self.assertEqual(clarification.candidate_action_types, [])
        self.assertEqual(clarification.candidate_country_codes, [])

    def test_rejects_an_unsupported_reason(self) -> None:
        with self.assertRaises(ValidationError):
            ConversationPendingClarification(
                reason="not_a_real_reason",
            )

    def test_candidate_country_codes_are_uppercased_and_deduplicated(
        self,
    ) -> None:
        clarification = ConversationPendingClarification(
            reason="select_country",
            candidate_country_codes=["es", "ES", "it"],
        )

        self.assertEqual(
            clarification.candidate_country_codes,
            ["ES", "IT"],
        )

    def test_rejects_an_unsupported_candidate_country_code(
        self,
    ) -> None:
        with self.assertRaises(ValidationError):
            ConversationPendingClarification(
                reason="select_country",
                candidate_country_codes=["ZZ"],
            )

    def test_rejects_unknown_fields(self) -> None:
        with self.assertRaises(ValidationError):
            ConversationPendingClarification(
                reason="select_country",
                extra_field="not allowed",
            )


def _contact(country_codes: list[str]) -> ConversationActionState:
    return ConversationActionState(
        type="contact",
        country_codes=country_codes,
    )


def _legal(
    country_codes: list[str],
    topics: list[str] | None = None,
) -> ConversationActionState:
    return ConversationActionState(
        type="legal_information",
        country_codes=country_codes,
        legal_topics=(
            topics
            if topics is not None
            else ["Termination of Employment Contracts"]
        ),
    )


def _comparison(
    country_codes: list[str],
    topics: list[str] | None = None,
) -> ConversationActionState:
    return ConversationActionState(
        type="comparison",
        country_codes=country_codes,
        legal_topics=(
            topics
            if topics is not None
            else ["Termination of Employment Contracts"]
        ),
    )


class ConversationStateTests(unittest.TestCase):
    def test_accepts_a_fully_empty_state(self) -> None:
        state = ConversationState()

        self.assertEqual(state.actions, [])
        self.assertIsNone(state.focus_action_index)
        self.assertIsNone(state.pending_clarification)

    def test_accepts_two_different_action_types_for_the_same_country(
        self,
    ) -> None:
        # A genuine mixed request (e.g. "the contact and the notice
        # rules for Spain") - different types share a country scope
        # without colliding.
        state = ConversationState(
            actions=[_contact(["ES"]), _legal(["ES"])],
            focus_action_index=None,
        )

        self.assertEqual(len(state.actions), 2)

    def test_accepts_the_same_action_type_for_different_countries(
        self,
    ) -> None:
        state = ConversationState(
            actions=[_legal(["ES"]), _legal(["IT"])],
            focus_action_index=None,
        )

        self.assertEqual(len(state.actions), 2)

    def test_rejects_duplicate_action_scope_even_with_different_topics(
        self,
    ) -> None:
        # The scope key is (type, country_codes) only - a changed
        # topic for the same type+country must replace the action,
        # never sit beside it as a second entry.
        with self.assertRaises(ValidationError):
            ConversationState(
                actions=[
                    _legal(["ES"], ["Termination of Employment Contracts"]),
                    _legal(["ES"], ["Employee Benefits"]),
                ],
                focus_action_index=0,
            )

    def test_focus_action_index_must_be_null_with_no_actions(
        self,
    ) -> None:
        with self.assertRaises(ValidationError):
            ConversationState(actions=[], focus_action_index=0)

    def test_focus_action_index_must_be_zero_with_one_action(
        self,
    ) -> None:
        state = ConversationState(
            actions=[_contact(["ES"])],
            focus_action_index=0,
        )

        self.assertEqual(state.focus_action_index, 0)

    def test_focus_action_index_cannot_be_null_with_one_action(
        self,
    ) -> None:
        with self.assertRaises(ValidationError):
            ConversationState(
                actions=[_contact(["ES"])],
                focus_action_index=None,
            )

    def test_focus_action_index_cannot_be_nonzero_with_one_action(
        self,
    ) -> None:
        with self.assertRaises(ValidationError):
            ConversationState(
                actions=[_contact(["ES"])],
                focus_action_index=1,
            )

    def test_focus_action_index_may_be_null_with_multiple_actions(
        self,
    ) -> None:
        state = ConversationState(
            actions=[_contact(["ES"]), _legal(["IT"])],
            focus_action_index=None,
        )

        self.assertIsNone(state.focus_action_index)

    def test_focus_action_index_may_select_among_multiple_actions(
        self,
    ) -> None:
        state = ConversationState(
            actions=[_contact(["ES"]), _legal(["IT"])],
            focus_action_index=1,
        )

        self.assertEqual(state.focus_action_index, 1)

    def test_focus_action_index_out_of_range_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            ConversationState(
                actions=[_contact(["ES"]), _legal(["IT"])],
                focus_action_index=5,
            )

    def test_focus_action_index_negative_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            ConversationState(
                actions=[_contact(["ES"]), _legal(["IT"])],
                focus_action_index=-1,
            )

    def test_comparison_action_requires_ordered_country_codes(
        self,
    ) -> None:
        with self.assertRaises(ValidationError):
            ConversationState(
                actions=[_comparison(["ES", "IT"])],
                focus_action_index=0,
                ordered_country_codes=[],
            )

    def test_ordered_country_codes_without_a_comparison_is_rejected(
        self,
    ) -> None:
        with self.assertRaises(ValidationError):
            ConversationState(
                actions=[_contact(["ES"])],
                focus_action_index=0,
                ordered_country_codes=["ES"],
            )

    def test_ordered_country_codes_must_match_the_comparison_countries(
        self,
    ) -> None:
        with self.assertRaises(ValidationError):
            ConversationState(
                actions=[_comparison(["ES", "IT"])],
                focus_action_index=0,
                ordered_country_codes=["ES", "BE"],
            )

    def test_ordered_country_codes_may_differ_in_order_from_the_action(
        self,
    ) -> None:
        # Set-equality is all that is required - preserving the
        # user's own comparison order is the point of this field, so
        # it need not match the action's internal list order.
        state = ConversationState(
            actions=[_comparison(["ES", "IT"])],
            focus_action_index=0,
            ordered_country_codes=["IT", "ES"],
        )

        self.assertEqual(state.ordered_country_codes, ["IT", "ES"])

    def test_ordered_country_codes_rejects_two_active_comparisons(
        self,
    ) -> None:
        with self.assertRaises(ValidationError):
            ConversationState(
                actions=[
                    _comparison(["ES", "IT"]),
                    _comparison(["BE", "IT"]),
                ],
                focus_action_index=None,
                ordered_country_codes=["ES", "IT"],
            )

    def test_rejects_more_actions_than_the_maximum(self) -> None:
        with self.assertRaises(ValidationError):
            ConversationState(
                actions=[
                    _legal([country.code])
                    for country in COUNTRIES[: MAX_ACTIONS + 1]
                ],
                focus_action_index=None,
            )

    def test_version_must_be_exactly_one(self) -> None:
        with self.assertRaises(ValidationError):
            ConversationState(version=2)

    def test_rejects_unknown_fields(self) -> None:
        with self.assertRaises(ValidationError):
            ConversationState(extra_field="not allowed")

    def test_rejects_a_state_serializing_past_the_size_ceiling(
        self,
    ) -> None:
        oversized_subject = "a" * MAX_SUBJECT_TEXT_CHARACTERS
        oversized_resolved_question = (
            "b" * MAX_RESOLVED_QUESTION_CHARACTERS
        )
        # Each term must be unique within its own group (case-
        # insensitively), so a numeric suffix distinguishes them while
        # staying at the per-term character ceiling.
        unique_near_max_terms = [
            (
                "c" * (MAX_CONCEPT_TERM_CHARACTERS - 3)
                + f"{index:03d}"
            )
            for index in range(MAX_CONCEPT_TERMS)
        ]

        padding_concepts = [
            ConversationSearchConcept(terms=unique_near_max_terms)
            for _ in range(MAX_SEARCH_CONCEPT_GROUPS)
        ]

        with self.assertRaises(ValidationError):
            ConversationState(
                actions=[
                    ConversationActionState(
                        type="legal_information",
                        country_codes=[country.code],
                        subject_text=oversized_subject,
                        resolved_question=oversized_resolved_question,
                        search_concepts=padding_concepts,
                    )
                    for country in COUNTRIES[:MAX_ACTIONS]
                ],
                focus_action_index=None,
            )

    def test_a_moderately_sized_state_stays_within_the_ceiling(
        self,
    ) -> None:
        state = ConversationState(
            actions=[
                _legal(["ES"]),
                _contact(["IT"]),
            ],
            focus_action_index=None,
        )

        serialized_length = len(
            json.dumps(
                state.model_dump(mode="json"),
                separators=(",", ":"),
            )
        )

        self.assertLess(
            serialized_length,
            MAX_CONVERSATION_STATE_JSON_CHARACTERS,
        )


if __name__ == "__main__":
    unittest.main()
