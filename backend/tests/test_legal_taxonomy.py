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

    def test_recognizes_plain_canonical_topic(
        self,
    ) -> None:
        self.assertEqual(
            get_canonical_legal_topic(
                section="Pay Equity Laws",
            ),
            "Pay Equity Laws",
        )

    def test_recognizes_canonical_topic_with_trailing_annotation(
        self,
    ) -> None:
        """
        GATE 0B.4 / PT_PAY_EQUITY_FINDING: a contributor's harmless
        "(NEW SECTION)" editorial annotation must not hide an
        otherwise-canonical heading from taxonomy matching.
        """

        self.assertEqual(
            get_canonical_legal_topic(
                section="PAY EQUITY LAWS (NEW SECTION)",
            ),
            "Pay Equity Laws",
        )

    def test_recognizes_numbered_canonical_topic_with_trailing_annotation(
        self,
    ) -> None:
        """The exact heading text found in the current PT source."""

        self.assertEqual(
            get_canonical_legal_topic(
                section="VI. PAY EQUITY LAWS (NEW SECTION)",
            ),
            "Pay Equity Laws",
        )

    def test_trailing_annotation_never_creates_a_false_match(
        self,
    ) -> None:
        """
        An unrelated custom heading that happens to carry a
        parenthetical remark must remain unrecognized - stripping the
        annotation must never turn non-canonical text into a false
        canonical match.
        """

        self.assertIsNone(
            get_canonical_legal_topic(
                section=(
                    "V060 Temporary Validation Section "
                    "(Draft)"
                ),
            )
        )

    def test_trailing_annotation_stripping_never_blanks_the_label(
        self,
    ) -> None:
        """A label that is nothing but a parenthetical must be left
        alone rather than reduced to an empty, always-matching
        string."""

        self.assertEqual(
            normalize_topic(
                section="(New Section)",
            ),
            "(New Section)",
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


class JurisdictionAliasSuffixTests(unittest.TestCase):
    """
    Mission "HOTFIX 0.4.4" - final targeted correction: a heading's
    trailing "in <jurisdiction>" suffix must be recognized for every
    safe alias the country registry itself already knows for that
    country, not only its canonical display name.
    """

    def test_recognizes_in_the_usa(self) -> None:
        self.assertEqual(
            get_canonical_legal_topic(
                section="Social Media and Data Privacy in the USA",
                country="United States",
            ),
            "Social Media and Data Privacy",
        )

    def test_recognizes_in_the_u_s(self) -> None:
        self.assertEqual(
            get_canonical_legal_topic(
                section="Social Media and Data Privacy in the U.S.",
                country="United States",
            ),
            "Social Media and Data Privacy",
        )

    def test_recognizes_in_the_u_s_a(self) -> None:
        self.assertEqual(
            get_canonical_legal_topic(
                section="Social Media and Data Privacy in the U.S.A.",
                country="United States",
            ),
            "Social Media and Data Privacy",
        )

    def test_recognizes_in_the_united_states(self) -> None:
        self.assertEqual(
            get_canonical_legal_topic(
                section=(
                    "Social Media and Data Privacy "
                    "in the United States"
                ),
                country="United States",
            ),
            "Social Media and Data Privacy",
        )

    def test_unrelated_country_suffix_still_works(self) -> None:
        # Proves the fix generalizes through the registry, not a
        # USA-only special case.
        self.assertEqual(
            get_canonical_legal_topic(
                section="Social Media and Data Privacy in Canada",
                country="Canada",
            ),
            "Social Media and Data Privacy",
        )

    def test_the_pronoun_us_is_never_read_as_united_states(self) -> None:
        self.assertIsNone(
            get_canonical_legal_topic(
                section="The employer told us about social media",
                country="United States",
            )
        )

    def test_a_non_canonical_heading_mentioning_usa_invents_nothing(
        self,
    ) -> None:
        self.assertIsNone(
            get_canonical_legal_topic(
                section="Recent Developments in the USA",
                country="United States",
            )
        )

    def test_a_jurisdiction_mid_title_is_not_stripped(self) -> None:
        # The alias appears in the middle, not as a terminal suffix -
        # this must not resolve to any of the 11 canonical topics.
        self.assertIsNone(
            get_canonical_legal_topic(
                section="In the USA, social media laws are strict",
                country="United States",
            )
        )


if __name__ == "__main__":
    unittest.main()