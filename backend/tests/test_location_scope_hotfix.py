from __future__ import annotations

import unittest

from app.models.catalog import (
    LegalCatalogCountry,
    LegalCatalogResponse,
)
from app.models.chat import LegalChatRequest
from app.routers.chat import (
    _build_contact_section,
    _resolve_current_country_scope,
    _resolve_unique_capital_country_code,
    _unavailable_countries_answer,
)


def _catalog() -> LegalCatalogResponse:
    return LegalCatalogResponse(
        countries=[
            LegalCatalogCountry(
                country_code="IT",
                country="Italy",
                chunk_count=20,
            ),
            LegalCatalogCountry(
                country_code="US",
                country="United States",
                chunk_count=20,
            ),
            LegalCatalogCountry(
                country_code="ES",
                country="Spain",
                chunk_count=20,
            ),
            LegalCatalogCountry(
                country_code="AU",
                country="Australia",
                chunk_count=20,
            ),
            LegalCatalogCountry(
                country_code="DE",
                country="Germany",
                chunk_count=20,
            ),
            LegalCatalogCountry(
                country_code="FR",
                country="France",
                chunk_count=20,
            ),
        ],
        legal_topics=[],
        subsections=[],
    )


class LocationScopeHotfixTests(unittest.TestCase):

    def test_rome_has_unique_capital_candidate(self):
        self.assertEqual(
            _resolve_unique_capital_country_code(
                "Rome",
                frozenset({"IT", "TG", "US"}),
            ),
            "IT",
        )

    def test_milan_is_not_capital_preferred(self):
        self.assertIsNone(
            _resolve_unique_capital_country_code(
                "Milan",
                frozenset({"IT", "US"}),
            )
        )

    def test_barcelona_is_not_capital_preferred(self):
        self.assertIsNone(
            _resolve_unique_capital_country_code(
                "Barcelona",
                frozenset({"ES", "VE"}),
            )
        )

    def test_contact_for_rome_resolves_italy(self):
        scope = _resolve_current_country_scope(
            LegalChatRequest(
                question=(
                    "Can I have the contact details for Rome?"
                )
            ),
            _catalog,
        )

        self.assertEqual(
            scope.available_codes,
            ["IT"],
        )
        self.assertEqual(
            scope.unavailable_codes,
            [],
        )

    def test_contact_for_milan_is_not_forced_when_two_supported(
        self,
    ):
        scope = _resolve_current_country_scope(
            LegalChatRequest(
                question=(
                    "Can I have the contact details for Milan?"
                )
            ),
            _catalog,
        )

        self.assertEqual(
            scope.available_codes,
            [],
        )
        self.assertEqual(
            scope.unavailable_codes,
            [],
        )

    def test_tunis_resolves_to_unsupported_tunisia(self):
        scope = _resolve_current_country_scope(
            LegalChatRequest(
                question=(
                    "Can I have the contact details for Tunis?"
                )
            ),
            _catalog,
        )

        self.assertEqual(
            scope.available_codes,
            [],
        )
        self.assertEqual(
            scope.unavailable_codes,
            ["TN"],
        )

    def test_tunisia_wording_is_unsupported_not_missing_contact(
        self,
    ):
        answer = _unavailable_countries_answer(
            ["TN"]
        )

        self.assertIn(
            "Tunisia",
            answer,
        )
        self.assertIn(
            "not currently covered",
            answer,
        )
        self.assertIn(
            "cannot provide employment-law information",
            answer,
        )
        self.assertNotIn(
            "could not find a validated",
            answer,
        )

    def test_tunisia_contact_section_does_not_fake_a_search(
        self,
    ):
        (
            answer,
            sources,
            total,
            took_ms,
        ) = _build_contact_section(
            country_codes=[],
            unavailable_country_codes=["TN"],
            citation_offset=0,
        )

        self.assertIn(
            "not currently covered",
            answer,
        )
        self.assertNotIn(
            "could not find a validated",
            answer,
        )
        self.assertEqual(sources, [])
        self.assertEqual(total, 0)
        self.assertEqual(took_ms, 0.0)


if __name__ == "__main__":
    unittest.main()
