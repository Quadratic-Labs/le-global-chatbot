"""Tests for OpenSearch legal document search."""

from __future__ import annotations

import unittest
from typing import Any

from opensearchpy.exceptions import (
    OpenSearchException,
)

from app.models.search import (
    LegalSearchRequest,
)
from app.services.legal_search import (
    InvalidLegalSearchRequestError,
    LegalSearchError,
    build_legal_search_body,
    search_legal_documents,
)
from app.services.opensearch_index import (
    LEGAL_DOCUMENTS_ALIAS,
)


class FakeOpenSearchClient:
    """Minimal OpenSearch client used by unit tests."""

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
            "took": 0,
            "hits": {
                "total": {
                    "value": 0,
                },
                "hits": [],
            },
        }


class LegalSearchTests(
    unittest.TestCase
):
    """Unit tests for legal BM25 search."""

    def test_build_query_with_filters(
        self,
    ) -> None:
        request = LegalSearchRequest(
            query=" notice period ",
            country_codes=[
                "gb",
                "GB",
                " fr ",
            ],
            legal_topics=[
                "Employment Termination",
                "Employment Termination",
            ],
            subsections=[
                "Notice Period",
            ],
            reference_year=2026,
            limit=5,
            offset=10,
        )

        body = build_legal_search_body(
            request
        )

        self.assertEqual(
            body["from"],
            10,
        )

        self.assertEqual(
            body["size"],
            5,
        )

        multi_match = (
            body["query"]["bool"]["must"][0][
                "multi_match"
            ]
        )

        self.assertEqual(
            multi_match["query"],
            "notice period",
        )

        self.assertEqual(
            multi_match["fields"],
            [
                "content^5",
                "subsection^3",
                "section^2",
            ],
        )

        filters = (
            body["query"]["bool"]["filter"]
        )

        self.assertIn(
            {
                "terms": {
                    "country_code": [
                        "GB",
                        "FR",
                    ]
                }
            },
            filters,
        )

        self.assertIn(
            {
                "terms": {
                    "legal_topic": [
                        "Employment Termination"
                    ]
                }
            },
            filters,
        )

        self.assertIn(
            {
                "terms": {
                    "subsection.keyword": [
                        "Notice Period"
                    ]
                }
            },
            filters,
        )

        self.assertIn(
            {
                "term": {
                    "language": "en",
                }
            },
            filters,
        )

        self.assertIn(
            {
                "term": {
                    "reference_year": 2026,
                }
            },
            filters,
        )

    def test_search_returns_structured_hits(
        self,
    ) -> None:
        client = FakeOpenSearchClient(
            response={
                "took": 7,
                "hits": {
                    "total": {
                        "value": 1,
                    },
                    "hits": [
                        {
                            "_score": 12.5,
                            "_source": {
                                "document_id": "document-1",
                                "chunk_id": "chunk-1",
                                "country": (
                                    "United Kingdom"
                                ),
                                "country_code": "GB",
                                "legal_topic": (
                                    "Employment "
                                    "Termination"
                                ),
                                "document_type": (
                                    "employment_law_overview"
                                ),
                                "language": "en",
                                "section": (
                                    "Employment "
                                    "Termination"
                                ),
                                "subsection": (
                                    "Notice Period"
                                ),
                                "content": (
                                    "The applicable "
                                    "notice period depends "
                                    "on the contract."
                                ),
                                "source_filename": (
                                    "Labour and Employment "
                                    "Law in UK 2026.docx"
                                ),
                                "source_format": "docx",
                                "reference_year": 2026,
                            },
                        }
                    ],
                },
            }
        )

        response = search_legal_documents(
            request=LegalSearchRequest(
                query="notice period",
                country_codes=["GB"],
            ),
            client=client,
        )

        self.assertEqual(
            client.index,
            LEGAL_DOCUMENTS_ALIAS,
        )

        self.assertEqual(
            response.total,
            1,
        )

        self.assertEqual(
            response.took_ms,
            7,
        )

        self.assertEqual(
            len(response.hits),
            1,
        )

        self.assertEqual(
            response.hits[0].chunk_id,
            "chunk-1",
        )

        self.assertEqual(
            response.hits[0].country_code,
            "GB",
        )

        self.assertEqual(
            response.hits[0].score,
            12.5,
        )

    def test_search_returns_empty_result(
        self,
    ) -> None:
        client = FakeOpenSearchClient()

        response = search_legal_documents(
            request=LegalSearchRequest(
                query="collective dismissal",
            ),
            client=client,
        )

        self.assertEqual(
            response.total,
            0,
        )

        self.assertEqual(
            response.hits,
            [],
        )

    def test_blank_normalized_query_is_rejected(
        self,
    ) -> None:
        request = LegalSearchRequest(
            query="  "
        )

        with self.assertRaises(
            InvalidLegalSearchRequestError
        ):
            build_legal_search_body(
                request
            )

    def test_opensearch_errors_are_wrapped(
        self,
    ) -> None:
        client = FakeOpenSearchClient(
            error=OpenSearchException(
                "Unavailable"
            )
        )

        with self.assertRaises(
            LegalSearchError
        ):
            search_legal_documents(
                request=LegalSearchRequest(
                    query="notice period",
                ),
                client=client,
            )


if __name__ == "__main__":
    unittest.main()