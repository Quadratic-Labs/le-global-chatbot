"""Tests for the indexed legal corpus catalog."""

from __future__ import annotations

import unittest
from typing import Any

from opensearchpy.exceptions import (
    OpenSearchException,
)

from app.services.legal_catalog import (
    LegalCatalogError,
    build_document_legal_topics_body,
    build_legal_catalog_body,
    get_document_legal_topics_by_country,
    get_legal_catalog,
)
from app.services.opensearch_index import (
    LEGAL_DOCUMENTS_ALIAS,
)
from tests.support.opensearch_fixtures import FakeOpenSearchClient

_EMPTY_CATALOG_RESPONSE: dict[str, Any] = {
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
        client = FakeOpenSearchClient(
            response=_EMPTY_CATALOG_RESPONSE
        )

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


class DocumentLegalTopicsByCountryTests(unittest.TestCase):
    """
    Tests for get_document_legal_topics_by_country (mission
    "ORDER 8F-A") - one compact, country-scoped aggregation for the
    LIVE legal_topic vocabulary actually indexed, distinct from
    get_legal_catalog's own global, unscoped aggregation.
    """

    def test_empty_country_codes_makes_no_opensearch_call(self) -> None:
        client = FakeOpenSearchClient()

        result = get_document_legal_topics_by_country(
            [],
            client=client,
        )

        self.assertEqual(result, {})
        self.assertIsNone(client.index)

    def test_body_is_scoped_to_requested_countries(self) -> None:
        body = build_document_legal_topics_body(["au", "AU", " be "])

        self.assertEqual(body["size"], 0)
        self.assertEqual(
            body["query"]["terms"]["country_code"],
            ["AU", "BE"],
        )
        self.assertIn("countries", body["aggs"])
        self.assertIn(
            "legal_topics", body["aggs"]["countries"]["aggs"]
        )

    def test_returns_canonical_and_custom_topics_per_country(
        self,
    ) -> None:
        client = FakeOpenSearchClient(
            response={
                "aggregations": {
                    "countries": {
                        "buckets": [
                            {
                                "key": "AU",
                                "doc_count": 3,
                                "legal_topics": {
                                    "buckets": [
                                        {
                                            "key": "Hiring Practices",
                                            "doc_count": 1,
                                        },
                                        {
                                            "key": (
                                                "V060 Temporary "
                                                "Validation Section"
                                            ),
                                            "doc_count": 1,
                                        },
                                        {
                                            "key": (
                                                "Foreign Employee Work "
                                                "Eligibility Checks"
                                            ),
                                            "doc_count": 1,
                                        },
                                    ]
                                },
                            },
                            {
                                "key": "BE",
                                "doc_count": 1,
                                "legal_topics": {
                                    "buckets": [
                                        {
                                            "key": "Hiring Practices",
                                            "doc_count": 1,
                                        },
                                    ]
                                },
                            },
                        ]
                    },
                }
            }
        )

        result = get_document_legal_topics_by_country(
            ["AU", "BE"],
            client=client,
        )

        self.assertEqual(
            client.index,
            LEGAL_DOCUMENTS_ALIAS,
        )

        self.assertEqual(
            result,
            {
                "AU": [
                    "Hiring Practices",
                    "V060 Temporary Validation Section",
                    "Foreign Employee Work Eligibility Checks",
                ],
                "BE": ["Hiring Practices"],
            },
        )

    def test_country_with_no_indexed_topics_is_absent(self) -> None:
        client = FakeOpenSearchClient(
            response={
                "aggregations": {
                    "countries": {
                        "buckets": [],
                    },
                }
            }
        )

        result = get_document_legal_topics_by_country(
            ["ZZ"],
            client=client,
        )

        self.assertEqual(result, {})

    def test_opensearch_error_is_wrapped(self) -> None:
        client = FakeOpenSearchClient(
            error=OpenSearchException("Unavailable")
        )

        with self.assertRaises(LegalCatalogError):
            get_document_legal_topics_by_country(
                ["AU"],
                client=client,
            )


if __name__ == "__main__":
    unittest.main()