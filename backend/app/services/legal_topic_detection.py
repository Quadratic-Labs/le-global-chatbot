"""Detect legal topics mentioned in employment law questions."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
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
            "probationary period",
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
)


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


def prepare_legal_chat_topics(
    request: LegalChatRequest,
) -> LegalChatRequest:
    """
    Add automatically detected topics when no topic filter exists.

    Explicit filters supplied by the API consumer always remain
    authoritative.
    """

    explicit_topics = _normalize_topics(
        request.legal_topics
    )

    if explicit_topics:
        legal_topics = explicit_topics

    else:
        legal_topics = detect_legal_topics(
            request.question
        )

    if legal_topics == request.legal_topics:
        return request

    return LegalChatRequest(
        question=request.question,
        country_codes=list(
            request.country_codes
        ),
        legal_topics=legal_topics,
        subsections=list(
            request.subsections
        ),
        language=request.language,
        reference_year=request.reference_year,
        max_sources=request.max_sources,
    )