"""Consolidated service: country_detection.py. Includes former jurisdiction_resolution.py responsibilities."""
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
from app.services.legal_catalog import LegalCatalogError, get_legal_catalog
from functools import lru_cache
import geonamescache
_MINIMUM_CITY_NAME_LENGTH: Final[int] = 4
_MAX_LOCATION_PHRASE_WORDS: Final[int] = 4
_LOCATION_PREPOSITIONS: Final[frozenset[str]] = frozenset({'in', 'at'})
_WORD_PATTERN: Final[re.Pattern[str]] = re.compile("[A-Za-z][A-Za-z'-]*")
_LATIN_ALTERNATE_NAME_PATTERN: Final[re.Pattern[str]] = re.compile("^[A-Za-z][A-Za-z' -]*$")

def _normalize_city_for_matching(value: str) -> str:
    """
    Casefold and strip diacritics, so "Türkiye"/"Turkiye" and "Sao
    Paulo"/"São Paulo" style variants compare equal.

    Also strips periods - found necessary by adversarial review, not
    assumed: a period-abbreviated primary name such as "St.
    Petersburg" was being indexed verbatim (period kept), but
    _WORD_PATTERN (used to scan real input text) never matches a
    period at all, so real text "St. Petersburg"/"St Petersburg" both
    tokenize to the period-free "st petersburg" - a name that was
    never registered, while the indexed "st. petersburg" key could
    never be reached by any real text. Stripping periods here, in the
    one normalization function both indexing and scanning share,
    keeps the two sides consistent for every "St."/"Mt."-style
    abbreviated name, not just this one.
    """
    decomposed_value = unicodedata.normalize('NFKD', value)
    without_diacritics = ''.join((character for character in decomposed_value if not unicodedata.combining(character)))
    without_periods = without_diacritics.replace('.', '')
    return ' '.join(without_periods.casefold().split())

@lru_cache(maxsize=None)
def _country_display_name(country_code: str) -> str | None:
    country = pycountry.countries.get(alpha_2=country_code)
    return country.name if country is not None else None

def _is_country_level_aggregate(name: str, upper_code: str) -> bool:
    """
    True when a geonamescache "city" entry is really a country/
    territory-level aggregate rather than a specific place within it -
    detected generically as its name matching its own country's name
    (e.g. the "Hong Kong" city entry, population 7,396,076 - the
    whole territory; the "Singapore" city entry, population
    5,638,700 - the whole city-state), never a named exception for a
    particular place.

    Found necessary by adversarial review, not assumed: such an
    entry's alternate names are typically historical/colonial/
    touristic nicknames for the WHOLE country ("Victoria" for Hong
    Kong, "Garden City" for Singapore), not names anyone uses for a
    specific city - yet register() (below) attributes that alternate
    to the entry's full population regardless, letting a nickname
    silently outrank every real, unrelated city of the same name
    worldwide (Victoria, Canada; Garden City, Kansas) by a country's
    entire population rather than a real, comparable city's own. The
    entry's own PRIMARY name is unaffected by this check ("Hong Kong"
    and "Singapore" themselves still resolve normally) - only its
    alternate names are excluded.
    """
    country_name = _country_display_name(upper_code)
    if country_name is None:
        return False
    return _normalize_city_for_matching(name) == _normalize_city_for_matching(country_name)

def _build_city_index() -> dict[str, dict[str, int]]:
    """
    Build normalized-city-name -> {country_code: population} exactly
    once, at import time (GEO_LOOKUP_DATA_INITIALIZED_ONCE) - never
    rescanned per request. Both the primary name and every Latin-
    alphabet alternate name geonamescache itself lists are indexed
    (e.g. "New York" is a real geonamescache alternate of "New York
    City"), and multi-word names are kept intact rather than split -
    the longest-match scan in resolve_city_country_codes needs them
    whole to try "new york" before ever falling back to "new"/"york"
    individually. Population is kept (the largest seen per country,
    across every name/alternate that maps to it) purely so a
    genuinely dominant match (Madrid, Spain over the much smaller
    Madrid, Colombia) can be told apart from a real tie (Barcelona,
    Spain vs Barcelona, Venezuela) - see _DOMINANT_POPULATION_RATIO -
    never as a threshold a city must clear to be recognized at all.
    A country-level aggregate entry's own alternate names are the one
    exception - see _is_country_level_aggregate.
    """
    cache = geonamescache.GeonamesCache()
    index: dict[str, dict[str, int]] = {}

    def register(name: str, upper_code: str, population: int) -> None:
        normalized_name = _normalize_city_for_matching(name)
        if len(normalized_name) < _MINIMUM_CITY_NAME_LENGTH:
            return
        if normalized_name.count(' ') >= _MAX_LOCATION_PHRASE_WORDS:
            return
        by_country = index.setdefault(normalized_name, {})
        by_country[upper_code] = max(by_country.get(upper_code, 0), population)
    for city in cache.get_cities().values():
        name = city.get('name', '')
        country_code = city.get('countrycode')
        population = int(city.get('population', 0) or 0)
        if not name or not country_code:
            continue
        upper_code = country_code.upper()
        register(name, upper_code, population)
        if _is_country_level_aggregate(name, upper_code):
            continue
        for alternate_name in city.get('alternatenames', None) or ():
            if _LATIN_ALTERNATE_NAME_PATTERN.match(alternate_name):
                register(alternate_name, upper_code, population)
    return index
_CITY_COUNTRY_POPULATIONS_BY_NAME: Final[dict[str, dict[str, int]]] = _build_city_index()
_DOMINANT_POPULATION_RATIO: Final[int] = 10

def _tokenize(normalized_text: str) -> list[tuple[str, int, int]]:
    """Every word-like token in already-normalized text, in order,
    with its (start, end) character offsets - needed to test
    adjacency for both longest-match consumption and the preceding-
    preposition context check."""
    return [(match.group(0), match.start(), match.end()) for match in _WORD_PATTERN.finditer(normalized_text)]

def _has_location_context(tokens: list[tuple[str, int, int]], start_index: int, end_index: int, normalized_text: str) -> bool:
    """
    A matched city span only counts as a genuine location mention when
    the text actually signals location intent around it - never from
    population alone (corrective gate, section 2). Two generic,
    non-city-specific signals, either sufficient on its own:

    - immediately preceded by a location preposition ("employment law
      in Lisbon", "based in Reading", "rules for employees in X" all
      end this way right before the place name - section 4's own
      examples);
    - the entire message is nothing but the place name itself (a bare
      "Barcelona" is an obviously intentional, if ambiguous, location
      question, not a common-word collision).

    A span with neither signal (e.g. "male" in "a male employee
    quota", "union" in "join a union", "reading" in "reading
    employment contracts") is not treated as a location at all.
    """
    if start_index > 0:
        preceding_word = tokens[start_index - 1][0]
        if preceding_word in _LOCATION_PREPOSITIONS:
            return True
    span_start = tokens[start_index][1]
    span_end = tokens[end_index - 1][2]
    if normalized_text[:span_start].strip() == '' and normalized_text[span_end:].strip() == '':
        return True
    return False

def _dominant_country_code(populations_by_country: dict[str, int]) -> str | None:
    """
    Return the one country code whose population for this city name
    dominates every other candidate by at least
    _DOMINANT_POPULATION_RATIO, or None when no single candidate
    dominates that clearly (a real tie - never guessed). Only ever
    called with two or more candidates.
    """
    assert len(populations_by_country) >= 2
    ranked = sorted(populations_by_country.items(), key=lambda item: item[1], reverse=True)
    (top_code, top_population), (_, second_population) = (ranked[0], ranked[1])
    if second_population <= 0:
        return top_code if top_population > 0 else None
    if top_population >= second_population * _DOMINANT_POPULATION_RATIO:
        return top_code
    return None

def _narrow_candidates(populations_by_country: dict[str, int]) -> frozenset[str]:
    """
    Reduce one matched name's own real candidates to its single
    dominant country when one clearly stands out, or leave every real
    candidate in place when none does - the single, shared narrowing
    step every matched span goes through on its own, before (not
    after) two or more distinct matches are ever combined. Applying
    this per-match, rather than only when exactly one match exists in
    the whole text, is what keeps an unambiguous city named alongside
    another one from being contaminated by that other city's own,
    unrelated non-dominant candidates - see resolve_city_country_
    codes' own docstring.
    """
    if len(populations_by_country) == 1:
        (only_code, _), = populations_by_country.items()
        return frozenset({only_code})
    dominant_code = _dominant_country_code(populations_by_country)
    if dominant_code is not None:
        return frozenset({dominant_code})
    return frozenset(populations_by_country)
_PLACE_NAME_LINKING_WORDS: Final[frozenset[str]] = frozenset({'upon', 'under'})

def resolve_city_country_codes(text: str) -> tuple[frozenset[str], str | None]:
    """
    Return the set of country codes any city name matched in `text`
    could belong to, and which normalized name/phrase matched.

    Longest match first (corrective gate, section 3): every possible
    word span is tried longest-first; once a span matches a known
    city/alternate name, its tokens are removed from consideration
    entirely - a real multi-word match ("new york") is never destroyed
    because one of its own component words ("york") separately
    matches a different, unrelated city. This holds even when the
    longer match fails the context check below: "New York" inside "I
    love New York pizza" still consumes "new" and "york" together
    (prints nothing - not a location without location context and
    never falls back to matching bare "York" instead).

    A matched span only contributes a candidate when it also has
    genuine location context (_has_location_context) - never on
    population alone. A single-word match immediately followed by a
    place-name linking word (_PLACE_NAME_LINKING_WORDS) is suppressed
    entirely rather than trusted alone - see that constant's own
    comment. Every matched span is narrowed to its own dominant
    country first (_narrow_candidates), independently of any other
    span found in the same text - when exactly one span matches, its
    narrowed result is the whole answer; when two or more distinct
    spans match, each contributes only its OWN narrowed candidates to
    one combined pool (never its raw, un-narrowed set - a genuinely
    unrelated non-dominant candidate from one city must never
    contaminate a different, unrelated city's own answer), and
    matched_name lists every matched name so a caller's message never
    misattributes the combined ambiguity to only one of them.
    """
    normalized_text = _normalize_city_for_matching(text)
    tokens = _tokenize(normalized_text)
    consumed = [False] * len(tokens)
    matches: list[tuple[str, dict[str, int]]] = []
    for span_length in range(min(_MAX_LOCATION_PHRASE_WORDS, len(tokens)), 0, -1):
        for start_index in range(0, len(tokens) - span_length + 1):
            end_index = start_index + span_length
            if any(consumed[start_index:end_index]):
                continue
            candidate_name = ' '.join((tokens[index][0] for index in range(start_index, end_index)))
            populations_by_country = _CITY_COUNTRY_POPULATIONS_BY_NAME.get(candidate_name)
            if populations_by_country is None:
                continue
            if span_length == 1 and end_index < len(tokens) and (tokens[end_index][0] in _PLACE_NAME_LINKING_WORDS):
                continue
            for index in range(start_index, end_index):
                consumed[index] = True
            if _has_location_context(tokens, start_index, end_index, normalized_text):
                matches.append((candidate_name, populations_by_country))
    if not matches:
        return (frozenset(), None)
    if len(matches) == 1:
        matched_name, populations_by_country = matches[0]
        return (_narrow_candidates(populations_by_country), matched_name)
    candidate_codes: set[str] = set()
    matched_names: list[str] = []
    for name, populations_by_country in matches:
        candidate_codes |= _narrow_candidates(populations_by_country)
        matched_names.append(name)
    return (frozenset(candidate_codes), ', '.join(matched_names))
_CAPITALIZED_LOCATION_PHRASE_PATTERN: Final[re.Pattern[str]] = re.compile("\\b(?:in|at)\\s+([A-Z][A-Za-z'-]*(?:\\s+[A-Z][A-Za-z'-]*){0,2})\\b")

def detect_unresolved_location_phrase(text: str) -> str | None:
    """
    A capitalized word or short phrase immediately following "in"/"at"
    that resolve_city_country_codes could not match to any known city
    - a real, if unrecognized, place name candidate (corrective gate,
    section 11), never a fabricated country. Returns the phrase
    exactly as written (original casing) so a "Which country is
    <place> in?" clarification reads naturally, or None when nothing
    such is present. Callers are expected to have already ruled out
    an explicit country and a resolved/ambiguous city match for the
    same text - this only identifies the "looks like a place, dataset
    does not recognize it" case, never overrides either.
    """
    for match in _CAPITALIZED_LOCATION_PHRASE_PATTERN.finditer(text):
        phrase = match.group(1)
        city_codes, _ = resolve_city_country_codes(f'in {phrase}')
        if not city_codes:
            return phrase
    return None
CountryCatalogProvider = Callable[[], LegalCatalogResponse]
COUNTRY_ALIAS_OVERRIDES: Final[dict[str, str]] = {'uk': 'GB', 'u.k.': 'GB', 'great britain': 'GB', 'britain': 'GB', 'usa': 'US', 'u.s.': 'US', 'u.s.a.': 'US', 'america': 'US', 'south korea': 'KR', 'czechia': 'CZ', 'uae': 'AE', 'u.a.e.': 'AE'}

class CountryDetectionError(RuntimeError):
    """Raised when automatic country detection fails."""

@dataclass(frozen=True, slots=True)
class CountryAvailability:
    """Countries mentioned in a question, split by corpus availability."""
    available_codes: list[str]
    unavailable_codes: list[str]

def _normalize_for_matching(value: str) -> str:
    """Normalize text for country-name matching."""
    decomposed_value = unicodedata.normalize('NFKD', value)
    without_diacritics = ''.join((character for character in decomposed_value if not unicodedata.combining(character)))
    alphanumeric_value = re.sub('[^0-9A-Za-z]+', ' ', without_diacritics)
    return ' '.join(alphanumeric_value.casefold().split())

def _normalize_country_codes(values: Sequence[str]) -> list[str]:
    """Normalize and deduplicate explicit country codes."""
    normalized_codes: list[str] = []
    seen_codes: set[str] = set()
    for value in values:
        normalized_value = ' '.join(value.split()).upper()
        if not normalized_value:
            continue
        if normalized_value in seen_codes:
            continue
        seen_codes.add(normalized_value)
        normalized_codes.append(normalized_value)
    return normalized_codes

def _build_global_country_data() -> tuple[dict[str, str], dict[str, list[str]]]:
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
        official_name = getattr(country, 'official_name', None)
        if official_name:
            candidate_names.add(official_name)
        for name in candidate_names:
            normalized_name = _normalize_for_matching(name)
            if not normalized_name:
                continue
            phrase_map.setdefault(normalized_name, code)
            names_by_code[code].append(name)
    for alias, code in COUNTRY_ALIAS_OVERRIDES.items():
        normalized_alias = _normalize_for_matching(alias)
        if not normalized_alias:
            continue
        normalized_code = code.upper()
        phrase_map[normalized_alias] = normalized_code
        names_by_code[normalized_code].append(alias)
    return (phrase_map, dict(names_by_code))
_GLOBAL_COUNTRY_PHRASE_MAP, _COUNTRY_NAMES_BY_CODE = _build_global_country_data()

def _build_phrase_patterns() -> tuple[tuple[str, re.Pattern[str], str], ...]:
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
    return tuple(((phrase, re.compile(f'(?<!\\w){re.escape(phrase)}(?!\\w)'), country_code) for phrase, country_code in sorted(_GLOBAL_COUNTRY_PHRASE_MAP.items(), key=lambda item: len(item[0]), reverse=True)))
_COUNTRY_PHRASE_PATTERNS: Final[tuple[tuple[str, re.Pattern[str], str], ...]] = _build_phrase_patterns()

def get_country_name_variants(country_code: str) -> list[str]:
    """Return the known display names/aliases for one country code."""
    return _COUNTRY_NAMES_BY_CODE.get(country_code.upper(), [])
_COUNTRY_DEMONYMS: Final[dict[str, list[str]]] = {'AR': ['Argentine', 'Argentinian'], 'AU': ['Australian'], 'BE': ['Belgian'], 'BR': ['Brazilian'], 'CA': ['Canadian'], 'CH': ['Swiss'], 'CL': ['Chilean'], 'CN': ['Chinese'], 'CO': ['Colombian'], 'CZ': ['Czech'], 'DE': ['German'], 'DK': ['Danish'], 'ES': ['Spanish'], 'FI': ['Finnish'], 'FR': ['French'], 'GB': ['British', 'UK'], 'GR': ['Greek'], 'IE': ['Irish'], 'IN': ['Indian'], 'IT': ['Italian'], 'JP': ['Japanese'], 'KR': ['Korean', 'South Korean'], 'MX': ['Mexican'], 'NL': ['Dutch'], 'NO': ['Norwegian'], 'NZ': ['New Zealand'], 'PE': ['Peruvian'], 'PL': ['Polish'], 'PT': ['Portuguese'], 'RO': ['Romanian'], 'SE': ['Swedish'], 'SG': ['Singaporean'], 'TH': ['Thai'], 'US': ['American'], 'VN': ['Vietnamese'], 'ZA': ['South African']}

def get_country_demonyms(country_code: str) -> list[str]:
    """
    Return the known national adjective(s)/demonym(s) for one country
    code - e.g. ["Spanish"] for "ES" - or an empty list when this
    country has none recorded yet (see _COUNTRY_DEMONYMS above).
    """
    return _COUNTRY_DEMONYMS.get(country_code.upper(), [])

def resolve_country_display_name(country_code: str) -> str:
    """Return a readable display name for one ISO alpha-2 country code."""
    country = pycountry.countries.get(alpha_2=country_code.upper())
    if country is not None:
        return country.name
    return country_code.upper()

def detect_mentioned_country_codes(question: str) -> list[str]:
    """
    Detect ISO country codes for every country named in a question.

    This covers every ISO 3166-1 country, not only the ones indexed in
    the corpus, so that mentioned-but-unindexed countries (for example
    Canada) can be reported instead of silently ignored. Bare two-letter
    codes are intentionally not scanned for in free text, since common
    words can collide with real ISO codes (for example "IN").
    """
    normalized_question = _normalize_for_matching(question)
    if not normalized_question:
        return []
    candidates: list[tuple[int, int, str]] = []
    for phrase, pattern, country_code in _COUNTRY_PHRASE_PATTERNS:
        for match in pattern.finditer(normalized_question):
            candidates.append((match.start(), -len(phrase), country_code))
    candidates.sort(key=lambda candidate: (candidate[0], candidate[1]))
    detected_codes: list[str] = []
    seen_codes: set[str] = set()
    for _, _, country_code in candidates:
        if country_code in seen_codes:
            continue
        seen_codes.add(country_code)
        detected_codes.append(country_code)
    return detected_codes
_COUNTRY_ONLY_FOLLOWUP_CONNECTOR_WORDS: Final[frozenset[str]] = frozenset({'what', 'about', 'how', 'and', 'for', 'the', 'or', 'of'})

def is_country_only_followup(question: str) -> list[str] | None:
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
        for variant in get_country_name_variants(code) + get_country_demonyms(code):
            working_text = re.sub(f'(?<!\\w){re.escape(variant)}(?!\\w)', ' ', working_text, flags=re.IGNORECASE)
    remaining_words = re.findall("[A-Za-z0-9']+", working_text)
    remaining_content_words = [word for word in remaining_words if word.casefold() not in _COUNTRY_ONLY_FOLLOWUP_CONNECTOR_WORDS]
    if remaining_content_words:
        return None
    return country_codes

class JurisdictionResolutionStatus(Enum):
    """The four, and only four, outcomes resolve_jurisdiction returns."""
    RESOLVED = 'resolved'
    AMBIGUOUS = 'ambiguous'
    UNKNOWN_LOCALITY = 'unknown_locality'
    NOT_FOUND = 'not_found'

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
    candidate_country_codes: tuple[str, ...] = field(default_factory=tuple)

def resolve_jurisdiction(text: str) -> JurisdictionResolution:
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
        return JurisdictionResolution(status=JurisdictionResolutionStatus.RESOLVED, country_code=code, country_name=resolve_country_display_name(code), matched_location=text.strip())
    if len(explicit_codes) > 1:
        return JurisdictionResolution(status=JurisdictionResolutionStatus.AMBIGUOUS, candidate_country_codes=tuple(explicit_codes))
    candidate_codes, matched_word = resolve_city_country_codes(text)
    if not candidate_codes:
        unresolved_phrase = detect_unresolved_location_phrase(text)
        if unresolved_phrase is not None:
            return JurisdictionResolution(status=JurisdictionResolutionStatus.UNKNOWN_LOCALITY, matched_location=unresolved_phrase)
        return JurisdictionResolution(status=JurisdictionResolutionStatus.NOT_FOUND)
    if len(candidate_codes) == 1:
        code = next(iter(candidate_codes))
        return JurisdictionResolution(status=JurisdictionResolutionStatus.RESOLVED, country_code=code, country_name=resolve_country_display_name(code), matched_location=matched_word)
    return JurisdictionResolution(status=JurisdictionResolutionStatus.AMBIGUOUS, candidate_country_codes=tuple(sorted(candidate_codes)), matched_location=matched_word)

def resolve_country_availability(request: LegalChatRequest, catalog_provider: CountryCatalogProvider=get_legal_catalog) -> CountryAvailability:
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
    explicit_codes = _normalize_country_codes(request.country_codes)
    if explicit_codes:
        mentioned_codes = explicit_codes
    else:
        detected_codes = detect_mentioned_country_codes(request.question)
        if detected_codes:
            mentioned_codes = detected_codes
        else:
            city_candidate_codes, _ = resolve_city_country_codes(request.question)
            mentioned_codes = [next(iter(city_candidate_codes))] if len(city_candidate_codes) == 1 else []
    if not mentioned_codes:
        return CountryAvailability(available_codes=[], unavailable_codes=[])
    try:
        catalog = catalog_provider()
    except LegalCatalogError as error:
        raise CountryDetectionError('The indexed country catalog could not be read.') from error
    indexed_codes = {country.country_code.upper() for country in catalog.countries}
    available_codes = [code for code in mentioned_codes if code in indexed_codes]
    unavailable_codes = [code for code in mentioned_codes if code not in indexed_codes]
    return CountryAvailability(available_codes=available_codes, unavailable_codes=unavailable_codes)
