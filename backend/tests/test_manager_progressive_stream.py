"""Manager: true progress delta formatting without canonical bullet flash."""
import unittest
import re

from app.services.legal_stream_presentation import ProgressiveLegalProse


class ProgressiveLegalProseTests(unittest.TestCase):
    def stream(self, source, *, labels, titles=False, themes=False, width=1):
        presenter = ProgressiveLegalProse(
            law_labels=labels,
            show_country_titles=titles,
            split_themes=themes,
        )
        pieces = []
        for n in range(0, len(source), width):
            piece = presenter.feed(source[n:n+width])
            pieces.append(piece)
            self.assertNotRegex(''.join(pieces), r'(?m)^\s*[-*•]\s+')
        pieces.append(presenter.finish())
        return ''.join(pieces), pieces

    def test_single_country_one_character_chunks(self):
        source = 'Germany\n- The normal day is eight hours [1].\n- Rest is eleven hours [2].'
        final, pieces = self.stream(source, labels={'Germany': 'Under German law'})
        self.assertEqual('Under German law, the normal day is eight hours [1]. Rest is eleven hours [2].', final)
        self.assertGreater(sum(bool(x) for x in pieces), 5)

    def test_comparison_country_titles_and_complete_conclusion(self):
        source = ('France\n- The week is 35 hours [1].\n\n'
                  'Spain\n- The week is 40 hours [2].\n\n'
                  'Comparison\n- The standards differ [1, 2].')
        final, _ = self.stream(source, labels={'France': 'Under French law', 'Spain': 'Under Spanish law'}, titles=True, width=2)
        self.assertEqual('France\nUnder French law, the week is 35 hours [1].\n\n'
                         'Spain\nUnder Spanish law, the week is 40 hours [2].\n\n'
                         'Comparison\nThe standards differ [1, 2].', final)

    def test_multitopic_separate_paragraphs(self):
        source = 'France\n- Working week 35 hours [1].\n- Paid leave five weeks [2].'
        final, _ = self.stream(source, labels={'France': 'Under French law'}, themes=True)
        self.assertEqual('Under French law, working week 35 hours [1].\n\nPaid leave five weeks [2].', final)

    def test_citation_chars_unchanged(self):
        source = 'France\n- Overtime rules apply [1, 2, 3].'
        final, _ = self.stream(source, labels={'France':'Under French law'})
        self.assertEqual(re.findall(r'\[[\d, ]+\]', source), re.findall(r'\[[\d, ]+\]', final))

    def test_do_not_misrender_partial_heading(self):
        p = ProgressiveLegalProse(law_labels={'Germany':'Under German law'})
        self.assertEqual('',p.feed('Ger'))
        self.assertEqual('',p.feed('many'))
        self.assertEqual('',p.feed('\n'))
        self.assertEqual('Under German law, ',p.feed('- '))
        self.assertEqual('',p.feed('T'))
        self.assertEqual('the',p.feed('he'))

    def test_raw_noncanonical_passes_through(self):
        final, _ = self.stream('Partial answer text without canonical headings.', labels={'Germany':'Under German law'})
        self.assertEqual('Partial answer text without canonical headings.', final)

    def test_repair_replaces_cleanly_and_future_deltas_use_new_text(self):
        p = ProgressiveLegalProse(law_labels={'Spain':'Under Spanish law'})
        for ch in 'Spain\n- Wrong initial answer [1].': p.feed(ch)
        v = p.replace('Spain\n- Corrected answer [2].')
        self.assertEqual('Under Spanish law, corrected answer [2].', v)
        self.assertEqual('', p.finish())

    def test_discard_resets_old_answer(self):
        p = ProgressiveLegalProse(law_labels={'Germany':'Under German law'})
        p.feed('Germany\n- Old statement [1].')
        p.reset()
        final = p.feed('Germany\n- New statement [2].')+p.finish()
        self.assertEqual('Under German law, new statement [2].', final)

    def test_acronym_casing_preserved(self):
        final, _ = self.stream('Germany\n- GDPR rules apply [1].', labels={'Germany':'Under German law'})
        self.assertEqual('Under German law, GDPR rules apply [1].', final)

    def test_legal_reference_not_dropped(self):
        final, _ = self.stream('Germany\n- The ECJ (C-55/18) requires recording [1].', labels={'Germany':'Under German law'})
        self.assertIn('(C-55/18)', final)


if __name__ == '__main__': unittest.main()
