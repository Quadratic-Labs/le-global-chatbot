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
    RequestUnderstandingAction,
    RequestUnderstandingResult,
)


def _delta(
    *,
    context_operation: str = "independent",
    explicit_action_types: list[str] | None = None,
    explicit_country_codes: list[str] | None = None,
    explicit_legal_topics: list[str] | None = None,
    explicit_subject_text: str | None = None,
) -> CurrentMessageDelta:
    return CurrentMessageDelta(
        explicit_action_types=explicit_action_types or [],
        explicit_country_codes=explicit_country_codes or [],
        explicit_legal_topics=explicit_legal_topics or [],
        explicit_subject_text=explicit_subject_text,
        context_operation=context_operation,
    )


def _hints(
    *,
    strong_contact_signal: bool = False,
    comparison_signal: bool = False,
) -> DeterministicHints:
    return DeterministicHints(
        strong_contact_signal=strong_contact_signal,
        comparison_signal=comparison_signal,
    )


def _ru_action(
    action_type: str,
    country_codes: list[str],
    **kwargs,
) -> RequestUnderstandingAction:
    return RequestUnderstandingAction(
        type=action_type,
        country_codes=country_codes,
        **kwargs,
    )


def _result(
    *,
    status: str = "clarification",
    actions: list[RequestUnderstandingAction] | None = None,
    clarification_reason: str | None = "ambiguous_request",
    delta: CurrentMessageDelta | None = None,
    is_follow_up: bool = True,
) -> RequestUnderstandingResult:
    """
    A stand-in "classifier's own result" - defaults to a generic,
    contentless clarification so that any test asserting an override
    can tell the engine's own inheritance/clarification apart from
    whatever the classifier alone produced.
    """

    return RequestUnderstandingResult(
        status=status,
        actions=actions or [],
        is_follow_up=is_follow_up,
        confidence=0.5,
        clarification_reason=clarification_reason,
        current_message_delta=(
            delta or _delta(context_operation="ambiguous")
        ),
    )


def _action_state(
    action_type: str,
    country_codes: list[str],
    **kwargs,
) -> ConversationActionState:
    return ConversationActionState(
        type=action_type,
        country_codes=country_codes,
        **kwargs,
    )


def _state(
    actions: list[ConversationActionState],
    *,
    focus_action_index: int | None = None,
    ordered_country_codes: list[str] | None = None,
) -> ConversationState:
    return ConversationState(
        actions=actions,
        focus_action_index=focus_action_index,
        ordered_country_codes=ordered_country_codes or [],
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


if __name__ == "__main__":
    unittest.main()
