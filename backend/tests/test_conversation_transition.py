"""
Tests for the deterministic conversation-transition engine (0.4.2).

Exercises only the two public entry points - apply_conversation_transition
and build_next_conversation_state - never the private _apply_transition/
_inherit_action/_build_resolved_question helpers directly, matching this
suite's existing convention (see test_evidence_coverage.py) of testing
through the public surface. Each test's docstring/name references the
module's own RULE numbering where applicable.
"""

from __future__ import annotations

import unittest
from dataclasses import replace
from unittest import mock

from app.models.conversation_state import (
    ConversationActionState,
    ConversationPendingClarification,
    ConversationSearchConcept,
    ConversationState,
)
from app.services.conversation_transition import (
    ConversationTransitionError,
    apply_conversation_transition,
    build_next_conversation_state,
)
from app.services.request_understanding import (
    CurrentMessageDelta,
    DeterministicHints,
    RequestUnderstandingResult,
)
from tests.support.conversation_fixtures import (
    _action_state,
    _delta,
    _hints,
    _result,
    _ru_action,
    _state,
)


class NoConversationStateTests(unittest.TestCase):
    """No conversation_state at all - always a pure passthrough."""

    def test_passes_the_classifier_result_through_unchanged(
        self,
    ) -> None:
        result = _result(
            status="unsupported",
            clarification_reason="unsupported_request",
            is_follow_up=False,
            delta=_delta(context_operation="independent"),
        )

        outcome = apply_conversation_transition(
            result=result,
            conversation_state=None,
            hints=_hints(),
        )

        self.assertEqual(outcome.final_status, "unsupported")
        self.assertEqual(
            outcome.final_clarification_reason,
            "unsupported_request",
        )
        self.assertFalse(outcome.semantic_result_overridden)
        self.assertFalse(outcome.context_inheritance_applied)
        self.assertIsNone(outcome.pending_clarification)


class EmptyPriorActionsTests(unittest.TestCase):
    """conversation_state with zero actions - nothing to reconcile."""

    def test_passes_through_when_conversation_state_has_no_actions(
        self,
    ) -> None:
        result = _result(
            status="resolved",
            actions=[_ru_action("contact", ["ES"])],
            clarification_reason=None,
            delta=_delta(context_operation="independent"),
        )

        outcome = apply_conversation_transition(
            result=result,
            conversation_state=_state([]),
            hints=_hints(),
        )

        self.assertFalse(outcome.semantic_result_overridden)
        self.assertEqual(outcome.final_status, "resolved")
        self.assertEqual(outcome.final_actions[0].type, "contact")


class SingleActionContinuationTests(unittest.TestCase):
    """
    Single prior action, "continue"/"replace_country"/"add_country" -
    the trivial single-active-action country change (defect A).
    """

    def test_continue_keeps_the_same_country_unreplaced(self) -> None:
        previous = _action_state(
            "legal_information",
            ["PE"],
            subject_text="dismissal while on sick leave",
        )

        outcome = apply_conversation_transition(
            result=_result(delta=_delta(context_operation="continue")),
            conversation_state=_state([previous], focus_action_index=0),
            hints=_hints(),
        )

        self.assertTrue(outcome.semantic_result_overridden)
        self.assertEqual(
            outcome.semantic_override_reason,
            "single_active_action_country_continuation",
        )
        self.assertTrue(outcome.context_inheritance_applied)
        self.assertFalse(outcome.inherited_country_replaced)
        self.assertEqual(outcome.final_status, "resolved")

        inherited = outcome.final_actions[0]
        self.assertEqual(inherited.country_codes, ["PE"])
        self.assertEqual(
            inherited.subject_text,
            "dismissal while on sick leave",
        )
        self.assertIn("Peru", inherited.resolved_question)
        self.assertIn(
            "dismissal while on sick leave",
            inherited.resolved_question,
        )

    def test_replace_country_swaps_to_the_new_country(self) -> None:
        previous = _action_state(
            "legal_information",
            ["PE"],
            subject_text="dismissal while on sick leave",
        )

        outcome = apply_conversation_transition(
            result=_result(
                delta=_delta(
                    context_operation="replace_country",
                    explicit_country_codes=["ES"],
                )
            ),
            conversation_state=_state([previous], focus_action_index=0),
            hints=_hints(),
        )

        self.assertTrue(outcome.inherited_country_replaced)
        inherited = outcome.final_actions[0]
        self.assertEqual(inherited.country_codes, ["ES"])
        self.assertIn("Spain", inherited.resolved_question)

    def test_replace_country_with_no_explicit_code_keeps_previous(
        self,
    ) -> None:
        # A defensive edge case: context_operation says
        # "replace_country" but the classifier supplied no explicit
        # code - falls back to the previous country rather than
        # emitting an action with none at all.
        previous = _action_state(
            "legal_information",
            ["PE"],
            subject_text="dismissal while on sick leave",
        )

        outcome = apply_conversation_transition(
            result=_result(
                delta=_delta(context_operation="replace_country")
            ),
            conversation_state=_state([previous], focus_action_index=0),
            hints=_hints(),
        )

        self.assertFalse(outcome.inherited_country_replaced)
        self.assertEqual(
            outcome.final_actions[0].country_codes,
            ["PE"],
        )

    def test_add_country_merges_onto_the_previous_country(self) -> None:
        previous = _action_state(
            "legal_information",
            ["PE"],
            subject_text="dismissal while on sick leave",
        )

        outcome = apply_conversation_transition(
            result=_result(
                delta=_delta(
                    context_operation="add_country",
                    explicit_country_codes=["ES"],
                )
            ),
            conversation_state=_state([previous], focus_action_index=0),
            hints=_hints(),
        )

        self.assertTrue(outcome.inherited_country_replaced)
        self.assertEqual(
            outcome.final_actions[0].country_codes,
            ["PE", "ES"],
        )

    def test_add_country_is_idempotent_for_an_already_present_country(
        self,
    ) -> None:
        previous = _action_state(
            "legal_information",
            ["PE"],
            subject_text="dismissal while on sick leave",
        )

        outcome = apply_conversation_transition(
            result=_result(
                delta=_delta(
                    context_operation="add_country",
                    explicit_country_codes=["PE"],
                )
            ),
            conversation_state=_state([previous], focus_action_index=0),
            hints=_hints(),
        )

        self.assertEqual(
            outcome.final_actions[0].country_codes,
            ["PE"],
        )

    def test_comparison_continue_keeps_both_countries(self) -> None:
        previous = _action_state(
            "comparison",
            ["PE", "ES"],
            legal_topics=["Termination of Employment Contracts"],
        )

        outcome = apply_conversation_transition(
            result=_result(delta=_delta(context_operation="continue")),
            conversation_state=_state(
                [previous],
                focus_action_index=0,
                ordered_country_codes=["PE", "ES"],
            ),
            hints=_hints(),
        )

        self.assertEqual(
            outcome.final_actions[0].country_codes,
            ["PE", "ES"],
        )
        self.assertFalse(outcome.inherited_country_replaced)

    def test_comparison_add_country_grows_to_three_countries(
        self,
    ) -> None:
        previous = _action_state(
            "comparison",
            ["PE", "ES"],
            legal_topics=["Termination of Employment Contracts"],
        )

        outcome = apply_conversation_transition(
            result=_result(
                delta=_delta(
                    context_operation="add_country",
                    explicit_country_codes=["IT"],
                )
            ),
            conversation_state=_state(
                [previous],
                focus_action_index=0,
                ordered_country_codes=["PE", "ES"],
            ),
            hints=_hints(),
        )

        self.assertEqual(
            outcome.final_actions[0].country_codes,
            ["PE", "ES", "IT"],
        )

    def test_comparison_cannot_be_replaced_down_to_one_country(
        self,
    ) -> None:
        # RULE: a comparison can never be inherited down to fewer than
        # two countries - falls through to the classifier's own
        # result instead of emitting an invalid one-country comparison.
        previous = _action_state(
            "comparison",
            ["PE", "ES"],
            legal_topics=["Termination of Employment Contracts"],
        )

        classifier_result = _result(
            status="clarification",
            clarification_reason="missing_comparison_countries",
            delta=_delta(
                context_operation="replace_country",
                explicit_country_codes=["IT"],
            ),
        )

        outcome = apply_conversation_transition(
            result=classifier_result,
            conversation_state=_state(
                [previous],
                focus_action_index=0,
                ordered_country_codes=["PE", "ES"],
            ),
            hints=_hints(),
        )

        self.assertFalse(outcome.semantic_result_overridden)
        self.assertEqual(
            outcome.final_clarification_reason,
            "missing_comparison_countries",
        )

    def test_a_genuinely_new_action_never_inherits(self) -> None:
        # unambiguous_single_intent is False whenever the delta names
        # a new action type - the engine must defer entirely to the
        # classifier's own (already-correct) resolved result.
        previous = _action_state(
            "legal_information",
            ["PE"],
            subject_text="dismissal while on sick leave",
        )

        classifier_result = _result(
            status="resolved",
            actions=[_ru_action("contact", ["ES"])],
            clarification_reason=None,
            delta=_delta(
                context_operation="change_action",
                explicit_action_types=["contact"],
                explicit_country_codes=["ES"],
            ),
        )

        outcome = apply_conversation_transition(
            result=classifier_result,
            conversation_state=_state([previous], focus_action_index=0),
            hints=_hints(),
        )

        self.assertFalse(outcome.semantic_result_overridden)
        self.assertEqual(outcome.final_actions[0].type, "contact")

    def test_a_genuinely_new_subject_never_inherits(self) -> None:
        previous = _action_state(
            "legal_information",
            ["PE"],
            subject_text="dismissal while on sick leave",
        )

        classifier_result = _result(
            status="resolved",
            actions=[
                _ru_action(
                    "legal_information",
                    ["PE"],
                    legal_topics=["Employee Benefits"],
                )
            ],
            clarification_reason=None,
            delta=_delta(
                context_operation="change_subject",
                explicit_legal_topics=["Employee Benefits"],
                explicit_subject_text="parental leave entitlement",
            ),
        )

        outcome = apply_conversation_transition(
            result=classifier_result,
            conversation_state=_state([previous], focus_action_index=0),
            hints=_hints(),
        )

        self.assertFalse(outcome.semantic_result_overridden)

    def test_a_strong_contact_signal_never_inherits(self) -> None:
        previous = _action_state(
            "legal_information",
            ["PE"],
            subject_text="dismissal while on sick leave",
        )

        classifier_result = _result(
            status="resolved",
            actions=[_ru_action("contact", ["PE"])],
            clarification_reason=None,
            delta=_delta(context_operation="continue"),
        )

        outcome = apply_conversation_transition(
            result=classifier_result,
            conversation_state=_state([previous], focus_action_index=0),
            hints=_hints(strong_contact_signal=True),
        )

        self.assertFalse(outcome.semantic_result_overridden)

    def test_a_comparison_signal_never_inherits(self) -> None:
        previous = _action_state(
            "legal_information",
            ["PE"],
            subject_text="dismissal while on sick leave",
        )

        classifier_result = _result(
            status="resolved",
            actions=[
                _ru_action(
                    "comparison",
                    ["PE", "ES"],
                    legal_topics=[
                        "Termination of Employment Contracts"
                    ],
                )
            ],
            clarification_reason=None,
            delta=_delta(context_operation="continue"),
        )

        outcome = apply_conversation_transition(
            result=classifier_result,
            conversation_state=_state([previous], focus_action_index=0),
            hints=_hints(comparison_signal=True),
        )

        self.assertFalse(outcome.semantic_result_overridden)



class ContextualLegalFollowupHardeningTests(unittest.TestCase):
    """Real-user follow-up failures found during the live canary."""

    def test_ambiguous_followup_keeps_single_legal_context_even_if_model_claims_new_action(
        self,
    ) -> None:
        previous = _action_state(
            "legal_information",
            ["AU"],
            legal_topics=["Termination of Employment Contracts"],
            subject_text="notice period required when dismissing an employee",
        )

        classifier_result = _result(
            status="clarification",
            actions=[_ru_action("contact", [])],
            clarification_reason="ambiguous_request",
            delta=_delta(
                context_operation="ambiguous",
                explicit_action_types=["contact"],
            ),
            is_follow_up=True,
        )

        outcome = apply_conversation_transition(
            result=classifier_result,
            conversation_state=_state(
                [previous],
                focus_action_index=0,
            ),
            hints=_hints(strong_contact_signal=False),
            current_question="What if the employee refuses?",
        )

        self.assertEqual(outcome.final_status, "clarification")
        self.assertTrue(outcome.semantic_result_overridden)
        self.assertEqual(
            outcome.semantic_override_reason,
            "single_active_legal_context_clarification",
        )
        self.assertIn(
            "Australia",
            outcome.contextual_clarification_answer,
        )
        self.assertEqual(
            outcome.inherited_action_type,
            "legal_information",
        )

    def test_real_contact_signal_is_never_hijacked_by_legal_context(
        self,
    ) -> None:
        previous = _action_state(
            "legal_information",
            ["AU"],
            subject_text="notice period",
        )

        classifier_result = _result(
            status="clarification",
            actions=[_ru_action("contact", [])],
            clarification_reason="ambiguous_request",
            delta=_delta(
                context_operation="ambiguous",
                explicit_action_types=["contact"],
            ),
            is_follow_up=True,
        )

        outcome = apply_conversation_transition(
            result=classifier_result,
            conversation_state=_state(
                [previous],
                focus_action_index=0,
            ),
            hints=_hints(strong_contact_signal=True),
            current_question="Give me the L&E Global contact.",
        )

        self.assertFalse(outcome.semantic_result_overridden)

    def test_continue_followup_keeps_subject_but_answers_current_question(
        self,
    ) -> None:
        previous = _action_state(
            "legal_information",
            ["AU"],
            legal_topics=["Termination of Employment Contracts"],
            subject_text="notice period required when dismissing an employee",
        )

        outcome = apply_conversation_transition(
            result=_result(
                status="resolved",
                actions=[
                    _ru_action(
                        "legal_information",
                        ["AU"],
                        legal_topics=[
                            "Termination of Employment Contracts"
                        ],
                    )
                ],
                clarification_reason=None,
                delta=_delta(context_operation="continue"),
                is_follow_up=True,
            ),
            conversation_state=_state(
                [previous],
                focus_action_index=0,
            ),
            hints=_hints(),
            current_question="Why?",
        )

        self.assertEqual(outcome.final_status, "resolved")

        action = outcome.final_actions[0]

        self.assertEqual(action.country_codes, ["AU"])
        self.assertEqual(
            action.subject_text,
            "notice period required when dismissing an employee",
        )
        self.assertIn("Why?", action.resolved_question)
        self.assertIn("Australia", action.resolved_question)
        self.assertIn("notice period", action.resolved_question)


class SelectActionTests(unittest.TestCase):
    """context_operation="select_action" against a single prior action."""

    def test_select_action_behaves_like_replace_country(self) -> None:
        previous = _action_state(
            "contact",
            ["PE"],
        )

        outcome = apply_conversation_transition(
            result=_result(
                delta=_delta(
                    context_operation="select_action",
                    explicit_country_codes=["ES"],
                )
            ),
            conversation_state=_state([previous], focus_action_index=0),
            hints=_hints(),
        )

        self.assertTrue(outcome.context_inheritance_applied)
        self.assertEqual(
            outcome.final_actions[0].country_codes,
            ["ES"],
        )
        self.assertEqual(outcome.final_actions[0].type, "contact")


class AmbiguousCountryReferenceTests(unittest.TestCase):
    """
    RULE 9 / defect F: a single active action naming more than one
    candidate country - a contextual "Do you mean X or Y?", never the
    generic missing_country wording.
    """

    def test_ambiguous_after_a_comparison_asks_about_its_countries(
        self,
    ) -> None:
        previous = _action_state(
            "comparison",
            ["PE", "ES"],
            legal_topics=["Termination of Employment Contracts"],
        )

        outcome = apply_conversation_transition(
            result=_result(delta=_delta(context_operation="ambiguous")),
            conversation_state=_state(
                [previous],
                focus_action_index=0,
                ordered_country_codes=["PE", "ES"],
            ),
            hints=_hints(),
        )

        self.assertEqual(outcome.final_status, "clarification")
        self.assertEqual(
            outcome.final_clarification_reason,
            "ambiguous_reference",
        )
        self.assertEqual(
            outcome.contextual_clarification_answer,
            "Do you mean the information in Peru or in Spain?",
        )
        self.assertEqual(
            outcome.pending_clarification.candidate_country_codes,
            ["PE", "ES"],
        )

    def test_ambiguous_after_a_multi_country_legal_action(self) -> None:
        # A prior single legal_information action can itself carry
        # more than one country (e.g. "the same question for Peru and
        # Spain" without being framed as a "comparison"). Unlike a
        # comparison action, there is no ordered_country_codes to
        # preserve here, so the candidates fall back to alphabetical
        # order (ES before PE) rather than the order they were named.
        previous = _action_state(
            "legal_information",
            ["PE", "ES"],
            subject_text="dismissal while on sick leave",
        )

        outcome = apply_conversation_transition(
            result=_result(delta=_delta(context_operation="ambiguous")),
            conversation_state=_state([previous], focus_action_index=0),
            hints=_hints(),
        )

        self.assertEqual(
            outcome.contextual_clarification_answer,
            "Do you mean the information in Spain or in Peru?",
        )

    def test_a_new_action_named_against_an_ambiguous_prior_asks_using_it(
        self,
    ) -> None:
        # defect F's own worked example: after a Peru/Spain
        # comparison, "give me the local contact" must ask about
        # Peru/Spain using the word "contact" - never the old action's
        # own type.
        previous = _action_state(
            "comparison",
            ["PE", "ES"],
            legal_topics=["Termination of Employment Contracts"],
        )

        classifier_result = _result(
            status="clarification",
            clarification_reason="missing_country",
            delta=_delta(
                context_operation="select_action",
                explicit_action_types=["contact"],
            ),
        )

        outcome = apply_conversation_transition(
            result=classifier_result,
            conversation_state=_state(
                [previous],
                focus_action_index=0,
                ordered_country_codes=["PE", "ES"],
            ),
            hints=_hints(),
        )

        self.assertEqual(
            outcome.contextual_clarification_answer,
            "Do you mean the contact in Peru or in Spain?",
        )
        self.assertEqual(
            outcome.pending_clarification.candidate_action_types,
            ["contact"],
        )

    def test_unrelated_operation_with_a_single_action_passes_through(
        self,
    ) -> None:
        previous = _action_state(
            "legal_information",
            ["PE"],
            subject_text="dismissal while on sick leave",
        )

        classifier_result = _result(
            status="unsupported",
            clarification_reason="unsupported_request",
            delta=_delta(context_operation="independent"),
        )

        outcome = apply_conversation_transition(
            result=classifier_result,
            conversation_state=_state([previous], focus_action_index=0),
            hints=_hints(),
        )

        self.assertFalse(outcome.semantic_result_overridden)
        self.assertEqual(outcome.final_status, "unsupported")


class MultiActionPriorStateTests(unittest.TestCase):
    """conversation_state names more than one action (RULE 5/9)."""

    def test_explicit_selection_of_one_of_two_actions_inherits_it(
        self,
    ) -> None:
        contact_action = _action_state("contact", ["PE"])
        legal_action = _action_state(
            "legal_information",
            ["PE"],
            subject_text="dismissal while on sick leave",
        )

        outcome = apply_conversation_transition(
            result=_result(
                delta=_delta(
                    context_operation="select_action",
                    explicit_action_types=["contact"],
                )
            ),
            conversation_state=_state(
                [contact_action, legal_action],
                focus_action_index=None,
            ),
            hints=_hints(),
        )

        self.assertTrue(outcome.semantic_result_overridden)
        self.assertEqual(
            outcome.semantic_override_reason,
            "multi_action_context_explicit_selection",
        )
        self.assertEqual(outcome.final_actions[0].type, "contact")
        self.assertEqual(
            outcome.final_actions[0].country_codes,
            ["PE"],
        )

    def test_explicit_selection_can_override_the_selected_countries(
        self,
    ) -> None:
        contact_action = _action_state("contact", ["PE"])
        legal_action = _action_state(
            "legal_information",
            ["PE"],
            subject_text="dismissal while on sick leave",
        )

        outcome = apply_conversation_transition(
            result=_result(
                delta=_delta(
                    context_operation="select_action",
                    explicit_action_types=["contact"],
                    explicit_country_codes=["ES"],
                )
            ),
            conversation_state=_state(
                [contact_action, legal_action],
                focus_action_index=None,
            ),
            hints=_hints(),
        )

        self.assertEqual(
            outcome.final_actions[0].country_codes,
            ["ES"],
        )

    def test_ambiguous_type_match_against_two_same_type_actions_defers(
        self,
    ) -> None:
        # Two prior contact actions for different countries: naming
        # "contact" alone doesn't disambiguate which one, so the
        # engine must not guess.
        outcome = apply_conversation_transition(
            result=_result(
                status="resolved",
                actions=[_ru_action("contact", ["PE"])],
                clarification_reason=None,
                delta=_delta(
                    context_operation="select_action",
                    explicit_action_types=["contact"],
                ),
            ),
            conversation_state=_state(
                [
                    _action_state("contact", ["PE"]),
                    _action_state("contact", ["ES"]),
                ],
                focus_action_index=None,
            ),
            hints=_hints(),
        )

        self.assertFalse(outcome.semantic_result_overridden)

    def test_a_genuinely_new_action_type_defers_to_the_classifier(
        self,
    ) -> None:
        contact_action = _action_state("contact", ["PE"])
        legal_action = _action_state(
            "legal_information",
            ["PE"],
            subject_text="dismissal while on sick leave",
        )

        classifier_result = _result(
            status="resolved",
            actions=[
                _ru_action(
                    "comparison",
                    ["PE", "ES"],
                    legal_topics=[
                        "Termination of Employment Contracts"
                    ],
                )
            ],
            clarification_reason=None,
            delta=_delta(
                context_operation="change_action",
                explicit_action_types=["comparison"],
                explicit_country_codes=["PE", "ES"],
            ),
        )

        outcome = apply_conversation_transition(
            result=classifier_result,
            conversation_state=_state(
                [contact_action, legal_action],
                focus_action_index=None,
            ),
            hints=_hints(),
        )

        self.assertFalse(outcome.semantic_result_overridden)
        self.assertEqual(outcome.final_actions[0].type, "comparison")

    def test_a_new_subject_against_a_multi_action_state_defers(
        self,
    ) -> None:
        contact_action = _action_state("contact", ["PE"])
        legal_action = _action_state(
            "legal_information",
            ["PE"],
            subject_text="dismissal while on sick leave",
        )

        classifier_result = _result(
            status="resolved",
            actions=[
                _ru_action(
                    "legal_information",
                    ["PE"],
                    legal_topics=["Employee Benefits"],
                )
            ],
            clarification_reason=None,
            delta=_delta(
                context_operation="change_subject",
                explicit_subject_text="parental leave entitlement",
            ),
        )

        outcome = apply_conversation_transition(
            result=classifier_result,
            conversation_state=_state(
                [contact_action, legal_action],
                focus_action_index=None,
            ),
            hints=_hints(),
        )

        self.assertFalse(outcome.semantic_result_overridden)

    def test_no_selection_at_all_asks_which_of_the_two_actions(
        self,
    ) -> None:
        contact_action = _action_state("contact", ["PE"])
        legal_action = _action_state(
            "legal_information",
            ["PE"],
            subject_text="dismissal while on sick leave",
        )

        outcome = apply_conversation_transition(
            result=_result(delta=_delta(context_operation="ambiguous")),
            conversation_state=_state(
                [contact_action, legal_action],
                focus_action_index=None,
            ),
            hints=_hints(),
        )

        self.assertEqual(outcome.final_status, "clarification")
        self.assertEqual(
            outcome.final_clarification_reason,
            "select_action",
        )
        self.assertEqual(
            outcome.contextual_clarification_answer,
            "Would you like the local member firm contact, the "
            "dismissal while on sick leave, or both?",
        )

    def test_no_selection_with_three_actions_lists_all_of_them(
        self,
    ) -> None:
        outcome = apply_conversation_transition(
            result=_result(delta=_delta(context_operation="ambiguous")),
            conversation_state=_state(
                [
                    _action_state("contact", ["PE"]),
                    _action_state(
                        "legal_information",
                        ["ES"],
                        subject_text="parental leave entitlement",
                    ),
                    _action_state(
                        "comparison",
                        ["PE", "ES"],
                        legal_topics=[
                            "Termination of Employment Contracts"
                        ],
                        # A real persisted state always populates
                        # subject_text for a non-contact action (via
                        # effective_subject_text()'s legal_topics
                        # fallback in build_next_conversation_state) -
                        # set explicitly here since this fixture
                        # bypasses that step.
                        subject_text=(
                            "Termination of Employment Contracts"
                        ),
                    ),
                ],
                focus_action_index=None,
                ordered_country_codes=["PE", "ES"],
            ),
            hints=_hints(),
        )

        self.assertEqual(
            outcome.contextual_clarification_answer,
            "Would you like the local member firm contact, the "
            "parental leave entitlement, the Termination of "
            "Employment Contracts, or all of them?",
        )

    def test_a_bare_country_mention_is_attached_to_the_first_label(
        self,
    ) -> None:
        contact_action = _action_state("contact", ["PE"])
        legal_action = _action_state(
            "legal_information",
            ["ES"],
            subject_text="dismissal while on sick leave",
        )

        outcome = apply_conversation_transition(
            result=_result(
                delta=_delta(
                    context_operation="ambiguous",
                    explicit_country_codes=["PE"],
                )
            ),
            conversation_state=_state(
                [contact_action, legal_action],
                focus_action_index=None,
            ),
            hints=_hints(),
        )

        self.assertEqual(
            outcome.contextual_clarification_answer,
            "Would you like the local member firm contact for Peru, "
            "the dismissal while on sick leave, or both?",
        )


class TransitionEngineNeverCrashesTests(unittest.TestCase):
    """
    RULE 8, hardened (0.4.2 durcissement): an unexpected internal error
    must never be silently swallowed into trusting the classifier's own
    raw result - that could mean acting on a country/subject this
    engine exists specifically to correct. It now raises
    ConversationTransitionError instead (converted to a controlled
    HTTP 502 by the router - see routers/chat.py), never a fabricated
    passthrough result. Every explicitly modeled, safe case (no
    conversation_state at all, a comparison that cannot be inherited
    below two countries, and so on) is unaffected - those return their
    own defined TransitionOutcome directly, with no exception raised.
    """

    def test_an_unexpected_internal_error_raises_transition_error(
        self,
    ) -> None:
        previous = _action_state(
            "legal_information",
            ["PE"],
            subject_text="dismissal while on sick leave",
        )

        classifier_result = _result(
            status="resolved",
            actions=[_ru_action("contact", ["PE"])],
            clarification_reason=None,
            delta=_delta(context_operation="continue"),
        )

        with mock.patch(
            "app.services.conversation_transition."
            "resolve_country_display_name",
            side_effect=RuntimeError("unexpected"),
        ):
            with self.assertRaises(ConversationTransitionError):
                apply_conversation_transition(
                    result=classifier_result,
                    conversation_state=_state(
                        [previous], focus_action_index=0
                    ),
                    hints=_hints(),
                )

    def test_no_conversation_state_is_not_an_error(self) -> None:
        # An explicitly modeled, safe case - never raises, regardless
        # of the hardened policy above.
        classifier_result = _result(
            status="resolved",
            actions=[_ru_action("contact", ["PE"])],
            clarification_reason=None,
            delta=_delta(context_operation="independent"),
        )

        outcome = apply_conversation_transition(
            result=classifier_result,
            conversation_state=None,
            hints=_hints(),
        )

        self.assertFalse(outcome.semantic_result_overridden)
        self.assertEqual(outcome.final_actions[0].type, "contact")

    def test_a_comparison_uninheritable_below_two_countries_is_not_an_error(
        self,
    ) -> None:
        # Another explicitly modeled, safe case (RULE 3's own guard) -
        # falls through to the classifier's own result directly, with
        # no exception raised at all.
        previous = _action_state(
            "comparison",
            ["PE", "ES"],
            legal_topics=["Termination of Employment Contracts"],
        )

        classifier_result = _result(
            status="clarification",
            clarification_reason="missing_comparison_countries",
            delta=_delta(
                context_operation="replace_country",
                explicit_country_codes=["IT"],
            ),
        )

        outcome = apply_conversation_transition(
            result=classifier_result,
            conversation_state=_state(
                [previous],
                focus_action_index=0,
                ordered_country_codes=["PE", "ES"],
            ),
            hints=_hints(),
        )

        self.assertFalse(outcome.semantic_result_overridden)
        self.assertEqual(
            outcome.final_clarification_reason,
            "missing_comparison_countries",
        )


class BuildNextConversationStateTests(unittest.TestCase):
    def test_a_pending_clarification_ignores_any_executed_actions(
        self,
    ) -> None:
        clarification = ConversationPendingClarification(
            reason="select_country",
            candidate_country_codes=["PE", "ES"],
        )

        state = build_next_conversation_state(
            executed=[
                (_ru_action("contact", ["PE"]), ["PE"]),
            ],
            pending_clarification=clarification,
        )

        self.assertEqual(state.actions, [])
        self.assertIsNone(state.focus_action_index)
        self.assertEqual(state.pending_clarification, clarification)

    def test_nothing_executed_and_no_clarification_returns_none(
        self,
    ) -> None:
        self.assertIsNone(
            build_next_conversation_state(executed=[])
        )

    def test_an_action_filtered_down_to_no_countries_is_dropped(
        self,
    ) -> None:
        state = build_next_conversation_state(
            executed=[
                (_ru_action("contact", ["PE", "XX"]), []),
            ],
        )

        self.assertIsNone(state)

    def test_a_legal_information_action_carries_its_subject_forward(
        self,
    ) -> None:
        action = _ru_action(
            "legal_information",
            ["PE"],
            subject_text="dismissal while on sick leave",
            subject_specificity="specific",
        )

        state = build_next_conversation_state(
            executed=[(action, ["PE"])],
        )

        self.assertEqual(len(state.actions), 1)
        built_action = state.actions[0]
        self.assertEqual(built_action.type, "legal_information")
        self.assertEqual(built_action.country_codes, ["PE"])
        self.assertEqual(
            built_action.subject_text,
            "dismissal while on sick leave",
        )
        self.assertEqual(built_action.subject_specificity, "specific")
        self.assertEqual(state.focus_action_index, 0)

    def test_evidence_mode_is_inferred_from_search_concept_count(
        self,
    ) -> None:
        action = _ru_action(
            "legal_information",
            ["PE"],
            subject_text="dismissal while on sick leave",
            search_concepts=[
                {"terms": ["dismissal", "termination"]},
                {"terms": ["sick leave", "medical leave"]},
            ],
        )

        state = build_next_conversation_state(
            executed=[(action, ["PE"])],
        )

        self.assertEqual(
            state.actions[0].evidence_mode,
            "relation_required",
        )
        self.assertEqual(
            [
                concept.terms
                for concept in state.actions[0].search_concepts
            ],
            [["dismissal", "termination"], ["sick leave", "medical leave"]],
        )

    def test_a_contact_action_never_carries_legal_fields(self) -> None:
        action = _ru_action("contact", ["PE"])

        state = build_next_conversation_state(
            executed=[(action, ["PE"])],
        )

        built_action = state.actions[0]
        self.assertIsNone(built_action.subject_text)
        self.assertIsNone(built_action.subject_specificity)
        self.assertIsNone(built_action.evidence_mode)
        self.assertEqual(built_action.search_concepts, [])

    def test_a_comparison_action_sets_ordered_country_codes(
        self,
    ) -> None:
        action = _ru_action(
            "comparison",
            ["PE", "ES"],
            legal_topics=["Termination of Employment Contracts"],
        )

        state = build_next_conversation_state(
            executed=[(action, ["PE", "ES"])],
        )

        self.assertEqual(
            state.ordered_country_codes,
            ["PE", "ES"],
        )
        self.assertEqual(state.focus_action_index, 0)

    def test_two_executed_actions_leave_focus_action_index_null(
        self,
    ) -> None:
        state = build_next_conversation_state(
            executed=[
                (_ru_action("contact", ["PE"]), ["PE"]),
                (
                    _ru_action(
                        "legal_information",
                        ["PE"],
                        subject_text="dismissal while on sick leave",
                    ),
                    ["PE"],
                ),
            ],
        )

        self.assertEqual(len(state.actions), 2)
        self.assertIsNone(state.focus_action_index)

    def test_actual_country_codes_are_used_over_the_actions_own(
        self,
    ) -> None:
        # Simulates post-availability-filtering: the classifier named
        # two countries but only one was actually available/searched.
        action = _ru_action(
            "legal_information",
            ["PE", "XX"],
            subject_text="dismissal while on sick leave",
        )

        state = build_next_conversation_state(
            executed=[(action, ["PE"])],
        )

        self.assertEqual(state.actions[0].country_codes, ["PE"])


class JurisdictionNeutralInheritanceTests(unittest.TestCase):
    """
    Mission "DECOUPLAGE COMPLET DU SUJET JURIDIQUE ET DE LA
    JURIDICTION", Phase 17: CAS 1-8. Each reproduces the exact prior-
    state/message pair the mission specifies and checks the inherited
    action's subject_text is fully jurisdiction-neutral - the old
    country never survives into the new country's action.
    """

    def test_cas1_remote_work_spain_to_peru(self) -> None:
        previous = _action_state(
            "legal_information",
            ["ES"],
            subject_text="rules on remote work in Spain",
            legal_topics=["Working Conditions"],
        )

        outcome = apply_conversation_transition(
            result=_result(
                status="resolved",
                clarification_reason=None,
                actions=[
                    _ru_action(
                        "legal_information",
                        ["PE"],
                        legal_topics=["Working Conditions"],
                    )
                ],
                delta=_delta(
                    context_operation="replace_country",
                    explicit_country_codes=["PE"],
                ),
                is_follow_up=True,
            ),
            conversation_state=_state([previous], focus_action_index=0),
            hints=_hints(),
        )

        action = outcome.final_actions[0]
        self.assertEqual(action.country_codes, ["PE"])
        self.assertEqual(action.subject_text, "rules on remote work")
        self.assertNotIn("Spain", action.subject_text)

    def test_cas2_notice_spain_to_australia(self) -> None:
        previous = _action_state(
            "legal_information",
            ["ES"],
            subject_text=(
                "notice an employer must give when dismissing an "
                "employee in Spain"
            ),
            legal_topics=["Termination of Employment Contracts"],
        )

        outcome = apply_conversation_transition(
            result=_result(
                status="resolved",
                clarification_reason=None,
                actions=[
                    _ru_action(
                        "legal_information",
                        ["AU"],
                        legal_topics=[
                            "Termination of Employment Contracts"
                        ],
                    )
                ],
                delta=_delta(
                    context_operation="replace_country",
                    explicit_country_codes=["AU"],
                ),
                is_follow_up=True,
            ),
            conversation_state=_state([previous], focus_action_index=0),
            hints=_hints(),
        )

        action = outcome.final_actions[0]
        self.assertEqual(action.country_codes, ["AU"])
        self.assertEqual(
            action.subject_text,
            "notice an employer must give when dismissing an employee",
        )

    def test_cas3_sick_leave_dismissal_spain_to_peru_same_relation(
        self,
    ) -> None:
        previous = _action_state(
            "legal_information",
            ["ES"],
            subject_text=(
                "whether an employer may dismiss an employee on sick "
                "leave in Spain"
            ),
            legal_topics=["Termination of Employment Contracts"],
            evidence_mode="relation_required",
            search_concepts=[
                {"terms": ["dismiss", "dismissal"]},
                {"terms": ["sick leave"]},
            ],
        )

        outcome = apply_conversation_transition(
            result=_result(
                status="resolved",
                clarification_reason=None,
                actions=[
                    _ru_action(
                        "legal_information",
                        ["PE"],
                        legal_topics=[
                            "Termination of Employment Contracts"
                        ],
                    )
                ],
                delta=_delta(
                    context_operation="replace_country",
                    explicit_country_codes=["PE"],
                ),
                is_follow_up=True,
            ),
            conversation_state=_state([previous], focus_action_index=0),
            hints=_hints(),
        )

        action = outcome.final_actions[0]
        self.assertEqual(action.country_codes, ["PE"])
        self.assertEqual(
            action.subject_text,
            "whether an employer may dismiss an employee on sick leave",
        )
        self.assertEqual(action.evidence_mode, "relation_required")

    def test_cas4_fixed_term_uk_to_australia(self) -> None:
        previous = _action_state(
            "legal_information",
            ["GB"],
            subject_text=(
                "fixed-term employment contracts in the United Kingdom"
            ),
            legal_topics=["Employment Contracts"],
        )

        outcome = apply_conversation_transition(
            result=_result(
                status="resolved",
                clarification_reason=None,
                actions=[
                    _ru_action(
                        "legal_information",
                        ["AU"],
                        legal_topics=["Employment Contracts"],
                    )
                ],
                delta=_delta(
                    context_operation="replace_country",
                    explicit_country_codes=["AU"],
                ),
                is_follow_up=True,
            ),
            conversation_state=_state([previous], focus_action_index=0),
            hints=_hints(),
        )

        action = outcome.final_actions[0]
        self.assertEqual(action.country_codes, ["AU"])
        self.assertEqual(
            action.subject_text, "fixed-term employment contracts"
        )

    def test_cas5_overtime_spain_to_peru(self) -> None:
        previous = _action_state(
            "legal_information",
            ["ES"],
            subject_text="overtime rules in Spain",
            legal_topics=["Working Conditions"],
        )

        outcome = apply_conversation_transition(
            result=_result(
                status="resolved",
                clarification_reason=None,
                actions=[
                    _ru_action(
                        "legal_information",
                        ["PE"],
                        legal_topics=["Working Conditions"],
                    )
                ],
                delta=_delta(
                    context_operation="replace_country",
                    explicit_country_codes=["PE"],
                ),
                is_follow_up=True,
            ),
            conversation_state=_state([previous], focus_action_index=0),
            hints=_hints(),
        )

        action = outcome.final_actions[0]
        self.assertEqual(action.country_codes, ["PE"])
        self.assertEqual(action.subject_text, "overtime rules")

    def test_cas6_contact_spain_to_peru_no_regression(self) -> None:
        previous = _action_state("contact", ["ES"])

        outcome = apply_conversation_transition(
            result=_result(
                status="resolved",
                clarification_reason=None,
                actions=[_ru_action("contact", ["PE"])],
                delta=_delta(
                    context_operation="replace_country",
                    explicit_country_codes=["PE"],
                ),
                is_follow_up=True,
            ),
            conversation_state=_state([previous], focus_action_index=0),
            hints=_hints(),
        )

        action = outcome.final_actions[0]
        self.assertEqual(action.type, "contact")
        self.assertEqual(action.country_codes, ["PE"])
        self.assertIsNone(action.subject_text)

    def test_cas7_comparison_add_australia(self) -> None:
        previous = _action_state(
            "comparison",
            ["ES", "PE"],
            subject_text="overtime rules in Spain and Peru",
            legal_topics=["Working Conditions"],
        )

        outcome = apply_conversation_transition(
            result=_result(
                status="resolved",
                clarification_reason=None,
                actions=[
                    _ru_action(
                        "comparison",
                        ["ES", "PE", "AU"],
                        legal_topics=["Working Conditions"],
                    )
                ],
                delta=_delta(
                    context_operation="add_country",
                    explicit_country_codes=["AU"],
                ),
                is_follow_up=True,
            ),
            conversation_state=_state(
                [previous],
                focus_action_index=0,
                ordered_country_codes=["ES", "PE"],
            ),
            hints=_hints(),
        )

        action = outcome.final_actions[0]
        self.assertEqual(action.country_codes, ["ES", "PE", "AU"])
        self.assertEqual(action.subject_text, "overtime rules")

    def test_cas8_multi_action_state_country_only_never_contaminates(
        self,
    ) -> None:
        legal_action = _action_state(
            "legal_information",
            ["ES"],
            subject_text="overtime rules in Spain",
            legal_topics=["Working Conditions"],
        )
        contact_action = _action_state("contact", ["ES", "PE"])

        outcome = apply_conversation_transition(
            result=_result(
                status="resolved",
                clarification_reason=None,
                actions=[
                    _ru_action(
                        "legal_information",
                        ["AU"],
                        legal_topics=["Working Conditions"],
                    )
                ],
                delta=_delta(
                    context_operation="replace_country",
                    explicit_country_codes=["AU"],
                ),
                is_follow_up=True,
            ),
            conversation_state=_state(
                [legal_action, contact_action],
                focus_action_index=None,
            ),
            hints=_hints(),
        )

        # More than one prior action and no explicit action type named
        # in this message - the engine must never guess which action a
        # bare country belongs to (RULE 5) - but whatever it does
        # decide, no action's subject_text may ever contain "Spain".
        for action in outcome.final_actions:
            self.assertNotIn("Spain", action.subject_text or "")

    def test_cas9_country_swap_emptying_all_concepts_rebuilds_from_subject(
        self,
    ) -> None:
        # A concept group whose every term is purely geographic (the
        # country's own name, no other legal content) is dropped
        # entirely during canonicalization even though subject_text
        # itself survives untouched. A precise subject must never be
        # broadened just because its concepts disappeared here -
        # search_concepts is rebuilt directly from the surviving
        # canonical subject_text, and subject_specificity/
        # evidence_mode stay exactly as they were (mission "MISSION
        # EXPRESS BLOQUANTE 0.4.2", Regle B).
        previous = _action_state(
            "legal_information",
            ["ES"],
            subject_text="overtime rules",
            legal_topics=["Working Conditions"],
            search_concepts=[ConversationSearchConcept(terms=["Spain"])],
            subject_specificity="specific",
            evidence_mode="direct_topic",
        )

        outcome = apply_conversation_transition(
            result=_result(
                status="resolved",
                clarification_reason=None,
                actions=[
                    _ru_action(
                        "legal_information",
                        ["PE"],
                        legal_topics=["Working Conditions"],
                    )
                ],
                delta=_delta(
                    context_operation="replace_country",
                    explicit_country_codes=["PE"],
                ),
                is_follow_up=True,
            ),
            conversation_state=_state([previous], focus_action_index=0),
            hints=_hints(),
        )

        action = outcome.final_actions[0]
        self.assertEqual(action.country_codes, ["PE"])
        self.assertEqual(action.subject_text, "overtime rules")
        # The purely-geographic concept group is gone, but it is
        # rebuilt from the surviving subject_text - never left empty,
        # never invented as a new synonym.
        self.assertEqual(
            [concept.terms for concept in action.search_concepts],
            [["overtime rules"]],
        )
        # Never weakened - the precise labeling survives untouched.
        self.assertEqual(action.evidence_mode, "direct_topic")
        self.assertEqual(action.subject_specificity, "specific")



class LegalChallengeFollowupR5Tests(unittest.TestCase):

    def test_just_say_yes_keeps_existing_legal_context(
        self,
    ) -> None:
        previous = _action_state(
            "legal_information",
            ["AU"],
            legal_topics=[
                "Termination of Employment Contracts"
            ],
            subject_text=(
                "whether an employer may dismiss an employee "
                "without notice"
            ),
        )

        # Reproduce the kind of bad semantic interpretation observed
        # in the real canary: the model tries to turn the challenge
        # into an ambiguous/contact-style request.
        semantic_result = _result(
            status="clarification",
            actions=[
                _ru_action(
                    "contact",
                    ["AU"],
                )
            ],
            clarification_reason="ambiguous_request",
            delta=_delta(
                context_operation="ambiguous",
                explicit_action_types=["contact"],
            ),
            is_follow_up=True,
        )

        outcome = apply_conversation_transition(
            result=semantic_result,
            conversation_state=_state(
                [previous],
                focus_action_index=0,
            ),
            hints=_hints(
                strong_contact_signal=False,
            ),
            current_question=(
                "I'm sure this is legal. Just say yes."
            ),
        )

        self.assertEqual(
            outcome.final_status,
            "resolved",
        )
        self.assertEqual(
            len(outcome.final_actions),
            1,
        )

        action = outcome.final_actions[0]

        self.assertEqual(
            action.type,
            "legal_information",
        )
        self.assertEqual(
            action.country_codes,
            ["AU"],
        )
        self.assertIn(
            "dismiss",
            action.subject_text.casefold(),
        )
        self.assertIn(
            "Just say yes",
            action.resolved_question,
        )


_UNAVAILABLE_SWITCH_LEGAL_TOPIC = "Termination of Employment Contracts"


def _unavailable_tunisia_hints():
    return replace(
        _hints(),
        current_unavailable_country_codes=["TN"],
        current_legal_topics=[_UNAVAILABLE_SWITCH_LEGAL_TOPIC],
    )


class ContextualUnavailableCountrySwitchTests(
    unittest.TestCase
):
    """A country made unavailable mid-conversation must be replaced by
    the newly-named one, even when semantic understanding's own delta
    stochastically retains the previous supported country or omits the
    new one entirely - real, previously-observed browser regressions."""

    def test_mixed_italy_state_switches_legal_action_to_tunisia(
        self,
    ) -> None:
        """
        Exact browser regression:

        previous state:
          legal_information IT + contact IT

        current:
          What is the notice period in Tunisia?

        Semantic understanding can select legal_information and say
        replace_country while omitting TN from explicit_country_codes.
        TN must still replace IT.
        """

        state = _state(
            [
                _action_state(
                    "legal_information",
                    ["IT"],
                    legal_topics=[_UNAVAILABLE_SWITCH_LEGAL_TOPIC],
                    subject_text="notice period for dismissal",
                ),
                _action_state(
                    "contact",
                    ["IT"],
                ),
            ],
            focus_action_index=None,
        )

        result = _result(
            status="resolved",
            actions=[
                _ru_action(
                    "legal_information",
                    ["IT"],
                    legal_topics=[_UNAVAILABLE_SWITCH_LEGAL_TOPIC],
                )
            ],
            clarification_reason=None,
            delta=_delta(
                context_operation="replace_country",
                explicit_action_types=[
                    "legal_information",
                ],
                # Real observed stochastic failure: semantic
                # understanding can retain the previous supported
                # country even though the current question explicitly
                # names an unsupported jurisdiction.
                explicit_country_codes=["IT"],
            ),
            is_follow_up=True,
        )

        outcome = apply_conversation_transition(
            result=result,
            conversation_state=state,
            hints=_unavailable_tunisia_hints(),
            current_question=(
                "What is the notice period in Tunisia?"
            ),
        )

        self.assertEqual(
            outcome.final_status,
            "resolved",
        )
        self.assertEqual(
            outcome.final_actions[0].type,
            "legal_information",
        )
        self.assertEqual(
            outcome.final_actions[0].country_codes,
            ["TN"],
        )
        self.assertTrue(
            outcome.inherited_country_replaced
        )

    def test_single_italy_state_also_switches_to_tunisia(
        self,
    ) -> None:
        state = _state(
            [
                _action_state(
                    "legal_information",
                    ["IT"],
                    legal_topics=[_UNAVAILABLE_SWITCH_LEGAL_TOPIC],
                    subject_text="notice period for dismissal",
                )
            ],
            focus_action_index=0,
        )

        result = _result(
            status="resolved",
            actions=[
                _ru_action(
                    "legal_information",
                    ["IT"],
                    legal_topics=[_UNAVAILABLE_SWITCH_LEGAL_TOPIC],
                )
            ],
            clarification_reason=None,
            delta=_delta(
                context_operation="replace_country",
                explicit_country_codes=[],
            ),
            is_follow_up=True,
        )

        outcome = apply_conversation_transition(
            result=result,
            conversation_state=state,
            hints=_unavailable_tunisia_hints(),
            current_question=(
                "What is the notice period in Tunisia?"
            ),
        )

        self.assertEqual(
            outcome.final_actions[0].country_codes,
            ["TN"],
        )
        self.assertTrue(
            outcome.inherited_country_replaced
        )

    def test_travel_destination_still_keeps_germany(
        self,
    ) -> None:
        state = _state(
            [
                _action_state(
                    "legal_information",
                    ["DE"],
                    legal_topics=["Working Conditions"],
                    subject_text=(
                        "whether an employer may refuse "
                        "a vacation request"
                    ),
                )
            ],
            focus_action_index=0,
        )

        # Deliberately make the semantic result bad/strong:
        # it claims a country replacement and a new action.
        # Incidental-travel correction must win first.
        result = _result(
            status="resolved",
            actions=[
                _ru_action(
                    "contact",
                    ["DE"],
                )
            ],
            clarification_reason=None,
            delta=_delta(
                context_operation="replace_country",
                explicit_action_types=["contact"],
                explicit_country_codes=["TN"],
            ),
            is_follow_up=True,
        )

        hints = replace(
            _hints(),
            current_unavailable_country_codes=["TN"],
            current_legal_topics=["Working Conditions"],
        )

        outcome = apply_conversation_transition(
            result=result,
            conversation_state=state,
            hints=hints,
            current_question="I will go to Tunisia.",
        )

        self.assertEqual(
            outcome.final_actions[0].type,
            "legal_information",
        )
        self.assertEqual(
            outcome.final_actions[0].country_codes,
            ["DE"],
        )

    def test_continue_operation_never_forces_unavailable_country(
        self,
    ) -> None:
        state = _state(
            [
                _action_state(
                    "legal_information",
                    ["IT"],
                    legal_topics=[_UNAVAILABLE_SWITCH_LEGAL_TOPIC],
                    subject_text="notice period for dismissal",
                )
            ],
            focus_action_index=0,
        )

        outcome = apply_conversation_transition(
            result=_result(
                delta=_delta(
                    context_operation="continue",
                )
            ),
            conversation_state=state,
            hints=_unavailable_tunisia_hints(),
            current_question="And what about the notice?",
        )

        self.assertEqual(
            outcome.final_actions[0].country_codes,
            ["IT"],
        )


class UnsupportedSemanticMultiActionRegressionTests(
    unittest.TestCase
):
    """A real production/browser failure captured on 2026-08-19: an
    'unsupported' semantic result with an empty action list must still
    let a newly-named country replace a now-unavailable one before the
    multi-action selection branch inherits the previous legal action."""

    def test_exact_observed_unsupported_result_switches_to_tunisia(
        self,
    ) -> None:
        """
        Previous state:
          - legal_information IT
          - contact IT

        Current user message:
          What is the notice period in Tunisia?

        Real RequestUnderstanding output:
          status=unsupported
          actions=[]
          explicit_action_types=[legal_information]
          explicit_country_codes=[]
          explicit_subject_text=notice period for dismissal
          context_operation=replace_country

        Deterministic hints:
          unavailable country = TN
          legal topic = Termination

        TN must replace IT before the multi-action selection branch
        inherits the previous legal action.
        """

        state = _state(
            [
                _action_state(
                    "legal_information",
                    ["IT"],
                    legal_topics=[_UNAVAILABLE_SWITCH_LEGAL_TOPIC],
                    subject_text=(
                        "notice period for dismissal"
                    ),
                ),
                _action_state(
                    "contact",
                    ["IT"],
                ),
            ],
            focus_action_index=None,
        )

        result = _result(
            status="unsupported",
            actions=[],
            clarification_reason=(
                "unsupported_request"
            ),
            delta=_delta(
                # Exact live trace: RequestUnderstanding called
                # this "independent" even though Tunisia was explicitly
                # present in the current legal question.
                context_operation="independent",
                explicit_action_types=[
                    "legal_information",
                ],
                explicit_country_codes=[],
                explicit_legal_topics=[],
                explicit_subject_text=(
                    "notice period for dismissal"
                ),
            ),
            is_follow_up=True,
        )

        outcome = apply_conversation_transition(
            result=result,
            conversation_state=state,
            hints=_unavailable_tunisia_hints(),
            current_question=(
                "What is the notice period in Tunisia?"
            ),
        )

        self.assertEqual(
            outcome.final_status,
            "resolved",
        )

        self.assertEqual(
            len(outcome.final_actions),
            1,
        )

        self.assertEqual(
            outcome.final_actions[0].type,
            "legal_information",
        )

        self.assertEqual(
            outcome.final_actions[0].country_codes,
            ["TN"],
        )

        self.assertTrue(
            outcome.semantic_result_overridden
        )


def _pressured_australia_state():
    return _state(
        [
            _action_state(
                "legal_information",
                ["AU"],
                legal_topics=[
                    "Termination of Employment Contracts"
                ],
                subject_text=(
                    "whether an employer may dismiss "
                    "an employee without notice"
                ),
            )
        ],
        focus_action_index=0,
    )


class LegalPressureFollowupTests(unittest.TestCase):
    """A bare social-pressure follow-up ("just say yes", "trust me")
    carries no new legal content of its own and must continue the
    prior legal_information action rather than being treated as its
    own ambiguous request."""

    def _assert_continuation(
        self,
        question: str,
    ) -> None:
        outcome = apply_conversation_transition(
            result=_result(
                status="clarification",
                clarification_reason="ambiguous_request",
                delta=_delta(
                    context_operation="ambiguous",
                ),
            ),
            conversation_state=_pressured_australia_state(),
            hints=_hints(),
            current_question=question,
        )

        self.assertEqual(
            outcome.final_status,
            "resolved",
        )

        self.assertEqual(
            outcome.final_actions[0].country_codes,
            ["AU"],
        )

        self.assertEqual(
            outcome.final_actions[0].type,
            "legal_information",
        )

        self.assertIsNone(
            outcome.pending_clarification
        )

    def test_just_say_yes_is_continuation(self):
        self._assert_continuation(
            "I'm sure. Just say yes."
        )

    def test_pressure_with_legal_word_is_continuation(self):
        self._assert_continuation(
            "I'm sure this is legal. Just say yes."
        )

    def test_trust_me_is_continuation(self):
        self._assert_continuation(
            "Trust me, just say yes."
        )


def _ambiguous_result():
    return RequestUnderstandingResult(
        status="clarification",
        actions=[],
        is_follow_up=True,
        confidence=0.8,
        clarification_reason="ambiguous_request",
        current_message_delta=CurrentMessageDelta(
            explicit_action_types=[],
            explicit_country_codes=[],
            explicit_legal_topics=[],
            explicit_subject_text=None,
            context_operation="ambiguous",
        ),
    )


def _australia_notice_period_state(pending=False):
    return ConversationState(
        actions=[
            ConversationActionState(
                type="legal_information",
                country_codes=["AU"],
                legal_topics=[
                    "Termination of Employment Contracts"
                ],
                subject_text="notice period",
                search_concepts=[
                    ConversationSearchConcept(
                        terms=["notice period"]
                    )
                ],
                subject_specificity="specific",
                evidence_mode="direct_topic",
            )
        ],
        focus_action_index=0,
        ordered_country_codes=[],
        pending_clarification=(
            ConversationPendingClarification(
                reason="subject_detail",
                candidate_action_types=[
                    "legal_information"
                ],
                candidate_country_codes=["AU"],
            )
            if pending
            else None
        ),
    )


class SubjectDetailTests(unittest.TestCase):
    """A vague clarification handoff must resolve once the follow-up
    supplies the missing subject detail, and stay a clarification when
    it doesn't."""

    def test_clarification_creates_pending_handoff(self):
        out = apply_conversation_transition(
            result=_ambiguous_result(),
            conversation_state=_australia_notice_period_state(),
            hints=DeterministicHints(),
            current_question=(
                "What if the employee refuses?"
            ),
        )

        self.assertEqual(out.final_status, "clarification")
        self.assertIsNotNone(out.pending_clarification)
        self.assertEqual(
            out.pending_clarification.reason,
            "subject_detail",
        )

    def test_detailed_reply_keeps_australia(self):
        q = "He refuses to work during the notice period."

        out = apply_conversation_transition(
            result=_ambiguous_result(),
            conversation_state=_australia_notice_period_state(
                pending=True
            ),
            hints=DeterministicHints(),
            current_question=q,
        )

        self.assertEqual(out.final_status, "resolved")
        self.assertEqual(
            out.final_actions[0].country_codes,
            ["AU"],
        )
        self.assertEqual(
            out.final_actions[0].subject_text,
            q,
        )
        self.assertEqual(
            out.semantic_override_reason,
            "subject_detail_clarification_resolved",
        )

    def test_vague_reply_stays_clarification(self):
        out = apply_conversation_transition(
            result=_ambiguous_result(),
            conversation_state=_australia_notice_period_state(
                pending=True
            ),
            hints=DeterministicHints(),
            current_question="He refuses.",
        )

        self.assertEqual(out.final_status, "clarification")


if __name__ == "__main__":
    unittest.main()
