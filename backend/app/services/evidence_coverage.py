"""
Deterministic, local evidence-coverage checks - no OpenAI call.

Decides whether a set of retrieved chunks actually supports the exact
subject a question asked about, distinct from merely belonging to the
right broad legal_topic/section. Reused by rag_answer.py before
generation (to gate direct/partial/insufficient - see
EvidenceAssessment) and by its own answer-quality validation (to
detect subject_drift in the generated text itself).

The three evidence_mode values (see request_understanding.py) drive
three different coverage policies:

- "broad_topic": the question genuinely is the whole topic area: any
  hit belonging to it counts as direct - no concept matching needed.
- "direct_topic": the question names one precise concept - at least
  one supplied search_concepts group (a set of direct synonyms) must
  be found, as a whole word/phrase, in at least one hit.
- "relation_required": the question depends on a relation between two
  or more concepts (e.g. "dismissal while on sick leave") - direct
  coverage requires every essential concept group to be found close
  together *within the same hit*, never merely present somewhere
  across different, unrelated hits (a real-world defect this mission
  fixes - see the 0.4.2 mission's rectificatif, section F).
"""

from __future__ import annotations

import re
import unicodedata
from typing import Final, Literal, Protocol

from app.models.search import LegalSearchHit


EvidenceStatus = Literal["direct", "partial", "insufficient"]

# How many normalized tokens apart two concept-group matches may be
# within the same hit and still count as describing one relation,
# rather than two unrelated statements that merely share a chunk.
# Centralized so it is covered by tests instead of re-guessed per call
# site - roughly a couple of sentences' worth of text.
RELATION_PROXIMITY_MAX_TOKENS: Final[int] = 40

_WHITESPACE_PATTERN: Final[re.Pattern[str]] = re.compile(r"\s+")
_DASH_PATTERN: Final[re.Pattern[str]] = re.compile(r"[‐-―\-]")
_TOKEN_PATTERN: Final[re.Pattern[str]] = re.compile(r"[a-z0-9]+")

# Narrow lexical normalization for legal qualifiers that may be
# present in RequestUnderstanding's synonym but omitted from the
# source's ordinary wording. We only strip one leading qualifier and
# only when at least two tokens remain, so a precise phrase such as
# "statutory severance pay" may match "severance pay", while
# "statutory severance" can never degrade into the overly broad
# single-token match "severance".
_OPTIONAL_LEADING_LEGAL_QUALIFIERS: Final[frozenset[str]] = frozenset(
    {
        "statutory",
        "mandatory",
    }
)

# Exact, conservative expansions for qualified concepts that would
# otherwise collapse to an unsafe one-word match. These expansions
# remain multi-word legal phrases.
_QUALIFIED_LEGAL_TERM_EXPANSIONS: Final[
    dict[tuple[str, ...], tuple[tuple[str, ...], ...]]
] = {
    ("statutory", "severance"): (
        ("severance", "pay"),
        ("severance", "payment"),
    ),
    ("mandatory", "severance"): (
        ("severance", "pay"),
        ("severance", "payment"),
    ),
}


class SearchConceptLike(Protocol):
    """Anything exposing a `terms: list[str]` attribute."""

    terms: list[str]


def normalize_for_matching(value: str) -> str:
    """
    Unicode-normalize, casefold, and collapse dashes/whitespace, so
    "non-compete", "non compete", and "Non‑Compete" all compare equal.
    """

    decomposed = unicodedata.normalize("NFKD", value)
    without_dashes = _DASH_PATTERN.sub(" ", decomposed)

    return _WHITESPACE_PATTERN.sub(
        " ", without_dashes.casefold()
    ).strip()


def _tokenize(value: str) -> list[str]:
    return _TOKEN_PATTERN.findall(normalize_for_matching(value))


def _term_match_positions(
    tokens: list[str],
    term: str,
) -> list[int]:
    """
    Every starting token index where `term` occurs as a contiguous
    run of whole tokens inside `tokens`.

    A narrowly-qualified legal synonym may additionally match its
    unqualified multi-word form. For example, "statutory severance
    pay" may match "severance pay". The fallback is deliberately
    disabled when fewer than two tokens would remain.
    """

    term_tokens = _tokenize(term)

    if not term_tokens:
        return []

    variants: list[list[str]] = [term_tokens]

    if (
        len(term_tokens) >= 3
        and term_tokens[0] in _OPTIONAL_LEADING_LEGAL_QUALIFIERS
    ):
        unqualified_tokens = term_tokens[1:]

        if len(unqualified_tokens) >= 2:
            variants.append(unqualified_tokens)

    for expansion in _QUALIFIED_LEGAL_TERM_EXPANSIONS.get(
        tuple(term_tokens),
        (),
    ):
        expanded_tokens = list(expansion)

        if expanded_tokens not in variants:
            variants.append(expanded_tokens)

    positions: set[int] = set()

    for variant_tokens in variants:
        term_length = len(variant_tokens)

        for start in range(
            len(tokens) - term_length + 1
        ):
            if (
                tokens[start:start + term_length]
                == variant_tokens
            ):
                positions.add(start)

    return sorted(positions)


def _hit_haystack(hit: LegalSearchHit) -> str:
    """
    Fields a concept may legitimately be found in for *coverage*
    purposes - deliberately excludes `section`, which names only the
    broad legal category (e.g. "Termination") every hit in that
    category shares: matching a concept against it would let any hit
    "cover" a concept merely by belonging to the right section, the
    exact failure this module exists to prevent. `subsection` is
    specific enough to count (e.g. "Remote Work", "Overtime"); Contact
    chunks are excluded upstream before this is ever called.
    """

    return " ".join(
        part
        for part in (hit.subsection, hit.content)
        if part
    )


def _concept_group_positions(
    tokens: list[str],
    concept: SearchConceptLike,
) -> list[int]:
    positions: list[int] = []

    for term in concept.terms:
        positions.extend(_term_match_positions(tokens, term))

    return positions


def _hit_covers_concept(
    hit: LegalSearchHit,
    concept: SearchConceptLike,
) -> bool:
    tokens = _tokenize(_hit_haystack(hit))

    return bool(_concept_group_positions(tokens, concept))


def _hit_covers_relation(
    hit: LegalSearchHit,
    search_concepts: list[SearchConceptLike],
) -> bool:
    """
    True only when every concept group has a match inside this one
    hit, with at least one pair of matches (one per group) within
    RELATION_PROXIMITY_MAX_TOKENS tokens of each other - never merely
    each group appearing anywhere, unrelated, in a long chunk.
    """

    tokens = _tokenize(_hit_haystack(hit))

    positions_by_group = [
        _concept_group_positions(tokens, concept)
        for concept in search_concepts
    ]

    if any(not positions for positions in positions_by_group):
        return False

    if len(positions_by_group) < 2:
        return bool(positions_by_group)

    first_group_positions = positions_by_group[0]

    for other_positions in positions_by_group[1:]:
        if not any(
            abs(a - b) <= RELATION_PROXIMITY_MAX_TOKENS
            for a in first_group_positions
            for b in other_positions
        ):
            return False

    return True


# Pure question-framing words - never a specific legal word, so
# removing them before a subject_text overlap check (see
# _hit_has_substantial_subject_overlap) cannot itself manufacture a
# false match; it only stops framing words from either diluting a
# real overlap count or, on their own, ever being mistaken for one.
_SUBJECT_FRAMING_WORDS: Final[frozenset[str]] = frozenset(
    {
        "the", "a", "an", "in", "on", "at", "for", "to", "of", "and",
        "or", "is", "are", "be", "been", "being", "do", "does", "did",
        "can", "could", "should", "would", "will", "shall", "must",
        "may", "might", "please", "tell", "me", "about", "you",
        "what", "which", "how", "when", "where", "who", "whom", "why",
        "summarise", "summarize", "summary", "explain", "describe",
        # Generic legal-question framing words - describe the *shape*
        # of the answer requested ("what rules apply", "what
        # conditions must be satisfied", "what information do you
        # have") rather than naming any specific legal subject. A
        # subject_text built only from these (e.g. a resolved subject
        # of literally "rules and conditions") must never, on its own,
        # be treated as distinctive enough to trust a lexical overlap
        # against - the same reasoning already applied above to
        # "summarise"/"explain"/"describe".
        "rules", "rule", "conditions", "condition", "information",
        "requirements", "requirement",
    }
)

def _significant_subject_tokens(subject_text: str) -> list[str]:
    """
    subject_text's own tokens with pure question-framing words
    removed - the residue is whatever the question is actually
    *about*, independent of how it happened to be phrased.
    """

    return [
        token
        for token in _tokenize(subject_text)
        if token not in _SUBJECT_FRAMING_WORDS and len(token) > 2
    ]


def _hit_has_substantial_subject_overlap(
    hit: LegalSearchHit,
    subject_text: str,
    *,
    expected_country_codes: frozenset[str] = frozenset(),
    expected_legal_topics: frozenset[str] = frozenset(),
) -> bool:
    """
    A weaker, last-resort signal than an exact concept-phrase match:
    true only when a genuine majority of subject_text's own
    significant words appear anywhere in the hit (a single-word
    subject, e.g. "notice", must match that one word - the same
    resolved subject already trusted elsewhere by _SubjectTextConcept
    for exact-phrase matching; this is the same trust, just applied
    word-by-word instead of as one contiguous phrase).

    This exists because a search_concepts group generated for one
    phrasing of a question ("what conditions must a non-compete
    clause satisfy") can fail to literally appear in a hit that a
    differently-phrased but equivalent question ("is a non-compete
    clause enforceable") would have matched, even though both target
    the exact same retrieved content - a false negative in the exact-
    phrase check, never a real absence of evidence (mission "HOTFIX
    0.4.4 - chat capabilities and evidence stability"). Matching is by
    exact token only, never a stem or substring (e.g. "work" does not
    match "working"), which is what keeps this from ever re-admitting
    the adjacent-topic false positives DirectTopicEvidenceTests/
    RelationRequiredEvidenceTests already guard against.
    """

    normalized_hit_country = hit.country_code.strip().upper()

    if (
        expected_country_codes
        and normalized_hit_country not in expected_country_codes
    ):
        return False

    normalized_hit_topic = (
        hit.legal_topic.strip()
        if hit.legal_topic is not None
        else ""
    )

    if (
        expected_legal_topics
        and normalized_hit_topic not in expected_legal_topics
    ):
        return False

    subject_tokens = _significant_subject_tokens(subject_text)

    if not subject_tokens:
        # Nothing distinctive left to judge by once framing words are
        # removed - falls through to the existing, stricter checks.
        return False

    hit_tokens = set(_tokenize(_hit_haystack(hit)))

    overlap_count = sum(
        1 for token in subject_tokens if token in hit_tokens
    )

    required_overlap = max(1, (len(subject_tokens) + 1) // 2)

    return overlap_count >= required_overlap


class _SubjectTextConcept:
    """A single-term SearchConceptLike built from an action's own
    canonical subject_text - the fallback direct concept used below
    when no search_concepts were supplied, never an invented
    synonym."""

    __slots__ = ("terms",)

    def __init__(self, subject_text: str) -> None:
        self.terms = [subject_text]


def evaluate_evidence_status(
    hits: list[LegalSearchHit],
    search_concepts: list[SearchConceptLike],
    evidence_mode: str,
    *,
    subject_text: str | None = None,
    reranked_direct_chunk_ids: frozenset[str] = frozenset(),
    expected_country_codes: frozenset[str] = frozenset(),
    expected_legal_topics: frozenset[str] = frozenset(),
) -> EvidenceStatus:
    """
    Evaluate one country's retrieved hits against one action's
    subject.

    `subject_text`, when given, is the fallback direct concept used
    whenever search_concepts is empty - "no concepts were supplied"
    must never be treated as automatic proof for direct_topic/
    relation_required (a general chunk on working hours or health and
    safety must not count as direct evidence for a remote-work
    question just because no search_concepts happened to be carried
    on the action); every hit must still actually contain the
    subject's own words to count as direct. broad_topic's own
    semantics (any hit in the section counts, by design) are
    untouched either way.

    `reranked_direct_chunk_ids` lets an already-active LLM reranker
    (see rag_answer.py's existing _rerank_hits, only ever run when
    RERANK_ENABLED) confirm a hit as a genuine direct/full-relation
    answer without this function needing a second, dedicated OpenAI
    call of its own - never required, since reranking is disabled by
    default in production and this local check must stand on its own.
    """

    if not hits:
        return "insufficient"

    if evidence_mode == "broad_topic":
        return "direct"

    effective_concepts: list[SearchConceptLike] = (
        search_concepts
        if search_concepts
        else (
            [_SubjectTextConcept(subject_text)] if subject_text else []
        )
    )

    if not effective_concepts:
        return "insufficient"

    if evidence_mode == "relation_required":
        for hit in hits:
            if hit.chunk_id in reranked_direct_chunk_ids:
                return "direct"

            if _hit_covers_relation(hit, effective_concepts):
                return "direct"

        # No single hit establishes the full relation - but if every
        # concept group is at least covered somewhere across the
        # candidate set, that is a genuine (if imperfect) partial
        # answer, never silently promoted to direct.
        any_group_covered = any(
            any(_hit_covers_concept(hit, concept) for hit in hits)
            for concept in effective_concepts
        )

        if any_group_covered:
            return "partial"

        if subject_text and any(
            _hit_has_substantial_subject_overlap(
                hit,
                subject_text,
                expected_country_codes=expected_country_codes,
                expected_legal_topics=expected_legal_topics,
            )
            for hit in hits
        ):
            return "partial"

        return "insufficient"

    # direct_topic: one precise concept, one group is enough.
    for hit in hits:
        if hit.chunk_id in reranked_direct_chunk_ids:
            return "direct"

        if any(
            _hit_covers_concept(hit, concept)
            for concept in effective_concepts
        ):
            return "direct"

    # The exact search_concepts phrasing never matched - a real risk
    # whenever a question is reworded ("what conditions must X
    # satisfy" vs "is X enforceable") in a way the model's own
    # synonym generation for *that* phrasing did not anticipate, even
    # though retrieval already returned hits for the right country
    # and canonical legal_topic. A genuine majority overlap with the
    # resolved subject_text itself is enough to let those hits reach
    # generation as partial evidence - never silently promoted to
    # direct, and never granted from a single incidental shared word.
    if subject_text and any(
        _hit_has_substantial_subject_overlap(
                hit,
                subject_text,
                expected_country_codes=expected_country_codes,
                expected_legal_topics=expected_legal_topics,
            )
        for hit in hits
    ):
        return "partial"

    return "insufficient"


def answer_mentions_concepts(
    answer_text: str,
    search_concepts: list[SearchConceptLike],
    evidence_mode: str,
) -> bool:
    """
    Whether a generated answer's own text actually engages the
    subject's essential concepts - used only to detect subject_drift
    (see rag_answer.py), never to gate retrieval.
    """

    if evidence_mode == "broad_topic" or not search_concepts:
        return True

    tokens = _tokenize(answer_text)

    if evidence_mode == "relation_required":
        return all(
            _concept_group_positions(tokens, concept)
            for concept in search_concepts
        )

    return any(
        _concept_group_positions(tokens, concept)
        for concept in search_concepts
    )
