"""
Locate legacy floating-shape Contact boxes inside a document's raw
document.xml - used only to detect whether a document's existing
Contact area is the OLD floating-shape format (so a mutation knows
whether it needs to blank/replace one) as part of building the
canonical Admin-managed contact table (see
app.services.contact_document_area.rebuild_canonical_contact_table).

History: this module used to ALSO build an ephemeral "effective" DOCX
for every single Download request (materialize_effective_docx()),
reusing or resizing that same floating box, or falling back to
inserting plain in-flow paragraphs when no box existed. That function
was removed (mission "DOCX HARDENING", 2026-08-24): every Admin
mutation already persists its effective result into the source DOCX
atomically before returning success, so a download was never a pure
read of already-correct bytes - it silently re-derived a DIFFERENT
DOCX package on every single call (proven: two consecutive downloads
of the same unchanged document produced different SHA256 hashes), and
for any document whose Contact area is the canonical table (no
floating shape left to reuse), it unconditionally inserted a SECOND,
redundant plain-text rendering of every contact into the body -
exactly the kind of accidental duplication a "download must be a pure
byte read" invariant exists to prevent. See
docs/RELEASE_COMPATIBILITY.md's DOCX hardening section and
backend/integration_tests/docx_contact_mutation_matrix.py.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from html import unescape

from app.services.docx_parser import (
    _CONTACT_PERSON_MARKERS,
    _EMAIL_PATTERN,
    _PHONE_PATTERN,
    _TEXT_BOX_CONTENT_PATTERN,
    _TEXT_BOX_PARAGRAPH_PATTERN,
    _TEXT_BOX_TOKEN_PATTERN,
    _clean_text_box_line,
    _normalize_text,
)


def _text_box_block_lines(
    raw_block_xml: str,
) -> list[str]:
    """
    The exact same per-block line extraction
    docx_parser.extract_text_box_blocks() applies to one already
    regex-isolated <w:txbxContent> inner XML fragment - kept in sync
    with that function so classification here matches parsing there.
    Blank paragraphs are dropped, matching the legacy parser's own
    behaviour (used only for classification, never for the hidden-
    marker box's own content, which needs blank lines preserved - see
    _text_box_block_lines_preserving_blanks below).
    """

    return [
        line
        for line in _text_box_block_lines_preserving_blanks(
            raw_block_xml
        )
        if line
    ]


def _text_box_block_lines_preserving_blanks(
    raw_block_xml: str,
) -> list[str]:
    """Like _text_box_block_lines, but keeps one "" entry per blank
    paragraph instead of dropping it - needed to recognize contact
    boundaries inside the hidden-marker box, where a blank paragraph is
    the delimiter between one contact's fields and the next."""

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


@dataclass(frozen=True, slots=True)
class _TemplateRun:
    """One contact-related floating text box run, located at the raw
    document.xml string level - span is the run's own <w:r>...</w:r>
    bounds (not just its txbxContent), so the whole box (both the
    DrawingML and VML representations together) can be replaced or
    resized as one unit."""

    start: int
    end: int
    width_emu: int
    height_emu: int


_RUN_TAG_PATTERN = re.compile(
    r"<w:r(?:\s[^>]*)?/>|<w:r(?:\s[^>]*)?>|</w:r>"
)


def _find_run_span(
    document_xml: str,
    inner_txbx_start: int,
) -> tuple[int, int] | None:
    """Given the start offset of a <w:txbxContent> match, find the
    bounds of the enclosing top-level <w:r>...</w:r> run - the atomic
    unit a floating shape (with both its DrawingML Choice and VML
    Fallback copies) lives inside.

    A floating shape's own txbxContent carries its OWN nested <w:r>
    runs (one per line of the box's visible text), so the first
    "</w:r>" found after the txbxContent's start is an INNER run's
    close tag, never the enclosing run's - the span must instead be
    found by balancing nested <w:r>/</w:r> tags from the enclosing
    run's own opening tag until the matching depth-zero close.
    """

    run_start = document_xml.rfind("<w:r ", 0, inner_txbx_start)
    run_start_alt = document_xml.rfind("<w:r>", 0, inner_txbx_start)
    if run_start_alt > run_start:
        run_start = run_start_alt

    if run_start == -1:
        return None

    depth = 0
    for match in _RUN_TAG_PATTERN.finditer(document_xml, run_start):
        token = match.group(0)
        if token.endswith("/>"):
            continue
        if token == "</w:r>":
            depth -= 1
            if depth == 0:
                return run_start, match.end()
        else:
            depth += 1

    return None


def _extract_extent(run_xml: str) -> tuple[int, int] | None:
    match = re.search(
        r'<wp:extent cx="(\d+)" cy="(\d+)"', run_xml
    )

    if match is None:
        return None

    return int(match.group(1)), int(match.group(2))


def _find_all_contact_runs(
    document_xml: str,
) -> list[_TemplateRun]:
    """
    Find every distinct contact-related floating-shape run in
    document.xml - deduplicated by run span, since a run's own XML
    contains the SAME visual box twice (once inside mc:Choice for
    modern renderers, once inside mc:Fallback for legacy ones); both
    copies belong to one run and must be treated as one unit.
    """

    runs: list[_TemplateRun] = []

    for match in _TEXT_BOX_CONTENT_PATTERN.finditer(document_xml):
        # A floating shape's Choice and Fallback branches each carry
        # their own <w:txbxContent> occurrence, but both live inside
        # the SAME enclosing <w:r> - once that run's span has been
        # captured from its first (Choice) occurrence, the second
        # (Fallback) occurrence falls entirely within that span and
        # must be skipped rather than re-resolved: by the time the
        # Fallback copy is reached, Choice's own nested paragraph runs
        # sit between the enclosing run's opening tag and the
        # Fallback's txbxContent, so a fresh nearest-preceding-<w:r>
        # search would find one of those inner runs instead of the
        # true enclosing run.
        if any(run.start <= match.start() < run.end for run in runs):
            continue

        lines = _text_box_block_lines(match.group(1))

        if not _is_contact_related_block(lines):
            continue

        span = _find_run_span(document_xml, match.start())

        if span is None:
            continue

        run_xml = document_xml[span[0]:span[1]]
        extent = _extract_extent(run_xml) or (0, 0)

        runs.append(
            _TemplateRun(
                start=span[0],
                end=span[1],
                width_emu=extent[0],
                height_emu=extent[1],
            )
        )

    return runs
