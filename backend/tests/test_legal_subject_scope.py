"""
Tests for legal_subject_scope.py - the centralized jurisdiction-
neutral-subject canonicalization service (see the module's own
docstring for the defect this exists to fix).
"""

from __future__ import annotations

import time
import unittest

from app.services.legal_subject_scope import (
    CanonicalSearchConcept,
    canonicalize_legal_subject,
)


class CanonicalizeSubjectTextParameterizedTests(unittest.TestCase):
    """Phase 15's 25 parametrized cases."""

    def _assert_subject(
        self,
        subject_text,
        country_codes,
        expected,
        *,
        msg=None,
    ):
        result = canonicalize_legal_subject(
            subject_text=subject_text,
            search_concepts=[],
            scoped_country_codes=country_codes,
        )
        self.assertEqual(result.subject_text, expected, msg=msg)

    def test_01_remote_work_in_spain(self):
        self._assert_subject(
            "remote work in Spain", ["ES"], "remote work"
        )

    def test_02_remote_work_for_spain(self):
        self._assert_subject(
            "remote work for Spain", ["ES"], "remote work"
        )

    def test_03_remote_work_under_spanish_law(self):
        self._assert_subject(
            "remote work under Spanish law", ["ES"], "remote work"
        )

    def test_04_under_spanish_employment_law_leading(self):
        self._assert_subject(
            "under Spanish employment law, remote work rules",
            ["ES"],
            "remote work rules",
        )

    def test_05_spain_colon(self):
        self._assert_subject(
            "Spain: remote work rules", ["ES"], "remote work rules"
        )

    def test_06_spain_possessive(self):
        self._assert_subject(
            "Spain's rules on remote work",
            ["ES"],
            "rules on remote work",
        )

    def test_07_dismissal_sick_leave_peru(self):
        self._assert_subject(
            "dismissal while on sick leave in Peru",
            ["PE"],
            "dismissal while on sick leave",
        )

    def test_08_fixed_term_united_kingdom_with_the(self):
        self._assert_subject(
            "fixed-term contracts in the United Kingdom",
            ["GB"],
            "fixed-term contracts",
        )

    def test_09_overtime_australia(self):
        self._assert_subject(
            "overtime rules in Australia", ["AU"], "overtime rules"
        )

    def test_10_overtime_sydney_no_city_taxonomy(self):
        # No city-to-country data exists anywhere in this codebase
        # (see country_detection.py) - "Sydney" is not a recognized
        # geographic-scope variant for AU, so it is left untouched.
        # Documented, deliberate limitation - see the mission report.
        result = canonicalize_legal_subject(
            subject_text="overtime rules in Sydney",
            search_concepts=[],
            scoped_country_codes=["AU"],
        )
        self.assertEqual(result.subject_text, "overtime rules in Sydney")
        self.assertFalse(result.changed)

    def test_11_overtime_spain_and_peru(self):
        self._assert_subject(
            "overtime rules in Spain and Peru",
            ["ES", "PE"],
            "overtime rules",
        )

    def test_12_compare_overtime_between_spain_and_peru(self):
        self._assert_subject(
            "compare overtime between Spain and Peru",
            ["ES", "PE"],
            "compare overtime",
        )

    def test_13_no_country_unchanged(self):
        result = canonicalize_legal_subject(
            subject_text="overtime rules",
            search_concepts=[],
            scoped_country_codes=["ES"],
        )
        self.assertEqual(result.subject_text, "overtime rules")
        self.assertFalse(result.changed)
        self.assertEqual(result.removed_country_codes, [])

    def test_14_different_case(self):
        self._assert_subject(
            "REMOTE WORK IN spain", ["ES"], "REMOTE WORK"
        )

    def test_15_multiple_spaces(self):
        # Whitespace is fully normalized once a geographic frame is
        # actually stripped - not merely preserved verbatim.
        self._assert_subject(
            "remote  work   in Spain", ["ES"], "remote work"
        )

    def test_16_straight_and_typographic_apostrophe(self):
        self._assert_subject(
            "Spain's rules on overtime",
            ["ES"],
            "rules on overtime",
            msg="straight apostrophe",
        )
        self._assert_subject(
            "Spain’s rules on overtime",
            ["ES"],
            "rules on overtime",
            msg="typographic right single quote",
        )

    def test_17_unicode_dashes_preserved_in_content(self):
        # Unicode dashes inside the retained subject content (not part
        # of a stripped geographic frame) must survive untouched.
        result = canonicalize_legal_subject(
            subject_text="fixed–term contracts in Spain",
            search_concepts=[],
            scoped_country_codes=["ES"],
        )
        self.assertEqual(result.subject_text, "fixed–term contracts")

    def test_18_terminal_punctuation_stripped(self):
        self._assert_subject(
            "remote work in Spain.", ["ES"], "remote work."
        )
        self._assert_subject(
            "remote work in Spain,", ["ES"], "remote work"
        )

    def test_19_empty_result_flagged(self):
        result = canonicalize_legal_subject(
            subject_text="Spain",
            search_concepts=[],
            scoped_country_codes=["ES"],
        )
        self.assertIsNone(result.subject_text)
        self.assertTrue(result.subject_became_empty)
        self.assertTrue(result.changed)

    def test_20_long_text_not_expanded(self):
        long_subject = "overtime rules in Spain " + ("x" * 500)
        result = canonicalize_legal_subject(
            subject_text=long_subject,
            search_concepts=[],
            scoped_country_codes=["ES"],
        )
        self.assertLessEqual(
            len(result.subject_text), len(long_subject)
        )

    def test_21_unicode_characters_do_not_crash(self):
        result = canonicalize_legal_subject(
            subject_text="rémunération rules in Spain",
            search_concepts=[],
            scoped_country_codes=["ES"],
        )
        self.assertEqual(
            result.subject_text, "rémunération rules"
        )

    def test_22_uk_short_alias(self):
        self._assert_subject(
            "fixed-term contracts in the UK",
            ["GB"],
            "fixed-term contracts",
        )
        self._assert_subject(
            "fixed-term contracts under UK law",
            ["GB"],
            "fixed-term contracts",
        )

    def test_23_uk_dotted_alias(self):
        self._assert_subject(
            "fixed-term contracts in the U.K.",
            ["GB"],
            "fixed-term contracts",
        )

    def test_24_british_demonym(self):
        self._assert_subject(
            "overtime rules under British law",
            ["GB"],
            "overtime rules",
        )

    def test_25_no_dangerous_partial_replacement(self):
        # "Spain" must not partially match inside an unrelated word -
        # word-boundary safety.
        result = canonicalize_legal_subject(
            subject_text="Spainish-sounding trademark dispute rules",
            search_concepts=[],
            scoped_country_codes=["ES"],
        )
        self.assertEqual(
            result.subject_text,
            "Spainish-sounding trademark dispute rules",
        )
        self.assertFalse(result.changed)


class CanonicalizePreservesLegalContentTests(unittest.TestCase):
    """Invariant 8: never mangle a law/institution name."""

    def test_fair_work_act_preserved(self):
        result = canonicalize_legal_subject(
            subject_text=(
                "whether the Fair Work Act applies to casual "
                "employees in Australia"
            ),
            search_concepts=[],
            scoped_country_codes=["AU"],
        )
        self.assertIn("Fair Work Act", result.subject_text)
        self.assertNotIn("Australia", result.subject_text)

    def test_workers_statute_preserved(self):
        result = canonicalize_legal_subject(
            subject_text=(
                "whether the Workers' Statute permits remote work "
                "in Spain"
            ),
            search_concepts=[],
            scoped_country_codes=["ES"],
        )
        self.assertIn("Workers' Statute", result.subject_text)
        self.assertNotIn(" in Spain", result.subject_text)

    def test_national_employment_standards_preserved(self):
        result = canonicalize_legal_subject(
            subject_text=(
                "how the National Employment Standards apply to "
                "overtime in Australia"
            ),
            search_concepts=[],
            scoped_country_codes=["AU"],
        )
        self.assertIn("National Employment Standards", result.subject_text)

    def test_labour_inspectorate_preserved(self):
        result = canonicalize_legal_subject(
            subject_text=(
                "the powers of the Labour Inspectorate in Spain"
            ),
            search_concepts=[],
            scoped_country_codes=["ES"],
        )
        self.assertIn("Labour Inspectorate", result.subject_text)
        self.assertNotIn("Spain", result.subject_text)

    def test_institution_name_not_used_as_scope_untouched(self):
        # A law/institution name that is not itself in a geographic
        # frame is never touched, even when it shares no words with
        # any country variant.
        result = canonicalize_legal_subject(
            subject_text="the National Employment Standards",
            search_concepts=[],
            scoped_country_codes=["AU"],
        )
        self.assertEqual(
            result.subject_text, "the National Employment Standards"
        )
        self.assertFalse(result.changed)


class CanonicalizeSearchConceptsTests(unittest.TestCase):
    def test_contaminated_terms_are_cleaned(self):
        result = canonicalize_legal_subject(
            subject_text=None,
            search_concepts=[
                CanonicalSearchConcept(
                    terms=[
                        "remote work in Spain",
                        "telework in Spain",
                        "working from home",
                    ]
                )
            ],
            scoped_country_codes=["ES"],
        )
        self.assertEqual(len(result.search_concepts), 1)
        self.assertEqual(
            result.search_concepts[0].terms,
            ["remote work", "telework", "working from home"],
        )
        self.assertTrue(result.changed)

    def test_group_that_becomes_fully_empty_is_dropped(self):
        result = canonicalize_legal_subject(
            subject_text=None,
            search_concepts=[
                CanonicalSearchConcept(terms=["Spain"]),
                CanonicalSearchConcept(terms=["overtime"]),
            ],
            scoped_country_codes=["ES"],
        )
        self.assertEqual(len(result.search_concepts), 1)
        self.assertEqual(result.search_concepts[0].terms, ["overtime"])

    def test_duplicate_terms_after_stripping_are_deduplicated(self):
        result = canonicalize_legal_subject(
            subject_text=None,
            search_concepts=[
                CanonicalSearchConcept(
                    terms=["remote work in Spain", "remote work for Spain"]
                )
            ],
            scoped_country_codes=["ES"],
        )
        self.assertEqual(
            result.search_concepts[0].terms, ["remote work"]
        )

    def test_evidence_mode_and_untouched_fields_are_the_callers_concern(
        self,
    ):
        # This module has no evidence_mode field at all - confirms it
        # never even has the opportunity to touch it (the caller keeps
        # its own evidence_mode unchanged when integrating this).
        result = canonicalize_legal_subject(
            subject_text="remote work in Spain",
            search_concepts=[],
            scoped_country_codes=["ES"],
        )
        self.assertFalse(hasattr(result, "evidence_mode"))


class RemovedCountryCodesTests(unittest.TestCase):
    def test_only_actually_present_codes_are_reported(self):
        result = canonicalize_legal_subject(
            subject_text="overtime rules in Spain",
            search_concepts=[],
            scoped_country_codes=["ES", "PE"],
        )
        self.assertEqual(result.removed_country_codes, ["ES"])

    def test_additional_country_codes_are_also_checked(self):
        result = canonicalize_legal_subject(
            subject_text="overtime rules in Spain",
            search_concepts=[],
            scoped_country_codes=["PE"],
            additional_country_codes=["ES"],
        )
        self.assertIn("ES", result.removed_country_codes)
        self.assertEqual(result.subject_text, "overtime rules")

    def test_no_country_codes_is_a_no_op(self):
        result = canonicalize_legal_subject(
            subject_text="overtime rules in Spain",
            search_concepts=[],
            scoped_country_codes=[],
        )
        self.assertEqual(result.subject_text, "overtime rules in Spain")
        self.assertFalse(result.changed)
        self.assertEqual(result.removed_country_codes, [])


class PerformanceBoundTests(unittest.TestCase):
    """Phase 22: no network call, bounded, negligible cost."""

    def test_no_network_related_imports(self):
        import ast
        import inspect

        import app.services.legal_subject_scope as module

        tree = ast.parse(inspect.getsource(module))
        imported_top_level_modules: set[str] = set()

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported_top_level_modules.add(
                        alias.name.split(".")[0]
                    )
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_top_level_modules.add(
                    node.module.split(".")[0]
                )

        forbidden_modules = {
            "openai",
            "httpx",
            "requests",
            "urllib3",
            "urllib",
        }

        self.assertEqual(
            imported_top_level_modules & forbidden_modules,
            set(),
        )

    def test_a_thousand_calls_complete_in_well_under_a_second(self):
        start = time.perf_counter()

        for _ in range(1000):
            canonicalize_legal_subject(
                subject_text="overtime rules in Spain and Peru",
                search_concepts=[
                    CanonicalSearchConcept(
                        terms=["overtime in Spain", "extra hours"]
                    )
                ],
                scoped_country_codes=["ES", "PE"],
            )

        elapsed = time.perf_counter() - start

        self.assertLess(elapsed, 1.0)


if __name__ == "__main__":
    unittest.main()
