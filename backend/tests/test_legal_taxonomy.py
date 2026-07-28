import unittest

from app.core.legal_taxonomy import (
    get_canonical_legal_topic,
    is_overview_section,
    normalize_topic,
)


class LegalTaxonomyTests(unittest.TestCase):
    def test_normalizes_standard_numbered_topic(
        self,
    ) -> None:
        self.assertEqual(
            normalize_topic(
                section="01. Hiring Practices",
                country="Spain",
            ),
            "Hiring Practices",
        )

    def test_removes_leading_separator(
        self,
    ) -> None:
        self.assertEqual(
            get_canonical_legal_topic(
                section="| 05. Pay Equity Laws",
                country="Belgium",
            ),
            "Pay Equity Laws",
        )

    def test_removes_trailing_separator(
        self,
    ) -> None:
        self.assertEqual(
            get_canonical_legal_topic(
                section=(
                    "Restrictive Covenants "
                    "in Australia|"
                ),
                country="Australia",
            ),
            "Restrictive Covenants",
        )

    def test_recognizes_australian_topic_variant(
        self,
    ) -> None:
        self.assertEqual(
            get_canonical_legal_topic(
                section=(
                    "Hiring practices "
                    "in Australia"
                ),
                country="Australia",
            ),
            "Hiring Practices",
        )

    def test_recognizes_czech_topic_variant(
        self,
    ) -> None:
        self.assertEqual(
            get_canonical_legal_topic(
                section=(
                    "1. Employment contract law "
                    "in the Czech Republic"
                ),
                country="Czech Republic",
            ),
            "Employment Contracts",
        )

    def test_recognizes_greek_topic_variant(
        self,
    ) -> None:
        self.assertEqual(
            get_canonical_legal_topic(
                section=(
                    "Employment contract law "
                    "in Greece"
                ),
                country="Greece",
            ),
            "Employment Contracts",
        )

    def test_recognizes_united_kingdom_suffix(
        self,
    ) -> None:
        self.assertEqual(
            get_canonical_legal_topic(
                section=(
                    "06. Social Media and Data "
                    "Privacy in the United Kingdom"
                ),
                country="United Kingdom",
            ),
            "Social Media and Data Privacy",
        )

    def test_rejects_body_sentence_starting_with_topic(
        self,
    ) -> None:
        self.assertIsNone(
            get_canonical_legal_topic(
                section=(
                    "Employment contracts in Australia "
                    "are formed in accordance with "
                    "general contract law."
                ),
                country="Australia",
            )
        )

    def test_rejects_unknown_topic(
        self,
    ) -> None:
        self.assertIsNone(
            get_canonical_legal_topic(
                section="12. Imaginary Legal Topic",
                country="Spain",
            )
        )

    def test_recognizes_country_overview(
        self,
    ) -> None:
        self.assertTrue(
            is_overview_section(
                section=(
                    "Employment Law Overview "
                    "Australia"
                ),
                country="Australia",
            )
        )

    def test_recognizes_general_as_overview(
        self,
    ) -> None:
        self.assertTrue(
            is_overview_section(
                section="General",
                country="Italy",
            )
        )


if __name__ == "__main__":
    unittest.main()