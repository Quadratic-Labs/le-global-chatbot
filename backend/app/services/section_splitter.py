"""Deterministic splitting of oversized parsed legal sections."""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Final

from app.services.docx_parser import ParsedSection


DEFAULT_MAX_CHARS: Final[int] = 6000

_PARAGRAPH_BREAK_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\n\s*\n+"
)

_SENTENCE_BREAK_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?<=[.!?])\s+"
    r"(?=[A-Z0-9À-ÖØ-Þ\"'“‘(])"
)


class SectionSplitterError(ValueError):
    """Raised when a parsed section cannot be safely split."""


def _validate_max_chars(
    max_chars: int,
) -> None:
    """Validate the configured hard character limit."""

    if max_chars < 100:
        raise SectionSplitterError(
            "max_chars must be at least 100."
        )


def _split_by_words(
    text: str,
    max_chars: int,
) -> list[str]:
    """
    Split text at whitespace boundaries.

    A single token longer than the hard limit, such as an abnormally
    long URL, is sliced as a final fallback.
    """

    words = text.split()

    if not words:
        return []

    chunks: list[str] = []
    current_words: list[str] = []

    def flush_current() -> None:
        if not current_words:
            return

        chunks.append(
            " ".join(current_words)
        )

        current_words.clear()

    for word in words:
        if len(word) > max_chars:
            flush_current()

            for start_index in range(
                0,
                len(word),
                max_chars,
            ):
                chunks.append(
                    word[
                        start_index:
                        start_index + max_chars
                    ]
                )

            continue

        candidate = (
            " ".join(
                (
                    *current_words,
                    word,
                )
            )
        )

        if (
            current_words
            and len(candidate) > max_chars
        ):
            flush_current()
            current_words.append(
                word
            )
        else:
            current_words.append(
                word
            )

    flush_current()

    return chunks


def _pack_units(
    units: Sequence[str],
    separator: str,
    max_chars: int,
) -> list[str]:
    """Pack already bounded units without exceeding the hard limit."""

    chunks: list[str] = []
    current_units: list[str] = []

    def flush_current() -> None:
        if not current_units:
            return

        chunks.append(
            separator.join(
                current_units
            )
        )

        current_units.clear()

    for raw_unit in units:
        unit = raw_unit.strip()

        if not unit:
            continue

        if len(unit) > max_chars:
            raise SectionSplitterError(
                "Internal splitter error: "
                "an oversized unit reached the packing stage."
            )

        candidate = separator.join(
            (
                *current_units,
                unit,
            )
        )

        if (
            current_units
            and len(candidate) > max_chars
        ):
            flush_current()
            current_units.append(
                unit
            )
        else:
            current_units.append(
                unit
            )

    flush_current()

    return chunks


def _split_oversized_paragraph(
    paragraph: str,
    max_chars: int,
) -> list[str]:
    """
    Split one oversized paragraph.

    Sentence boundaries are preferred. Whitespace splitting is used
    when a sentence itself exceeds the hard limit.
    """

    sentences = [
        sentence.strip()
        for sentence in _SENTENCE_BREAK_PATTERN.split(
            paragraph
        )
        if sentence.strip()
    ]

    if len(sentences) <= 1:
        return _split_by_words(
            text=paragraph,
            max_chars=max_chars,
        )

    bounded_sentences: list[str] = []

    for sentence in sentences:
        if len(sentence) <= max_chars:
            bounded_sentences.append(
                sentence
            )
        else:
            bounded_sentences.extend(
                _split_by_words(
                    text=sentence,
                    max_chars=max_chars,
                )
            )

    return _pack_units(
        units=bounded_sentences,
        separator=" ",
        max_chars=max_chars,
    )


def split_text(
    text: str,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> list[str]:
    """
    Split text deterministically while preserving its logical order.

    Priority:

    1. paragraph boundaries;
    2. sentence boundaries;
    3. whitespace boundaries;
    4. hard slicing for exceptionally long individual tokens.
    """

    _validate_max_chars(
        max_chars
    )

    normalized_text = text.strip()

    if not normalized_text:
        return []

    if len(normalized_text) <= max_chars:
        return [
            normalized_text
        ]

    paragraphs = [
        paragraph.strip()
        for paragraph in _PARAGRAPH_BREAK_PATTERN.split(
            normalized_text
        )
        if paragraph.strip()
    ]

    chunks: list[str] = []
    current_paragraphs: list[str] = []

    def flush_current_paragraphs() -> None:
        if not current_paragraphs:
            return

        chunks.append(
            "\n\n".join(
                current_paragraphs
            )
        )

        current_paragraphs.clear()

    for paragraph in paragraphs:
        if len(paragraph) > max_chars:
            flush_current_paragraphs()

            chunks.extend(
                _split_oversized_paragraph(
                    paragraph=paragraph,
                    max_chars=max_chars,
                )
            )

            continue

        candidate = "\n\n".join(
            (
                *current_paragraphs,
                paragraph,
            )
        )

        if (
            current_paragraphs
            and len(candidate) > max_chars
        ):
            flush_current_paragraphs()
            current_paragraphs.append(
                paragraph
            )
        else:
            current_paragraphs.append(
                paragraph
            )

    flush_current_paragraphs()

    if any(
        len(chunk) > max_chars
        for chunk in chunks
    ):
        raise SectionSplitterError(
            "The splitter produced a chunk above "
            f"the configured limit of {max_chars} characters."
        )

    return chunks


def split_parsed_sections(
    parsed_sections: Sequence[ParsedSection],
    max_chars: int = DEFAULT_MAX_CHARS,
) -> list[ParsedSection]:
    """
    Split only oversized ParsedSection objects.

    The legal section and subsection labels remain unchanged. Multiple
    pieces with the same structural path are later assigned distinct,
    deterministic occurrences by document_chunk_builder.py.
    """

    _validate_max_chars(
        max_chars
    )

    split_sections: list[ParsedSection] = []

    for parsed_section in parsed_sections:
        content_parts = split_text(
            text=parsed_section.content,
            max_chars=max_chars,
        )

        for content_part in content_parts:
            split_sections.append(
                ParsedSection(
                    section=parsed_section.section,
                    subsection=parsed_section.subsection,
                    content=content_part,
                    is_custom_legal_topic=parsed_section.is_custom_legal_topic,
                )
            )

    return split_sections