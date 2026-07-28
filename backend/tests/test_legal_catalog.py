"""Tests for the indexed legal corpus catalog."""

from __future__ import annotations

import unittest
from typing import Any

from opensearchpy.exceptions import (
    OpenSearchException,
)

from app.services.legal_catalog import (
    LegalCatalogError,
    build_legal_catalog_body,
    get_legal_catalog,
)
from app.services.opensearch_index import (
    LEGAL_DOCUMENTS_ALIAS,
)


class FakeOpenSearchClient:
    """Minimal OpenSearch client for catalog tests."""

    def __init__(
        self,
        response: dict[str, Any] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.response = response
        self.error = error
        self.index: str | None = None
        self.body: dict[str, Any] | None = None

    def search(
        self,
        index: str,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        self.index = index
        self.body = body

        if self.error is not None:
            raise self.error

        return self.response or {
            "aggregations": {
                "countries": {
                    "buckets": [],
                },
                "legal_topics": {
                    "buckets": [],
                },
                "subsections": {
                    "buckets": [],
                },
            }
        }


class LegalCatalogTests(unittest.TestCase):
    """Tests for legal catalog aggregation parsing."""

    def test_catalog_body_contains_required_aggregations(
        self,
    ) -> None:
        body = build_legal_catalog_body()

        self.assertEqual(
            body["size"],
            0,
        )

        self.assertIn(
            "countries",
            body["aggs"],
        )

        self.assertIn(
            "legal_topics",
            body["aggs"],
        )

        self.assertIn(
            "subsections",
            body["aggs"],
        )

    def test_catalog_returns_structured_values(
        self,
    ) -> None:
        client = FakeOpenSearchClient(
            response={
                "aggregations": {
                    "countries": {
                        "buckets": [
                            {
                                "key": "GB",
                                "doc_count": 41,
                                "country_names": {
                                    "buckets": [
                                        {
                                            "key": (
                                                "United Kingdom"
                                            ),
                                            "doc_count": 41,
                                        }
                                    ]
                                },
                            },
                            {
                                "key": "ES",
                                "doc_count": 49,
                                "country_names": {
                                    "buckets": [
                                        {
                                            "key": "Spain",
                                            "doc_count": 49,
                                        }
                                    ]
                                },
                            },
                        ]
                    },
                    "legal_topics": {
                        "buckets": [
                            {
                                "key": (
                                    "Employment Contracts"
                                ),
                                "doc_count": 25,
                            }
                        ]
                    },
                    "subsections": {
                        "buckets": [
                            {
                                "key": "Notice Period",
                                "doc_count": 12,
                            }
                        ]
                    },
                }
            }
        )

        response = get_legal_catalog(
            client=client
        )

        self.assertEqual(
            client.index,
            LEGAL_DOCUMENTS_ALIAS,
        )

        self.assertEqual(
            len(response.countries),
            2,
        )

        self.assertEqual(
            response.countries[0].country_code,
            "GB",
        )

        self.assertEqual(
            response.countries[0].country,
            "United Kingdom",
        )

        self.assertEqual(
            response.countries[0].chunk_count,
            41,
        )

        self.assertEqual(
            response.legal_topics[0].value,
            "Employment Contracts",
        )

        self.assertEqual(
            response.subsections[0].value,
            "Notice Period",
        )

    def test_empty_catalog_is_supported(
        self,
    ) -> None:
        client = FakeOpenSearchClient()

        response = get_legal_catalog(
            client=client
        )

        self.assertEqual(
            response.countries,
            [],
        )

        self.assertEqual(
            response.legal_topics,
            [],
        )

        self.assertEqual(
            response.subsections,
            [],
        )

    def test_opensearch_error_is_wrapped(
        self,
    ) -> None:
        client = FakeOpenSearchClient(
            error=OpenSearchException(
                "Unavailable"
            )
        )

        with self.assertRaises(
            LegalCatalogError
        ):
            get_legal_catalog(
                client=client
            )


if __name__ == "__main__":
    unittest.main()