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
    build_contact_lookup_body,
    build_legal_search_body,
    search_contact_chunks,
    search_legal_documents,
)
from app.services.opensearch_index import (
    LEGAL_DOCUMENTS_ALIAS,
)
from tests.support.opensearch_fixtures import FakeOpenSearchClient

_EMPTY_SEARCH_RESPONSE: dict[str, Any] = {
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

    def test_minimum_should_match_relaxed_with_country_filter(
        self,
    ) -> None:
        request = LegalSearchRequest(
            query="overtime rules",
            country_codes=[
                "GB",
            ],
        )

        body = build_legal_search_body(
            request
        )

        multi_match = (
            body["query"]["bool"]["must"][0][
                "multi_match"
            ]
        )

        self.assertEqual(
            multi_match["minimum_should_match"],
            "1",
        )

    def test_minimum_should_match_relaxed_with_topic_filter(
        self,
    ) -> None:
        request = LegalSearchRequest(
            query="overtime rules",
            legal_topics=[
                "Working Conditions",
            ],
        )

        body = build_legal_search_body(
            request
        )

        multi_match = (
            body["query"]["bool"]["must"][0][
                "multi_match"
            ]
        )

        self.assertEqual(
            multi_match["minimum_should_match"],
            "1",
        )

    def test_minimum_should_match_relaxed_with_subsection_filter(
        self,
    ) -> None:
        request = LegalSearchRequest(
            query="overtime rules",
            subsections=[
                "Overtime",
            ],
        )

        body = build_legal_search_body(
            request
        )

        multi_match = (
            body["query"]["bool"]["must"][0][
                "multi_match"
            ]
        )

        self.assertEqual(
            multi_match["minimum_should_match"],
            "1",
        )

    def test_minimum_should_match_strict_without_filters(
        self,
    ) -> None:
        request = LegalSearchRequest(
            query="overtime rules",
        )

        body = build_legal_search_body(
            request
        )

        multi_match = (
            body["query"]["bool"]["must"][0][
                "multi_match"
            ]
        )

        self.assertEqual(
            multi_match["minimum_should_match"],
            "70%",
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
        client = FakeOpenSearchClient(
            response=_EMPTY_SEARCH_RESPONSE
        )

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

    def test_contact_subsection_is_excluded_by_default(
        self,
    ) -> None:
        request = LegalSearchRequest(
            query="notice period",
            country_codes=["PE"],
        )

        body = build_legal_search_body(
            request
        )

        self.assertIn(
            {
                "term": {
                    "subsection.keyword": "Contact",
                }
            },
            body["query"]["bool"]["must_not"],
        )

    def test_contact_subsection_not_excluded_when_explicitly_requested(
        self,
    ) -> None:
        request = LegalSearchRequest(
            query="notice period",
            country_codes=["PE"],
            subsections=["Contact"],
        )

        body = build_legal_search_body(
            request
        )

        self.assertNotIn(
            {
                "term": {
                    "subsection.keyword": "Contact",
                }
            },
            body["query"]["bool"]["must_not"],
        )

    def test_contact_lookup_body_filters_by_country_and_subsection(
        self,
    ) -> None:
        body = build_contact_lookup_body(
            [
                "pe",
                "PE",
                " au ",
            ]
        )

        self.assertIn(
            {
                "terms": {
                    "country_code": [
                        "PE",
                        "AU",
                    ]
                }
            },
            body["query"]["bool"]["filter"],
        )

        self.assertIn(
            {
                "term": {
                    "subsection.keyword": "Contact",
                }
            },
            body["query"]["bool"]["filter"],
        )

        self.assertNotIn(
            "must",
            body["query"]["bool"],
        )

    def test_search_contact_chunks_returns_hits(
        self,
    ) -> None:
        client = FakeOpenSearchClient(
            response={
                "took": 3,
                "hits": {
                    "total": {
                        "value": 1,
                    },
                    "hits": [
                        {
                            "_score": 0.0,
                            "_source": {
                                "document_id": "document-1",
                                "chunk_id": "chunk-1-contact",
                                "country": "Peru",
                                "country_code": "PE",
                                "legal_topic": None,
                                "document_type": "overview",
                                "language": "en",
                                "section": (
                                    "Employment Law "
                                    "Overview Peru"
                                ),
                                "subsection": "Contact",
                                "content": (
                                    "Member firm: Test\n"
                                    "Email: x@example.com"
                                ),
                                "source_filename": (
                                    "Employment Law "
                                    "Overview Peru "
                                    "2026.docx"
                                ),
                                "source_format": "docx",
                                "reference_year": 2026,
                            },
                        }
                    ],
                },
            }
        )

        response = search_contact_chunks(
            country_codes=[
                "PE",
            ],
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
            len(response.hits),
            1,
        )

        self.assertEqual(
            response.hits[0].subsection,
            "Contact",
        )

    def test_search_contact_chunks_with_no_countries_skips_opensearch(
        self,
    ) -> None:
        def _unexpected_search(
            index: str,
            body: dict[str, Any],
        ) -> dict[str, Any]:
            raise AssertionError(
                "OpenSearch must not be called with no "
                "country codes."
            )

        client = FakeOpenSearchClient()
        client.search = _unexpected_search  # type: ignore[method-assign]

        response = search_contact_chunks(
            country_codes=[],
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

    def test_search_contact_chunks_wraps_opensearch_errors(
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
            search_contact_chunks(
                country_codes=[
                    "PE",
                ],
                client=client,
            )


class DocumentLegalTopicFilterTests(unittest.TestCase):
    """
    Retrieval tests for mission "ORDER 8F-A", section 14 - the
    legal_topic terms filter is completely generic (any string value),
    so a live, Admin-created custom section title must filter exactly
    like a canonical topic - no special-casing required anywhere in
    this module. These four scenarios mirror a realistic, seeded
    Australia corpus: one canonical topic and two custom section
    titles, one of which (section B) deliberately overlaps a canonical
    trigger phrase's semantics without being collapsed to it.
    """

    def test_canonical_topic_filters_exactly(self) -> None:
        request = LegalSearchRequest(
            query="hiring rules",
            country_codes=["AU"],
            legal_topics=["Hiring Practices"],
        )

        body = build_legal_search_body(request)

        self.assertIn(
            {"terms": {"legal_topic": ["Hiring Practices"]}},
            body["query"]["bool"]["filter"],
        )

    def test_custom_section_title_filters_exactly(self) -> None:
        request = LegalSearchRequest(
            query="foreign employee work eligibility checks",
            country_codes=["AU"],
            legal_topics=[
                "Foreign Employee Work Eligibility Checks"
            ],
        )

        body = build_legal_search_body(request)

        self.assertIn(
            {
                "terms": {
                    "legal_topic": [
                        "Foreign Employee Work Eligibility Checks"
                    ]
                }
            },
            body["query"]["bool"]["filter"],
        )

    def test_other_custom_section_title_filters_exactly(self) -> None:
        request = LegalSearchRequest(
            query="V060 Temporary Validation Section",
            country_codes=["AU"],
            legal_topics=[
                "V060 Temporary Validation Section"
            ],
        )

        body = build_legal_search_body(request)

        self.assertIn(
            {
                "terms": {
                    "legal_topic": [
                        "V060 Temporary Validation Section"
                    ]
                }
            },
            body["query"]["bool"]["filter"],
        )

    def test_topic_text_only_omits_any_topic_filter(self) -> None:
        """
        Scenario C from the mission's own retrieval-filter priority
        (section 7): when neither a canonical nor a document topic
        resolved, no hard legal_topic filter is fabricated at all -
        retrieval stays country-scoped free text across every section,
        canonical or custom.
        """

        request = LegalSearchRequest(
            query="temporary validation",
            country_codes=["AU"],
        )

        body = build_legal_search_body(request)

        filters = body["query"]["bool"]["filter"]

        self.assertFalse(
            any(
                "legal_topic" in one_filter.get("terms", {})
                for one_filter in filters
            )
        )


if __name__ == "__main__":
    unittest.main()