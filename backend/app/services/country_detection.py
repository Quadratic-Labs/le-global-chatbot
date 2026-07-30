"""Detect countries mentioned in legal questions."""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final

import pycountry

from app.models.catalog import LegalCatalogResponse
from app.models.chat import LegalChatRequest
from app.services.legal_catalog import (
    LegalCatalogError,
    get_legal_catalog,
)


CountryCatalogProvider = Callable[
    [],
    LegalCatalogResponse,
]


COUNTRY_ALIAS_OVERRIDES: Final[dict[str, str]] = {
    "uk": "GB",
    "u.k.": "GB",
    "great britain": "GB",
    "britain": "GB",
    "usa": "US",
    "u.s.": "US",
    "u.s.a.": "US",
    "america": "US",
    "south korea": "KR",
    "czechia": "CZ",
    "uae": "AE",
    "u.a.e.": "AE",
}


class CountryDetectionError(RuntimeError):
    """Raised when automatic country detection fails."""


@dataclass(frozen=True, slots=True)
class CountryAvailability:
    """Countries mentioned in a question, split by corpus availability."""

    available_codes: list[str]
    unavailable_codes: list[str]


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


def _build_global_country_data() -> (
    tuple[
        dict[str, str],
        dict[str, list[str]],
    ]
):
    """
    Build a worldwide country phrase map and a reverse name index.

    The phrase map covers every ISO 3166-1 country (via pycountry) plus
    a small set of business aliases, so a country can be recognized as
    "mentioned" even when it is not part of the indexed corpus. The
    reverse index returns the display names/aliases known for one code,
    used to build user-facing messages.
    """

    phrase_map: dict[str, str] = {}
    names_by_code: defaultdict[str, list[str]] = defaultdict(list)

    for country in pycountry.countries:
        code = country.alpha_2.upper()

        candidate_names = {country.name}

        official_name = getattr(
            country,
            "official_name",
            None,
        )

        if official_name:
            candidate_names.add(
                official_name
            )

        for name in candidate_names:
            normalized_name = _normalize_for_matching(
                name
            )

            if not normalized_name:
                continue

            phrase_map.setdefault(
                normalized_name,
                code,
            )

            names_by_code[code].append(
                name
            )

    for alias, code in COUNTRY_ALIAS_OVERRIDES.items():
        normalized_alias = _normalize_for_matching(
            alias
        )

        if not normalized_alias:
            continue

        normalized_code = code.upper()

        phrase_map[normalized_alias] = (
            normalized_code
        )

        names_by_code[normalized_code].append(
            alias
        )

    return (
        phrase_map,
        dict(names_by_code),
    )


(
    _GLOBAL_COUNTRY_PHRASE_MAP,
    _COUNTRY_NAMES_BY_CODE,
) = _build_global_country_data()


def get_country_name_variants(
    country_code: str,
) -> list[str]:
    """Return the known display names/aliases for one country code."""

    return _COUNTRY_NAMES_BY_CODE.get(
        country_code.upper(),
        [],
    )


def resolve_country_display_name(
    country_code: str,
) -> str:
    """Return a readable display name for one ISO alpha-2 country code."""

    country = pycountry.countries.get(
        alpha_2=country_code.upper()
    )

    if country is not None:
        return country.name

    return country_code.upper()


def detect_mentioned_country_codes(
    question: str,
) -> list[str]:
    """
    Detect ISO country codes for every country named in a question.

    This covers every ISO 3166-1 country, not only the ones indexed in
    the corpus, so that mentioned-but-unindexed countries (for example
    Canada) can be reported instead of silently ignored. Bare two-letter
    codes are intentionally not scanned for in free text, since common
    words can collide with real ISO codes (for example "IN").
    """

    normalized_question = _normalize_for_matching(
        question
    )

    if not normalized_question:
        return []

    candidates: list[
        tuple[int, int, str]
    ] = []

    sorted_phrases = sorted(
        _GLOBAL_COUNTRY_PHRASE_MAP,
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
                    _GLOBAL_COUNTRY_PHRASE_MAP[
                        phrase
                    ],
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


def resolve_country_availability(
    request: LegalChatRequest,
    catalog_provider: CountryCatalogProvider = (
        get_legal_catalog
    ),
) -> CountryAvailability:
    """
    Split requested/mentioned countries into available and unavailable.

    Explicit country_codes always take priority over free-text
    detection. Every mentioned code (explicit or detected) is checked
    against the indexed corpus, so a country outside the corpus is
    reported instead of triggering an unfiltered search.
    """

    explicit_codes = _normalize_country_codes(
        request.country_codes
    )

    mentioned_codes = (
        explicit_codes
        if explicit_codes
        else detect_mentioned_country_codes(
            request.question
        )
    )

    if not mentioned_codes:
        return CountryAvailability(
            available_codes=[],
            unavailable_codes=[],
        )

    try:
        catalog = catalog_provider()

    except LegalCatalogError as error:
        raise CountryDetectionError(
            "The indexed country catalog "
            "could not be read."
        ) from error

    indexed_codes = {
        country.country_code.upper()
        for country in catalog.countries
    }

    available_codes = [
        code
        for code in mentioned_codes
        if code in indexed_codes
    ]

    unavailable_codes = [
        code
        for code in mentioned_codes
        if code not in indexed_codes
    ]

    return CountryAvailability(
        available_codes=available_codes,
        unavailable_codes=unavailable_codes,
    )
