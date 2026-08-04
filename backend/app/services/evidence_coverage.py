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
    """Every starting token index where `term` occurs as a contiguous
    run of whole tokens inside `tokens`."""

    term_tokens = _tokenize(term)

    if not term_tokens:
        return []

    positions: list[int] = []
    term_length = len(term_tokens)

    for start in range(len(tokens) - term_length + 1):
        if tokens[start:start + term_length] == term_tokens:
            positions.append(start)

    return positions


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

        return "partial" if any_group_covered else "insufficient"

    # direct_topic: one precise concept, one group is enough.
    for hit in hits:
        if hit.chunk_id in reranked_direct_chunk_ids:
            return "direct"

        if any(
            _hit_covers_concept(hit, concept)
            for concept in effective_concepts
        ):
            return "direct"

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
