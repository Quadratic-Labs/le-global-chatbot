import unittest
from app.services.legal_answer_presentation import (
    present_validated_legal_answer_as_prose as present,
)


class LegalAnswerPresentationTests(unittest.TestCase):
    def test_single_country_fluid_paragraph(self):
        original = (
            'Under French law:\n'
            '- The maximum average weekly working time is 44 hours. [1]\n'
            '- Additional limits include 48 hours per week and 10 per day. [2]'
        )
        result = present(original, law_labels={'Under French law:': 'Under French law'})
        self.assertEqual(
            'Under French law, the maximum average weekly working time is '
            '44 hours. [1] Additional limits include 48 hours per week '
            'and 10 per day. [2]', result
        )
        self.assertNotIn('\n-', result)
        self.assertNotIn('law:', result)

    def test_two_countries_and_comparison(self):
        original = (
            'France\n- The week is 35 hours. [1]\n\n'
            'Spain\n- The week is 40 hours. [2]\n\n'
            'Comparison\n- France has fewer hours than Spain. [1, 2]'
        )
        result = present(original, law_labels={
            'France':'Under French law', 'Spain':'Under Spanish law',
        })
        self.assertEqual(
            'France\nUnder French law, the week is 35 hours. [1]\n\n'
            'Spain\nUnder Spanish law, the week is 40 hours. [2]\n\n'
            'Comparison\nFrance has fewer hours than Spain. [1, 2]', result
        )

    def test_multiple_topics_can_use_separate_paragraphs(self):
        original = 'France\n- Working time is 35 hours. [1]\n- Paid leave is 30 days. [2]'
        result = present(original, law_labels={'France':'Under French law'}, split_themes=True)
        self.assertEqual(
            'Under French law, working time is 35 hours. [1]\n\n'
            'Paid leave is 30 days. [2]', result
        )

    def test_partial_uncertainty_referral_preserved(self):
        original = (
            'Under French law:\n- The week is 35 hours. [1]\n'
            '- For the unconfirmed legal point(s), please contact the member firm.'
        )
        result = present(original, law_labels={'Under French law:':'Under French law'})
        self.assertIn('unconfirmed legal point(s)', result)
        self.assertIn('35 hours. [1]', result)
        self.assertNotIn('\n-', result)

    def test_unexpected_input_never_rewritten(self):
        original = 'France\nLegal answer without bullet [1]'
        self.assertEqual(original, present(original, law_labels={'France':'Under French law'}))

    def test_contact_only_and_unavailable_unchanged(self):
        original = 'For guidance please contact the member firm.'
        self.assertEqual(original, present(original, law_labels={'France':'Under French law'}))

    def test_citation_numbers_preserved(self):
        original = 'France\n- A rule [2, 3].\n- An exception [6].'
        result = present(original, law_labels={'France':'Under French law'})
        self.assertIn('[2, 3]', result)
        self.assertIn('[6]', result)
        self.assertNotIn('[1]', result)

    def test_uk_and_us_prefixes(self):
        for heading, label in [('United Kingdom','Under UK law'), ('United States','Under US law')]:
            with self.subTest(heading=heading):
                self.assertTrue(present(heading + '\n- Employees have rights. [1]',
                    law_labels={heading:label}).startswith(label + ', employees'))

    def test_no_duplicate_country_heading_after_formatting(self):
        result = present('Under Spanish law:\n- Employees have rights. [1]',
                         law_labels={'Under Spanish law:':'Under Spanish law'})
        self.assertEqual('Under Spanish law, employees have rights. [1]', result)


if __name__ == '__main__':
    unittest.main()
