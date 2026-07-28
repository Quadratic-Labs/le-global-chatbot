"""Search indexed legal content using OpenSearch BM25."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Final

from opensearchpy import OpenSearch
from opensearchpy.exceptions import OpenSearchException
from pydantic import ValidationError

from app.clients.opensearch import (
    get_opensearch_client,
)
from app.models.search import (
    LegalSearchHit,
    LegalSearchRequest,
    LegalSearchResponse,
)
from app.services.opensearch_index import (
    LEGAL_DOCUMENTS_ALIAS,
)


SEARCH_SOURCE_FIELDS: Final[list[str]] = [
    "document_id",
    "chunk_id",
    "country",
    "country_code",
    "legal_topic",
    "document_type",
    "language",
    "section",
    "subsection",
    "content",
    "source_filename",
    "source_format",
    "reference_year",
]


class LegalSearchError(RuntimeError):
    """Raised when legal search cannot be completed."""


class InvalidLegalSearchRequestError(ValueError):
    """Raised when normalized search criteria are invalid."""


def _normalize_text(
    value: str,
) -> str:
    """Normalize whitespace in one search value."""

    return " ".join(
        value.split()
    )


def _normalize_filter_values(
    values: Sequence[str],
    uppercase: bool = False,
) -> list[str]:
    """Normalize and deduplicate filter values."""

    normalized_values: list[str] = []
    seen_values: set[str] = set()

    for value in values:
        normalized_value = _normalize_text(
            value
        )

        if uppercase:
            normalized_value = (
                normalized_value.upper()
            )

        if not normalized_value:
            continue

        if normalized_value in seen_values:
            continue

        seen_values.add(
            normalized_value
        )

        normalized_values.append(
            normalized_value
        )

    return normalized_values


def build_legal_search_body(
    request: LegalSearchRequest,
) -> dict[str, Any]:
    """Build an OpenSearch BM25 query body."""

    normalized_query = _normalize_text(
        request.query
    )

    if len(normalized_query) < 2:
        raise InvalidLegalSearchRequestError(
            "Search query must contain at least "
            "two non-whitespace characters."
        )

    filters: list[dict[str, Any]] = []

    country_codes = _normalize_filter_values(
        request.country_codes,
        uppercase=True,
    )

    if country_codes:
        filters.append(
            {
                "terms": {
                    "country_code": country_codes,
                }
            }
        )

    legal_topics = _normalize_filter_values(
        request.legal_topics
    )

    if legal_topics:
        filters.append(
            {
                "terms": {
                    "legal_topic": legal_topics,
                }
            }
        )

    subsections = _normalize_filter_values(
        request.subsections
    )

    if subsections:
        filters.append(
            {
                "terms": {
                    "subsection.keyword": subsections,
                }
            }
        )

    if request.language is not None:
        normalized_language = _normalize_text(
            request.language
        ).lower()

        if normalized_language:
            filters.append(
                {
                    "term": {
                        "language": normalized_language,
                    }
                }
            )

    if request.reference_year is not None:
        filters.append(
            {
                "term": {
                    "reference_year": (
                        request.reference_year
                    ),
                }
            }
        )

    return {
        "from": request.offset,
        "size": request.limit,
        "track_total_hits": True,
        "_source": SEARCH_SOURCE_FIELDS,
        "query": {
            "bool": {
                "must": [
                    {
                        "multi_match": {
                            "query": normalized_query,
                            "fields": [
                                "content^5",
                                "subsection^3",
                                "section^2",
                            ],
                            "type": "best_fields",
                            "minimum_should_match": (
                                "70%"
                            ),
                            "tie_breaker": 0.2,
                        }
                    }
                ],
                "filter": filters,
            }
        },
    }


def _extract_total_hits(
    response: dict[str, Any],
) -> int:
    """Extract the total hit count from OpenSearch."""

    hits_section = response.get(
        "hits",
        {},
    )

    if not isinstance(
        hits_section,
        dict,
    ):
        raise LegalSearchError(
            "OpenSearch returned an invalid hits section."
        )

    raw_total = hits_section.get(
        "total",
        0,
    )

    if isinstance(
        raw_total,
        dict,
    ):
        return int(
            raw_total.get(
                "value",
                0,
            )
        )

    return int(
        raw_total
    )


def _extract_search_hits(
    response: dict[str, Any],
) -> list[LegalSearchHit]:
    """Convert raw OpenSearch hits into API models."""

    hits_section = response.get(
        "hits",
        {},
    )

    if not isinstance(
        hits_section,
        dict,
    ):
        raise LegalSearchError(
            "OpenSearch returned an invalid hits section."
        )

    raw_hits = hits_section.get(
        "hits",
        [],
    )

    if not isinstance(
        raw_hits,
        list,
    ):
        raise LegalSearchError(
            "OpenSearch returned an invalid hit list."
        )

    search_hits: list[LegalSearchHit] = []

    for raw_hit in raw_hits:
        if not isinstance(
            raw_hit,
            dict,
        ):
            raise LegalSearchError(
                "OpenSearch returned an invalid hit."
            )

        source = raw_hit.get(
            "_source",
        )

        if not isinstance(
            source,
            dict,
        ):
            raise LegalSearchError(
                "OpenSearch hit is missing its source."
            )

        try:
            search_hit = LegalSearchHit(
                score=float(
                    raw_hit.get(
                        "_score",
                        0.0,
                    )
                    or 0.0
                ),
                **source,
            )

        except (
            TypeError,
            ValueError,
            ValidationError,
        ) as error:
            raise LegalSearchError(
                "OpenSearch returned an invalid "
                "legal document."
            ) from error

        search_hits.append(
            search_hit
        )

    return search_hits


def search_legal_documents(
    request: LegalSearchRequest,
    client: OpenSearch | None = None,
) -> LegalSearchResponse:
    """Execute one legal document search."""

    search_body = build_legal_search_body(
        request
    )

    opensearch_client = (
        client
        if client is not None
        else get_opensearch_client()
    )

    try:
        response = opensearch_client.search(
            index=LEGAL_DOCUMENTS_ALIAS,
            body=search_body,
        )

    except OpenSearchException as error:
        raise LegalSearchError(
            "OpenSearch legal search failed."
        ) from error

    if not isinstance(
        response,
        dict,
    ):
        raise LegalSearchError(
            "OpenSearch returned an invalid response."
        )

    return LegalSearchResponse(
        query=_normalize_text(
            request.query
        ),
        total=_extract_total_hits(
            response
        ),
        limit=request.limit,
        offset=request.offset,
        took_ms=int(
            response.get(
                "took",
                0,
            )
        ),
        hits=_extract_search_hits(
            response
        ),
    )