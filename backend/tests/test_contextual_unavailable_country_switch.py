from __future__ import annotations

from dataclasses import replace
import unittest

from app.services.conversation_transition import (
    apply_conversation_transition,
)

from tests.test_conversation_transition import (
    _action_state,
    _delta,
    _hints,
    _result,
    _ru_action,
    _state,
)


LEGAL_TOPIC = "Termination of Employment Contracts"


def unavailable_tunisia_hints():
    return replace(
        _hints(),
        current_unavailable_country_codes=["TN"],
        current_legal_topics=[LEGAL_TOPIC],
    )


class ContextualUnavailableCountrySwitchTests(
    unittest.TestCase
):

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
                    legal_topics=[LEGAL_TOPIC],
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
                    legal_topics=[LEGAL_TOPIC],
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
            hints=unavailable_tunisia_hints(),
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
                    legal_topics=[LEGAL_TOPIC],
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
                    legal_topics=[LEGAL_TOPIC],
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
            hints=unavailable_tunisia_hints(),
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
                    legal_topics=[LEGAL_TOPIC],
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
            hints=unavailable_tunisia_hints(),
            current_question="And what about the notice?",
        )

        self.assertEqual(
            outcome.final_actions[0].country_codes,
            ["IT"],
        )


class UnsupportedSemanticMultiActionRegressionTests(
    unittest.TestCase
):

    def test_exact_observed_unsupported_result_switches_to_tunisia(
        self,
    ) -> None:
        """
        Exact production/browser failure captured on 2026-08-19.

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
                    legal_topics=[LEGAL_TOPIC],
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
            hints=unavailable_tunisia_hints(),
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


if __name__ == "__main__":
    unittest.main()
