"""
Topic-coverage warnings for admin document uploads.

Mission "ORDER 3", sections 10/11: a document that fails to parse at
all is still a hard error (InvalidDocxFormatError,
UndeterminableDocumentCountryError, UnknownLegalTopicError, ... -
unchanged, see document_chunk_builder.py). This module only classifies
documents that DID parse successfully but cover fewer than the
majority of the product's 11 supported legal topics - informational,
never blocking on its own; the admin decides via confirm_warnings
(see AdminDocumentWarningConfirmationRequiredError in
admin_document_replacement.py).

Deliberately deterministic and local: no LLM, no network call, reuses
app.core.legal_taxonomy's own LEGAL_TOPICS list rather than a second,
independent one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from app.core.legal_taxonomy import LEGAL_TOPICS
from app.models.document import DocumentChunk


EXPECTED_TOPICS_COUNT: Final[int] = len(LEGAL_TOPICS)

# A strict majority of 11 - mission wording verbatim: "6 correspond à
# la majorité stricte de 11."
_STRUCTURE_WARNING_MAX_RECOGNIZED: Final[int] = 5


STRUCTURE_WARNING_CODE: Final[str] = "structure_warning"
CONTEXT_WARNING_CODE: Final[str] = "context_warning"


@dataclass(frozen=True, slots=True)
class TopicCoverageWarning:
    """One non-blocking warning about a document's topic coverage."""

    code: str
    message: str
    recognized_topics_count: int
    expected_topics_count: int
    recognized_topics: tuple[str, ...]
    missing_topics: tuple[str, ...]


def recognized_topics_for(
    chunks: list[DocumentChunk],
) -> tuple[str, ...]:
    """
    Every distinct canonical legal topic actually present among a
    document's comparator chunks - never the overview chunks, which
    carry legal_topic=None by design.
    """

    return tuple(
        topic
        for topic in LEGAL_TOPICS
        if any(
            chunk.document_type == "comparator"
            and chunk.legal_topic == topic
            for chunk in chunks
        )
    )


def evaluate_topic_coverage(
    chunks: list[DocumentChunk],
) -> TopicCoverageWarning | None:
    """
    Classify one successfully-parsed document's topic coverage.

    Returns None when recognized_topics_count >= 6 (a strict majority
    of the 11 supported topics) - no warning at all. Otherwise returns
    exactly one warning: CONTEXT_WARNING when zero topics were
    recognized (the document is readable and its country is known, but
    nothing in it matches the product's Employment Law taxonomy at
    all - its relevance is in doubt), STRUCTURE_WARNING when 1-5 were
    recognized (readable, relevant, but atypically thin coverage).
    """

    recognized = recognized_topics_for(chunks)
    recognized_count = len(recognized)

    missing = tuple(
        topic
        for topic in LEGAL_TOPICS
        if topic not in recognized
    )

    if recognized_count > _STRUCTURE_WARNING_MAX_RECOGNIZED:
        return None

    if recognized_count == 0:
        return TopicCoverageWarning(
            code=CONTEXT_WARNING_CODE,
            message=(
                "The document's country was detected, but none of "
                "the document's content matched any of the "
                f"{EXPECTED_TOPICS_COUNT} supported Employment Law "
                "topics. The document may be outside the expected "
                "Labour and Employment Law context - review before "
                "confirming."
            ),
            recognized_topics_count=recognized_count,
            expected_topics_count=EXPECTED_TOPICS_COUNT,
            recognized_topics=recognized,
            missing_topics=missing,
        )

    return TopicCoverageWarning(
        code=STRUCTURE_WARNING_CODE,
        message=(
            f"The document only covers {recognized_count} of the "
            f"{EXPECTED_TOPICS_COUNT} supported Employment Law "
            "topics, fewer than the usual majority. This may be an "
            "atypical or incomplete document - review before "
            "confirming."
        ),
        recognized_topics_count=recognized_count,
        expected_topics_count=EXPECTED_TOPICS_COUNT,
        recognized_topics=recognized,
        missing_topics=missing,
    )
