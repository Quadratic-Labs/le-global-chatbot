"""
Resolve a real-world city mentioned in text to its country - the one
piece of jurisdiction resolution country_detection.py's own worldwide,
pycountry-backed country/alias/demonym scan does not cover.

Mission "ORDER 5C-GEO" / corrective gate: the legal corpus is organized
by country, so a question naming only a city ("employment law in
Lisbon") must still resolve to a country before availability/RAG
routing can proceed. This module is deliberately city-only and has no
dependency on country_detection.py (which itself depends on this
module for the city fallback - see country_detection.resolve_
jurisdiction, the combined country-then-city primitive callers should
actually use). It never guesses when a city name is genuinely ambiguous
across more than one real country - resolve_city_country_codes below
returns every real candidate rather than picking one.

The city dataset is a local, offline snapshot bundled by the
`geonamescache` package (MIT licensed, no network access at runtime) -
never a live geocoding API.

Resolution contract (corrective gate, section 2):

    EXPLICIT COUNTRY -> LONGEST LOCATION PHRASE -> CITY CANDIDATES
        -> CONFIDENCE / AMBIGUITY -> COUNTRY

Population is a ranking signal between multiple real candidates for
the *same* matched phrase only (_DOMINANT_POPULATION_RATIO below) -
never a existence/recognition gate. An earlier revision rejected any
single-candidate match below an absolute population floor; direct
inspection of the real dataset showed this could not be made to work
(Male/Maldives and Reading/England both clear any floor low enough to
still admit Lisbon or Tunis) and, more fundamentally, it silently
refused to recognize entirely legitimate small cities (national
capitals such as Vaduz, San Marino, or Valletta are all indexed here
at well under 10,000 people). What actually distinguishes "male" the
common English word from "Male" the capital of the Maldives is never
population - it is whether the word is being used as a location at
all, which is a *context* question (see _has_location_context below),
not a population question.
"""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache
from typing import Final

import geonamescache
import pycountry


_MINIMUM_CITY_NAME_LENGTH: Final[int] = 4

# The longest real multi-word city/alternate name this index actually
# contains ("andorra la vella") is three words; one extra word of
# headroom costs nothing (n-gram generation is bounded by input length
# x this constant, not by dataset size) and protects against a longer
# legitimate name appearing in a future geonamescache release.
_MAX_LOCATION_PHRASE_WORDS: Final[int] = 4

# Generic location-introducing prepositions - not a phrase parser
# (corrective gate, section 4: "ne pas transformer cette liste en
# gigantesque parser artisanal"). Every context example the mission
# itself gives ("employment law in X", "employees in X", "rules in
# X", "working in X", "based in X", "law for employees in X") ends
# with a bare "in" (or "at") immediately before the place name - a
# single-token lookback for these two words covers all of them without
# needing to enumerate the compound phrases themselves.
_LOCATION_PREPOSITIONS: Final[frozenset[str]] = frozenset({"in", "at"})

_WORD_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"[A-Za-z][A-Za-z'-]*"
)

# geonamescache's own alternate-name list is heavily multilingual
# (transliterations in Cyrillic, CJK, Arabic, etc. sit alongside
# genuine English forms - e.g. New York City's alternates include
# both "New York" and "Niu-Jorka"). Restricting to this pattern keeps
# only the Latin-alphabet forms that could ever match English-language
# input in the first place; the rest are harmless but useless to index
# (they can never be produced by _WORD_PATTERN scanning English text).
_LATIN_ALTERNATE_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^[A-Za-z][A-Za-z' -]*$"
)


def _normalize_for_matching(value: str) -> str:
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

    decomposed_value = unicodedata.normalize("NFKD", value)

    without_diacritics = "".join(
        character
        for character in decomposed_value
        if not unicodedata.combining(character)
    )

    without_periods = without_diacritics.replace(".", "")

    return " ".join(without_periods.casefold().split())


@lru_cache(maxsize=None)
def _country_display_name(country_code: str) -> str | None:
    # Only ever called from _build_city_index, at import time, once
    # per city (34,006 calls) across a maximum of a few hundred
    # distinct country codes - cached so the real, measured ~270ms
    # this lookup added to TOTAL_GEO_INIT_MS collapses to the cost of
    # a few hundred real pycountry lookups, not tens of thousands.
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

    return (
        _normalize_for_matching(name)
        == _normalize_for_matching(country_name)
    )


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
        normalized_name = _normalize_for_matching(name)

        if len(normalized_name) < _MINIMUM_CITY_NAME_LENGTH:
            return

        if (
            normalized_name.count(" ")
            >= _MAX_LOCATION_PHRASE_WORDS
        ):
            return

        by_country = index.setdefault(normalized_name, {})

        by_country[upper_code] = max(
            by_country.get(upper_code, 0),
            population,
        )

    for city in cache.get_cities().values():
        name = city.get("name", "")
        country_code = city.get("countrycode")
        population = int(city.get("population", 0) or 0)

        if not name or not country_code:
            continue

        upper_code = country_code.upper()

        register(name, upper_code, population)

        if _is_country_level_aggregate(name, upper_code):
            continue

        for alternate_name in city.get("alternatenames", None) or ():
            if _LATIN_ALTERNATE_NAME_PATTERN.match(alternate_name):
                register(alternate_name, upper_code, population)

    return index


_CITY_COUNTRY_POPULATIONS_BY_NAME: Final[
    dict[str, dict[str, int]]
] = _build_city_index()

# A generic, demonstrable tie-break, never a per-city special case
# (mission "ORDER 5C-GEO", section 11: "aucun hardcode Barcelona" -
# this rule is checked against Barcelona too, and correctly leaves it
# ambiguous). Audited on the real, current dataset (corrective gate,
# section 10) rather than re-picked on intuition: 3,649 indexed names
# have two or more country candidates; sorting every one of them by
# its top1/top2 population ratio shows a continuous spread with no
# natural gap to snap to (1,210 pairs sit under 2x - genuine, roughly
# equal-sized ties like Barcelona ES/VE ~2x, Valencia ES/VE/US/EC ~2x,
# Cambridge's four candidates topping out ~1.1x between the top two -
# and the distribution climbs smoothly from there). At the ratio 10
# boundary specifically, cities most people would call "obviously one
# place" but which do have a real, smaller same-named counterpart
# elsewhere (Geneva CH/US ~9.25x, Florence IT/US ~9.17x, Milan IT/US
# ~9.51x, Coventry GB/US ~9.72x) stay AMBIGUOUS, which is the correct,
# conservative call under "never guess" - while Washington US/GB
# ~10.28x, Panama PA/US ~10.66x and Guangzhou CN/KR ~11.49x clear it
# and resolve. 10 remains a reasonable, conservative line through that
# spread, not an arbitrary round number kept out of inertia - kept
# unchanged (corrective gate, section 10: "ne pas changer uniquement
# parce que 10 semble arbitraire").
_DOMINANT_POPULATION_RATIO: Final[int] = 10


def _tokenize(normalized_text: str) -> list[tuple[str, int, int]]:
    """Every word-like token in already-normalized text, in order,
    with its (start, end) character offsets - needed to test
    adjacency for both longest-match consumption and the preceding-
    preposition context check."""

    return [
        (match.group(0), match.start(), match.end())
        for match in _WORD_PATTERN.finditer(normalized_text)
    ]


def _has_location_context(
    tokens: list[tuple[str, int, int]],
    start_index: int,
    end_index: int,
    normalized_text: str,
) -> bool:
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

    if (
        normalized_text[:span_start].strip() == ""
        and normalized_text[span_end:].strip() == ""
    ):
        return True

    return False


def _dominant_country_code(
    populations_by_country: dict[str, int],
) -> str | None:
    """
    Return the one country code whose population for this city name
    dominates every other candidate by at least
    _DOMINANT_POPULATION_RATIO, or None when no single candidate
    dominates that clearly (a real tie - never guessed). Only ever
    called with two or more candidates.
    """

    assert len(populations_by_country) >= 2

    ranked = sorted(
        populations_by_country.items(),
        key=lambda item: item[1],
        reverse=True,
    )

    (top_code, top_population), (_, second_population) = (
        ranked[0],
        ranked[1],
    )

    if second_population <= 0:
        return top_code if top_population > 0 else None

    if top_population >= second_population * _DOMINANT_POPULATION_RATIO:
        return top_code

    return None


def _narrow_candidates(
    populations_by_country: dict[str, int],
) -> frozenset[str]:
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


# Generic English place-name linking words - not a Kingston-upon-
# Thames-specific exception. Compound British place names of the
# form "X upon/under Y" (Newcastle upon Tyne, Kingston upon Hull,
# Burton upon Trent - all three genuinely indexed) are common enough
# that a bare head word immediately followed by one of these should
# not be trusted alone: found by adversarial review that "Kingston
# upon Thames" (a real place geonamescache simply never recorded as
# an alternate name) falls through to matching bare "kingston", which
# is itself genuinely ambiguous across four unrelated countries - and
# none of them is the United Kingdom, the one place a name like this
# could ever plausibly mean. Suppressing the bare match here (rather
# than confidently offering a candidate list that omits the one
# clearly-intended country) only ever fires when the longer, qualified
# phrase was NOT itself found - "Newcastle upon Tyne" is a three-word
# span the longest-match scan already finds and consumes before this
# single-word fallback is ever reached, so this never affects it.
_PLACE_NAME_LINKING_WORDS: Final[frozenset[str]] = frozenset(
    {"upon", "under"}
)


def resolve_city_country_codes(
    text: str,
) -> tuple[frozenset[str], str | None]:
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

    normalized_text = _normalize_for_matching(text)
    tokens = _tokenize(normalized_text)
    consumed = [False] * len(tokens)
    matches: list[tuple[str, dict[str, int]]] = []

    for span_length in range(
        min(_MAX_LOCATION_PHRASE_WORDS, len(tokens)),
        0,
        -1,
    ):
        for start_index in range(0, len(tokens) - span_length + 1):
            end_index = start_index + span_length

            if any(consumed[start_index:end_index]):
                continue

            candidate_name = " ".join(
                tokens[index][0]
                for index in range(start_index, end_index)
            )

            populations_by_country = (
                _CITY_COUNTRY_POPULATIONS_BY_NAME.get(candidate_name)
            )

            if populations_by_country is None:
                continue

            if (
                span_length == 1
                and end_index < len(tokens)
                and tokens[end_index][0]
                in _PLACE_NAME_LINKING_WORDS
            ):
                continue

            for index in range(start_index, end_index):
                consumed[index] = True

            if _has_location_context(
                tokens,
                start_index,
                end_index,
                normalized_text,
            ):
                matches.append((candidate_name, populations_by_country))

    if not matches:
        return frozenset(), None

    if len(matches) == 1:
        matched_name, populations_by_country = matches[0]

        return _narrow_candidates(populations_by_country), matched_name

    candidate_codes: set[str] = set()
    matched_names: list[str] = []

    for name, populations_by_country in matches:
        candidate_codes |= _narrow_candidates(populations_by_country)
        matched_names.append(name)

    return frozenset(candidate_codes), ", ".join(matched_names)


_CAPITALIZED_LOCATION_PHRASE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\b(?:in|at)\s+"
    r"([A-Z][A-Za-z'-]*(?:\s+[A-Z][A-Za-z'-]*){0,2})"
    r"\b"
)


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
        city_codes, _ = resolve_city_country_codes(
            f"in {phrase}"
        )

        if not city_codes:
            return phrase

    return None
