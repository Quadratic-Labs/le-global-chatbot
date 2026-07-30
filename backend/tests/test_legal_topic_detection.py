"""Tests for automatic legal-topic scope detection."""

from __future__ import annotations

import unittest

from app.models.chat import LegalChatRequest
from app.services.legal_topic_detection import (
    detect_legal_topics,
    is_overview_question,
    resolve_legal_scope,
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

    def test_discrimination_detects_anti_discrimination_laws(
        self,
    ) -> None:
        topics = detect_legal_topics(
            "What protections exist against "
            "workplace harassment?"
        )

        self.assertEqual(
            topics,
            [
                "Anti-Discrimination Laws",
            ],
        )

    def test_equal_pay_detects_pay_equity_laws(
        self,
    ) -> None:
        topics = detect_legal_topics(
            "What are the equal pay requirements?"
        )

        self.assertEqual(
            topics,
            [
                "Pay Equity Laws",
            ],
        )

    def test_trade_union_detects_trade_union_topic(
        self,
    ) -> None:
        topics = detect_legal_topics(
            "How does collective bargaining work?"
        )

        self.assertEqual(
            topics,
            [
                "Trade Unions and Employers Associations",
            ],
        )

    def test_business_transfer_detects_transfer_of_undertakings(
        self,
    ) -> None:
        topics = detect_legal_topics(
            "What happens to employees "
            "in a business transfer?"
        )

        self.assertEqual(
            topics,
            [
                "Transfer of Undertakings",
            ],
        )

    def test_employee_monitoring_detects_social_media_topic(
        self,
    ) -> None:
        topics = detect_legal_topics(
            "Can an employer monitor employee "
            "emails in Spain?"
        )

        self.assertEqual(
            topics,
            [
                "Social Media and Data Privacy",
            ],
        )

    def test_tax_question_detects_no_topic(
        self,
    ) -> None:
        topics = detect_legal_topics(
            "What are the corporate income "
            "tax rules in Spain?"
        )

        self.assertEqual(
            topics,
            [],
        )

    def test_vat_question_detects_no_topic(
        self,
    ) -> None:
        topics = detect_legal_topics(
            "What is the VAT rate in Italy?"
        )

        self.assertEqual(
            topics,
            [],
        )

    def test_patents_question_detects_no_topic(
        self,
    ) -> None:
        topics = detect_legal_topics(
            "What about patents and inventions "
            "for employees in Spain?"
        )

        self.assertEqual(
            topics,
            [],
        )

    def test_overview_phrase_is_recognized(
        self,
    ) -> None:
        self.assertTrue(
            is_overview_question(
                "Employment law overview Spain"
            )
        )

    def test_non_overview_question_is_not_recognized(
        self,
    ) -> None:
        self.assertFalse(
            is_overview_question(
                "What is the VAT rate in Italy?"
            )
        )


class LegalScopeTests(unittest.TestCase):
    """Tests for the combined legal-topic scope decision."""

    def test_explicit_topics_take_priority(
        self,
    ) -> None:
        scope = resolve_legal_scope(
            LegalChatRequest(
                question="What is the notice period?",
                legal_topics=[
                    "Employee Benefits",
                ],
            )
        )

        self.assertEqual(
            scope.legal_topics,
            [
                "Employee Benefits",
            ],
        )

        self.assertTrue(
            scope.is_supported
        )

    def test_detected_topics_are_supported(
        self,
    ) -> None:
        scope = resolve_legal_scope(
            LegalChatRequest(
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
        )

        self.assertEqual(
            scope.legal_topics,
            [
                "Employment Contracts",
                "Termination of Employment Contracts",
            ],
        )

        self.assertTrue(
            scope.is_supported
        )

    def test_overview_question_is_supported_without_topics(
        self,
    ) -> None:
        scope = resolve_legal_scope(
            LegalChatRequest(
                question="Employment law overview Spain",
                country_codes=[
                    "ES",
                ],
            )
        )

        self.assertEqual(
            scope.legal_topics,
            [],
        )

        self.assertTrue(
            scope.is_overview_question
        )

        self.assertTrue(
            scope.is_supported
        )

    def test_explicit_subsection_is_supported_without_topics(
        self,
    ) -> None:
        scope = resolve_legal_scope(
            LegalChatRequest(
                question="Tell me more about this.",
                subsections=[
                    "Notice Period",
                ],
            )
        )

        self.assertTrue(
            scope.is_supported
        )

    def test_out_of_scope_question_is_not_supported(
        self,
    ) -> None:
        scope = resolve_legal_scope(
            LegalChatRequest(
                question=(
                    "What are the corporate income "
                    "tax rules in Spain?"
                ),
                country_codes=[
                    "ES",
                ],
            )
        )

        self.assertEqual(
            scope.legal_topics,
            [],
        )

        self.assertFalse(
            scope.is_overview_question
        )

        self.assertFalse(
            scope.is_supported
        )


if __name__ == "__main__":
    unittest.main()
