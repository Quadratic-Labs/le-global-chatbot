"""Exact user-facing editorial contract for validated legal answers."""
import inspect
import re
import unittest

from app.services.legal_answer_presentation import (
    present_validated_legal_answer_as_prose as present,
)
from app.routers.chat import _present_single_country_evidence


class ManagerEditorialContractTests(unittest.TestCase):
    def test_single_country_no_title_bullets_or_colon(self):
        text = present(
            'Under French law:\n- The week is 35 hours [1].\n- Maximum 48 hours [2].',
            law_labels={'Under French law:':'Under French law'},
        )
        self.assertEqual(
            'Under French law, the week is 35 hours [1]. Maximum 48 hours [2].',
            text,
        )

    def test_comparison_exact_country_titles_and_conclusion(self):
        text = present(
            'France\n- The week is 35 hours [1].\n\n'
            'Spain\n- The week is 40 hours [2].\n\n'
            'Comparison\n- The standards differ [1, 2].',
            law_labels={'France':'Under French law', 'Spain':'Under Spanish law'},
        )
        self.assertEqual(
            'France\nUnder French law, the week is 35 hours [1].\n\n'
            'Spain\nUnder Spanish law, the week is 40 hours [2].\n\n'
            'Comparison\nThe standards differ [1, 2].',
            text,
        )
        self.assertFalse(re.search(r'(?m)^\s*[-*•]\s+', text))

    def test_six_country_headers_not_lost(self):
        pairs = [('Australia', 'Australian'), ('Belgium', 'Belgian'),
                 ('Canada', 'Canadian'), ('France', 'French'),
                 ('Germany', 'German'), ('Italy', 'Italian')]
        canonical = '\n\n'.join(
            f'{country}\n- A cited legal statement [1].'
            for country, _ in pairs
        ) + '\n\nComparison\n- The rules vary by country [1].'
        labels = {country: f'Under {demonym} law' for country, demonym in pairs}
        result = present(canonical, law_labels=labels)
        for country, demonym in pairs:
            self.assertIn(f'{country}\nUnder {demonym} law, ', result)
        self.assertIn('Comparison\nThe rules vary by country [1].', result)

    def test_multi_topic_separate_paragraphs(self):
        text = present(
            'France\n- Working time is 35 hours [1].\n- Leave is five weeks [2].',
            law_labels={'France': 'Under French law'}, split_themes=True,
        )
        self.assertEqual(
            'Under French law, working time is 35 hours [1].\n\n'
            'Leave is five weeks [2].', text,
        )

    def test_uncertainty_placeholder_eliminated_at_source(self):
        source = inspect.getsource(_present_single_country_evidence)
        self.assertNotIn('unconfirmed legal point(s)', source)
        self.assertIn('The available documents do not establish', source)
        self.assertIn('contact the L&E Global member firm', source)

    def test_uncertain_only_remains_contact_referral_not_raw_llm_uncertainty(self):
        result = _present_single_country_evidence(
            'France\n- I cannot reliably confirm the dismissal notice. [1]\n'
            '- The meeting requires five days. [2]', 'FR',
            original_question='What is the dismissal notice in France?',
        )
        self.assertNotIn('five days', result)
        self.assertNotIn('cannot reliably', result.lower())
        self.assertNotIn('point(s)', result)
        self.assertIn('contact the L&E Global member firm in France', result)

    def test_uncertain_mixed_retains_supported_and_referral(self):
        result = _present_single_country_evidence(
            'France\n- The working week is 35 hours. [1]\n'
            '- I cannot reliably confirm the exact fine. [2]', 'FR',
            original_question='What are working hours in France, and the exact fine?',
        )
        self.assertIn('35 hours. [1]', result)
        self.assertNotIn('cannot reliably', result.lower())
        self.assertNotIn('point(s)', result)
        self.assertEqual(result.count('member firm'), 1)

    def test_malformed_answer_not_invented_or_rewritten(self):
        original = 'France\nA free text legal opinion [1]'
        self.assertEqual(original, present(original, law_labels={'France':'Under French law'}))


if __name__ == '__main__':
    unittest.main()
