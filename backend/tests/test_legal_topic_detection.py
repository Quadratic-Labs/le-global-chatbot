"""Tests for automatic legal-topic detection."""

from __future__ import annotations

import unittest

from app.models.chat import LegalChatRequest
from app.services.legal_topic_detection import (
    detect_legal_topics,
    prepare_legal_chat_topics,
)


class LegalTopicDetectionTests(unittest.TestCase):
    """Tests for deterministic legal-topic detection."""

    def test_notice_period_detects_relevant_topics(
        self,
    ) -> None:
        topics = detect_legal_topics(
            "Compare statutory notice periods "
            "in the UK and Spain."
        )

        self.assertEqual(
            topics,
            [
                "Employment Contracts",
                "Termination of Employment Contracts",
            ],
        )

    def test_termination_question_detects_topic(
        self,
    ) -> None:
        topics = detect_legal_topics(
            "Can an employee challenge "
            "an unfair dismissal?"
        )

        self.assertEqual(
            topics,
            [
                "Termination of Employment Contracts",
            ],
        )

    def test_overtime_detects_working_conditions(
        self,
    ) -> None:
        topics = detect_legal_topics(
            "What are the overtime rules?"
        )

        self.assertEqual(
            topics,
            [
                "Working Conditions",
            ],
        )

    def test_explicit_topics_take_priority(
        self,
    ) -> None:
        request = LegalChatRequest(
            question="What is the notice period?",
            legal_topics=[
                "Employee Benefits",
            ],
        )

        prepared_request = (
            prepare_legal_chat_topics(
                request
            )
        )

        self.assertEqual(
            prepared_request.legal_topics,
            [
                "Employee Benefits",
            ],
        )

    def test_detected_topics_are_added(
        self,
    ) -> None:
        request = LegalChatRequest(
            question=(
                "Compare statutory notice periods "
                "in the UK and Spain."
            ),
            country_codes=[
                "GB",
                "ES",
            ],
            max_sources=4,
        )

        prepared_request = (
            prepare_legal_chat_topics(
                request
            )
        )

        self.assertEqual(
            prepared_request.country_codes,
            [
                "GB",
                "ES",
            ],
        )

        self.assertEqual(
            prepared_request.legal_topics,
            [
                "Employment Contracts",
                "Termination of Employment Contracts",
            ],
        )

        self.assertEqual(
            prepared_request.max_sources,
            4,
        )

    def test_unknown_question_keeps_empty_topics(
        self,
    ) -> None:
        request = LegalChatRequest(
            question=(
                "What rules should an employer consider?"
            )
        )

        prepared_request = (
            prepare_legal_chat_topics(
                request
            )
        )

        self.assertEqual(
            prepared_request.legal_topics,
            [],
        )


if __name__ == "__main__":
    unittest.main()