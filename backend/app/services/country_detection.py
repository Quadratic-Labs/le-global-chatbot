"""Detect countries mentioned in legal questions."""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Final

import pycountry

from app.models.catalog import LegalCatalogResponse
from app.models.chat import LegalChatRequest
from app.services.jurisdiction_resolution import (
    detect_unresolved_location_phrase,
    resolve_city_country_codes,
)
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


def _build_phrase_patterns() -> tuple[
    tuple[str, re.Pattern[str], str], ...
]:
    """
    Precompile every (phrase, pattern, country_code) triple exactly
    once, longest phrase first, at import time.

    Mission "ORDER 5C-GEO", section 22/23: detect_mentioned_country_
    codes runs on every chat request; re-sorting ~400+ phrases and
    re-compiling one regex per phrase on every single call (as this
    used to do) is exactly the "mapping reconstruit à chaque request"
    the mission asks to find and fix - a real, measured ~1.4ms per
    call, almost entirely spent recompiling, not matching. The
    dataset itself (pycountry's own country list) never changes at
    runtime, so there is nothing to invalidate.
    """

    return tuple(
        (
            phrase,
            re.compile(
                rf"(?<!\w){re.escape(phrase)}(?!\w)"
            ),
            country_code,
        )
        for phrase, country_code in sorted(
            _GLOBAL_COUNTRY_PHRASE_MAP.items(),
            key=lambda item: len(item[0]),
            reverse=True,
        )
    )


_COUNTRY_PHRASE_PATTERNS: Final[
    tuple[tuple[str, re.Pattern[str], str], ...]
] = _build_phrase_patterns()


def get_country_name_variants(
    country_code: str,
) -> list[str]:
    """Return the known display names/aliases for one country code."""

    return _COUNTRY_NAMES_BY_CODE.get(
        country_code.upper(),
        [],
    )


# National adjectives/demonyms ("Spanish", "British") for the countries
# this product's questions actually name - pycountry has no demonym
# field at all, and this is not a re-derivation of the name/code
# mappings above (those still come exclusively from pycountry): it is
# new data this system did not have before, added specifically so
# legal_subject_scope.py can recognize "under Spanish law" as the same
# geographic-scope frame as "in Spain" (see the jurisdiction-neutral-
# subject mission's Phase 12). Deliberately not exhaustive for every
# ISO country - covers the corpus's own supported countries plus other
# common ones a real question might name in a comparison. A country
# missing here simply is not recognized in a demonym-based frame yet;
# it is still recognized in every plain name-based frame above.
_COUNTRY_DEMONYMS: Final[dict[str, list[str]]] = {
    "AR": ["Argentine", "Argentinian"],
    "AU": ["Australian"],
    "BE": ["Belgian"],
    "BR": ["Brazilian"],
    "CA": ["Canadian"],
    "CH": ["Swiss"],
    "CL": ["Chilean"],
    "CN": ["Chinese"],
    "CO": ["Colombian"],
    "CZ": ["Czech"],
    "DE": ["German"],
    "DK": ["Danish"],
    "ES": ["Spanish"],
    "FI": ["Finnish"],
    "FR": ["French"],
    "GB": ["British", "UK"],
    "GR": ["Greek"],
    "IE": ["Irish"],
    "IN": ["Indian"],
    "IT": ["Italian"],
    "JP": ["Japanese"],
    "KR": ["Korean", "South Korean"],
    "MX": ["Mexican"],
    "NL": ["Dutch"],
    "NO": ["Norwegian"],
    "NZ": ["New Zealand"],
    "PE": ["Peruvian"],
    "PL": ["Polish"],
    "PT": ["Portuguese"],
    "RO": ["Romanian"],
    "SE": ["Swedish"],
    "SG": ["Singaporean"],
    "TH": ["Thai"],
    "US": ["American"],
    "VN": ["Vietnamese"],
    "ZA": ["South African"],
}


def get_country_demonyms(
    country_code: str,
) -> list[str]:
    """
    Return the known national adjective(s)/demonym(s) for one country
    code - e.g. ["Spanish"] for "ES" - or an empty list when this
    country has none recorded yet (see _COUNTRY_DEMONYMS above).
    """

    return _COUNTRY_DEMONYMS.get(
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

    for phrase, pattern, country_code in _COUNTRY_PHRASE_PATTERNS:
        for match in pattern.finditer(
            normalized_question
        ):
            candidates.append(
                (
                    match.start(),
                    -len(
                        phrase
                    ),
                    country_code,
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


# Connector/interrogative words that carry no legal-subject content of
# their own - stripping them (alongside any detected country name) is
# what tells "Peru?"/"What about Peru?"/"How about the United Kingdom?"
# apart from a real new question like "Overtime in Peru?", without a
# long enumerated list of full phrases (see the mission's own
# "ne pas ajouter une longue liste de phrases completes").
_COUNTRY_ONLY_FOLLOWUP_CONNECTOR_WORDS: Final[frozenset[str]] = frozenset(
    {
        "what",
        "about",
        "how",
        "and",
        "for",
        "the",
        "or",
        "of",
    }
)


def is_country_only_followup(
    question: str,
) -> list[str] | None:
    """
    Return the mentioned country code(s) when `question` names one or
    more countries and nothing else of substance - "Peru?", "What
    about Peru?", "How about the United Kingdom?", "For Spain?" - or
    None when it also carries its own legal-subject content ("Overtime
    in Peru?", "Contacts in Spain", "Compare Spain and Peru", "Peru
    working conditions", "Dismissal in Australia") or names no country
    at all.

    Deterministic and local - reuses detect_mentioned_country_codes for
    country recognition, never a separate list of countries. A message
    naming zero countries is never country-only (there is nothing to
    replace country_codes with).
    """

    country_codes = detect_mentioned_country_codes(question)

    if not country_codes:
        return None

    working_text = question

    for code in country_codes:
        for variant in get_country_name_variants(
            code
        ) + get_country_demonyms(code):
            working_text = re.sub(
                rf"(?<!\w){re.escape(variant)}(?!\w)",
                " ",
                working_text,
                flags=re.IGNORECASE,
            )

    remaining_words = re.findall(
        r"[A-Za-z0-9']+",
        working_text,
    )
    remaining_content_words = [
        word
        for word in remaining_words
        if word.casefold()
        not in _COUNTRY_ONLY_FOLLOWUP_CONNECTOR_WORDS
    ]

    if remaining_content_words:
        return None

    return country_codes


class JurisdictionResolutionStatus(Enum):
    """The four, and only four, outcomes resolve_jurisdiction returns."""

    RESOLVED = "resolved"
    AMBIGUOUS = "ambiguous"
    UNKNOWN_LOCALITY = "unknown_locality"
    NOT_FOUND = "not_found"


@dataclass(frozen=True, slots=True)
class JurisdictionResolution:
    """
    One resolved (or refused-to-guess) jurisdiction for a piece of text.

    `candidate_country_codes` is populated only for AMBIGUOUS - the
    real, distinct countries a matched city name (or, for two or more
    explicit countries, the countries themselves) could plausibly
    mean, never a guess at which one is "most likely". `matched_
    location` is set for RESOLVED, AMBIGUOUS-by-city (never AMBIGUOUS-
    by-multiple-explicit-countries, which sets only candidate_country_
    codes), and UNKNOWN_LOCALITY - the last of these means the text
    looks like it names a place ("employment law in <X>") that neither
    an explicit country nor the city dataset recognizes; matched_
    location then carries that phrase as written, for a caller to ask
    which country it is in, never a fabricated country code.
    """

    status: JurisdictionResolutionStatus
    country_code: str | None = None
    country_name: str | None = None
    matched_location: str | None = None
    candidate_country_codes: tuple[str, ...] = field(
        default_factory=tuple
    )


def resolve_jurisdiction(
    text: str,
) -> JurisdictionResolution:
    """
    Resolve the one jurisdiction `text` most likely refers to.

    Priority (mission "ORDER 5C-GEO", section 10): an explicit country
    name, alias, or demonym anywhere in the text always wins outright,
    even when the same text also contains a city name ("Barcelona,
    Spain" resolves to ES directly, never flagged ambiguous by
    "Barcelona" alone). Only once zero explicit countries are found
    does a city match (jurisdiction_resolution.resolve_city_country_
    codes) get considered - and only when it resolves to exactly one
    real country is that returned as RESOLVED; more than one candidate
    country is AMBIGUOUS, never guessed.

    A text naming two or more explicit countries (e.g. a country
    comparison) is also AMBIGUOUS from this single-jurisdiction
    primitive's own point of view - callers that need multi-country
    handling (comparisons) already use detect_mentioned_country_codes
    directly and never go through this function for that case.

    A text that looks like it names a place ("employment law in
    <X>") but resolves neither as an explicit country nor as a known
    city is UNKNOWN_LOCALITY, not NOT_FOUND (corrective gate, section
    11) - the two are genuinely different situations for a caller: no
    location signal at all versus a location signal for a place this
    dataset simply does not know, which deserves asking the user which
    country it is in rather than the generic "specify a country"
    prompt, and must never fabricate a country code for it.
    """

    explicit_codes = detect_mentioned_country_codes(text)

    if len(explicit_codes) == 1:
        code = explicit_codes[0]

        return JurisdictionResolution(
            status=JurisdictionResolutionStatus.RESOLVED,
            country_code=code,
            country_name=resolve_country_display_name(code),
            matched_location=text.strip(),
        )

    if len(explicit_codes) > 1:
        return JurisdictionResolution(
            status=JurisdictionResolutionStatus.AMBIGUOUS,
            candidate_country_codes=tuple(explicit_codes),
        )

    candidate_codes, matched_word = resolve_city_country_codes(
        text
    )

    if not candidate_codes:
        unresolved_phrase = detect_unresolved_location_phrase(
            text
        )

        if unresolved_phrase is not None:
            return JurisdictionResolution(
                status=(
                    JurisdictionResolutionStatus.UNKNOWN_LOCALITY
                ),
                matched_location=unresolved_phrase,
            )

        return JurisdictionResolution(
            status=JurisdictionResolutionStatus.NOT_FOUND,
        )

    if len(candidate_codes) == 1:
        code = next(iter(candidate_codes))

        return JurisdictionResolution(
            status=JurisdictionResolutionStatus.RESOLVED,
            country_code=code,
            country_name=resolve_country_display_name(code),
            matched_location=matched_word,
        )

    return JurisdictionResolution(
        status=JurisdictionResolutionStatus.AMBIGUOUS,
        candidate_country_codes=tuple(
            sorted(candidate_codes)
        ),
        matched_location=matched_word,
    )


def resolve_country_availability(
    request: LegalChatRequest,
    catalog_provider: CountryCatalogProvider = (
        get_legal_catalog
    ),
) -> CountryAvailability:
    """
    Split requested/mentioned countries into available and unavailable.

    Explicit country_codes always take priority over free-text
    detection, which itself takes priority over a city-name fallback
    (resolve_jurisdiction) - a question naming only a city
    ("employment law in Lisbon") is treated exactly as if it had
    named that city's country outright, but only when the city
    resolves unambiguously; a genuinely ambiguous city name (e.g.
    "Barcelona" alone) or no match at all contributes nothing here,
    exactly as if no location had been mentioned (mission
    "ORDER 5C-GEO", section 11: never guessed at this layer). Every
    mentioned code (explicit, detected, or city-resolved) is checked
    against the indexed corpus, so a country outside the corpus is
    reported instead of triggering an unfiltered search.
    """

    explicit_codes = _normalize_country_codes(
        request.country_codes
    )

    if explicit_codes:
        mentioned_codes = explicit_codes

    else:
        # detect_mentioned_country_codes already supports naming
        # several countries at once (a free-text comparison, e.g.
        # "Compare France and Germany") - that multi-country result
        # must flow through unchanged. The city fallback is only ever
        # consulted when it finds NOTHING at all, and only ever
        # contributes a single code (never several - a question
        # naming two cities in two different countries is out of
        # scope for this single-city fallback; see
        # resolve_city_country_codes's own docstring).
        detected_codes = detect_mentioned_country_codes(
            request.question
        )

        if detected_codes:
            mentioned_codes = detected_codes

        else:
            city_candidate_codes, _ = resolve_city_country_codes(
                request.question
            )

            mentioned_codes = (
                [next(iter(city_candidate_codes))]
                if len(city_candidate_codes) == 1
                else []
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
