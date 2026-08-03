"""Tests for chatbot frontend configuration."""

from __future__ import annotations

import unittest

from app.models.catalog import (
    LegalCatalogCountry,
    LegalCatalogResponse,
    LegalCatalogValue,
)
from app.models.chat import HISTORY_MAX_MESSAGES
from app.services.frontend_config import (
    API_VERSION,
    FrontendConfigError,
    get_frontend_config,
)
from app.services.legal_catalog import (
    LegalCatalogError,
)


def _build_catalog() -> LegalCatalogResponse:
    """Build one test legal catalog."""

    return LegalCatalogResponse(
        countries=[
            LegalCatalogCountry(
                country_code="GB",
                country="United Kingdom",
                chunk_count=41,
            )
        ],
        legal_topics=[
            LegalCatalogValue(
                value="Employment Contracts",
                chunk_count=20,
            )
        ],
        subsections=[
            LegalCatalogValue(
                value="Notice Period",
                chunk_count=12,
            )
        ],
    )


class FrontendConfigTests(unittest.TestCase):
    """Tests for frontend bootstrap configuration."""

    def test_frontend_config_contains_catalog(
        self,
    ) -> None:
        response = get_frontend_config(
            catalog_provider=_build_catalog
        )

        self.assertEqual(
            response.api_version,
            API_VERSION,
        )

        self.assertEqual(
            response.default_language,
            "en",
        )

        self.assertEqual(
            response.supported_languages,
            ["en"],
        )

        self.assertEqual(
            len(response.catalog.countries),
            1,
        )

        self.assertEqual(
            response.catalog.countries[0].country_code,
            "GB",
        )

    def test_frontend_config_exposes_input_limits(
        self,
    ) -> None:
        response = get_frontend_config(
            catalog_provider=_build_catalog
        )

        self.assertEqual(
            response.limits.question_min_length,
            2,
        )

        self.assertEqual(
            response.limits.question_max_length,
            2000,
        )

        self.assertEqual(
            response.limits.max_sources_default,
            6,
        )

        self.assertEqual(
            response.limits.max_sources_min,
            1,
        )

        self.assertEqual(
            response.limits.max_sources_max,
            10,
        )

        self.assertEqual(
            response.limits.max_history_messages,
            HISTORY_MAX_MESSAGES,
        )

    def test_catalog_errors_are_wrapped(
        self,
    ) -> None:
        def failing_catalog_provider() -> LegalCatalogResponse:
            raise LegalCatalogError(
                "OpenSearch unavailable"
            )

        with self.assertRaises(
            FrontendConfigError
        ):
            get_frontend_config(
                catalog_provider=(
                    failing_catalog_provider
                )
            )


if __name__ == "__main__":
    unittest.main()