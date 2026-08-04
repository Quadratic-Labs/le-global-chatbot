"""Tests for automatic country detection and availability."""

from __future__ import annotations

import unittest

from app.models.catalog import (
    LegalCatalogCountry,
    LegalCatalogResponse,
)
from app.models.chat import LegalChatRequest
from app.services.country_detection import (
    CountryDetectionError,
    detect_mentioned_country_codes,
    is_country_only_followup,
    resolve_country_availability,
    resolve_country_display_name,
)
from app.services.legal_catalog import LegalCatalogError


def _build_catalog() -> LegalCatalogResponse:
    """Build the country catalog used by tests."""

    return LegalCatalogResponse(
        countries=[
            LegalCatalogCountry(
                country_code="GB",
                country="United Kingdom",
                chunk_count=41,
            ),
            LegalCatalogCountry(
                country_code="ES",
                country="Spain",
                chunk_count=49,
            ),
            LegalCatalogCountry(
                country_code="IT",
                country="Italy",
                chunk_count=63,
            ),
            LegalCatalogCountry(
                country_code="CZ",
                country="Czech Republic",
                chunk_count=54,
            ),
        ],
        legal_topics=[],
        subsections=[],
    )


def _catalog_provider() -> LegalCatalogResponse:
    """Return the test catalog."""

    return _build_catalog()


class CountryDetectionTests(unittest.TestCase):
    """Tests for worldwide country-name detection."""

    def test_detects_alias_and_country_name(
        self,
    ) -> None:
        detected_codes = detect_mentioned_country_codes(
            "Compare notice periods in "
            "the UK and Spain."
        )

        self.assertEqual(
            detected_codes,
            [
                "GB",
                "ES",
            ],
        )

    def test_bare_uppercase_code_in_free_text_is_not_detected(
        self,
    ) -> None:
        detected_codes = detect_mentioned_country_codes(
            "What is the notice period in IT?"
        )

        self.assertEqual(
            detected_codes,
            [],
        )

    def test_lowercase_word_is_not_country_code(
        self,
    ) -> None:
        detected_codes = detect_mentioned_country_codes(
            "Can it be terminated immediately?"
        )

        self.assertEqual(
            detected_codes,
            [],
        )

    def test_detects_country_alias(
        self,
    ) -> None:
        detected_codes = detect_mentioned_country_codes(
            "What rules apply in Czechia?"
        )

        self.assertEqual(
            detected_codes,
            [
                "CZ",
            ],
        )

    def test_detects_country_outside_the_corpus(
        self,
    ) -> None:
        detected_codes = detect_mentioned_country_codes(
            "What are the overtime rules in Canada?"
        )

        self.assertEqual(
            detected_codes,
            [
                "CA",
            ],
        )


class IsCountryOnlyFollowupTests(unittest.TestCase):
    """
    Mission "CORRECTION FINALE CIBLEE 0.4.2" - deterministic
    classification of a bare country-only follow-up, distinct from a
    message that also carries its own legal subject.
    """

    def test_bare_peru_with_question_mark(self) -> None:
        self.assertEqual(is_country_only_followup("Peru?"), ["PE"])

    def test_bare_australia_without_question_mark(self) -> None:
        self.assertEqual(is_country_only_followup("Australia"), ["AU"])

    def test_what_about_peru(self) -> None:
        self.assertEqual(
            is_country_only_followup("What about Peru?"), ["PE"]
        )

    def test_and_peru(self) -> None:
        self.assertEqual(is_country_only_followup("And Peru?"), ["PE"])

    def test_and_australia(self) -> None:
        self.assertEqual(
            is_country_only_followup("And Australia?"), ["AU"]
        )

    def test_how_about_the_united_kingdom(self) -> None:
        self.assertEqual(
            is_country_only_followup("How about the United Kingdom?"),
            ["GB"],
        )

    def test_for_spain(self) -> None:
        self.assertEqual(is_country_only_followup("For Spain?"), ["ES"])

    def test_overtime_in_peru_is_not_country_only(self) -> None:
        self.assertIsNone(is_country_only_followup("Overtime in Peru?"))

    def test_sick_leave_in_peru_is_not_country_only(self) -> None:
        self.assertIsNone(
            is_country_only_followup("What about sick leave in Peru?")
        )

    def test_contacts_in_spain_is_not_country_only(self) -> None:
        self.assertIsNone(
            is_country_only_followup("Contacts in Spain")
        )

    def test_compare_spain_and_peru_is_not_country_only(self) -> None:
        self.assertIsNone(
            is_country_only_followup("Compare Spain and Peru")
        )

    def test_peru_working_conditions_is_not_country_only(self) -> None:
        self.assertIsNone(
            is_country_only_followup("Peru working conditions")
        )

    def test_dismissal_in_australia_is_not_country_only(self) -> None:
        self.assertIsNone(
            is_country_only_followup("Dismissal in Australia")
        )

    def test_no_country_mentioned_returns_none(self) -> None:
        self.assertIsNone(
            is_country_only_followup("Tell me about overtime rules.")
        )

    def test_general_question_naming_a_country_is_not_country_only(
        self,
    ) -> None:
        # The mission's own explicit "must keep working" example - a
        # genuine new question must never be misclassified as bare.
        self.assertIsNone(
            is_country_only_followup(
                "Tell me about working conditions in Peru."
            )
        )


class CountryAvailabilityTests(unittest.TestCase):
    """Tests for splitting mentioned countries by corpus availability."""

    def test_available_country_is_reported_as_available(
        self,
    ) -> None:
        availability = resolve_country_availability(
            request=LegalChatRequest(
                question=(
                    "Compare notice periods in "
                    "the UK and Spain."
                )
            ),
            catalog_provider=_catalog_provider,
        )

        self.assertEqual(
            availability.available_codes,
            [
                "GB",
                "ES",
            ],
        )

        self.assertEqual(
            availability.unavailable_codes,
            [],
        )

    def test_unavailable_country_is_reported_not_ignored(
        self,
    ) -> None:
        availability = resolve_country_availability(
            request=LegalChatRequest(
                question=(
                    "What is the law in France?"
                )
            ),
            catalog_provider=_catalog_provider,
        )

        self.assertEqual(
            availability.available_codes,
            [],
        )

        self.assertEqual(
            availability.unavailable_codes,
            [
                "FR",
            ],
        )

    def test_mixed_available_and_unavailable_countries(
        self,
    ) -> None:
        availability = resolve_country_availability(
            request=LegalChatRequest(
                question=(
                    "Compare overtime in Spain and Canada."
                )
            ),
            catalog_provider=_catalog_provider,
        )

        self.assertEqual(
            availability.available_codes,
            [
                "ES",
            ],
        )

        self.assertEqual(
            availability.unavailable_codes,
            [
                "CA",
            ],
        )

    def test_no_country_mentioned_is_empty_scope(
        self,
    ) -> None:
        availability = resolve_country_availability(
            request=LegalChatRequest(
                question=(
                    "What is the statutory notice period?"
                )
            ),
            catalog_provider=_catalog_provider,
        )

        self.assertEqual(
            availability.available_codes,
            [],
        )

        self.assertEqual(
            availability.unavailable_codes,
            [],
        )

    def test_explicit_codes_are_checked_against_the_catalog(
        self,
    ) -> None:
        catalog_called = False

        def catalog_provider() -> LegalCatalogResponse:
            nonlocal catalog_called
            catalog_called = True

            return _build_catalog()

        availability = resolve_country_availability(
            request=LegalChatRequest(
                question=(
                    "Compare the UK and Spain."
                ),
                country_codes=[
                    " it ",
                ],
            ),
            catalog_provider=catalog_provider,
        )

        self.assertTrue(
            catalog_called
        )

        self.assertEqual(
            availability.available_codes,
            [
                "IT",
            ],
        )

    def test_explicit_unavailable_code_is_reported(
        self,
    ) -> None:
        availability = resolve_country_availability(
            request=LegalChatRequest(
                question="What is the law here?",
                country_codes=[
                    "ca",
                ],
            ),
            catalog_provider=_catalog_provider,
        )

        self.assertEqual(
            availability.available_codes,
            [],
        )

        self.assertEqual(
            availability.unavailable_codes,
            [
                "CA",
            ],
        )

    def test_catalog_error_is_wrapped(
        self,
    ) -> None:
        def failing_catalog_provider() -> (
            LegalCatalogResponse
        ):
            raise LegalCatalogError(
                "unavailable"
            )

        with self.assertRaises(
            CountryDetectionError
        ):
            resolve_country_availability(
                request=LegalChatRequest(
                    question="What is the law in Canada?"
                ),
                catalog_provider=failing_catalog_provider,
            )


class CountryDisplayNameTests(unittest.TestCase):
    """Tests for resolving readable country names from codes."""

    def test_resolves_known_code(
        self,
    ) -> None:
        self.assertEqual(
            resolve_country_display_name("CA"),
            "Canada",
        )

        self.assertEqual(
            resolve_country_display_name("gb"),
            "United Kingdom",
        )

    def test_unknown_code_falls_back_to_the_code(
        self,
    ) -> None:
        self.assertEqual(
            resolve_country_display_name("ZZ"),
            "ZZ",
        )


if __name__ == "__main__":
    unittest.main()
