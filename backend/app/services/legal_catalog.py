"""Read the available legal corpus catalog from OpenSearch."""

from __future__ import annotations

from typing import Any, Final

from opensearchpy import OpenSearch
from opensearchpy.exceptions import OpenSearchException

from app.clients.opensearch import (
    get_opensearch_client,
)
from app.models.catalog import (
    LegalCatalogCountry,
    LegalCatalogResponse,
    LegalCatalogValue,
)
from app.services.opensearch_index import (
    LEGAL_DOCUMENTS_ALIAS,
)


MAX_COUNTRIES: Final[int] = 300
MAX_LEGAL_TOPICS: Final[int] = 500
MAX_SUBSECTIONS: Final[int] = 1000


class LegalCatalogError(RuntimeError):
    """Raised when the legal corpus catalog cannot be read."""


def build_legal_catalog_body() -> dict[str, Any]:
    """Build the OpenSearch aggregation request."""

    return {
        "size": 0,
        "aggs": {
            "countries": {
                "terms": {
                    "field": "country_code",
                    "size": MAX_COUNTRIES,
                    "order": {
                        "_key": "asc",
                    },
                },
                "aggs": {
                    "country_names": {
                        "terms": {
                            "field": "country",
                            "size": 1,
                        }
                    }
                },
            },
            "legal_topics": {
                "terms": {
                    "field": "legal_topic",
                    "size": MAX_LEGAL_TOPICS,
                    "order": {
                        "_key": "asc",
                    },
                }
            },
            "subsections": {
                "terms": {
                    "field": "subsection.keyword",
                    "size": MAX_SUBSECTIONS,
                    "order": {
                        "_key": "asc",
                    },
                }
            },
        },
    }


def _get_aggregation_buckets(
    aggregations: dict[str, Any],
    aggregation_name: str,
) -> list[dict[str, Any]]:
    """Read and validate one aggregation bucket list."""

    aggregation = aggregations.get(
        aggregation_name
    )

    if not isinstance(
        aggregation,
        dict,
    ):
        raise LegalCatalogError(
            "OpenSearch returned an invalid "
            f"{aggregation_name} aggregation."
        )

    buckets = aggregation.get(
        "buckets"
    )

    if not isinstance(
        buckets,
        list,
    ):
        raise LegalCatalogError(
            "OpenSearch returned invalid buckets for "
            f"{aggregation_name}."
        )

    validated_buckets: list[
        dict[str, Any]
    ] = []

    for bucket in buckets:
        if not isinstance(
            bucket,
            dict,
        ):
            raise LegalCatalogError(
                "OpenSearch returned an invalid "
                f"{aggregation_name} bucket."
            )

        validated_buckets.append(
            bucket
        )

    return validated_buckets


def _extract_country_name(
    bucket: dict[str, Any],
    country_code: str,
) -> str:
    """Extract the country name from one country-code bucket."""

    country_names = bucket.get(
        "country_names"
    )

    if not isinstance(
        country_names,
        dict,
    ):
        return country_code

    name_buckets = country_names.get(
        "buckets"
    )

    if not isinstance(
        name_buckets,
        list,
    ):
        return country_code

    if not name_buckets:
        return country_code

    first_name_bucket = name_buckets[0]

    if not isinstance(
        first_name_bucket,
        dict,
    ):
        return country_code

    country_name = first_name_bucket.get(
        "key"
    )

    if not isinstance(
        country_name,
        str,
    ):
        return country_code

    normalized_country_name = (
        country_name.strip()
    )

    return (
        normalized_country_name
        or country_code
    )


def _extract_countries(
    aggregations: dict[str, Any],
) -> list[LegalCatalogCountry]:
    """Convert country aggregation buckets into API models."""

    countries: list[
        LegalCatalogCountry
    ] = []

    for bucket in _get_aggregation_buckets(
        aggregations=aggregations,
        aggregation_name="countries",
    ):
        country_code = bucket.get(
            "key"
        )

        if not isinstance(
            country_code,
            str,
        ):
            raise LegalCatalogError(
                "OpenSearch returned an invalid country code."
            )

        normalized_country_code = (
            country_code.strip().upper()
        )

        if not normalized_country_code:
            raise LegalCatalogError(
                "OpenSearch returned an empty country code."
            )

        countries.append(
            LegalCatalogCountry(
                country_code=normalized_country_code,
                country=_extract_country_name(
                    bucket=bucket,
                    country_code=(
                        normalized_country_code
                    ),
                ),
                chunk_count=int(
                    bucket.get(
                        "doc_count",
                        0,
                    )
                ),
            )
        )

    return countries


def _extract_catalog_values(
    aggregations: dict[str, Any],
    aggregation_name: str,
) -> list[LegalCatalogValue]:
    """Convert one text aggregation into catalog values."""

    values: list[
        LegalCatalogValue
    ] = []

    for bucket in _get_aggregation_buckets(
        aggregations=aggregations,
        aggregation_name=aggregation_name,
    ):
        raw_value = bucket.get(
            "key"
        )

        if not isinstance(
            raw_value,
            str,
        ):
            raise LegalCatalogError(
                "OpenSearch returned an invalid value for "
                f"{aggregation_name}."
            )

        normalized_value = raw_value.strip()

        if not normalized_value:
            continue

        values.append(
            LegalCatalogValue(
                value=normalized_value,
                chunk_count=int(
                    bucket.get(
                        "doc_count",
                        0,
                    )
                ),
            )
        )

    return values


def get_legal_catalog(
    client: OpenSearch | None = None,
) -> LegalCatalogResponse:
    """Return the legal classifications currently indexed."""

    opensearch_client = (
        client
        if client is not None
        else get_opensearch_client()
    )

    try:
        response = opensearch_client.search(
            index=LEGAL_DOCUMENTS_ALIAS,
            body=build_legal_catalog_body(),
        )

    except OpenSearchException as error:
        raise LegalCatalogError(
            "OpenSearch legal catalog request failed."
        ) from error

    if not isinstance(
        response,
        dict,
    ):
        raise LegalCatalogError(
            "OpenSearch returned an invalid catalog response."
        )

    aggregations = response.get(
        "aggregations"
    )

    if not isinstance(
        aggregations,
        dict,
    ):
        raise LegalCatalogError(
            "OpenSearch returned no valid catalog aggregations."
        )

    return LegalCatalogResponse(
        countries=_extract_countries(
            aggregations
        ),
        legal_topics=_extract_catalog_values(
            aggregations=aggregations,
            aggregation_name="legal_topics",
        ),
        subsections=_extract_catalog_values(
            aggregations=aggregations,
            aggregation_name="subsections",
        ),
    )