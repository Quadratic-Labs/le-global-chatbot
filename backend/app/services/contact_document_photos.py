"""Consolidated service: contact_document_photos.py. Includes former document_contact_materializer.py responsibilities."""
from __future__ import annotations
import hashlib
import posixpath
import re
import zipfile
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Sequence
from app.services.contact_photos import ContactPhotoExtractionError, extract_contact_photo_candidates
from collections.abc import Sequence
from html import unescape
from app.services.docx_parser import _CONTACT_PERSON_MARKERS, _EMAIL_PATTERN, _PHONE_PATTERN, _TEXT_BOX_CONTENT_PATTERN, _TEXT_BOX_PARAGRAPH_PATTERN, _TEXT_BOX_TOKEN_PATTERN, _clean_text_box_line, _normalize_text

def _text_box_block_lines(raw_block_xml: str) -> list[str]:
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
    return [line for line in _text_box_block_lines_preserving_blanks(raw_block_xml) if line]

def _text_box_block_lines_preserving_blanks(raw_block_xml: str) -> list[str]:
    """Like _text_box_block_lines, but keeps one "" entry per blank
    paragraph instead of dropping it - needed to recognize contact
    boundaries inside the hidden-marker box, where a blank paragraph is
    the delimiter between one contact's fields and the next."""
    lines: list[str] = []
    for paragraph_xml in _TEXT_BOX_PARAGRAPH_PATTERN.findall(raw_block_xml):
        tokens = [match.group(1) if match.group(1) is not None else ', ' for match in _TEXT_BOX_TOKEN_PATTERN.finditer(paragraph_xml)]
        line = _clean_text_box_line(_normalize_text(unescape(''.join(tokens))))
        lines.append(line)
    return lines

def _is_contact_related_block(lines: Sequence[str]) -> bool:
    """
    Whether one text-box block plausibly represents part of a Contact
    card - a "CONTACT PERSON" marker block, or a firm/office block
    carrying an email or phone - the identical predicate
    parse_contact_blocks() applies inline, so a block is blanked here
    if and only if it would have contributed to the parsed contacts in
    the first place.
    """
    if any((line.strip().casefold() in _CONTACT_PERSON_MARKERS for line in lines)):
        return True
    joined = ' '.join(lines)
    return bool(_EMAIL_PATTERN.search(joined) or _PHONE_PATTERN.search(joined))

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
_RUN_TAG_PATTERN = re.compile('<w:r(?:\\s[^>]*)?/>|<w:r(?:\\s[^>]*)?>|</w:r>')

def _find_run_span(document_xml: str, inner_txbx_start: int) -> tuple[int, int] | None:
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
    run_start = document_xml.rfind('<w:r ', 0, inner_txbx_start)
    run_start_alt = document_xml.rfind('<w:r>', 0, inner_txbx_start)
    if run_start_alt > run_start:
        run_start = run_start_alt
    if run_start == -1:
        return None
    depth = 0
    for match in _RUN_TAG_PATTERN.finditer(document_xml, run_start):
        token = match.group(0)
        if token.endswith('/>'):
            continue
        if token == '</w:r>':
            depth -= 1
            if depth == 0:
                return (run_start, match.end())
        else:
            depth += 1
    return None

def _extract_extent(run_xml: str) -> tuple[int, int] | None:
    match = re.search('<wp:extent cx="(\\d+)" cy="(\\d+)"', run_xml)
    if match is None:
        return None
    return (int(match.group(1)), int(match.group(2)))

def _find_all_contact_runs(document_xml: str) -> list[_TemplateRun]:
    """
    Find every distinct contact-related floating-shape run in
    document.xml - deduplicated by run span, since a run's own XML
    contains the SAME visual box twice (once inside mc:Choice for
    modern renderers, once inside mc:Fallback for legacy ones); both
    copies belong to one run and must be treated as one unit.
    """
    runs: list[_TemplateRun] = []
    for match in _TEXT_BOX_CONTENT_PATTERN.finditer(document_xml):
        if any((run.start <= match.start() < run.end for run in runs)):
            continue
        lines = _text_box_block_lines(match.group(1))
        if not _is_contact_related_block(lines):
            continue
        span = _find_run_span(document_xml, match.start())
        if span is None:
            continue
        run_xml = document_xml[span[0]:span[1]]
        extent = _extract_extent(run_xml) or (0, 0)
        runs.append(_TemplateRun(start=span[0], end=span[1], width_emu=extent[0], height_emu=extent[1]))
    return runs

class ContactDocumentPhotoError(RuntimeError):
    """The source DOCX could not be safely mutated for a contact
    photo - callers must guarantee zero mutation happened."""
_DOCUMENT_XML_PART = 'word/document.xml'
_RELS_PART = 'word/_rels/document.xml.rels'
_CONTENT_TYPES_PART = '[Content_Types].xml'
_EXTENSION_BY_CONTENT_TYPE = {'image/jpeg': 'jpg', 'image/png': 'png', 'image/webp': 'webp'}
_TEXT_BOX_CONTENT_PATTERN = re.compile('<w:txbxContent>(.*?)</w:txbxContent>', re.DOTALL)
_TEXT_TAG_PATTERN = re.compile('<w:t[^>]*>(.*?)</w:t>', re.DOTALL)
_TAG_STRIP_PATTERN = re.compile('<[^>]+>')
_WHITESPACE_PATTERN = re.compile('\\s+')

def _normalize_text(value: str) -> str:
    return _WHITESPACE_PATTERN.sub(' ', value).strip()

def _textbox_plain_text(inner_xml: str) -> str:
    return _normalize_text(' '.join((match.group(1) for match in _TEXT_TAG_PATTERN.finditer(inner_xml))))

def _tag_span(tag_name: str, document_xml: str, inner_start: int) -> tuple[int, int] | None:
    """
    The (start, end) span of the <{tag_name}>...</{tag_name}>
    element enclosing a position already known to be inside it -
    tag_name is the FULL, namespace-qualified tag (e.g. "w:p", "w:r",
    "wp:anchor") -
    balancing nested same-named tags rather than trusting the first
    closing tag found, since a floating shape's own txbxContent
    legitimately nests further <w:r>/<w:p> elements of its own (the
    exact same reasoning already proven against the real corpus in
    document_contact_materializer.py's _find_run_span).

    Finding the enclosing open tag is NOT simply "the nearest preceding
    open tag": a textbox can contain its own fully self-closed sibling
    elements (e.g. a trailing empty <w:p/> right before the txbxContent
    that holds it closes) before inner_start, and the nearest preceding
    open tag textually would then be that already-closed sibling, not
    the true ancestor. A real DE source document proved this: the
    mc:Choice copy of a "CONTACT PERSON" textbox ends in a self-
    contained trailing empty paragraph, and naively rfind-ing the
    nearest preceding "<w:p " from the mc:Fallback copy's own
    txbxContent landed on that already-closed sibling instead of the
    actual enclosing paragraph. Tracking a stack of currently-open tags
    while scanning forward from the top of the document is the only way
    to know which open tag is still unclosed at inner_start.
    """
    tag_pattern = re.compile(f'<{tag_name}(?:\\s[^>]*)?/>|<{tag_name}(?:\\s[^>]*)?>|</{tag_name}>')
    close_token = f'</{tag_name}>'
    open_stack: list[int] = []
    for match in tag_pattern.finditer(document_xml, 0, inner_start):
        token = match.group(0)
        if token.endswith('/>'):
            continue
        if token == close_token:
            if open_stack:
                open_stack.pop()
        else:
            open_stack.append(match.start())
    if not open_stack:
        return None
    start = open_stack[-1]
    depth = 0
    for match in tag_pattern.finditer(document_xml, start):
        token = match.group(0)
        if token.endswith('/>'):
            continue
        if token == close_token:
            depth -= 1
            if depth == 0:
                return (start, match.end())
        else:
            depth += 1
    return None

def _run_span_for_relationship(document_xml: str, relationship_id: str) -> tuple[int, int] | None:
    needle = f'r:embed="{relationship_id}"'
    position = document_xml.find(needle)
    if position == -1:
        return None
    return _tag_span('w:r', document_xml, position)

def _relationship_targets(rels_xml: str) -> set[str]:
    return set(re.findall('Target="([^"]*)"', rels_xml))

def _remove_relationship_entry(rels_xml: str, relationship_id: str) -> str:
    pattern = re.compile(f'<Relationship\\s+[^>]*Id="{re.escape(relationship_id)}"[^>]*/>')
    return pattern.sub('', rels_xml, count=1)

def _next_relationship_id(rels_xml: str) -> str:
    numbers = [int(match) for match in re.findall('Id="rId(\\d+)"', rels_xml)]
    next_number = max(numbers) + 1 if numbers else 1
    return f'rId{next_number}'

def _next_media_filename(existing_names: set[str], extension: str) -> str:
    numbers = [int(match) for name in existing_names for match in re.findall('^image(\\d+)\\.[A-Za-z0-9]+$', posixpath.basename(name))]
    next_number = max(numbers) + 1 if numbers else 1
    return f'image{next_number}.{extension}'

def _add_relationship_entry(rels_xml: str, relationship_id: str, target: str) -> str:
    entry = f'<Relationship Id="{relationship_id}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="{target}"/>'
    return rels_xml.replace('</Relationships>', entry + '</Relationships>', 1)

def _ensure_default_extension(content_types_xml: str, extension: str, content_type: str) -> str:
    if f'Extension="{extension}"' in content_types_xml:
        return content_types_xml
    entry = f'<Default Extension="{extension}" ContentType="{content_type}"/>'
    return content_types_xml.replace('</Types>', entry + '</Types>', 1)

def _ensure_content_type_override(content_types_xml: str, part_name: str, content_type: str) -> str:
    """
    Force one specific part's content type regardless of its file
    extension - the OPC-correct way to replace a JPEG with PNG bytes
    (or vice versa) at the SAME media filename, so the relationship
    and every run/blip referencing it never need to change at all
    (mission section 7A: "handle OPC relationship/content-type
    correctness safely" when the format changes on replace).
    """
    escaped_part = re.escape(part_name)
    override_pattern = re.compile(f'<Override\\s+PartName="{escaped_part}"[^>]*/>')
    stripped = override_pattern.sub('', content_types_xml, count=1)
    entry = f'<Override PartName="{part_name}" ContentType="{content_type}"/>'
    return stripped.replace('</Types>', entry + '</Types>', 1)

def _rewrite_zip(source_bytes: bytes, *, replacements: dict[str, str] | None=None, add_parts: dict[str, bytes] | None=None, remove_parts: set[str] | None=None) -> bytes:
    """
    Copy every zip entry unchanged except the explicitly named
    replacements/removals/additions - never regenerates the package
    from scratch, so every untouched part (styles, other media,
    relationships for OTHER parts, custom XML) survives byte-for-byte
    (mirrors document_contact_materializer.py's own
    _rewrite_document_xml_part).
    """
    replacements = replacements or {}
    remove_parts = remove_parts or set()
    add_parts = add_parts or {}
    output_buffer = BytesIO()
    with zipfile.ZipFile(BytesIO(source_bytes)) as source_zip:
        with zipfile.ZipFile(output_buffer, 'w', zipfile.ZIP_DEFLATED) as output_zip:
            for item in source_zip.infolist():
                if item.filename in remove_parts:
                    continue
                data = source_zip.read(item.filename)
                if item.filename in replacements:
                    replacement = replacements[item.filename]
                    data = replacement.encode('utf-8') if isinstance(replacement, str) else replacement
                output_zip.writestr(item, data)
            for part_name, part_bytes in add_parts.items():
                output_zip.writestr(part_name, part_bytes)
    return output_buffer.getvalue()

@dataclass(frozen=True, slots=True)
class _LocatedPhoto:
    relationship_id: str
    media_path: str

def _locate_photo_by_sha256(source_path: Path, target_sha256: str) -> _LocatedPhoto:
    """
    The one accepted contact-photo candidate (the SAME structural
    rules extract_contact_photo_candidates() already applies) whose
    CURRENT media bytes hash to target_sha256 - the exact identity a
    persisted ContactRecord.photo_sha256 already carries. Never
    guesses: a sha256 that matches zero or more than one accepted
    candidate fails closed.
    """
    try:
        candidates = extract_contact_photo_candidates(source_path)
    except ContactPhotoExtractionError as error:
        raise ContactDocumentPhotoError(f'Could not inspect the source document for contact photos: {error}') from error
    matches = [candidate for candidate in candidates if candidate.sha256 == target_sha256]
    if len(matches) != 1:
        raise ContactDocumentPhotoError(f"The contact's current photo could not be uniquely located in the source document - refusing to guess. ({len(matches)} structural matches for the expected photo.)")
    return _LocatedPhoto(relationship_id=matches[0].relationship_id, media_path=matches[0].media_path)

def replace_contact_photo_in_document(source_path: Path, *, target_sha256: str, new_data: bytes, new_content_type: str) -> bytes:
    """
    Swap the contact's own CURRENT photo (located by its exact
    persisted SHA-256, never by position/order) for new_data, in
    place - preserving the existing run/relationship/geometry exactly.
    Same content type: only the media bytes at the SAME part name
    change (zero XML touched at all). Different content type (JPEG
    <-> PNG <-> WebP): a Content_Types Override forces the correct
    type for that SAME part name, still without touching the
    relationship or any run/blip.
    """
    located = _locate_photo_by_sha256(source_path, target_sha256)
    source_bytes = source_path.read_bytes()
    existing_extension = posixpath.splitext(located.media_path)[1].lstrip('.').lower()
    new_extension = _EXTENSION_BY_CONTENT_TYPE.get(new_content_type)
    if new_extension is None:
        raise ContactDocumentPhotoError(f'Unsupported replacement content type: {new_content_type!r}.')
    with zipfile.ZipFile(BytesIO(source_bytes)) as archive:
        content_types_xml = archive.read(_CONTENT_TYPES_PART).decode('utf-8', errors='ignore')
    if new_extension != existing_extension:
        part_name = '/' + located.media_path
        content_types_xml = _ensure_content_type_override(content_types_xml, part_name, new_content_type)
    new_zip_bytes = _rewrite_zip(source_bytes, replacements={located.media_path: new_data, _CONTENT_TYPES_PART: content_types_xml})
    _validate_replacement(new_zip_bytes, expected_sha256=hashlib.sha256(new_data).hexdigest(), previous_sha256=target_sha256)
    return new_zip_bytes

def remove_contact_photo_from_document(source_path: Path, *, target_sha256: str) -> bytes:
    """
    Remove ONLY the run carrying the contact's own current photo
    (located by its exact persisted SHA-256). The relationship entry
    and the media part itself are cleaned up too, but strictly only
    when nothing else in the resulting document still references
    them - other contacts' photos, logos, and covers are never
    touched.
    """
    located = _locate_photo_by_sha256(source_path, target_sha256)
    source_bytes = source_path.read_bytes()
    with zipfile.ZipFile(BytesIO(source_bytes)) as archive:
        document_xml = archive.read(_DOCUMENT_XML_PART).decode('utf-8', errors='ignore')
        rels_xml = archive.read(_RELS_PART).decode('utf-8', errors='ignore')
    run_span = _run_span_for_relationship(document_xml, located.relationship_id)
    if run_span is None:
        raise ContactDocumentPhotoError("The contact's photo run could not be located for removal - refusing to guess.")
    new_document_xml = document_xml[:run_span[0]] + document_xml[run_span[1]:]
    still_referenced_in_document = f'"{located.relationship_id}"' in new_document_xml
    new_rels_xml = rels_xml
    if not still_referenced_in_document:
        new_rels_xml = _remove_relationship_entry(rels_xml, located.relationship_id)
    media_target = located.media_path.removeprefix('word/')
    remaining_targets = _relationship_targets(new_rels_xml)
    remove_media = media_target not in remaining_targets
    new_zip_bytes = _rewrite_zip(source_bytes, replacements={_DOCUMENT_XML_PART: new_document_xml, _RELS_PART: new_rels_xml}, remove_parts={located.media_path} if remove_media else None)
    _validate_removal(new_zip_bytes, removed_sha256=target_sha256)
    return new_zip_bytes

@dataclass(frozen=True, slots=True)
class _LocatedZone:
    paragraph_span: tuple[int, int]
    run_span: tuple[int, int]

def _locate_contact_person_zone(document_xml: str, contact_person: str, other_contact_persons: Sequence[str]=()) -> _LocatedZone | None:
    """
    The target person's own "CONTACT PERSON" zone - located by
    structural content (a txbxContent whose own text contains both
    the literal marker "CONTACT PERSON" and the target person's name),
    never by document-order position, so it stays correct regardless
    of how many other contacts/zones the same document has. Returns
    both the enclosing PARAGRAPH span (where a new run textually
    belongs) and the enclosing RUN span (the shape's own <w:r>, both
    its DrawingML Choice and legacy VML Fallback copies together -
    used to read the zone's own real anchor geometry, so a new photo
    can be positioned to genuinely overlap it rather than guessing a
    fixed offset).

    Deduplicates by enclosing PARAGRAPH span, not by regex match: a
    floating shape's modern DrawingML copy and its legacy VML fallback
    copy each produce their own separate <w:txbxContent> regex match
    for the exact same visual zone (the same reasoning already proven
    in document_contact_materializer.py's own run-dedup fix), so
    counting raw matches would see a real single-person document as
    "ambiguous" every time.

    A zone whose own text ALSO names another contact (Belgium-style: a
    single "CONTACT PERSON(S)" block naming two people, disambiguated
    only by two SEPARATE photos' own geometry, never by two separate
    zones) is deliberately excluded rather than guessed at - inserting
    a new photo into a zone shared with someone else's name has no
    safe, deterministic place to put it. None also when the match is
    not unique after that filter (zero or more than one remaining) -
    callers must fail closed rather than guess in either case.
    """
    normalized_person = _normalize_text(contact_person).casefold()
    normalized_others = [_normalize_text(other).casefold() for other in other_contact_persons if other and _normalize_text(other)]
    seen_spans: set[tuple[int, int]] = set()
    matches: list[_LocatedZone] = []
    for match in _TEXT_BOX_CONTENT_PATTERN.finditer(document_xml):
        text = _textbox_plain_text(match.group(1)).casefold()
        if 'contact person' not in text or normalized_person not in text:
            continue
        paragraph_span = _tag_span('w:p', document_xml, match.start())
        if paragraph_span is None or paragraph_span in seen_spans:
            continue
        seen_spans.add(paragraph_span)
        if any((other in text for other in normalized_others)):
            continue
        run_span = _tag_span('w:r', document_xml, match.start())
        if run_span is None:
            continue
        matches.append(_LocatedZone(paragraph_span=paragraph_span, run_span=run_span))
    if len(matches) != 1:
        return None
    return matches[0]

@dataclass(frozen=True, slots=True)
class _AnchorGeometry:
    relative_from: str
    x_emu: int
    width_emu: int
_POSITION_H_PATTERN = re.compile('<wp:positionH\\s+relativeFrom="([^"]*)"\\s*>\\s*<wp:posOffset>(-?\\d+)</wp:posOffset>')
_EXTENT_PATTERN = re.compile('<wp:extent\\s+cx="(\\d+)"\\s+cy="(\\d+)"')

def _anchor_geometry_in_span(document_xml: str, span: tuple[int, int]) -> _AnchorGeometry | None:
    """
    The real horizontal position/width extract_contact_photo_
    candidates() itself reads (contact_photos.py's own
    _anchor_geometry()) for the ONE floating shape occupying this
    exact span - read directly from the raw XML slice a caller has
    already isolated (e.g. via _tag_span), never a second full-
    document lxml parse. None when no wp:anchor positionH/extent can
    be found there at all - callers must fail closed rather than
    guess a placement from an unknown geometry.
    """
    span_xml = document_xml[span[0]:span[1]]
    position_match = _POSITION_H_PATTERN.search(span_xml)
    extent_match = _EXTENT_PATTERN.search(span_xml)
    if position_match is None or extent_match is None:
        return None
    return _AnchorGeometry(relative_from=position_match.group(1), x_emu=int(position_match.group(2)), width_emu=int(extent_match.group(1)))

def _centered_position_h_emu(zone: _AnchorGeometry, photo_width_emu: int) -> int:
    """
    A horizontal offset that puts the new photo's own center exactly
    at the target zone's own center - contact_photos.py's own
    _geometry_matches_contact_zone() accepts a match whenever the
    image's center falls anywhere inside [zone.x, zone.x2], so this
    is satisfied by construction regardless of the zone's or the
    photo's own relative width, never a fixed generic guess.
    """
    return zone.x_emu + zone.width_emu // 2 - photo_width_emu // 2

def _build_photo_run_xml(*, relationship_id: str, width_emu: int, height_emu: int, position_h_emu: int, position_v_emu: int, relative_from: str, doc_pr_id: str='700000002', cnv_pr_id: str='700000003', relative_height: str='251999999') -> str:
    """
    A minimal, DrawingML-only floating portrait run - deliberately the
    same simple shape already found for the corpus's own simplest real
    contact photos (a bare <pic:pic><a:blip> with no VML fallback, no
    HD-Photo compatibility layer): the goal is a document Word and
    LibreOffice both render correctly, not byte-parity with every
    possible existing photo's own optional extras.

    doc_pr_id/cnv_pr_id/relative_height default to the original fixed
    values (existing callers only ever add ONE new photo per document
    at a time, so a fixed value has never collided) - a caller adding
    a photo to a document that may already contain another freshly-
    added photo (contact_document_area.py, adding a second-or-later
    contact) must pass document-wide-unique values instead, since
    wp:docPr/pic:cNvPr ids AND relativeHeight z-order values are all
    required to be unique within the document.
    """
    return f'<w:r xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture"><w:rPr><w:noProof/></w:rPr><w:drawing><wp:anchor distT="0" distB="0" distL="114300" distR="114300" simplePos="0" relativeHeight="{relative_height}" behindDoc="0" locked="0" layoutInCell="1" allowOverlap="1"><wp:simplePos x="0" y="0"/><wp:positionH relativeFrom="{relative_from}"><wp:posOffset>{position_h_emu}</wp:posOffset></wp:positionH><wp:positionV relativeFrom="paragraph"><wp:posOffset>{position_v_emu}</wp:posOffset></wp:positionV><wp:extent cx="{width_emu}" cy="{height_emu}"/><wp:effectExtent l="0" t="0" r="0" b="0"/><wp:wrapNone/><wp:docPr id="{doc_pr_id}" name="Contact photo {relationship_id}"/><wp:cNvGraphicFramePr/><a:graphic xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture"><pic:pic><pic:nvPicPr><pic:cNvPr id="{cnv_pr_id}" name="Contact photo"/><pic:cNvPicPr/></pic:nvPicPr><pic:blipFill><a:blip r:embed="{relationship_id}"/><a:stretch><a:fillRect/></a:stretch></pic:blipFill><pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="{width_emu}" cy="{height_emu}"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr></pic:pic></a:graphicData></a:graphic></wp:anchor></w:drawing></w:r>'
_DEFAULT_PHOTO_WIDTH_EMU = 774700
_DEFAULT_PHOTO_HEIGHT_EMU = 990600
_DEFAULT_POSITION_H_EMU = 900430
_DEFAULT_POSITION_V_EMU = 300000

def add_contact_photo_to_document(source_path: Path, *, contact_person: str, new_data: bytes, new_content_type: str, other_contact_persons: Sequence[str]=()) -> bytes:
    """
    Insert a brand-new photo for a contact who currently has none, as
    an additional run right after the SAME paragraph that already
    carries that person's own "CONTACT PERSON" zone (located by name).
    Positioned at that zone's own REAL, computed horizontal offset
    (read directly from its own <wp:anchor>, never a fixed generic
    guess) so the round-trip geometry-association rule
    extract_contact_photo_candidates() applies is satisfied by
    construction - required for correctness the moment a document
    has more than one photo total, since the extractor's fallback
    ("exactly one remaining plausible portrait") only ever helps when
    there is truly just one unassociated photo in the whole document.

    Fails closed (ContactDocumentPhotoError) when that zone cannot be
    located unambiguously, when the document has no "CONTACT PERSON"
    zone at all for this person, or when that zone's own anchor
    geometry cannot be determined - never guesses a placement.
    """
    new_extension = _EXTENSION_BY_CONTENT_TYPE.get(new_content_type)
    if new_extension is None:
        raise ContactDocumentPhotoError(f'Unsupported photo content type: {new_content_type!r}.')
    source_bytes = source_path.read_bytes()
    with zipfile.ZipFile(BytesIO(source_bytes)) as archive:
        document_xml = archive.read(_DOCUMENT_XML_PART).decode('utf-8', errors='ignore')
        rels_xml = archive.read(_RELS_PART).decode('utf-8', errors='ignore')
        content_types_xml = archive.read(_CONTENT_TYPES_PART).decode('utf-8', errors='ignore')
        existing_names = set(archive.namelist())
    located_zone = _locate_contact_person_zone(document_xml, contact_person, other_contact_persons)
    if located_zone is None:
        raise ContactDocumentPhotoError(f'Could not deterministically locate a unique CONTACT PERSON zone for {contact_person!r} in the source document - refusing to guess a photo placement.')
    zone_geometry = _anchor_geometry_in_span(document_xml, located_zone.run_span)
    if zone_geometry is None:
        raise ContactDocumentPhotoError(f'The CONTACT PERSON zone found for {contact_person!r} has no determinable anchor geometry - refusing to guess a photo placement.')
    relationship_id = _next_relationship_id(rels_xml)
    media_filename = _next_media_filename(existing_names, new_extension)
    media_path = f'word/media/{media_filename}'
    run_xml = _build_photo_run_xml(relationship_id=relationship_id, width_emu=_DEFAULT_PHOTO_WIDTH_EMU, height_emu=_DEFAULT_PHOTO_HEIGHT_EMU, position_h_emu=_centered_position_h_emu(zone_geometry, _DEFAULT_PHOTO_WIDTH_EMU), position_v_emu=_DEFAULT_POSITION_V_EMU, relative_from=zone_geometry.relative_from)
    zone_end = located_zone.paragraph_span[1]
    new_document_xml = document_xml[:zone_end] + run_xml + document_xml[zone_end:]
    new_rels_xml = _add_relationship_entry(rels_xml, relationship_id, target=f'media/{media_filename}')
    content_type_for_extension = new_content_type
    new_content_types_xml = _ensure_default_extension(content_types_xml, new_extension, content_type_for_extension)
    new_zip_bytes = _rewrite_zip(source_bytes, replacements={_DOCUMENT_XML_PART: new_document_xml, _RELS_PART: new_rels_xml, _CONTENT_TYPES_PART: new_content_types_xml}, add_parts={media_path: new_data})
    _validate_addition(new_zip_bytes, expected_sha256=hashlib.sha256(new_data).hexdigest())
    return new_zip_bytes

def add_new_contact_photo_to_document(source_path: Path, *, new_data: bytes, new_content_type: str) -> bytes:
    """
    Anchor a BRAND-NEW contact's first photo - one who, by definition,
    has no existing "CONTACT PERSON" zone anywhere in the document
    yet, since they were just created - to the document's own largest
    EXISTING "CONTACT PERSON" zone (by the same width*height
    criterion document_contact_materializer.py's own single-contact
    download path already uses to pick its "primary" run - but
    filtered first to contact_photos.py's own narrower zone
    definition specifically, "CONTACT PERSON" text, not merely
    document_contact_materializer.py's broader "email or phone
    pattern" contact-related-block test: a firm/address box matches
    the latter but is never a zone extract_contact_photo_candidates()
    itself would recognize, so anchoring to one would never actually
    satisfy the round-trip check - confirmed directly against the
    real Argentina corpus while building this). This is never a
    name-based lookup (mission "FINAL BLOCKER", section 3):
    add_contact_photo_to_document() above locates an EXISTING zone
    that already names the target contact; a genuinely new contact's
    name cannot possibly appear anywhere in the document yet, so there
    is no "the right zone for this person" to search for.

    A photo's association with a specific ContactRecord is always
    through ContactRecord.photo_sha256 in ContactState - never through
    a zone's own text (replace/remove above locate an EXISTING photo
    by exact SHA-256, not by name either) - so the new photo does not
    need its own dedicated "CONTACT PERSON <name>" text to be
    correctly served to the public chatbot or found on the next
    Admin GET; it only needs SOME accepted contact-related zone to
    geometrically anchor to, with that zone's own REAL position read
    directly from its anchor (never a fixed guess), so
    extract_contact_photo_candidates() accepts it by construction even
    when the document already has one or more OTHER photos.

    Fails closed (ContactDocumentPhotoError) when the document has no
    contact-related textbox at all to anchor to, or when that zone's
    own geometry cannot be determined - never constructs one from
    scratch and never inserts at an arbitrary position such as the
    document's end (mission section 6).
    """
    new_extension = _EXTENSION_BY_CONTENT_TYPE.get(new_content_type)
    if new_extension is None:
        raise ContactDocumentPhotoError(f'Unsupported photo content type: {new_content_type!r}.')
    source_bytes = source_path.read_bytes()
    with zipfile.ZipFile(BytesIO(source_bytes)) as archive:
        document_xml = archive.read(_DOCUMENT_XML_PART).decode('utf-8', errors='ignore')
        rels_xml = archive.read(_RELS_PART).decode('utf-8', errors='ignore')
        content_types_xml = archive.read(_CONTENT_TYPES_PART).decode('utf-8', errors='ignore')
        existing_names = set(archive.namelist())
    contact_person_runs = [run for run in _find_all_contact_runs(document_xml) if 'contact person' in _textbox_plain_text(document_xml[run.start:run.end]).casefold()]
    if not contact_person_runs:
        raise ContactDocumentPhotoError("This document has no existing CONTACT PERSON zone to anchor a new contact's photo to - refusing to insert one at an arbitrary position.")
    primary_run = max(contact_person_runs, key=lambda run: run.width_emu * run.height_emu)
    zone_geometry = _anchor_geometry_in_span(document_xml, (primary_run.start, primary_run.end))
    if zone_geometry is None:
        raise ContactDocumentPhotoError("The document's own contact area has no determinable anchor geometry - refusing to guess a photo placement.")
    relationship_id = _next_relationship_id(rels_xml)
    media_filename = _next_media_filename(existing_names, new_extension)
    media_path = f'word/media/{media_filename}'
    run_xml = _build_photo_run_xml(relationship_id=relationship_id, width_emu=_DEFAULT_PHOTO_WIDTH_EMU, height_emu=_DEFAULT_PHOTO_HEIGHT_EMU, position_h_emu=_centered_position_h_emu(zone_geometry, _DEFAULT_PHOTO_WIDTH_EMU), position_v_emu=_DEFAULT_POSITION_V_EMU, relative_from=zone_geometry.relative_from)
    insertion_point = primary_run.end
    new_document_xml = document_xml[:insertion_point] + run_xml + document_xml[insertion_point:]
    new_rels_xml = _add_relationship_entry(rels_xml, relationship_id, target=f'media/{media_filename}')
    new_content_types_xml = _ensure_default_extension(content_types_xml, new_extension, new_content_type)
    new_zip_bytes = _rewrite_zip(source_bytes, replacements={_DOCUMENT_XML_PART: new_document_xml, _RELS_PART: new_rels_xml, _CONTENT_TYPES_PART: new_content_types_xml}, add_parts={media_path: new_data})
    _validate_addition(new_zip_bytes, expected_sha256=hashlib.sha256(new_data).hexdigest())
    return new_zip_bytes

def _write_temp_docx(data: bytes) -> Path:
    import tempfile
    file_descriptor, temp_path_str = tempfile.mkstemp(suffix='.docx')
    with open(file_descriptor, 'wb') as handle:
        handle.write(data)
    return Path(temp_path_str)

def _validate_replacement(new_zip_bytes: bytes, *, expected_sha256: str, previous_sha256: str) -> None:
    temp_path = _write_temp_docx(new_zip_bytes)
    try:
        candidates = extract_contact_photo_candidates(temp_path)
    except ContactPhotoExtractionError as error:
        raise ContactDocumentPhotoError(f'The updated document could not be re-parsed for contact photos - no change was saved: {error}') from error
    finally:
        temp_path.unlink(missing_ok=True)
    sha_values = {candidate.sha256 for candidate in candidates}
    if expected_sha256 not in sha_values:
        raise ContactDocumentPhotoError('The replaced photo did not round-trip back out of the updated document - no change was saved.')
    if previous_sha256 in sha_values:
        raise ContactDocumentPhotoError('The previous photo is still present after replacement - no change was saved.')

def _validate_removal(new_zip_bytes: bytes, *, removed_sha256: str) -> None:
    temp_path = _write_temp_docx(new_zip_bytes)
    try:
        candidates = extract_contact_photo_candidates(temp_path)
    except ContactPhotoExtractionError as error:
        raise ContactDocumentPhotoError(f'The updated document could not be re-parsed for contact photos - no change was saved: {error}') from error
    finally:
        temp_path.unlink(missing_ok=True)
    if removed_sha256 in {candidate.sha256 for candidate in candidates}:
        raise ContactDocumentPhotoError('The removed photo is still present after removal - no change was saved.')

def _validate_addition(new_zip_bytes: bytes, *, expected_sha256: str) -> None:
    temp_path = _write_temp_docx(new_zip_bytes)
    try:
        candidates = extract_contact_photo_candidates(temp_path)
    except ContactPhotoExtractionError as error:
        raise ContactDocumentPhotoError(f'The updated document could not be re-parsed for contact photos - no change was saved: {error}') from error
    finally:
        temp_path.unlink(missing_ok=True)
    if expected_sha256 not in {candidate.sha256 for candidate in candidates}:
        raise ContactDocumentPhotoError('The added photo did not round-trip back out of the updated document as an accepted contact photo - no change was saved.')
