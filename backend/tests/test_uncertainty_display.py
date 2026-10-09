import unittest

from app.routers.chat import (
    _present_single_country_evidence,
    _format_single_country_legal_answer,
)


class UncertaintyDisplayTests(unittest.TestCase):

    def test_uncertain_first_bullet_suppresses_nearby_rules(self):
        answer = (
            "France\n"
            "- I cannot reliably confirm the notice period. [1]\n"
            "- The preliminary meeting requires five days. [1]"
        )
        result = _present_single_country_evidence(answer, "FR")

        self.assertNotIn("Under French law:", result)
        self.assertNotIn("five days", result)
        self.assertNotIn("cannot reliably", result)
        self.assertIn("contact the L&E Global member firm", result)

    def test_reliable_then_uncertain_keeps_reliable_answer(self):
        answer = (
            "France\n"
            "- The normal working week is 35 hours. [1]\n"
            "- I cannot reliably confirm the notice period. [2]\n"
            "- The preliminary meeting takes five days. [2]"
        )
        result = _present_single_country_evidence(answer, "FR")
        result = _format_single_country_legal_answer(result, "FR")

        self.assertTrue(result.startswith("Under French law:"))
        self.assertIn("35 hours. [1]", result)
        self.assertNotIn("five days", result)
        self.assertNotIn("cannot reliably", result)
        self.assertIn("contact the L&E Global member firm", result)

    def test_independent_question_uncertain_before_reliable(self):
        question = (
            "In France, what is the statutory notice period for "
            "termination, and what are the normal working hours?"
        )
        answer = (
            "France\n"
            "- I cannot reliably confirm the statutory notice period. [1]\n"
            "- The usual working week is 35 hours. [2]\n"
            "- A preliminary dismissal meeting requires five days' notice. [3]"
        )
        presented = _present_single_country_evidence(
            answer, "FR", original_question=question
        )
        presented = _format_single_country_legal_answer(presented, "FR")
        self.assertTrue(presented.startswith("Under French law:"))
        self.assertIn("35 hours. [2]", presented)
        self.assertNotIn("preliminary dismissal meeting", presented)
        self.assertNotIn("cannot reliably", presented)
        self.assertEqual(presented.count("member firm"), 1)

    def test_independent_question_reliable_before_uncertain(self):
        question = (
            "In France, what are the normal working hours, "
            "and what is the statutory notice period?"
        )
        answer = (
            "France\n"
            "- The normal working week is 35 hours. [1]\n"
            "- I cannot reliably confirm the statutory notice period. [2]\n"
            "- A dismissal meeting requires five days' notice. [3]"
        )
        presented = _present_single_country_evidence(
            answer, "FR", original_question=question
        )
        self.assertIn("35 hours. [1]", presented)
        self.assertNotIn("five days", presented)
        self.assertIn("member firm", presented)

    def test_independent_question_uncertain_between_supported_items(self):
        question = (
            "In France, what are the normal working hours, "
            "what is the statutory termination notice, "
            "and how many paid annual leave days are allowed?"
        )
        answer = (
            "France\n"
            "- The normal working week is 35 hours. [1]\n"
            "- I cannot reliably confirm the statutory termination notice. [2]\n"
            "- Paid annual leave is 30 working days. [3]"
        )
        presented = _present_single_country_evidence(
            answer, "FR", original_question=question
        )
        self.assertIn("35 hours. [1]", presented)
        self.assertIn("30 working days. [3]", presented)
        self.assertNotIn("cannot reliably", presented)
        self.assertEqual(presented.count("member firm"), 1)

    def test_only_one_question_still_rejects_adjacent_procedure(self):
        question = "What is the statutory notice period for termination in France?"
        answer = (
            "France\n"
            "- I cannot reliably confirm the statutory notice period. [1]\n"
            "- The preliminary interview requires five working days. [2]"
        )
        presented = _present_single_country_evidence(
            answer, "FR", original_question=question
        )
        self.assertNotIn("five working days", presented)
        self.assertFalse(presented.startswith("France"))

    def test_uncertainty_without_identifiable_subject_keeps_safe_fallback(self):
        question = (
            "In France, what is the statutory notice period for "
            "termination, and what are the normal working hours?"
        )
        answer = (
            "France\n"
            "- I cannot reliably confirm this information. [1]\n"
            "- The usual working week is 35 hours. [2]"
        )
        presented = _present_single_country_evidence(
            answer, "FR", original_question=question
        )
        self.assertNotIn("35 hours", presented)
        self.assertIn("member firm", presented)

    def test_no_extra_bullet_without_source_citation(self):
        question = (
            "In France, what is the statutory notice period for "
            "termination, and what are the normal working hours?"
        )
        answer = (
            "France\n"
            "- I cannot reliably confirm the statutory notice period. [1]\n"
            "- The usual working week is 35 hours."
        )
        presented = _present_single_country_evidence(
            answer, "FR", original_question=question
        )
        self.assertNotIn("35 hours", presented)

    def test_unrelated_question_not_split_on_simple_and(self):
        question = (
            "What is the statutory notice period and the exceptions in France?"
        )
        answer = (
            "France\n"
            "- I cannot reliably confirm the statutory notice period. [1]\n"
            "- A preliminary meeting must be announced five days ahead. [2]"
        )
        presented = _present_single_country_evidence(
            answer, "FR", original_question=question
        )
        self.assertNotIn("five days", presented)

    def test_reliable_answer_unchanged(self):
        answer = (
            "France\n"
            "- The normal working week is 35 hours. [1]"
        )
        self.assertEqual(
            _present_single_country_evidence(answer, "FR"),
            answer,
        )

    def test_other_country_unchanged(self):
        answer = (
            "Spain\n"
            "- I cannot reliably confirm this rule. [1]"
        )
        self.assertEqual(
            _present_single_country_evidence(answer, "FR"),
            answer,
        )


if __name__ == "__main__":
    unittest.main()
