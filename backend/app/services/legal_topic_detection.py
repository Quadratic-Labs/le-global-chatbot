"""Detect legal topics mentioned in employment law questions."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from app.models.chat import LegalChatRequest


TOPIC_RULES: Final[
    tuple[
        tuple[tuple[str, ...], tuple[str, ...]],
        ...,
    ]
] = (
    (
        (
            "hiring practices",
            "recruitment",
            "background check",
            "interview questions",
            "work permit",
            "employment visa",
            "pre-employment screening",
            "local entity",
        ),
        (
            "Hiring Practices",
        ),
    ),
    (
        (
            "notice period",
            "notice periods",
            "statutory notice",
            "termination notice",
            "notice of termination",
        ),
        (
            "Employment Contracts",
            "Termination of Employment Contracts",
        ),
    ),
    (
        (
            "termination",
            "dismissal",
            "dismissed",
            "redundancy",
            "severance",
            "wrongful termination",
            "unfair dismissal",
        ),
        (
            "Termination of Employment Contracts",
        ),
    ),
    (
        (
            "employment contract",
            "employment contracts",
            "fixed term contract",
            "fixed-term contract",
            "probation",
            "probation period",
            "probation periods",
            "probationary period",
            "probationary periods",
            "trial period",
        ),
        (
            "Employment Contracts",
        ),
    ),
    (
        (
            "working time",
            "working hours",
            "working week",
            "overtime",
            "rest period",
            "night work",
        ),
        (
            "Working Conditions",
        ),
    ),
    (
        (
            "discrimination",
            "harassment",
            "reasonable accommodation",
            "protected characteristic",
        ),
        (
            "Anti-Discrimination Laws",
        ),
    ),
    (
        (
            "equal pay",
            "pay equity",
            "gender pay gap",
        ),
        (
            "Pay Equity Laws",
        ),
    ),
    (
        (
            "employee monitoring",
            "monitoring employees",
            "monitor employee",
            "monitor employees",
            "electronic communications",
            "data privacy",
            "personal data",
            "social media",
        ),
        (
            "Social Media and Data Privacy",
        ),
    ),
    (
        (
            "non compete",
            "non-compete",
            "restrictive covenant",
            "restrictive covenants",
            "non solicitation",
            "non-solicitation",
        ),
        (
            "Restrictive Covenants",
        ),
    ),
    (
        (
            "transfer of undertaking",
            "transfer of undertakings",
            "business transfer",
            "tupe",
        ),
        (
            "Transfer of Undertakings",
        ),
    ),
    (
        (
            "trade union",
            "trade unions",
            "works council",
            "collective bargaining",
            "employee representative",
        ),
        (
            "Trade Unions and Employers Associations",
        ),
    ),
    (
        (
            "annual leave",
            "paid leave",
            "sick leave",
            "maternity leave",
            "paternity leave",
            "parental leave",
            "employee benefits",
            "social security",
        ),
        (
            "Employee Benefits",
        ),
    ),
)


CANONICAL_LEGAL_TOPICS: Final[tuple[str, ...]] = tuple(
    sorted(
        {
            legal_topic
            for _, legal_topics in TOPIC_RULES
            for legal_topic in legal_topics
        }
    )
)


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
