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


def _normalize_text(value: str) -> str:
    """Remove unnecessary whitespace while keeping readable text."""

    return " ".join(
        value
        .replace("\xa0", " ")
        .split()
    )


def _clean_structural_label(value: str) -> str:
    """Remove decorative separators surrounding a title."""

    normalized = _normalize_text(
        value
    )

    without_leading_decoration = (
        _LEADING_DECORATION_PATTERN.sub(
            "",
            normalized,
        )
    )

    without_trailing_decoration = (
        _TRAILING_DECORATION_PATTERN.sub(
            "",
            without_leading_decoration,
        )
    )

    return without_trailing_decoration.strip()


def _has_alnum(text: str) -> bool:
    """Return whether text contains useful alphanumeric content."""

    return any(
        character.isalnum()
        for character in text
    )


def _get_heading_level(
    paragraph: Paragraph,
) -> int | None:
    """Return the Word heading level when available."""

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

    paragraph_properties = (
        paragraph._p.pPr
    )

    if (
        paragraph_properties is not None
        and paragraph_properties.outlineLvl
        is not None
    ):
        return (
            int(
                paragraph_properties
                .outlineLvl
                .val
            )
            + 1
        )

    return None


def _has_numbering(
    paragraph: Paragraph,
) -> bool:
    """Return whether the paragraph belongs to a numbered list."""

    paragraph_properties = (
        paragraph._p.pPr
    )

    return (
        paragraph_properties is not None
        and paragraph_properties.numPr
        is not None
    )


def _has_topic_number_prefix(
    text: str,
) -> bool:
    """Return whether text begins with an explicit topic number."""

    return (
        _TOPIC_NUMBER_PREFIX_PATTERN.match(
            text
        )
        is not None
    )


def _has_explicit_bold_text(
    paragraph: Paragraph,
) -> bool:
    """Return whether at least one visible text run is explicitly bold."""

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
        and value.lower()
        in _FALSE_XML_VALUES
    )


def _is_explicitly_unbolded(
    paragraph: Paragraph,
) -> bool:
    """
    Detect a Heading style explicitly overridden as non-bold.

    Some L&E documents contain body paragraphs using Heading 2 while
    explicitly setting bold=false. They must remain legal content.
    """

    paragraph_properties = (
        paragraph._p.pPr
    )

    if paragraph_properties is not None:
        run_properties = (
            paragraph_properties.find(
                qn("w:rPr")
            )
        )

        if run_properties is not None:
            bold_property = (
                run_properties.find(
                    qn("w:b")
                )
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
    """
    Return whether a recognized topic has a structural signal.

    Exact taxonomy matching alone is insufficient because ordinary
    content may occasionally equal or contain a topic label.
    """

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


def _is_main_legal_section(
    paragraph: Paragraph,
    text: str,
    heading_level: int | None,
    country: str | None,
) -> bool:
    """Return whether a paragraph starts one of the 11 legal topics."""

    legal_topic = (
        get_canonical_legal_topic(
            section=text,
            country=country,
        )
    )

    if legal_topic is None:
        return False

    return _has_main_section_signal(
        paragraph=paragraph,
        text=text,
        heading_level=heading_level,
    )


def _is_overview_heading(
    paragraph: Paragraph,
    text: str,
    heading_level: int | None,
    country: str | None,
) -> bool:
    """Return whether a paragraph starts the country overview."""

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
    Accept a normal Heading 1 when no country metadata is supplied.

    This compatibility mode supports generic DOCX parsing and the
    original unit tests.

    When a country is supplied, the parser remains strict: only a
    recognized L&E topic or overview may start a main section.
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


def _is_reliable_subheading(
    paragraph: Paragraph,
    heading_level: int | None,
) -> bool:
    """
    Return whether a paragraph is a reliable subsection heading.

    Only Heading 2 is used for subsection boundaries. Heading 3 and
    Heading 4 are deliberately retained as content because several
    source documents use those styles for lists and body paragraphs.
    """

    if heading_level != 2:
        return False

    if _has_numbering(
        paragraph
    ):
        return False

    if _is_explicitly_unbolded(
        paragraph
    ):
        return False

    return True


def _is_ignored_text(
    text: str,
) -> bool:
    """Return whether text is only a decorative separator."""

    return (
        text
        in _IGNORED_PARAGRAPH_TEXTS
    )


def _iter_block_items(
    document: DocxDocument,
) -> Iterator[BlockItem]:
    """
    Yield paragraphs and tables in their real document order.

    Using document.paragraphs alone would silently ignore content
    stored inside Word tables.
    """

    for child in (
        document.element.body
        .iterchildren()
    ):
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
    """
    Convert a Word table into readable plain text.

    Each row becomes one line and cells are separated with a vertical
    bar. Decorative empty tables are ignored.
    """

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
    Parse an L&E DOCX and group content into legal sections.

    When country is supplied, main sections are recognized through the
    approved L&E taxonomy combined with Word structural signals.

    When country is omitted, a reliable Heading 1 is accepted as a
    generic main section.

    Heading 2 paragraphs become subsections only when they are not
    numbered and are not explicitly overridden as non-bold.

    Unrecognized Heading 1 paragraphs remain content in strict L&E
    mode, preventing silent loss in malformed source documents.
    """

    file_path = (
        file_path.resolve()
    )

    if not file_path.is_file():
        raise FileNotFoundError(
            f"DOCX file not found: {file_path}"
        )

    if (
        file_path.suffix.lower()
        != ".docx"
    ):
        raise ValueError(
            "Unsupported document format: "
            f"{file_path.suffix}"
        )

    document = Document(
        file_path
    )

    parsed_sections: list[
        ParsedSection
    ] = []

    current_section = (
        f"Employment Law Overview {country}"
        if country
        else "General"
    )

    current_subsection: str | None = None

    content_buffer: list[str] = []

    def flush_content() -> None:
        content = "\n\n".join(
            content_buffer
        ).strip()

        if (
            content
            and _has_alnum(
                content
            )
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

            heading_level = (
                _get_heading_level(
                    block_item
                )
            )

            if _is_main_legal_section(
                paragraph=block_item,
                text=text,
                heading_level=heading_level,
                country=country,
            ):
                flush_content()

                current_section = (
                    _clean_structural_label(
                        text
                    )
                )

                current_subsection = None
                continue

            if _is_overview_heading(
                paragraph=block_item,
                text=text,
                heading_level=heading_level,
                country=country,
            ):
                flush_content()

                current_section = (
                    _clean_structural_label(
                        text
                    )
                )

                current_subsection = None
                continue

            if _is_generic_main_heading(
                paragraph=block_item,
                heading_level=heading_level,
                country=country,
            ):
                flush_content()

                current_section = (
                    _clean_structural_label(
                        text
                    )
                )

                current_subsection = None
                continue

            if _is_reliable_subheading(
                paragraph=block_item,
                heading_level=heading_level,
            ):
                flush_content()

                current_subsection = (
                    _clean_structural_label(
                        text
                    )
                )

                continue

            content_buffer.append(
                text
            )

            continue

        table_content = (
            _serialize_table(
                block_item
            )
        )

        if table_content:
            content_buffer.append(
                table_content
            )

    flush_content()

    return parsed_sections