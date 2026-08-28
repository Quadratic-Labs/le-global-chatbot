"""
Keeps a legal action's subject_text/search_concepts jurisdiction-
neutral - the country belongs only in country_codes, never baked into
the transferable subject description.

Mission: "DECOUPLAGE COMPLET DU SUJET JURIDIQUE ET DE LA JURIDICTION".
The defect this exists to fix: RequestUnderstanding sometimes returns
subject_text like "rules on remote work (telework) in Spain" instead of
"rules on remote work (telework)" - and a bare country follow-up
("Peru?") only ever replaces country_codes (see conversation_transition
._inherit_action), so the OLD country silently survives inside the
inherited subject_text, the retrieval query built from it, and the
insufficient/partial message shown for the NEW country. This module is
the single centralized place that strips a known geographic scope back
out, so every trust boundary that touches subject_text/search_concepts
(RequestUnderstanding's own output, a client-supplied ConversationState,
conversation_transition's inheritance, evidence-spec construction) can
call the same pure, deterministic function rather than each re-solving
the same problem - or not solving it at all.

Pure text processing only - no network call, no OpenAI, no OpenSearch.
Only ever removes a country when it appears in one of the specific
geographic grammatical frames enumerated below (an "in/for/within X"
prepositional phrase, a "the supplied source under X law" clause, a
leading "X:"/"X's" scope marker, or an "X and Y"/"between X and Y"
comparison join) - never a blind find-and-replace of the country's name
anywhere in the text, which could otherwise mangle a law name, an
institution name, or a citation that happens to share a word with a
country name.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

from app.services.country_detection import (
    get_country_demonyms,
    get_country_name_variants,
)


class SearchConceptLike(Protocol):
    terms: list[str]


@dataclass(frozen=True, slots=True)
class CanonicalSearchConcept:
    """One canonicalized synonym group - same shape as
    ConversationSearchConcept, kept independent of it so this module
    has no dependency on the Pydantic conversation-state models."""

    terms: list[str]


@dataclass(frozen=True, slots=True)
class CanonicalizedLegalSubject:
    """
    The result of canonicalizing one action's subject/search_concepts.

    `removed_country_codes` names exactly which of the given country
    codes were actually found (and stripped) in the original text -
    never a superset assumed just because a code was passed in.
    `subject_became_empty` is true only when `subject_text` was
    non-empty before canonicalization and became empty/whitespace-only
    after - the caller (never this module) decides the safe policy for
    that case (a targeted clarification, never a silent general
    search - see the mission's Phase 9/13).
    """

    subject_text: str | None
    search_concepts: list[CanonicalSearchConcept]
    changed: bool
    removed_country_codes: list[str]
    subject_became_empty: bool


_GEOGRAPHIC_FRAME_PATTERN_CACHE: dict[
    tuple[str, ...],
    list[re.Pattern[str]],
] = {}

_PRESENCE_PATTERN_CACHE: dict[tuple[str, ...], re.Pattern[str]] = {}


def _unique_sorted_longest_first(values: list[str]) -> list[str]:
    unique_casefolded: dict[str, str] = {}

    for value in values:
        stripped = value.strip()

        if not stripped:
            continue

        unique_casefolded.setdefault(stripped.casefold(), stripped)

    return sorted(
        unique_casefolded.values(),
        key=len,
        reverse=True,
    )


def _alternation(variants: list[str]) -> str:
    return "|".join(re.escape(variant) for variant in variants)


def _compile_geographic_frame_patterns(
    country_codes: tuple[str, ...],
) -> list[re.Pattern[str]]:
    name_variants = _unique_sorted_longest_first(
        [
            variant
            for code in country_codes
            for variant in get_country_name_variants(code)
        ]
    )
    demonym_variants = _unique_sorted_longest_first(
        [
            variant
            for code in country_codes
            for variant in get_country_demonyms(code)
        ]
    )

    patterns: list[re.Pattern[str]] = []

    if name_variants:
        name_alt = _alternation(name_variants)
        # A plain `\b` after the alternation fails whenever a variant
        # itself ends in a non-word character (e.g. "U.K.", "U.S.A.")
        # followed by end-of-string or another non-word character -
        # both sides of that position are then non-word, so `\b`
        # never fires there at all. A negative lookahead for a word
        # character is boundary-safe regardless of how the variant
        # itself ends.
        trailing_edge = r"(?!\w)"

        # Possessive: "Spain's rules on overtime" -> "rules on overtime".
        patterns.append(
            re.compile(
                rf"^(?:the\s+)?(?:{name_alt})['’]s\s+",
                re.IGNORECASE,
            )
        )

        # Leading colon: "Spain: overtime rules" -> "overtime rules".
        patterns.append(
            re.compile(
                rf"^(?:the\s+)?(?:{name_alt})\s*:\s*",
                re.IGNORECASE,
            )
        )

        # Leading connector + comma: "In Spain, overtime rules" ->
        # "overtime rules".
        patterns.append(
            re.compile(
                rf"^(?:in|for|within)\s+(?:the\s+)?(?:{name_alt})"
                rf"\s*,\s*",
                re.IGNORECASE,
            )
        )

        # "between X and Y": "compare pay between Spain and Peru" ->
        # "compare pay".
        patterns.append(
            re.compile(
                rf"\s+between\s+(?:the\s+)?(?:{name_alt})\s+and\s+"
                rf"(?:the\s+)?(?:{name_alt}){trailing_edge}",
                re.IGNORECASE,
            )
        )

        # Trailing "in/for/within X (and Y)*": "overtime rules in
        # Spain and Peru" -> "overtime rules".
        patterns.append(
            re.compile(
                rf"\s+(?:in|for|within)\s+(?:the\s+)?(?:{name_alt})"
                rf"(?:\s+and\s+(?:the\s+)?(?:{name_alt}))*{trailing_edge}",
                re.IGNORECASE,
            )
        )

        # The whole text is nothing but the country name(s) - a
        # degenerate case (e.g. a model producing subject_text="Spain"
        # on its own) carries zero transferable legal-subject
        # information, so it must become empty rather than survive as
        # a bare country name pretending to be a subject.
        patterns.append(
            re.compile(
                rf"^(?:the\s+)?(?:{name_alt})"
                rf"(?:\s+and\s+(?:the\s+)?(?:{name_alt}))*$",
                re.IGNORECASE,
            )
        )

    if demonym_variants:
        demonym_alt = _alternation(demonym_variants)

        # Leading demonym clause: "Under Spanish law, remote work
        # rules" -> "remote work rules".
        patterns.append(
            re.compile(
                rf"^(?:under|according\s+to|pursuant\s+to)\s+"
                rf"(?:{demonym_alt})\s+(?:employment\s+)?law\s*,\s*",
                re.IGNORECASE,
            )
        )

        # Trailing demonym clause: "overtime rules under Spanish
        # employment law" -> "overtime rules".
        patterns.append(
            re.compile(
                rf"\s+(?:under|according\s+to|pursuant\s+to)\s+"
                rf"(?:{demonym_alt})\s+(?:employment\s+)?law\b",
                re.IGNORECASE,
            )
        )

    return patterns


def _geographic_frame_patterns(
    country_codes: list[str],
) -> list[re.Pattern[str]]:
    key = tuple(sorted(set(code.upper() for code in country_codes)))

    if not key:
        return []

    cached = _GEOGRAPHIC_FRAME_PATTERN_CACHE.get(key)

    if cached is None:
        cached = _compile_geographic_frame_patterns(key)
        _GEOGRAPHIC_FRAME_PATTERN_CACHE[key] = cached

    return cached


def _presence_pattern(country_codes: list[str]) -> re.Pattern[str] | None:
    key = tuple(sorted(set(code.upper() for code in country_codes)))

    if not key:
        return None

    cached = _PRESENCE_PATTERN_CACHE.get(key)

    if cached is not None:
        return cached

    variants = _unique_sorted_longest_first(
        [
            variant
            for code in key
            for variant in (
                get_country_name_variants(code)
                + get_country_demonyms(code)
            )
        ]
    )

    if not variants:
        return None

    pattern = re.compile(
        rf"\b(?:{_alternation(variants)})\b",
        re.IGNORECASE,
    )
    _PRESENCE_PATTERN_CACHE[key] = pattern

    return pattern


def _clean_whitespace_and_punctuation(text: str) -> str:
    collapsed = re.sub(r"\s+", " ", text).strip()

    # Only strips punctuation left orphaned by a removed geographic
    # frame at either edge - never punctuation the caller's own text
    # legitimately started or ended with as content (a citation's
    # trailing period, for instance, is never reached by this
    # function - subject_text carries no citations).
    without_leading = re.sub(r"^[,:;\-\s]+", "", collapsed)
    without_trailing = re.sub(r"[,:;\-\s]+$", "", without_leading)

    return without_trailing


def _strip_geographic_scope(
    text: str,
    country_codes: list[str],
) -> tuple[str, bool]:
    patterns = _geographic_frame_patterns(country_codes)

    if not patterns:
        return text, False

    working_text = text
    changed = False

    for pattern in patterns:
        new_text = pattern.sub("", working_text)

        if new_text != working_text:
            changed = True
            working_text = new_text

    if changed:
        working_text = _clean_whitespace_and_punctuation(working_text)

    return working_text, changed


def _codes_actually_present(
    text: str,
    country_codes: list[str],
) -> list[str]:
    present: list[str] = []

    for code in country_codes:
        pattern = _presence_pattern([code])

        if pattern is not None and pattern.search(text):
            present.append(code.upper())

    return present


def canonicalize_legal_subject(
    *,
    subject_text: str | None,
    search_concepts: list[SearchConceptLike],
    scoped_country_codes: list[str],
    additional_country_codes: list[str] | None = None,
) -> CanonicalizedLegalSubject:
    """
    Strip a known geographic scope back out of subject_text/
    search_concepts, leaving the transferable legal subject untouched.

    `scoped_country_codes` are the country(ies) this action is
    currently scoped to (its own country_codes) - always checked.
    `additional_country_codes` are extra codes worth checking on top
    (typically a prior turn's own country_codes, when canonicalizing
    an inherited subject a second time against the union of old and
    new countries - see conversation_transition.py). Passing no
    country codes at all is a no-op: there is nothing to strip.
    """

    all_codes = list(scoped_country_codes) + list(
        additional_country_codes or []
    )

    removed_country_codes = (
        _codes_actually_present(subject_text, all_codes)
        if subject_text
        else []
    )

    canonical_subject_text = subject_text
    subject_changed = False
    subject_became_empty = False

    if subject_text:
        stripped_text, subject_changed = _strip_geographic_scope(
            subject_text,
            all_codes,
        )

        if subject_changed:
            canonical_subject_text = stripped_text or None
            subject_became_empty = not stripped_text

    canonical_concepts: list[CanonicalSearchConcept] = []
    concepts_changed = False

    for concept in search_concepts:
        canonical_terms: list[str] = []
        seen_terms: set[str] = set()

        for term in concept.terms:
            stripped_term, term_changed = _strip_geographic_scope(
                term,
                all_codes,
            )

            if term_changed:
                concepts_changed = True

            normalized_term = stripped_term.strip()

            if not normalized_term:
                continue

            casefolded = normalized_term.casefold()

            if casefolded in seen_terms:
                continue

            seen_terms.add(casefolded)
            canonical_terms.append(normalized_term)

        if canonical_terms:
            canonical_concepts.append(
                CanonicalSearchConcept(terms=canonical_terms)
            )
        else:
            concepts_changed = concepts_changed or bool(concept.terms)

    return CanonicalizedLegalSubject(
        subject_text=canonical_subject_text,
        search_concepts=canonical_concepts,
        changed=subject_changed or concepts_changed,
        removed_country_codes=removed_country_codes,
        subject_became_empty=subject_became_empty,
    )
