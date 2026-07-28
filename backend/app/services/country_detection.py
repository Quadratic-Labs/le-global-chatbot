"""Detect countries mentioned in legal questions."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Sequence
from typing import Final

from app.models.catalog import (
    LegalCatalogCountry,
    LegalCatalogResponse,
)
from app.models.chat import LegalChatRequest
from app.services.legal_catalog import (
    LegalCatalogError,
    get_legal_catalog,
)


CountryCatalogProvider = Callable[
    [],
    LegalCatalogResponse,
]


COUNTRY_ALIASES: Final[
    dict[str, tuple[str, ...]]
] = {
    "GB": (
        "UK",
        "U.K.",
        "Great Britain",
        "Britain",
    ),
    "US": (
        "USA",
        "U.S.A.",
        "United States of America",
    ),
    "CZ": (
        "Czechia",
    ),
    "KR": (
        "South Korea",
        "Republic of Korea",
    ),
    "AE": (
        "UAE",
        "U.A.E.",
        "United Arab Emirates",
    ),
}


UPPERCASE_COUNTRY_CODE_PATTERN: Final[
    re.Pattern[str]
] = re.compile(
    r"(?<![A-Za-z])([A-Z]{2})(?![A-Za-z])"
)


class CountryDetectionError(RuntimeError):
    """Raised when automatic country detection fails."""


def _normalize_for_matching(
    value: str,
) -> str:
    """Normalize text for country-name matching."""

    decomposed_value = unicodedata.normalize(
        "NFKD",
        value,
    )

    without_diacritics = "".join(
        character
        for character in decomposed_value
        if not unicodedata.combining(
            character
        )
    )

    alphanumeric_value = re.sub(
        r"[^0-9A-Za-z]+",
        " ",
        without_diacritics,
    )

    return " ".join(
        alphanumeric_value.casefold().split()
    )


def _normalize_country_codes(
    values: Sequence[str],
) -> list[str]:
    """Normalize and deduplicate explicit country codes."""

    normalized_codes: list[str] = []
    seen_codes: set[str] = set()

    for value in values:
        normalized_value = (
            " ".join(
                value.split()
            )
            .upper()
        )

        if not normalized_value:
            continue

        if normalized_value in seen_codes:
            continue

        seen_codes.add(
            normalized_value
        )

        normalized_codes.append(
            normalized_value
        )

    return normalized_codes


def _build_country_phrase_map(
    countries: Sequence[LegalCatalogCountry],
) -> dict[str, str]:
    """Build country-name and alias mappings."""

    available_codes = {
        country.country_code.upper()
        for country in countries
    }

    phrase_map: dict[str, str] = {}

    for country in countries:
        country_code = (
            country.country_code.upper()
        )

        normalized_country_name = (
            _normalize_for_matching(
                country.country
            )
        )

        if normalized_country_name:
            phrase_map[
                normalized_country_name
            ] = country_code

    for country_code, aliases in (
        COUNTRY_ALIASES.items()
    ):
        if country_code not in available_codes:
            continue

        for alias in aliases:
            normalized_alias = (
                _normalize_for_matching(
                    alias
                )
            )

            if normalized_alias:
                phrase_map[
                    normalized_alias
                ] = country_code

    return phrase_map


def _find_named_country_candidates(
    question: str,
    countries: Sequence[LegalCatalogCountry],
) -> list[tuple[int, int, str]]:
    """Find country names and aliases inside a question."""

    normalized_question = (
        _normalize_for_matching(
            question
        )
    )

    phrase_map = _build_country_phrase_map(
        countries
    )

    candidates: list[
        tuple[int, int, str]
    ] = []

    sorted_phrases = sorted(
        phrase_map,
        key=len,
        reverse=True,
    )

    for phrase in sorted_phrases:
        pattern = re.compile(
            rf"(?<!\w){re.escape(phrase)}(?!\w)"
        )

        for match in pattern.finditer(
            normalized_question
        ):
            candidates.append(
                (
                    match.start(),
                    -len(
                        phrase
                    ),
                    phrase_map[
                        phrase
                    ],
                )
            )

    return candidates


def _find_country_code_candidates(
    question: str,
    countries: Sequence[LegalCatalogCountry],
) -> list[tuple[int, int, str]]:
    """Find explicit uppercase ISO country codes."""

    available_codes = {
        country.country_code.upper()
        for country in countries
    }

    candidates: list[
        tuple[int, int, str]
    ] = []

    for match in (
        UPPERCASE_COUNTRY_CODE_PATTERN.finditer(
            question
        )
    ):
        country_code = match.group(
            1
        )

        if country_code not in available_codes:
            continue

        candidates.append(
            (
                match.start(),
                -2,
                country_code,
            )
        )

    return candidates


def detect_country_codes(
    question: str,
    catalog_provider: CountryCatalogProvider = (
        get_legal_catalog
    ),
) -> list[str]:
    """Detect indexed countries mentioned in a question."""

    normalized_question = question.strip()

    if not normalized_question:
        return []

    try:
        catalog = catalog_provider()

    except LegalCatalogError as error:
        raise CountryDetectionError(
            "The indexed country catalog "
            "could not be read."
        ) from error

    candidates = (
        _find_named_country_candidates(
            question=normalized_question,
            countries=catalog.countries,
        )
        + _find_country_code_candidates(
            question=normalized_question,
            countries=catalog.countries,
        )
    )

    candidates.sort(
        key=lambda candidate: (
            candidate[0],
            candidate[1],
        )
    )

    detected_codes: list[str] = []
    seen_codes: set[str] = set()

    for _, _, country_code in candidates:
        if country_code in seen_codes:
            continue

        seen_codes.add(
            country_code
        )

        detected_codes.append(
            country_code
        )

    return detected_codes


def prepare_legal_chat_request(
    request: LegalChatRequest,
    catalog_provider: CountryCatalogProvider = (
        get_legal_catalog
    ),
) -> LegalChatRequest:
    """
    Add detected countries when no explicit filter exists.

    Explicit filters always remain authoritative.
    """

    explicit_country_codes = (
        _normalize_country_codes(
            request.country_codes
        )
    )

    if explicit_country_codes:
        return LegalChatRequest(
            question=request.question,
            country_codes=explicit_country_codes,
            legal_topics=list(
                request.legal_topics
            ),
            subsections=list(
                request.subsections
            ),
            language=request.language,
            reference_year=request.reference_year,
            max_sources=request.max_sources,
        )

    detected_country_codes = (
        detect_country_codes(
            question=request.question,
            catalog_provider=catalog_provider,
        )
    )

    if not detected_country_codes:
        return request

    return LegalChatRequest(
        question=request.question,
        country_codes=detected_country_codes,
        legal_topics=list(
            request.legal_topics
        ),
        subsections=list(
            request.subsections
        ),
        language=request.language,
        reference_year=request.reference_year,
        max_sources=request.max_sources,
    )