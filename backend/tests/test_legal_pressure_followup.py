from __future__ import annotations

import unittest

from app.services.conversation_transition import (
    apply_conversation_transition,
)

from tests.support.conversation_fixtures import (
    _action_state,
    _delta,
    _hints,
    _result,
    _state,
)


def australia_state():
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
            conversation_state=australia_state(),
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


if __name__ == "__main__":
    unittest.main()
