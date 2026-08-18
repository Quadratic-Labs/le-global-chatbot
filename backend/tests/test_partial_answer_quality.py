from __future__ import annotations

import unittest

from app.models.conversation_state import (
    ConversationSearchConcept,
)
from app.services.rag_answer import (
    INSUFFICIENT_EVIDENCE_ANSWER_TEMPLATE,
    PARTIAL_EVIDENCE_INSTRUCTION_TEMPLATE,
    _validate_partial_answer_relevance,
)


class PartialAnswerQualityTests(unittest.TestCase):

    def setUp(self) -> None:
        self.concepts = [
            ConversationSearchConcept(
                terms=[
                    "vacation request",
                    "annual leave request",
                    "employer refuse vacation",
                ]
            )
        ]

    def test_unrelated_padding_after_limitation_is_subject_drift(
        self,
    ) -> None:
        errors = _validate_partial_answer_relevance(
            answer=(
                "Spain\n"
                "- I cannot reliably confirm whether an employer "
                "may refuse an annual leave request [1].\n"
                "- Employees are protected against retaliation "
                "when asserting employment rights [2]."
            ),
            search_concepts=self.concepts,
            evidence_mode="direct_topic",
            country_codes=["ES"],
        )

        self.assertTrue(errors)
        self.assertTrue(
            all(
                error.error_type == "subject_drift"
                for error in errors
            )
        )

    def test_directly_relevant_supporting_rule_is_allowed(
        self,
    ) -> None:
        errors = _validate_partial_answer_relevance(
            answer=(
                "Spain\n"
                "- I cannot reliably confirm whether an employer "
                "may refuse an annual leave request [1].\n"
                "- Annual leave requests are governed by the "
                "applicable vacation rules [1]."
            ),
            search_concepts=self.concepts,
            evidence_mode="direct_topic",
            country_codes=["ES"],
        )

        self.assertEqual(errors, [])

    def test_limitation_templates_do_not_expose_internal_wording(
        self,
    ) -> None:
        combined = (
            INSUFFICIENT_EVIDENCE_ANSWER_TEMPLATE
            + PARTIAL_EVIDENCE_INSTRUCTION_TEMPLATE
        ).casefold()

        self.assertNotIn(
            "available l&e global information",
            combined,
        )



    def test_choice_of_law_rejects_social_security_padding(
        self,
    ) -> None:
        concepts = [
            ConversationSearchConcept(
                terms=[
                    "which country's employment law applies",
                    "applicable employment law",
                    "law governing the employment relationship",
                ]
            )
        ]

        errors = _validate_partial_answer_relevance(
            answer=(
                "France\n"
                "- I cannot reliably confirm which country's "
                "employment law governs the relationship [1].\n"
                "- A foreign employer and employee must be "
                "registered with the French social security "
                "office [1]."
            ),
            search_concepts=concepts,
            evidence_mode="direct_topic",
            country_codes=["FR"],
        )

        self.assertTrue(errors)
        self.assertEqual(
            errors[0].error_type,
            "subject_drift",
        )

    def test_choice_of_law_allows_actual_governing_law_rule(
        self,
    ) -> None:
        concepts = [
            ConversationSearchConcept(
                terms=[
                    "which country's employment law applies",
                    "applicable employment law",
                ]
            )
        ]

        errors = _validate_partial_answer_relevance(
            answer=(
                "Germany\n"
                "- I cannot reliably confirm which country's "
                "employment law governs the relationship [1].\n"
                "- Rights of employees temporarily sent to Germany "
                "may be determined by foreign employment law, "
                "subject to mandatory Posted Workers Act "
                "requirements [1]."
            ),
            search_concepts=concepts,
            evidence_mode="direct_topic",
            country_codes=["DE"],
        )

        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
