"""
Build an "effective" DOCX for Download that combines a document's
current legal content with its CURRENT structured Contact state
(mission "ORDER 8G-B2.1").

The persisted source DOCX is never written to. Every existing
text-box-based Contact block (a firm/office card, a "CONTACT PERSON"
card, and their mc:Fallback VML duplicates) is blanked in a fresh
in-memory copy using the exact same block classification
extract_contacts_from_docx() itself relies on, then one deterministic,
human-readable Contact block reflecting the current structured state is
appended as ordinary paragraphs - see
app.services.docx_parser.extract_deterministic_contact_blocks, which
this module's own output is designed to round-trip through exactly.
"""

from __future__ import annotations

import zipfile
from collections.abc import Sequence
from html import unescape
from io import BytesIO
from pathlib import Path

from docx import Document as DocxDocument

from app.services.docx_parser import (
    DETERMINISTIC_CONTACT_BLOCK_MARKER,
    DETERMINISTIC_NO_CONTACTS_LINE,
    ExtractedContact,
    _CONTACT_PERSON_MARKERS,
    _EMAIL_PATTERN,
    _PHONE_PATTERN,
    _TEXT_BOX_CONTENT_PATTERN,
    _TEXT_BOX_PARAGRAPH_PATTERN,
    _TEXT_BOX_TOKEN_PATTERN,
    _clean_text_box_line,
    _normalize_text,
    build_contact_chunk_content,
)

_DOCUMENT_XML_PART: str = "word/document.xml"


def _text_box_block_lines(
    raw_block_xml: str,
) -> list[str]:
    """
    The exact same per-block line extraction
    docx_parser.extract_text_box_blocks() applies to one already
    regex-isolated <w:txbxContent> inner XML fragment - kept in sync
    with that function so classification here matches parsing there.
    """

    lines: list[str] = []

    for paragraph_xml in _TEXT_BOX_PARAGRAPH_PATTERN.findall(
        raw_block_xml
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

    return lines


def _is_contact_related_block(
    lines: Sequence[str],
) -> bool:
    """
    Whether one text-box block plausibly represents part of a Contact
    card - a "CONTACT PERSON" marker block, or a firm/office block
    carrying an email or phone - the identical predicate
    parse_contact_blocks() applies inline, so a block is blanked here
    if and only if it would have contributed to the parsed contacts in
    the first place.
    """

    if any(
        line.strip().casefold() in _CONTACT_PERSON_MARKERS
        for line in lines
    ):
        return True

    joined = " ".join(lines)

    return bool(
        _EMAIL_PATTERN.search(joined)
        or _PHONE_PATTERN.search(joined)
    )


def _blank_contact_text_boxes(
    document_xml: str,
) -> str:
    """
    Return document_xml with every contact-related <w:txbxContent>
    span's inner content replaced by one empty paragraph - both the
    DrawingML and mc:Fallback VML copies of the same visual box, since
    each is its own independent regex match. Every other text box
    (branding, document title, decorative, or anything unrelated to
    Contact) is left completely untouched, since only spans classified
    as contact-related are ever replaced.
    """

    pieces: list[str] = []
    cursor = 0

    for match in _TEXT_BOX_CONTENT_PATTERN.finditer(document_xml):
        start, end = match.span()
        lines = _text_box_block_lines(match.group(1))

        pieces.append(document_xml[cursor:start])

        if _is_contact_related_block(lines):
            pieces.append(
                "<w:txbxContent><w:p/></w:txbxContent>"
            )
        else:
            pieces.append(document_xml[start:end])

        cursor = end

    pieces.append(document_xml[cursor:])

    return "".join(pieces)


def _rewrite_document_xml_part(
    source_bytes: bytes,
    new_document_xml: str,
) -> bytes:
    """
    Copy every zip entry from source_bytes unchanged except
    word/document.xml, which is replaced - never regenerates the
    package from scratch, so every other part (styles, media,
    relationships, custom XML) is preserved byte-for-byte.
    """

    output_buffer = BytesIO()

    with zipfile.ZipFile(BytesIO(source_bytes)) as source_zip:
        with zipfile.ZipFile(
            output_buffer,
            "w",
            zipfile.ZIP_DEFLATED,
        ) as output_zip:
            for item in source_zip.infolist():
                data = source_zip.read(item.filename)

                if item.filename == _DOCUMENT_XML_PART:
                    data = new_document_xml.encode("utf-8")

                output_zip.writestr(item, data)

    return output_buffer.getvalue()


def _append_deterministic_contact_block(
    docx_bytes: bytes,
    contacts: Sequence[ExtractedContact],
) -> bytes:
    """
    Re-open the (already text-box-blanked) document via python-docx and
    append the deterministic Contact block as ordinary paragraphs -
    letting python-docx handle correct placement ahead of the body's
    final sectPr, rather than hand-splicing paragraph XML.
    """

    document = DocxDocument(BytesIO(docx_bytes))

    marker_paragraph = document.add_paragraph()
    marker_run = marker_paragraph.add_run(
        DETERMINISTIC_CONTACT_BLOCK_MARKER
    )
    marker_run.bold = True

    if not contacts:
        document.add_paragraph(
            DETERMINISTIC_NO_CONTACTS_LINE
        )
    else:
        chunk_text = build_contact_chunk_content(contacts)

        for entry_index, entry in enumerate(
            chunk_text.split("\n\n")
        ):
            if entry_index > 0:
                document.add_paragraph("")

            for line in entry.split("\n"):
                document.add_paragraph(line)

    output = BytesIO()
    document.save(output)

    return output.getvalue()


def materialize_effective_docx(
    *,
    source_path: Path,
    contacts: Sequence[ExtractedContact],
) -> bytes:
    """
    Build the bytes of the effective (current) DOCX for source_path:
    its existing legal content, with every existing Contact text box
    blanked and one deterministic block reflecting `contacts` appended.

    Never writes to source_path. `contacts` may be empty - an
    originally zero-contact document, or one whose last Admin contact
    was deleted, correctly produces a downloaded DOCX with no stale
    Contact information at all.
    """

    source_bytes = source_path.read_bytes()

    with zipfile.ZipFile(BytesIO(source_bytes)) as archive:
        document_xml = archive.read(
            _DOCUMENT_XML_PART
        ).decode(
            "utf-8",
            errors="ignore",
        )

    blanked_xml = _blank_contact_text_boxes(document_xml)
    blanked_bytes = _rewrite_document_xml_part(
        source_bytes,
        blanked_xml,
    )

    return _append_deterministic_contact_block(
        blanked_bytes,
        contacts,
    )
