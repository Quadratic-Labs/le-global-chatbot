"""Detect legal topics mentioned in employment law questions."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from app.models.chat import LegalChatRequest
from app.services.legal_topic_taxonomy import (
    CANONICAL_LEGAL_TOPICS,
    TOPIC_RULES,
)

__all__ = [
    "CANONICAL_LEGAL_TOPICS",
    "TOPIC_RULES",
    "detect_document_legal_topics",
]


OVERVIEW_PHRASES: Final[tuple[str, ...]] = (
    "employment law overview",
    "labour law overview",
    "labor law overview",
    "main employment law rules",
    "key employment law issues",
    "general employment law",
    "employment law in general",
)


@dataclass(frozen=True, slots=True)
class LegalScope:
    """Legal-topic scope resolved for one question."""

    legal_topics: list[str]
    is_overview_question: bool
    is_supported: bool


def _normalize_text(
    value: str,
) -> str:
    """Normalize text for deterministic phrase matching."""

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


def _normalize_topics(
    values: Sequence[str],
) -> list[str]:
    """Normalize and deduplicate explicit legal topics."""

    normalized_topics: list[str] = []
    seen_topics: set[str] = set()

    for value in values:
        normalized_value = " ".join(
            value.split()
        )

        if not normalized_value:
            continue

        if normalized_value in seen_topics:
            continue

        seen_topics.add(
            normalized_value
        )

        normalized_topics.append(
            normalized_value
        )

    return normalized_topics


MIN_DOCUMENT_TOPIC_WORDS: Final[int] = 2


def detect_document_legal_topics(
    question: str,
    document_topics: Sequence[str],
) -> list[str]:
    """
    Deterministically detect LIVE document legal_topic titles (mission
    "ORDER 8F-A") explicitly named in a question.

    Distinct from detect_legal_topics: that function matches a fixed
    set of canonical CONCEPT phrases against CANONICAL_LEGAL_TOPICS
    (legal_topic_taxonomy.py) and never changes with the indexed
    corpus. This one matches the actual, currently-indexed section
    title text itself - including any Admin-created custom section -
    supplied by the caller (see legal_catalog.get_document_legal_topics_
    by_country), never invented or guessed here.

    Deliberately conservative and exact: a document topic is detected
    only when its full normalized title appears verbatim as a
    substring of the normalized question - never a fuzzy/partial/
    single-word match. This must survive being the ONLY signal
    available when RequestUnderstanding's own LLM call fails entirely
    (see routers/chat.py's _resolve_conservative_fallback), so it never
    depends on the model succeeding.
    """

    normalized_question = _normalize_text(
        question
    )

    if not normalized_question:
        return []

    detected_topics: list[str] = []
    seen_topics: set[str] = set()

    for topic in document_topics:
        if topic in seen_topics:
            continue

        normalized_topic = _normalize_text(
            topic
        )

        if not normalized_topic:
            continue

        # A single common word (e.g. a one-word canonical topic that
        # also happens to be indexed as its own legal_topic) is never
        # enough to deterministically claim an explicit title match -
        # only a genuinely multi-word, specific section title is.
        if len(normalized_topic.split()) < MIN_DOCUMENT_TOPIC_WORDS:
            continue

        if normalized_topic in normalized_question:
            seen_topics.add(topic)
            detected_topics.append(topic)

    return detected_topics


def detect_legal_topics(
    question: str,
) -> list[str]:
    """
    Detect canonical legal topics from a question.

    Several topics may be returned because some concepts,
    such as notice periods, may appear in more than one
    canonical section across country documents.
    """

    normalized_question = _normalize_text(
        question
    )

    if not normalized_question:
        return []

    detected_topics: list[str] = []
    seen_topics: set[str] = set()

    for phrases, legal_topics in TOPIC_RULES:
        phrase_detected = any(
            _normalize_text(
                phrase
            ) in normalized_question
            for phrase in phrases
        )

        if not phrase_detected:
            continue

        for legal_topic in legal_topics:
            if legal_topic in seen_topics:
                continue

            seen_topics.add(
                legal_topic
            )

            detected_topics.append(
                legal_topic
            )

    return detected_topics


def is_overview_question(
    question: str,
) -> bool:
    """Return whether a question asks for a general country overview."""

    normalized_question = _normalize_text(
        question
    )

    if not normalized_question:
        return False

    return any(
        _normalize_text(
            phrase
        ) in normalized_question
        for phrase in OVERVIEW_PHRASES
    )


def resolve_legal_scope(
    request: LegalChatRequest,
) -> LegalScope:
    """
    Resolve the legal-topic scope of one question.

    A question is considered supported when it carries an explicit or
    detected legal topic, an explicit subsection, or asks for a general
    overview. Anything else (unrelated legal areas such as tax, VAT, or
    patents) is reported as unsupported so the caller can skip
    retrieval entirely instead of searching without a topic filter.
    """

    explicit_topics = _normalize_topics(
        request.legal_topics
    )

    explicit_subsections = _normalize_topics(
        request.subsections
    )

    if explicit_topics:
        return LegalScope(
            legal_topics=explicit_topics,
            is_overview_question=False,
            is_supported=True,
        )

    detected_topics = detect_legal_topics(
        request.question
    )

    overview_question = is_overview_question(
        request.question
    )

    is_supported = bool(
        detected_topics
        or explicit_subsections
        or overview_question
    )

    return LegalScope(
        legal_topics=detected_topics,
        is_overview_question=overview_question,
        is_supported=is_supported,
    )
