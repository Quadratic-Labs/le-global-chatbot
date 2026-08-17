from __future__ import annotations

import re
import zipfile
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from html import unescape
from pathlib import Path
from typing import IO, Final, TypeAlias

from docx import Document
from docx.document import Document as DocxDocument
from docx.opc.exceptions import PackageNotFoundError
from docx.oxml.ns import qn
from docx.oxml.table import CT_Tbl
from docx.oxml.text.paragraph import CT_P
from docx.table import Table
from docx.text.paragraph import Paragraph

from app.core.legal_taxonomy import (
    get_canonical_legal_topic,
    is_overview_section,
)
from app.core.subsection_taxonomy import (
    get_canonical_subsection,
    get_subsection_topic_override,
)


_HEADING_STYLE_PATTERN = re.compile(
    r"^(?:heading|titre)\s*([1-9])$",
    re.IGNORECASE,
)

_TOPIC_NUMBER_PREFIX_PATTERN = re.compile(
    r"^\s*"
    r"(?:[|¦=]+\s*)?"
    r"(?:\d{1,2}|[IVX]{1,6})\s*[.)]\s*",
)

_LEADING_DECORATION_PATTERN = re.compile(
    r"^\s*[|¦=]+\s*"
)

_TRAILING_DECORATION_PATTERN = re.compile(
    r"\s*[|¦=]+\s*$"
)

_IGNORED_PARAGRAPH_TEXTS = frozenset(
    {
        "|",
        "¦",
        "=",
    }
)

_FALSE_XML_VALUES = frozenset(
    {
        "0",
        "false",
        "off",
    }
)

_MAX_CUSTOM_TOPIC_HEADING_LENGTH: Final[int] = 100

_SENTENCE_TERMINATOR_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"[.;,:]$"
)

# ORDER 8A-C: a dedicated Word paragraph style is the deterministic,
# DOCX-native marker for an ADMIN-added top-level legal topic - see
# document_mutation.py, which creates/reuses this exact style name
# whenever it inserts a new section. Recognizing it never depends on
# the document's own native heading convention (Heading 1, bold-only,
# numbered, or anything else), on the country, on document position,
# or on the visible title text - only on this one style being applied.
ADMIN_SECTION_STYLE_NAME: Final[str] = "LE Global Admin Section Heading"

BlockItem: TypeAlias = Paragraph | Table


@dataclass(frozen=True, slots=True)
class ParsedSection:
    """A structured section extracted from a DOCX document."""

    section: str
    subsection: str | None
    content: str

    # True only for a top-level legal topic recognized outside the
    # fixed LEGAL_TOPICS taxonomy (an admin-added custom section). Lets
    # the chunk builder accept it as its own legal_topic instead of
    # raising UnknownLegalTopicError, without weakening that check for
    # any other, genuinely-unrecognized heading.
    is_custom_legal_topic: bool = False


def _normalize_text(
    value: str,
) -> str:
    """Normalize whitespace while keeping readable text."""

    return " ".join(
        value
        .replace("\xa0", " ")
        .split()
    )


def _clean_structural_label(
    value: str,
) -> str:
    """Remove decorative separators surrounding a title."""

    normalized = _normalize_text(
        value
    )

    normalized = _LEADING_DECORATION_PATTERN.sub(
        "",
        normalized,
    )

    normalized = _TRAILING_DECORATION_PATTERN.sub(
        "",
        normalized,
    )

    return normalized.strip()


def _has_alnum(
    text: str,
) -> bool:
    """Return whether text contains useful alphanumeric content."""

    return any(
        character.isalnum()
        for character in text
    )


def _get_heading_level(
    paragraph: Paragraph,
) -> int | None:
    """Return the effective Word heading level."""

    style_name = (
        paragraph.style.name
        if paragraph.style is not None
        else ""
    )

    style_match = _HEADING_STYLE_PATTERN.match(
        style_name.strip()
    )

    if style_match:
        return int(
            style_match.group(1)
        )

    paragraph_properties = paragraph._p.pPr

    if (
        paragraph_properties is not None
        and paragraph_properties.outlineLvl is not None
    ):
        return (
            int(
                paragraph_properties.outlineLvl.val
            )
            + 1
        )

    return None


def _has_numbering(
    paragraph: Paragraph,
) -> bool:
    """Return whether the paragraph belongs to a Word list."""

    paragraph_properties = paragraph._p.pPr

    return (
        paragraph_properties is not None
        and paragraph_properties.numPr is not None
    )


def _has_topic_number_prefix(
    text: str,
) -> bool:
    """Return whether text begins with a legal-topic number."""

    return (
        _TOPIC_NUMBER_PREFIX_PATTERN.match(
            text
        )
        is not None
    )


def _has_explicit_bold_text(
    paragraph: Paragraph,
) -> bool:
    """Return whether visible text is explicitly formatted in bold."""

    return any(
        run.bold is True
        for run in paragraph.runs
        if run.text.strip()
    )


def _is_false_xml_boolean(
    value: str | None,
) -> bool:
    """Interpret an explicit false Word XML boolean value."""

    return (
        value is not None
        and value.lower() in _FALSE_XML_VALUES
    )


def _is_explicitly_unbolded(
    paragraph: Paragraph,
) -> bool:
    """
    Detect a paragraph whose bold formatting was explicitly disabled.

    Some source documents use heading styles for ordinary body text
    and override their inherited bold formatting.
    """

    paragraph_properties = paragraph._p.pPr

    if paragraph_properties is not None:
        run_properties = paragraph_properties.find(
            qn("w:rPr")
        )

        if run_properties is not None:
            bold_property = run_properties.find(
                qn("w:b")
            )

            if (
                bold_property is not None
                and _is_false_xml_boolean(
                    bold_property.get(
                        qn("w:val")
                    )
                )
            ):
                return True

    visible_runs = [
        run
        for run in paragraph.runs
        if run.text.strip()
    ]

    return (
        bool(visible_runs)
        and all(
            run.bold is False
            for run in visible_runs
        )
    )


def _has_main_section_signal(
    paragraph: Paragraph,
    text: str,
    heading_level: int | None,
) -> bool:
    """Return whether a legal-topic paragraph is structurally a title."""

    return any(
        (
            heading_level == 1,
            _has_numbering(
                paragraph
            ),
            _has_topic_number_prefix(
                text
            ),
            _has_explicit_bold_text(
                paragraph
            ),
        )
    )


def _get_main_legal_topic(
    paragraph: Paragraph,
    text: str,
    heading_level: int | None,
    country: str | None,
) -> str | None:
    """Return the canonical topic when a main section is detected."""

    legal_topic = get_canonical_legal_topic(
        section=text,
        country=country,
    )

    if legal_topic is None:
        return None

    if not _has_main_section_signal(
        paragraph=paragraph,
        text=text,
        heading_level=heading_level,
    ):
        return None

    return legal_topic


def is_admin_section_heading(
    paragraph: Paragraph,
) -> bool:
    """
    Return whether a paragraph carries the dedicated ADMIN-added
    top-level-section marker style (ORDER 8A-C, section 2).

    This is the sole, deterministic identity check for an admin-
    created section: it never depends on the document's own native
    heading convention, on country, on document position, or on the
    visible title text - only on this exact style name. Nothing in
    any real historical L&E document uses this project-specific style
    name, so it can never collide with genuine native content.
    """

    style = paragraph.style

    return (
        style is not None
        and style.name == ADMIN_SECTION_STYLE_NAME
    )


_TOPIC_SIGNAL_HEADING_LEVEL_1: Final[str] = "heading_level_1"
_TOPIC_SIGNAL_NUMBERING: Final[str] = "numbering"
_TOPIC_SIGNAL_PREFIX: Final[str] = "topic_number_prefix"
_TOPIC_SIGNAL_BOLD: Final[str] = "explicit_bold"


def _main_section_signals(
    paragraph: Paragraph,
    text: str,
    heading_level: int | None,
) -> frozenset[str]:
    """Return which individual main-section structural signals hold."""

    signals: set[str] = set()

    if heading_level == 1:
        signals.add(
            _TOPIC_SIGNAL_HEADING_LEVEL_1
        )

    if _has_numbering(
        paragraph
    ):
        signals.add(
            _TOPIC_SIGNAL_NUMBERING
        )

    if _has_topic_number_prefix(
        text
    ):
        signals.add(
            _TOPIC_SIGNAL_PREFIX
        )

    if _has_explicit_bold_text(
        paragraph
    ):
        signals.add(
            _TOPIC_SIGNAL_BOLD
        )

    return frozenset(
        signals
    )


def _learn_custom_topic_signal_requirement(
    document: DocxDocument,
    country: str | None,
) -> frozenset[str] | None:
    """
    Learn which structural signals this document's own confirmed legal
    topics consistently carry, so a custom (non-taxonomy) heading is
    only ever recognized against the SAME evidentiary bar - never a
    weaker one.

    Returns None (never accept a custom topic) when the document has
    no confirmed topic to learn from, or when its confirmed topics do
    not consistently use a real Word Heading 1 / outline-level-0
    paragraph. Bold text, list numbering, and textual "N." prefixes are
    also used pervasively by ordinary subsections and body emphasis in
    these documents, so on their own they are too ambiguous to promote
    an unrecognized heading to a legal topic - only Heading 1 is a
    structural marker reserved for real top-level sections. Documents
    whose own topics rely solely on those weaker signals (no Heading 1
    at all) do not support custom top-level topics.
    """

    if country is None:
        return None

    confirmed_signal_sets: list[frozenset[str]] = []

    for block_item in _iter_block_items(
        document
    ):
        if not isinstance(
            block_item,
            Paragraph,
        ):
            continue

        text = _normalize_text(
            block_item.text
        )

        if (
            not text
            or _is_ignored_text(
                text
            )
        ):
            continue

        heading_level = _get_heading_level(
            block_item
        )

        legal_topic = _get_main_legal_topic(
            paragraph=block_item,
            text=text,
            heading_level=heading_level,
            country=country,
        )

        if legal_topic is None:
            continue

        confirmed_signal_sets.append(
            _main_section_signals(
                paragraph=block_item,
                text=text,
                heading_level=heading_level,
            )
        )

    if not confirmed_signal_sets:
        return None

    required = confirmed_signal_sets[0]

    for signals in confirmed_signal_sets[1:]:
        required = required & signals

    if _TOPIC_SIGNAL_HEADING_LEVEL_1 not in required:
        return None

    return required


def _looks_like_topic_title(
    label: str,
) -> bool:
    """
    Return whether text reads like a section title rather than a
    sentence or list-item fragment.

    A real legal-topic heading is a short title case/sentence case
    phrase (e.g. "Hiring Practices", or "12. Remote Working" with the
    document's own numbering convention); it never starts with a
    lowercase word (once any leading number prefix is set aside) or
    ends with sentence punctuation the way an enumerated list item or
    clause does (e.g. "the Corporations Act;").
    """

    if not label:
        return False

    unprefixed = _TOPIC_NUMBER_PREFIX_PATTERN.sub(
        "",
        label,
    ) or label

    if not unprefixed[0].isupper():
        return False

    if _SENTENCE_TERMINATOR_PATTERN.search(
        label
    ):
        return False

    return True


def _get_custom_legal_topic(
    paragraph: Paragraph,
    text: str,
    heading_level: int | None,
    country: str | None,
    past_front_matter: bool,
    required_signals: frozenset[str] | None,
) -> str | None:
    """
    Return a custom (non-taxonomy) legal-topic title, when justified.

    An admin can add a brand-new top-level topic that has no entry in
    the fixed LEGAL_TOPICS taxonomy (e.g. "Remote Working"). Recognizing
    it generically requires evidence at least as strong as whatever
    this specific document's OWN confirmed topics already carry
    (required_signals, from _learn_custom_topic_signal_requirement) -
    a single ambiguous signal such as bold text is not enough on its
    own, since ordinary subsections also use bold text.

    Only considered once the document's own front matter has been left
    behind (its overview or first main heading has already been seen),
    so a title page or introductory heading is never mistaken for a
    legal topic.
    """

    if country is None:
        return None

    if not past_front_matter:
        return None

    if required_signals is None:
        return None

    candidate_signals = _main_section_signals(
        paragraph=paragraph,
        text=text,
        heading_level=heading_level,
    )

    if not required_signals.issubset(
        candidate_signals
    ):
        return None

    label = _clean_structural_label(
        text
    )

    # A real topic heading is a short title, not a full sentence - this
    # also matches the "reasonable length" convention new custom
    # section titles must already follow (see admin ADD validation).
    # Excludes the rare list item that happens to share the document's
    # own heading signal combination by formatting accident.
    if len(label) > _MAX_CUSTOM_TOPIC_HEADING_LENGTH:
        return None

    if not _looks_like_topic_title(
        label
    ):
        return None

    return label


def _get_subsection_topic_override(
    paragraph: Paragraph,
    text: str,
) -> str | None:
    """
    Return a one-off legal-topic override for specific known headings.

    See SUBSECTION_TOPIC_OVERRIDES in subsection_taxonomy.py for why
    this table exists: some content-topic mismatches cannot be
    detected from DOCX structure alone.
    """

    if not _has_explicit_bold_text(
        paragraph
    ):
        return None

    return get_subsection_topic_override(
        text
    )


def _is_overview_heading(
    paragraph: Paragraph,
    text: str,
    heading_level: int | None,
    country: str | None,
) -> bool:
    """Return whether the paragraph starts the document overview."""

    if not is_overview_section(
        section=text,
        country=country,
    ):
        return False

    return any(
        (
            heading_level == 1,
            _has_explicit_bold_text(
                paragraph
            ),
        )
    )


def _is_generic_main_heading(
    paragraph: Paragraph,
    heading_level: int | None,
    country: str | None,
) -> bool:
    """
    Accept Heading 1 in generic mode.

    Strict L&E parsing is activated whenever country is supplied.
    """

    if country is not None:
        return False

    if heading_level != 1:
        return False

    if _is_explicitly_unbolded(
        paragraph
    ):
        return False

    return True


def _get_subsection_label(
    paragraph: Paragraph,
    text: str,
    heading_level: int | None,
    parent_topic: str | None,
    country: str | None,
) -> str | None:
    """
    Return a subsection label when the evidence is reliable.

    Generic mode accepts normal Heading 2 paragraphs.

    Strict L&E mode requires:

    - a match in the controlled subsection taxonomy; and
    - Heading 2, Heading 4, or explicit bold formatting.

    Unknown bold paragraphs remain part of the legal content.
    """

    if _is_explicitly_unbolded(
        paragraph
    ):
        return None

    if country is None:
        if heading_level != 2:
            return None

        if _has_numbering(
            paragraph
        ):
            return None

        return _clean_structural_label(
            text
        )

    canonical_subsection = get_canonical_subsection(
        parent_topic=parent_topic,
        subsection=text,
    )

    if canonical_subsection is None:
        return None

    has_structural_signal = any(
        (
            heading_level == 2,
            heading_level == 4,
            _has_explicit_bold_text(
                paragraph
            ),
        )
    )

    if not has_structural_signal:
        return None

    return canonical_subsection


def _is_ignored_text(
    text: str,
) -> bool:
    """Return whether text is only a decorative separator."""

    return text in _IGNORED_PARAGRAPH_TEXTS


def _iter_block_items(
    document: DocxDocument,
) -> Iterator[BlockItem]:
    """Yield paragraphs and tables in their real document order."""

    for child in document.element.body.iterchildren():
        if isinstance(
            child,
            CT_P,
        ):
            yield Paragraph(
                child,
                document,
            )

        elif isinstance(
            child,
            CT_Tbl,
        ):
            yield Table(
                child,
                document,
            )


def _serialize_table(
    table: Table,
) -> str:
    """Convert a meaningful Word table to readable plain text."""

    serialized_rows: list[str] = []

    for row in table.rows:
        cells = [
            _normalize_text(
                cell.text
            )
            for cell in row.cells
        ]

        if not any(
            cells
        ):
            continue

        serialized_row = " | ".join(
            cells
        ).strip()

        if not _has_alnum(
            serialized_row
        ):
            continue

        serialized_rows.append(
            serialized_row
        )

    return "\n".join(
        serialized_rows
    )


def parse_docx_sections(
    file_path: Path,
    country: str | None = None,
) -> list[ParsedSection]:
    """
    Parse a DOCX file into structured sections.

    Strict L&E mode is used when country is provided:

    - main sections must match the legal taxonomy;
    - subsections must match the controlled subsection taxonomy;
    - Heading 2, Heading 4, and bold legacy labels are supported;
    - unknown formatted paragraphs remain content.

    Generic mode is used without country:

    - Heading 1 creates a section;
    - Heading 2 creates a subsection.
    """

    file_path = file_path.resolve()

    if not file_path.is_file():
        raise FileNotFoundError(
            f"DOCX file not found: {file_path}"
        )

    if file_path.suffix.lower() != ".docx":
        raise ValueError(
            "Unsupported document format: "
            f"{file_path.suffix}"
        )

    document = Document(
        file_path
    )

    parsed_sections: list[ParsedSection] = []

    current_section = (
        f"Employment Law Overview {country}"
        if country
        else "General"
    )

    current_legal_topic: str | None = None
    current_subsection: str | None = None
    current_is_custom_legal_topic = False

    # Becomes True once the document's own overview or first main
    # heading has been seen, so a title-page or introductory heading
    # is never mistaken for a custom (non-taxonomy) legal topic.
    past_front_matter = False

    # The structural signal combination this document's own confirmed
    # topics consistently carry (or None if there is none to learn),
    # used as the evidentiary bar a custom heading must also clear.
    required_custom_topic_signals = _learn_custom_topic_signal_requirement(
        document=document,
        country=country,
    )

    # Set while inside a one-off SUBSECTION_TOPIC_OVERRIDES block: holds
    # the (section, legal_topic) to restore as soon as the next heading
    # of any kind is found, so the override never permanently changes
    # what topic subsequent subsections of the enclosing section
    # resolve against.
    pending_topic_override: (
        tuple[str, str | None, bool] | None
    ) = None

    content_buffer: list[str] = []

    def flush_content() -> None:
        content = "\n\n".join(
            content_buffer
        ).strip()

        if content and _has_alnum(
            content
        ):
            parsed_sections.append(
                ParsedSection(
                    section=current_section,
                    subsection=current_subsection,
                    content=content,
                    is_custom_legal_topic=current_is_custom_legal_topic,
                )
            )

        content_buffer.clear()

    for block_item in _iter_block_items(
        document
    ):
        if isinstance(
            block_item,
            Paragraph,
        ):
            text = _normalize_text(
                block_item.text
            )

            if (
                not text
                or _is_ignored_text(
                    text
                )
            ):
                continue

            heading_level = _get_heading_level(
                block_item
            )

            if is_admin_section_heading(
                block_item
            ):
                flush_content()

                current_section = _clean_structural_label(
                    text
                )

                current_legal_topic = current_section
                current_subsection = None
                current_is_custom_legal_topic = True
                pending_topic_override = None
                past_front_matter = True
                continue

            topic_override = _get_subsection_topic_override(
                paragraph=block_item,
                text=text,
            )

            if topic_override is not None:
                flush_content()

                if pending_topic_override is None:
                    pending_topic_override = (
                        current_section,
                        current_legal_topic,
                        current_is_custom_legal_topic,
                    )

                current_section = _clean_structural_label(
                    text
                )

                current_legal_topic = topic_override
                current_subsection = None
                current_is_custom_legal_topic = False
                continue

            legal_topic = _get_main_legal_topic(
                paragraph=block_item,
                text=text,
                heading_level=heading_level,
                country=country,
            )

            if legal_topic is not None:
                flush_content()

                current_section = _clean_structural_label(
                    text
                )

                current_legal_topic = legal_topic
                current_subsection = None
                current_is_custom_legal_topic = False
                pending_topic_override = None
                past_front_matter = True
                continue

            if _is_overview_heading(
                paragraph=block_item,
                text=text,
                heading_level=heading_level,
                country=country,
            ):
                flush_content()

                current_section = _clean_structural_label(
                    text
                )

                current_legal_topic = None
                current_subsection = None
                current_is_custom_legal_topic = False
                pending_topic_override = None
                past_front_matter = True
                continue

            if _is_generic_main_heading(
                paragraph=block_item,
                heading_level=heading_level,
                country=country,
            ):
                flush_content()

                current_section = _clean_structural_label(
                    text
                )

                current_legal_topic = None
                current_subsection = None
                current_is_custom_legal_topic = False
                pending_topic_override = None
                past_front_matter = True
                continue

            custom_legal_topic = _get_custom_legal_topic(
                paragraph=block_item,
                text=text,
                heading_level=heading_level,
                country=country,
                past_front_matter=past_front_matter,
                required_signals=required_custom_topic_signals,
            )

            if custom_legal_topic is not None:
                flush_content()

                current_section = _clean_structural_label(
                    text
                )

                current_legal_topic = custom_legal_topic
                current_subsection = None
                current_is_custom_legal_topic = True
                pending_topic_override = None
                continue

            subsection_parent_topic = (
                pending_topic_override[1]
                if pending_topic_override is not None
                else current_legal_topic
            )

            subsection_label = _get_subsection_label(
                paragraph=block_item,
                text=text,
                heading_level=heading_level,
                parent_topic=subsection_parent_topic,
                country=country,
            )

            if subsection_label is not None:
                flush_content()

                if pending_topic_override is not None:
                    (
                        current_section,
                        current_legal_topic,
                        current_is_custom_legal_topic,
                    ) = pending_topic_override

                    pending_topic_override = None

                current_subsection = subsection_label
                continue

            content_buffer.append(
                text
            )

            continue

        table_content = _serialize_table(
            block_item
        )

        if table_content:
            content_buffer.append(
                table_content
            )

    flush_content()

    return parsed_sections


@dataclass(frozen=True, slots=True)
class TopicLocation:
    """
    One top-level legal topic's raw position within a DOCX body.

    For mutation purposes only (Edit/Add a section) - not a substitute
    for parse_docx_sections. Subsections are not tracked here: a
    mutation always replaces a topic's content in full, and the normal
    parser re-derives subsection metadata from the result afterwards.
    """

    legal_topic: str
    is_custom_legal_topic: bool
    heading_element: CT_P
    body_elements: tuple[CT_P | CT_Tbl, ...]


def locate_top_level_topics(
    document: DocxDocument,
    country: str,
) -> list[TopicLocation]:
    """
    Find each top-level legal-topic heading (canonical or custom) in an
    already-open DOCX document and the raw body elements between it
    and the next top-level heading-like paragraph (main topic,
    overview, or generic-main heading).

    Takes the same in-memory document object a caller intends to
    mutate (rather than a file path), so the returned elements belong
    to the exact tree the caller will modify and save - never a
    separate, throwaway parse of the same file.

    Reuses the same heading-classification rules as
    parse_docx_sections, so a topic's boundary here always matches
    what the parser would extract as that topic's content.
    """

    required_custom_topic_signals = _learn_custom_topic_signal_requirement(
        document=document,
        country=country,
    )

    locations: list[TopicLocation] = []

    current_heading_element: CT_P | None = None
    current_legal_topic: str | None = None
    current_is_custom = False
    current_body_elements: list[CT_P | CT_Tbl] = []
    past_front_matter = False

    def flush_topic() -> None:
        if (
            current_heading_element is not None
            and current_legal_topic is not None
        ):
            locations.append(
                TopicLocation(
                    legal_topic=current_legal_topic,
                    is_custom_legal_topic=current_is_custom,
                    heading_element=current_heading_element,
                    body_elements=tuple(
                        current_body_elements
                    ),
                )
            )

    for block_item in _iter_block_items(
        document
    ):
        if isinstance(
            block_item,
            Paragraph,
        ):
            text = _normalize_text(
                block_item.text
            )

            if (
                not text
                or _is_ignored_text(
                    text
                )
            ):
                if current_legal_topic is not None:
                    current_body_elements.append(
                        block_item._p
                    )

                continue

            heading_level = _get_heading_level(
                block_item
            )

            if is_admin_section_heading(
                block_item
            ):
                flush_topic()

                current_heading_element = block_item._p
                current_legal_topic = _clean_structural_label(
                    text
                )
                current_is_custom = True
                current_body_elements = []
                past_front_matter = True
                continue

            legal_topic = _get_main_legal_topic(
                paragraph=block_item,
                text=text,
                heading_level=heading_level,
                country=country,
            )

            if legal_topic is not None:
                flush_topic()

                current_heading_element = block_item._p
                current_legal_topic = legal_topic
                current_is_custom = False
                current_body_elements = []
                past_front_matter = True
                continue

            if _is_overview_heading(
                paragraph=block_item,
                text=text,
                heading_level=heading_level,
                country=country,
            ):
                flush_topic()

                current_heading_element = None
                current_legal_topic = None
                current_body_elements = []
                past_front_matter = True
                continue

            if _is_generic_main_heading(
                paragraph=block_item,
                heading_level=heading_level,
                country=country,
            ):
                flush_topic()

                current_heading_element = None
                current_legal_topic = None
                current_body_elements = []
                past_front_matter = True
                continue

            custom_legal_topic = _get_custom_legal_topic(
                paragraph=block_item,
                text=text,
                heading_level=heading_level,
                country=country,
                past_front_matter=past_front_matter,
                required_signals=required_custom_topic_signals,
            )

            if custom_legal_topic is not None:
                flush_topic()

                current_heading_element = block_item._p
                current_legal_topic = custom_legal_topic
                current_is_custom = True
                current_body_elements = []
                continue

            # Subsections, one-off topic overrides, and plain content
            # all stay part of the current topic's own body elements.
            if current_legal_topic is not None:
                current_body_elements.append(
                    block_item._p
                )

            continue

        if current_legal_topic is not None:
            current_body_elements.append(
                block_item._tbl
            )

    flush_topic()

    return locations


# --- Contact-card extraction -------------------------------------------
#
# Word text boxes (w:drawing > wps:txbx > w:txbxContent) are anchored
# drawings, not part of the document body's main paragraph/table flow
# that parse_docx_sections walks - so the firm/office and "CONTACT
# PERSON" cards every L&E Global document carries in its introduction
# are invisible to it. These functions are a separate, additive reader
# for that one container, used only to build one dedicated Contact
# chunk per document; they never change how normal legal sections are
# parsed or chunked.

_CONTACT_PERSON_MARKERS: Final[frozenset[str]] = frozenset(
    {
        "contact person",
        "contact persons",
    }
)

_EMAIL_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
)

_WEBSITE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?:https?://\S+|www\.\S+)",
    re.IGNORECASE,
)

_PHONE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\+?\d[\d\s().-]{5,}\d"
)

_MISSING_WORD_BOUNDARY_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?<=[a-z0-9])(?=[A-Z])"
)

_REPEATED_COMMA_PATTERN: Final[re.Pattern[str]] = re.compile(
    r",\s*,+"
)

_TEXT_BOX_CONTENT_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"<w:txbxContent>(.*?)</w:txbxContent>",
    re.DOTALL,
)

_TEXT_BOX_PARAGRAPH_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"<w:p[ >].*?</w:p>",
    re.DOTALL,
)

# Matches either a run's text (captured in group 1) or a line break,
# in document order, so a break between two runs can be turned into a
# separator instead of silently disappearing between two <w:t> tags.
_TEXT_BOX_TOKEN_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"<w:t[^>]*>(.*?)</w:t>|<w:br\s*/>",
    re.DOTALL,
)


@dataclass(frozen=True, slots=True)
class ExtractedContact:
    """One validated L&E Global contact, extracted from a source DOCX."""

    member_firm: str | None = None
    contact_person: str | None = None
    email: str | None = None
    phone: str | None = None
    address: str | None = None
    website: str | None = None

    def has_any_field(self) -> bool:
        """Return whether at least one field was actually found."""

        return any(
            (
                self.member_firm,
                self.contact_person,
                self.email,
                self.phone,
                self.address,
                self.website,
            )
        )


def _clean_text_box_line(
    value: str,
) -> str:
    """
    Repair the two artifacts of reconstructing one visual line from
    separate XML runs and line breaks:

    - a missing space where two visually wrapped runs were joined
      without one, for example "BuildingEC3A" -> "Building EC3A",
      since Word text boxes routinely rely on the box width to wrap a
      line rather than an explicit break;
    - a doubled comma where a run's own trailing punctuation meets the
      comma this reader inserts for a line break, for example
      "Castro, " + break + "Ono" -> "Castro, , Ono" -> "Castro, Ono".
    """

    without_missing_boundaries = (
        _MISSING_WORD_BOUNDARY_PATTERN.sub(
            " ",
            value,
        )
    )

    return _REPEATED_COMMA_PATTERN.sub(
        ",",
        without_missing_boundaries,
    )


def extract_text_box_blocks(
    file_path: Path,
) -> list[list[str]]:
    """
    Extract every text box's non-empty paragraph lines, in order.

    Compatibility fallbacks (mc:Fallback) duplicate the same text box
    content for older Word versions, so a block identical to the one
    immediately before it is treated as that duplicate and dropped.
    """

    try:
        with zipfile.ZipFile(file_path) as archive:
            document_xml = archive.read(
                "word/document.xml"
            ).decode(
                "utf-8",
                errors="ignore",
            )

    except (
        KeyError,
        OSError,
        zipfile.BadZipFile,
    ):
        return []

    blocks: list[list[str]] = []
    previous_lines: list[str] | None = None

    for raw_block in _TEXT_BOX_CONTENT_PATTERN.findall(
        document_xml
    ):
        lines: list[str] = []

        for paragraph_xml in _TEXT_BOX_PARAGRAPH_PATTERN.findall(
            raw_block
        ):
            tokens = [
                match.group(1)
                if match.group(1) is not None
                else ", "
                for match in _TEXT_BOX_TOKEN_PATTERN.finditer(
                    paragraph_xml
                )
            ]

            line = _clean_text_box_line(
                _normalize_text(
                    unescape(
                        "".join(tokens)
                    )
                )
            )

            if line:
                lines.append(line)

        if lines and lines == previous_lines:
            continue

        if lines:
            blocks.append(lines)
            previous_lines = lines

    return blocks


def parse_contact_blocks(
    blocks: Sequence[Sequence[str]],
    country: str | None = None,
) -> list[ExtractedContact]:
    """
    Classify already-extracted text-box blocks into contact entries.

    Pure text-processing function - takes plain lines, not a DOCX
    file - so it can be exercised directly with a synthetic block
    structure. Two block shapes are recognized generically, without
    any per-document or per-country assumption:

    - a "CONTACT PERSON" (or "CONTACT PERSONS") block: a line matching
      that marker, followed by a name and one or more emails;
    - a firm/office block: any other block containing an email or a
      phone-like pattern (a bare website mention alone is not enough,
      since a generic site link can appear elsewhere in a document
      without being part of a contact card). Its first line is taken
      as the member firm name; any other line becomes the address,
      except one that repeats the supplied country name, since that
      is expected to come from validated document metadata instead.

    Firm/office blocks and CONTACT PERSON blocks are collected
    separately, in document order, then paired position-by-position:
    the first firm block with the first CONTACT PERSON block found
    anywhere in the document, and so on - since different source
    documents lay these two cards out in different relative order. A
    document with several such pairs yields several contacts, in
    order. A block of either kind with no counterpart is still
    reported using only the fields it actually has - never inventing
    the other side. Exact duplicate entries (the same document's
    compatibility fallback markup surfacing as a second, near-identical
    block) are collapsed to one.
    """

    normalized_country = (
        _normalize_text(
            country
        ).casefold()
        if country
        else None
    )

    firm_entries: list[dict[str, str | None]] = []
    person_entries: list[
        tuple[str | None, str | None]
    ] = []

    for block in blocks:
        marker_index = next(
            (
                index
                for index, line in enumerate(block)
                if line.strip().casefold()
                in _CONTACT_PERSON_MARKERS
            ),
            None,
        )

        if marker_index is not None:
            emails: list[str] = []
            name_lines: list[str] = []

            for line in block[marker_index + 1:]:
                email_match = _EMAIL_PATTERN.search(
                    line
                )

                if email_match:
                    emails.append(
                        email_match.group(0)
                    )
                    continue

                name_lines.append(
                    line
                )

            person_entries.append(
                (
                    (
                        name_lines[0]
                        if name_lines
                        else None
                    ),
                    (
                        ", ".join(emails)
                        if emails
                        else None
                    ),
                )
            )

            continue

        joined = " ".join(
            block
        )

        phone_match = _PHONE_PATTERN.search(
            joined
        )

        website_match = _WEBSITE_PATTERN.search(
            joined
        )

        if not (
            _EMAIL_PATTERN.search(joined)
            or phone_match
        ):
            continue

        address_lines = [
            line
            for line in block[1:]
            if not (
                phone_match
                and line == phone_match.group(0)
            )
            and not (
                website_match
                and line == website_match.group(0)
            )
            and (
                normalized_country is None
                or line.casefold() != normalized_country
            )
        ]

        firm_entries.append(
            {
                "member_firm": (
                    block[0]
                    if block
                    else None
                ),
                "phone": (
                    phone_match.group(0)
                    if phone_match
                    else None
                ),
                "website": (
                    website_match.group(0)
                    if website_match
                    else None
                ),
                "address": (
                    ", ".join(
                        address_lines
                    )
                    if address_lines
                    else None
                ),
            }
        )

    # Collapse near-duplicate compatibility blocks that the raw-line
    # dedup in extract_text_box_blocks missed because they differ in
    # some incidental XML detail (for example two different internal
    # hyperlink relationship ids for the same displayed email), so
    # they do not each consume a slot in the position-based pairing
    # below.
    deduplicated_person_entries: list[
        tuple[str | None, str | None]
    ] = []
    seen_person_keys: set[
        tuple[str | None, str | None]
    ] = set()

    for person_entry in person_entries:
        if person_entry in seen_person_keys:
            continue

        seen_person_keys.add(
            person_entry
        )

        deduplicated_person_entries.append(
            person_entry
        )

    person_entries = deduplicated_person_entries

    deduplicated_firm_entries: list[
        dict[str, str | None]
    ] = []
    seen_firm_keys: set[
        tuple[str | None, ...]
    ] = set()

    for firm_entry in firm_entries:
        firm_key = tuple(
            firm_entry.get(field_name)
            for field_name in (
                "member_firm",
                "phone",
                "website",
                "address",
            )
        )

        if firm_key in seen_firm_keys:
            continue

        seen_firm_keys.add(
            firm_key
        )

        deduplicated_firm_entries.append(
            firm_entry
        )

    firm_entries = deduplicated_firm_entries

    pair_count = max(
        len(firm_entries),
        len(person_entries),
    )

    contacts: list[ExtractedContact] = []
    seen_contacts: set[
        tuple[str | None, str | None, str | None]
    ] = set()

    for index in range(pair_count):
        firm_fields = (
            firm_entries[index]
            if index < len(firm_entries)
            else {}
        )

        contact_person, email = (
            person_entries[index]
            if index < len(person_entries)
            else (None, None)
        )

        contact = ExtractedContact(
            member_firm=firm_fields.get(
                "member_firm"
            ),
            contact_person=contact_person,
            email=email,
            phone=firm_fields.get(
                "phone"
            ),
            address=firm_fields.get(
                "address"
            ),
            website=firm_fields.get(
                "website"
            ),
        )

        if not contact.has_any_field():
            continue

        dedup_key = (
            contact.member_firm,
            contact.contact_person,
            contact.email,
        )

        if dedup_key in seen_contacts:
            continue

        seen_contacts.add(
            dedup_key
        )

        contacts.append(
            contact
        )

    return contacts


def extract_contacts_from_docx(
    file_path: Path,
    country: str | None = None,
) -> list[ExtractedContact]:
    """
    Extract every validated contact card from one source DOCX.

    Checks for the deterministic, Admin-managed block (mission "ORDER
    8G-B2.1") first - present only on a DOCX this system itself
    materialized for Download - and falls back to the ordinary
    text-box-based legacy parser for every real, organically-uploaded
    document, where that marker is never present.
    """

    deterministic_contacts = extract_deterministic_contact_blocks(
        file_path
    )

    if deterministic_contacts is not None:
        return deterministic_contacts

    return parse_contact_blocks(
        extract_text_box_blocks(
            file_path
        ),
        country=country,
    )


def build_contact_chunk_content(
    contacts: Sequence[ExtractedContact],
) -> str:
    """Build the structured text of one Contact-subsection chunk."""

    entries: list[str] = []

    for contact in contacts:
        lines: list[str] = []

        if contact.member_firm:
            lines.append(
                f"Member firm: {contact.member_firm}"
            )

        if contact.contact_person:
            lines.append(
                f"Contact person: {contact.contact_person}"
            )

        if contact.email:
            lines.append(
                f"Email: {contact.email}"
            )

        if contact.phone:
            lines.append(
                f"Phone: {contact.phone}"
            )

        if contact.address:
            lines.append(
                f"Address: {contact.address}"
            )

        if contact.website:
            lines.append(
                f"Website: {contact.website}"
            )

        if lines:
            entries.append(
                "\n".join(
                    lines
                )
            )

    return "\n\n".join(
        entries
    )


# --- Deterministic Contact block (mission "ORDER 8G-B2.1") -----------
#
# A Download of the effective (current) DOCX materializes structured
# Contact state as one plain, ordinary paragraph-based block instead of
# reconstructing a new floating text box from scratch - far more
# reliable to write and to re-parse than trying to clone the
# heterogeneous legacy text-box/VML-fallback shapes real source
# documents use. This marker is deliberately distinctive so it can
# never coincidentally match real, organically-authored legal document
# text; extract_contacts_from_docx() checks for it first (see below)
# and, if present, uses ONLY this block - a materialized download's own
# legacy text boxes are already blanked by the same operation that adds
# this block (app.services.document_contact_materializer), so there is
# never a real conflict between the two representations.

DETERMINISTIC_CONTACT_BLOCK_MARKER: Final[str] = (
    "L&E GLOBAL - CURRENT CONTACT INFORMATION (ADMIN-MANAGED)"
)

DETERMINISTIC_NO_CONTACTS_LINE: Final[str] = (
    "(No contacts currently configured.)"
)

_DETERMINISTIC_FIELD_LABELS: Final[
    tuple[tuple[str, str], ...]
] = (
    ("Member firm: ", "member_firm"),
    ("Contact person: ", "contact_person"),
    ("Email: ", "email"),
    ("Phone: ", "phone"),
    ("Address: ", "address"),
    ("Website: ", "website"),
)


def _parse_deterministic_contact_entry(
    lines: Sequence[str],
) -> ExtractedContact:
    """Parse one blank-line-delimited group of "Label: value" lines
    (the exact inverse of build_contact_chunk_content's own per-contact
    formatting) into one ExtractedContact."""

    fields: dict[str, str] = {}

    for line in lines:
        for label, field_name in _DETERMINISTIC_FIELD_LABELS:
            if line.startswith(label):
                fields[field_name] = line[len(label):].strip()
                break

    return ExtractedContact(**fields)


def extract_deterministic_contact_blocks(
    source: Path | IO[bytes],
) -> list[ExtractedContact] | None:
    """
    Recognize the deterministic, Admin-managed Contact block a
    materialized Download DOCX carries.

    Returns None (never an empty list) when the marker paragraph is
    absent, so callers can fall back to the ordinary text-box-based
    legacy parser - an empty list is reserved for "the marker is
    present and explicitly states zero contacts".
    """

    try:
        document = Document(source)

    except (
        KeyError,
        OSError,
        zipfile.BadZipFile,
        PackageNotFoundError,
    ):
        return None

    paragraph_texts = [
        paragraph.text
        for paragraph in document.paragraphs
    ]

    try:
        marker_index = paragraph_texts.index(
            DETERMINISTIC_CONTACT_BLOCK_MARKER
        )

    except ValueError:
        return None

    remaining = paragraph_texts[marker_index + 1:]

    if (
        remaining
        and remaining[0].strip() == DETERMINISTIC_NO_CONTACTS_LINE
    ):
        return []

    contacts: list[ExtractedContact] = []
    current_lines: list[str] = []

    def flush_current_entry() -> None:
        if current_lines:
            contacts.append(
                _parse_deterministic_contact_entry(
                    current_lines
                )
            )

    for text in remaining:
        stripped = text.strip()

        if stripped == "":
            flush_current_entry()
            current_lines = []
            continue

        current_lines.append(stripped)

    flush_current_entry()

    return contacts