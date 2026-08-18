"""Regression for contextual clarification handoff."""

from __future__ import annotations

import unittest

from app.models.conversation_state import (
    ConversationActionState,
    ConversationPendingClarification,
    ConversationSearchConcept,
    ConversationState,
)
from app.services.conversation_transition import (
    apply_conversation_transition,
)
from app.services.request_understanding import (
    CurrentMessageDelta,
    DeterministicHints,
    RequestUnderstandingResult,
)


def ambiguous():
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


def state(pending=False):
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

    def test_clarification_creates_pending_handoff(self):
        out = apply_conversation_transition(
            result=ambiguous(),
            conversation_state=state(),
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
            result=ambiguous(),
            conversation_state=state(pending=True),
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
            result=ambiguous(),
            conversation_state=state(pending=True),
            hints=DeterministicHints(),
            current_question="He refuses.",
        )

        self.assertEqual(out.final_status, "clarification")


if __name__ == "__main__":
    unittest.main()
