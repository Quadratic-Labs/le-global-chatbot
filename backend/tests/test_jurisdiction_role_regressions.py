from __future__ import annotations

import inspect
import unittest

from app.services.conversation_transition import (
    _same_subject_country_followup,
    apply_conversation_transition,
)
from app.services.rag_answer import (
    ANSWER_QUALITY_INSTRUCTIONS,
    INSUFFICIENT_EVIDENCE_ANSWER_TEMPLATE,
    PARTIAL_EVIDENCE_INSTRUCTION_TEMPLATE,
    _build_model_input,
)
from app.services.request_understanding import (
    UNDERSTANDING_INSTRUCTIONS,
)

from tests.test_conversation_transition import (
    _action_state,
    _delta,
    _hints,
    _result,
    _ru_action,
    _state,
)


def german_vacation_state():
    return _state(
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


class JurisdictionRoleRegressionTests(unittest.TestCase):

    def test_booked_trip_continues_germany(self):
        outcome = apply_conversation_transition(
            result=_result(
                status="clarification",
                clarification_reason="ambiguous_request",
                delta=_delta(
                    context_operation="ambiguous",
                ),
            ),
            conversation_state=german_vacation_state(),
            hints=_hints(),
            current_question="I already booked the trip.",
        )

        self.assertEqual(outcome.final_status, "resolved")
        self.assertEqual(
            outcome.final_actions[0].country_codes,
            ["DE"],
        )

    def test_spain_destination_does_not_become_jurisdiction(self):
        outcome = apply_conversation_transition(
            result=_result(
                status="resolved",
                actions=[
                    _ru_action(
                        "contact",
                        ["ES"],
                    )
                ],
                clarification_reason=None,
                delta=_delta(
                    context_operation="change_action",
                    explicit_action_types=["contact"],
                    explicit_country_codes=["ES"],
                ),
            ),
            conversation_state=german_vacation_state(),
            hints=_hints(),
            current_question="I will go to Spain.",
        )

        self.assertEqual(outcome.final_status, "resolved")
        self.assertEqual(
            outcome.final_actions[0].type,
            "legal_information",
        )
        self.assertEqual(
            outcome.final_actions[0].country_codes,
            ["DE"],
        )

    def test_explicit_spanish_law_replaces_germany(self):
        question = (
            "How does the same issue work under Spanish law?"
        )

        outcome = apply_conversation_transition(
            result=_result(
                status="clarification",
                clarification_reason=(
                    "missing_comparison_countries"
                ),
                delta=_delta(
                    context_operation="ambiguous",
                    explicit_country_codes=["ES"],
                ),
            ),
            conversation_state=german_vacation_state(),
            hints=_hints(
                comparison_signal=True,
            ),
            current_question=question,
        )

        self.assertEqual(outcome.final_status, "resolved")
        self.assertEqual(
            outcome.final_actions[0].type,
            "legal_information",
        )
        self.assertEqual(
            outcome.final_actions[0].country_codes,
            ["ES"],
        )

    def test_real_comparison_is_not_collapsed(self):
        self.assertIsNone(
            _same_subject_country_followup(
                "Compare Germany and Spain on annual leave."
            )
        )

    def test_semantic_country_role_is_encoded(self):
        self.assertIn(
            "travel destination",
            UNDERSTANDING_INSTRUCTIONS,
        )
        self.assertIn(
            "does NOT by itself replace",
            UNDERSTANDING_INSTRUCTIONS,
        )

    def test_answer_quality_contract_is_installed(self):
        self.assertIn(
            "does NOT establish",
            ANSWER_QUALITY_INSTRUCTIONS,
        )
        self.assertIn(
            "EXACT proposition",
            ANSWER_QUALITY_INSTRUCTIONS,
        )
        self.assertIn(
            "cannot reliably determine",
            INSUFFICIENT_EVIDENCE_ANSWER_TEMPLATE,
        )
        self.assertNotIn(
            "available validated L&E Global documents only partially",
            PARTIAL_EVIDENCE_INSTRUCTION_TEMPLATE,
        )

        source = inspect.getsource(_build_model_input)

        self.assertIn(
            "ANSWER_QUALITY_INSTRUCTIONS",
            source,
        )


if __name__ == "__main__":
    unittest.main()
