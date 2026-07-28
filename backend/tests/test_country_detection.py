"""Tests for automatic country detection."""

from __future__ import annotations

import unittest

from app.models.catalog import (
    LegalCatalogCountry,
    LegalCatalogResponse,
)
from app.models.chat import LegalChatRequest
from app.services.country_detection import (
    detect_country_codes,
    prepare_legal_chat_request,
)


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
    """Tests for country extraction from questions."""

    def test_detects_alias_and_country_name(
        self,
    ) -> None:
        detected_codes = detect_country_codes(
            question=(
                "Compare notice periods in "
                "the UK and Spain."
            ),
            catalog_provider=_catalog_provider,
        )

        self.assertEqual(
            detected_codes,
            [
                "GB",
                "ES",
            ],
        )

    def test_detects_explicit_uppercase_code(
        self,
    ) -> None:
        detected_codes = detect_country_codes(
            question=(
                "What is the notice period in IT?"
            ),
            catalog_provider=_catalog_provider,
        )

        self.assertEqual(
            detected_codes,
            [
                "IT",
            ],
        )

    def test_lowercase_word_is_not_country_code(
        self,
    ) -> None:
        detected_codes = detect_country_codes(
            question=(
                "Can it be terminated immediately?"
            ),
            catalog_provider=_catalog_provider,
        )

        self.assertEqual(
            detected_codes,
            [],
        )

    def test_detects_country_alias(
        self,
    ) -> None:
        detected_codes = detect_country_codes(
            question=(
                "What rules apply in Czechia?"
            ),
            catalog_provider=_catalog_provider,
        )

        self.assertEqual(
            detected_codes,
            [
                "CZ",
            ],
        )

    def test_unavailable_country_is_ignored(
        self,
    ) -> None:
        detected_codes = detect_country_codes(
            question=(
                "What is the law in France?"
            ),
            catalog_provider=_catalog_provider,
        )

        self.assertEqual(
            detected_codes,
            [],
        )

    def test_explicit_filters_take_priority(
        self,
    ) -> None:
        catalog_called = False

        def catalog_provider() -> LegalCatalogResponse:
            nonlocal catalog_called
            catalog_called = True

            return _build_catalog()

        prepared_request = (
            prepare_legal_chat_request(
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
        )

        self.assertFalse(
            catalog_called
        )

        self.assertEqual(
            prepared_request.country_codes,
            [
                "IT",
            ],
        )

    def test_detected_filters_are_added_to_request(
        self,
    ) -> None:
        prepared_request = (
            prepare_legal_chat_request(
                request=LegalChatRequest(
                    question=(
                        "Compare the UK and Spain."
                    ),
                    max_sources=4,
                ),
                catalog_provider=_catalog_provider,
            )
        )

        self.assertEqual(
            prepared_request.country_codes,
            [
                "GB",
                "ES",
            ],
        )

        self.assertEqual(
            prepared_request.max_sources,
            4,
        )


if __name__ == "__main__":
    unittest.main()