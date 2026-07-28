from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TypeAlias

from docx import Document
from docx.document import Document as DocxDocument
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
)


_HEADING_STYLE_PATTERN = re.compile(
    r"^(?:heading|titre)\s*([1-9])$",
    re.IGNORECASE,
)

_TOPIC_NUMBER_PREFIX_PATTERN = re.compile(
    r"^\s*"
    r"(?:[|¦=]+\s*)?"
    r"\d{1,2}\s*[.)]\s*",
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

BlockItem: TypeAlias = Paragraph | Table


@dataclass(frozen=True, slots=True)
class ParsedSection:
    """A structured section extracted from a DOCX document."""

    section: str
    subsection: str | None
    content: str


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
                continue

            subsection_label = _get_subsection_label(
                paragraph=block_item,
                text=text,
                heading_level=heading_level,
                parent_topic=current_legal_topic,
                country=country,
            )

            if subsection_label is not None:
                flush_content()

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