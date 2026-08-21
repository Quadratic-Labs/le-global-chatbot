"""
Real, persisted-source-DOCX synchronization for one contact's photo.

The persisted source DOCX is downloadable by Admin and is what the
chatbot ultimately serves - unlike Contact business-field text (which
is only ever materialized into an EPHEMERAL copy at download time, see
document_contact_materializer.py; the persisted file itself is never
touched for a business-field edit), a contact PHOTO is required to be
a real, permanent part of the persisted document (see mission
"COMPLETE CONTACT PHOTO CRUD + DOCX SOURCE SYNCHRONIZATION", section
6): "The DOCX is not decorative backup data... photo CRUD MUST modify
the real current DOCX."

Three primitives, each locating its target through the SAME
deterministic structural rules extract_contact_photo_candidates()
already trusts (geometry, CONTACT PERSON zones, portrait-like display
ratio) - never positional/order guessing, never OCR, never facial
recognition, never an LLM, never a country-specific branch:

    replace_contact_photo_in_document - exact-SHA-256 match locates
        the contact's OWN current accepted photo; only its media bytes
        (and, if the image format changed, a Content_Types Override
        for that exact part) change - the run/relationship/geometry
        that already display it are never touched.

    remove_contact_photo_from_document - the same exact-SHA-256 match
        locates the owning run, which is removed in full (both a
        modern DrawingML copy and any legacy VML fallback copy live in
        ONE run); the relationship and media part are cleaned up too,
        but ONLY when nothing else in the document still references
        them.

    add_contact_photo_to_document - a brand-new floating image is
        anchored inside the SAME paragraph that already carries the
        target contact's own "CONTACT PERSON" zone (located by name,
        the same zone extract_contact_photo_candidates() geometrically
        associates a photo with), positioned so it passes that exact
        same geometry-association rule.

A target that cannot be located unambiguously (zero matches, or more
than one - the same "never guess" discipline
extract_contact_photo_candidates() and contact_people.py already
apply) raises ContactDocumentPhotoError instead of mutating anything.
Every function returns brand-new DOCX bytes; none of them ever writes
to source_path - committing that to disk (and rolling back on
failure) is the caller's job (app.services.admin_contact_photos),
mirroring how admin_document_sections.py's own DOCX writers
(replace_top_level_topic etc.) never touch the real source file
themselves either.
"""

from __future__ import annotations

import hashlib
import posixpath
import re
import zipfile
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Sequence

from app.services.contact_photos import (
    ContactPhotoExtractionError,
    extract_contact_photo_candidates,
)


class ContactDocumentPhotoError(RuntimeError):
    """The source DOCX could not be safely mutated for a contact
    photo - callers must guarantee zero mutation happened."""


_DOCUMENT_XML_PART = "word/document.xml"
_RELS_PART = "word/_rels/document.xml.rels"
_CONTENT_TYPES_PART = "[Content_Types].xml"

_EXTENSION_BY_CONTENT_TYPE = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
}

_TEXT_BOX_CONTENT_PATTERN = re.compile(
    r"<w:txbxContent>(.*?)</w:txbxContent>",
    re.DOTALL,
)
_TEXT_TAG_PATTERN = re.compile(
    r"<w:t[^>]*>(.*?)</w:t>",
    re.DOTALL,
)
_TAG_STRIP_PATTERN = re.compile(r"<[^>]+>")
_WHITESPACE_PATTERN = re.compile(r"\s+")


def _normalize_text(value: str) -> str:
    return _WHITESPACE_PATTERN.sub(" ", value).strip()


def _textbox_plain_text(inner_xml: str) -> str:
    return _normalize_text(
        " ".join(
            match.group(1)
            for match in _TEXT_TAG_PATTERN.finditer(inner_xml)
        )
    )


def _tag_span(
    tag_name: str,
    document_xml: str,
    inner_start: int,
) -> tuple[int, int] | None:
    """
    The (start, end) span of the <w:{tag_name}>...</w:{tag_name}>
    element enclosing a position already known to be inside it -
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

    tag_pattern = re.compile(
        rf"<w:{tag_name}(?:\s[^>]*)?/>"
        rf"|<w:{tag_name}(?:\s[^>]*)?>"
        rf"|</w:{tag_name}>"
    )
    close_token = f"</w:{tag_name}>"

    open_stack: list[int] = []

    for match in tag_pattern.finditer(document_xml, 0, inner_start):
        token = match.group(0)

        if token.endswith("/>"):
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

        if token.endswith("/>"):
            continue

        if token == close_token:
            depth -= 1

            if depth == 0:
                return start, match.end()

        else:
            depth += 1

    return None


def _run_span_for_relationship(
    document_xml: str,
    relationship_id: str,
) -> tuple[int, int] | None:
    needle = f'r:embed="{relationship_id}"'
    position = document_xml.find(needle)

    if position == -1:
        return None

    return _tag_span("r", document_xml, position)


def _relationship_targets(rels_xml: str) -> set[str]:
    return set(
        re.findall(
            r'Target="([^"]*)"',
            rels_xml,
        )
    )


def _remove_relationship_entry(
    rels_xml: str,
    relationship_id: str,
) -> str:
    pattern = re.compile(
        rf'<Relationship\s+[^>]*Id="{re.escape(relationship_id)}"'
        rf'[^>]*/>'
    )
    return pattern.sub("", rels_xml, count=1)


def _next_relationship_id(rels_xml: str) -> str:
    numbers = [
        int(match)
        for match in re.findall(r'Id="rId(\d+)"', rels_xml)
    ]
    next_number = (max(numbers) + 1) if numbers else 1
    return f"rId{next_number}"


def _next_media_filename(
    existing_names: set[str],
    extension: str,
) -> str:
    numbers = [
        int(match)
        for name in existing_names
        for match in re.findall(
            r"^image(\d+)\.[A-Za-z0-9]+$",
            posixpath.basename(name),
        )
    ]
    next_number = (max(numbers) + 1) if numbers else 1
    return f"image{next_number}.{extension}"


def _add_relationship_entry(
    rels_xml: str,
    relationship_id: str,
    target: str,
) -> str:
    entry = (
        f'<Relationship Id="{relationship_id}" '
        'Type="http://schemas.openxmlformats.org/officeDocument/'
        '2006/relationships/image" '
        f'Target="{target}"/>'
    )
    return rels_xml.replace(
        "</Relationships>",
        entry + "</Relationships>",
        1,
    )


def _ensure_default_extension(
    content_types_xml: str,
    extension: str,
    content_type: str,
) -> str:
    if f'Extension="{extension}"' in content_types_xml:
        return content_types_xml

    entry = f'<Default Extension="{extension}" ContentType="{content_type}"/>'
    return content_types_xml.replace(
        "</Types>",
        entry + "</Types>",
        1,
    )


def _ensure_content_type_override(
    content_types_xml: str,
    part_name: str,
    content_type: str,
) -> str:
    """
    Force one specific part's content type regardless of its file
    extension - the OPC-correct way to replace a JPEG with PNG bytes
    (or vice versa) at the SAME media filename, so the relationship
    and every run/blip referencing it never need to change at all
    (mission section 7A: "handle OPC relationship/content-type
    correctness safely" when the format changes on replace).
    """

    escaped_part = re.escape(part_name)
    override_pattern = re.compile(
        rf'<Override\s+PartName="{escaped_part}"[^>]*/>'
    )
    stripped = override_pattern.sub("", content_types_xml, count=1)

    entry = (
        f'<Override PartName="{part_name}" '
        f'ContentType="{content_type}"/>'
    )
    return stripped.replace(
        "</Types>",
        entry + "</Types>",
        1,
    )


def _rewrite_zip(
    source_bytes: bytes,
    *,
    replacements: dict[str, str] | None = None,
    add_parts: dict[str, bytes] | None = None,
    remove_parts: set[str] | None = None,
) -> bytes:
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
        with zipfile.ZipFile(
            output_buffer,
            "w",
            zipfile.ZIP_DEFLATED,
        ) as output_zip:
            for item in source_zip.infolist():
                if item.filename in remove_parts:
                    continue

                data = source_zip.read(item.filename)

                if item.filename in replacements:
                    replacement = replacements[item.filename]
                    data = (
                        replacement.encode("utf-8")
                        if isinstance(replacement, str)
                        else replacement
                    )

                output_zip.writestr(item, data)

            for part_name, part_bytes in add_parts.items():
                output_zip.writestr(part_name, part_bytes)

    return output_buffer.getvalue()


@dataclass(frozen=True, slots=True)
class _LocatedPhoto:
    relationship_id: str
    media_path: str


def _locate_photo_by_sha256(
    source_path: Path,
    target_sha256: str,
) -> _LocatedPhoto:
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
        raise ContactDocumentPhotoError(
            f"Could not inspect the source document for contact "
            f"photos: {error}"
        ) from error

    matches = [
        candidate
        for candidate in candidates
        if candidate.sha256 == target_sha256
    ]

    if len(matches) != 1:
        raise ContactDocumentPhotoError(
            "The contact's current photo could not be uniquely "
            "located in the source document - refusing to guess. "
            f"({len(matches)} structural matches for the expected "
            "photo.)"
        )

    return _LocatedPhoto(
        relationship_id=matches[0].relationship_id,
        media_path=matches[0].media_path,
    )


def replace_contact_photo_in_document(
    source_path: Path,
    *,
    target_sha256: str,
    new_data: bytes,
    new_content_type: str,
) -> bytes:
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

    existing_extension = posixpath.splitext(located.media_path)[1].lstrip(".").lower()
    new_extension = _EXTENSION_BY_CONTENT_TYPE.get(new_content_type)

    if new_extension is None:
        raise ContactDocumentPhotoError(
            f"Unsupported replacement content type: {new_content_type!r}."
        )

    with zipfile.ZipFile(BytesIO(source_bytes)) as archive:
        content_types_xml = archive.read(
            _CONTENT_TYPES_PART
        ).decode("utf-8", errors="ignore")

    if new_extension != existing_extension:
        part_name = "/" + located.media_path
        content_types_xml = _ensure_content_type_override(
            content_types_xml,
            part_name,
            new_content_type,
        )

    new_zip_bytes = _rewrite_zip(
        source_bytes,
        replacements={
            located.media_path: new_data,
            _CONTENT_TYPES_PART: content_types_xml,
        },
    )

    _validate_replacement(
        new_zip_bytes,
        expected_sha256=hashlib.sha256(new_data).hexdigest(),
        previous_sha256=target_sha256,
    )

    return new_zip_bytes


def remove_contact_photo_from_document(
    source_path: Path,
    *,
    target_sha256: str,
) -> bytes:
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
        document_xml = archive.read(
            _DOCUMENT_XML_PART
        ).decode("utf-8", errors="ignore")
        rels_xml = archive.read(
            _RELS_PART
        ).decode("utf-8", errors="ignore")

    run_span = _run_span_for_relationship(
        document_xml, located.relationship_id
    )

    if run_span is None:
        raise ContactDocumentPhotoError(
            "The contact's photo run could not be located for "
            "removal - refusing to guess."
        )

    new_document_xml = (
        document_xml[: run_span[0]] + document_xml[run_span[1] :]
    )

    still_referenced_in_document = (
        f'"{located.relationship_id}"' in new_document_xml
    )

    new_rels_xml = rels_xml

    if not still_referenced_in_document:
        new_rels_xml = _remove_relationship_entry(
            rels_xml, located.relationship_id
        )

    media_target = located.media_path.removeprefix("word/")
    remaining_targets = _relationship_targets(new_rels_xml)
    remove_media = media_target not in remaining_targets

    new_zip_bytes = _rewrite_zip(
        source_bytes,
        replacements={
            _DOCUMENT_XML_PART: new_document_xml,
            _RELS_PART: new_rels_xml,
        },
        remove_parts=(
            {located.media_path} if remove_media else None
        ),
    )

    _validate_removal(new_zip_bytes, removed_sha256=target_sha256)

    return new_zip_bytes


def _locate_contact_person_zone(
    document_xml: str,
    contact_person: str,
    other_contact_persons: Sequence[str] = (),
) -> tuple[int, int] | None:
    """
    The (start, end) span of the SINGLE <w:p>...</w:p> paragraph
    carrying the target person's own "CONTACT PERSON" zone - located
    by structural content (a txbxContent whose own text contains both
    the literal marker "CONTACT PERSON" and the target person's name),
    never by document-order position, so it stays correct regardless
    of how many other contacts/zones the same document has.

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
    normalized_others = [
        _normalize_text(other).casefold()
        for other in other_contact_persons
        if other and _normalize_text(other)
    ]

    seen_spans: set[tuple[int, int]] = set()
    matches: list[tuple[int, int]] = []

    for match in _TEXT_BOX_CONTENT_PATTERN.finditer(document_xml):
        text = _textbox_plain_text(match.group(1)).casefold()

        if (
            "contact person" not in text
            or normalized_person not in text
        ):
            continue

        span = _tag_span("p", document_xml, match.start())

        if span is None or span in seen_spans:
            continue

        seen_spans.add(span)

        if any(
            other in text
            for other in normalized_others
        ):
            continue

        matches.append(span)

    if len(matches) != 1:
        return None

    return matches[0]


def _build_photo_run_xml(
    *,
    relationship_id: str,
    width_emu: int,
    height_emu: int,
    position_h_emu: int,
    position_v_emu: int,
    relative_from: str,
) -> str:
    """
    A minimal, DrawingML-only floating portrait run - deliberately the
    same simple shape already found for the corpus's own simplest real
    contact photos (a bare <pic:pic><a:blip> with no VML fallback, no
    HD-Photo compatibility layer): the goal is a document Word and
    LibreOffice both render correctly, not byte-parity with every
    possible existing photo's own optional extras.
    """

    return (
        '<w:r xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
        'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
        'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
        'xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture">'
        "<w:rPr><w:noProof/></w:rPr><w:drawing>"
        '<wp:anchor distT="0" distB="0" distL="114300" distR="114300" '
        'simplePos="0" relativeHeight="251999999" behindDoc="0" '
        'locked="0" layoutInCell="1" allowOverlap="1">'
        '<wp:simplePos x="0" y="0"/>'
        f'<wp:positionH relativeFrom="{relative_from}">'
        f"<wp:posOffset>{position_h_emu}</wp:posOffset>"
        "</wp:positionH>"
        '<wp:positionV relativeFrom="paragraph">'
        f"<wp:posOffset>{position_v_emu}</wp:posOffset>"
        "</wp:positionV>"
        f'<wp:extent cx="{width_emu}" cy="{height_emu}"/>'
        '<wp:effectExtent l="0" t="0" r="0" b="0"/>'
        "<wp:wrapNone/>"
        f'<wp:docPr id="700000002" name="Contact photo {relationship_id}"/>'
        "<wp:cNvGraphicFramePr/>"
        '<a:graphic xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
        '<a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">'
        "<pic:pic>"
        "<pic:nvPicPr>"
        '<pic:cNvPr id="700000003" name="Contact photo"/>'
        "<pic:cNvPicPr/>"
        "</pic:nvPicPr>"
        "<pic:blipFill>"
        f'<a:blip r:embed="{relationship_id}"/>'
        "<a:stretch><a:fillRect/></a:stretch>"
        "</pic:blipFill>"
        "<pic:spPr>"
        '<a:xfrm><a:off x="0" y="0"/>'
        f'<a:ext cx="{width_emu}" cy="{height_emu}"/>'
        "</a:xfrm>"
        '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom>'
        "</pic:spPr>"
        "</pic:pic>"
        "</a:graphicData></a:graphic>"
        "</wp:anchor></w:drawing></w:r>"
    )


_DEFAULT_PHOTO_WIDTH_EMU = 774700
_DEFAULT_PHOTO_HEIGHT_EMU = 990600
_DEFAULT_POSITION_H_EMU = 900430
_DEFAULT_POSITION_V_EMU = 300000


def add_contact_photo_to_document(
    source_path: Path,
    *,
    contact_person: str,
    new_data: bytes,
    new_content_type: str,
    other_contact_persons: Sequence[str] = (),
) -> bytes:
    """
    Insert a brand-new photo for a contact who currently has none, as
    an additional run inside the SAME paragraph that already carries
    that person's own "CONTACT PERSON" zone (located by name - the
    same zone extract_contact_photo_candidates() geometrically
    associates a photo with). Positioned at that zone's own horizontal
    offset so the round-trip geometry-association rule accepts it by
    construction, never by luck.

    Fails closed (ContactDocumentPhotoError) when that zone cannot be
    located unambiguously, or when the document has no "CONTACT
    PERSON" zone at all for this person - never guesses a placement.
    """

    new_extension = _EXTENSION_BY_CONTENT_TYPE.get(new_content_type)

    if new_extension is None:
        raise ContactDocumentPhotoError(
            f"Unsupported photo content type: {new_content_type!r}."
        )

    source_bytes = source_path.read_bytes()

    with zipfile.ZipFile(BytesIO(source_bytes)) as archive:
        document_xml = archive.read(
            _DOCUMENT_XML_PART
        ).decode("utf-8", errors="ignore")
        rels_xml = archive.read(
            _RELS_PART
        ).decode("utf-8", errors="ignore")
        content_types_xml = archive.read(
            _CONTENT_TYPES_PART
        ).decode("utf-8", errors="ignore")
        existing_names = set(archive.namelist())

    zone_span = _locate_contact_person_zone(
        document_xml, contact_person, other_contact_persons
    )

    if zone_span is None:
        raise ContactDocumentPhotoError(
            f"Could not deterministically locate a unique CONTACT "
            f"PERSON zone for {contact_person!r} in the source "
            "document - refusing to guess a photo placement."
        )

    relationship_id = _next_relationship_id(rels_xml)
    media_filename = _next_media_filename(existing_names, new_extension)
    media_path = f"word/media/{media_filename}"

    run_xml = _build_photo_run_xml(
        relationship_id=relationship_id,
        width_emu=_DEFAULT_PHOTO_WIDTH_EMU,
        height_emu=_DEFAULT_PHOTO_HEIGHT_EMU,
        position_h_emu=_DEFAULT_POSITION_H_EMU,
        position_v_emu=_DEFAULT_POSITION_V_EMU,
        relative_from="page",
    )

    zone_start, zone_end = zone_span
    new_document_xml = (
        document_xml[:zone_end]
        + run_xml
        + document_xml[zone_end:]
    )

    new_rels_xml = _add_relationship_entry(
        rels_xml,
        relationship_id,
        target=f"media/{media_filename}",
    )

    content_type_for_extension = new_content_type
    new_content_types_xml = _ensure_default_extension(
        content_types_xml,
        new_extension,
        content_type_for_extension,
    )

    new_zip_bytes = _rewrite_zip(
        source_bytes,
        replacements={
            _DOCUMENT_XML_PART: new_document_xml,
            _RELS_PART: new_rels_xml,
            _CONTENT_TYPES_PART: new_content_types_xml,
        },
        add_parts={media_path: new_data},
    )

    _validate_addition(
        new_zip_bytes,
        expected_sha256=hashlib.sha256(new_data).hexdigest(),
    )

    return new_zip_bytes


def _write_temp_docx(data: bytes) -> Path:
    import tempfile

    file_descriptor, temp_path_str = tempfile.mkstemp(suffix=".docx")

    with open(file_descriptor, "wb") as handle:
        handle.write(data)

    return Path(temp_path_str)


def _validate_replacement(
    new_zip_bytes: bytes,
    *,
    expected_sha256: str,
    previous_sha256: str,
) -> None:
    temp_path = _write_temp_docx(new_zip_bytes)

    try:
        candidates = extract_contact_photo_candidates(temp_path)

    except ContactPhotoExtractionError as error:
        raise ContactDocumentPhotoError(
            f"The updated document could not be re-parsed for "
            f"contact photos - no change was saved: {error}"
        ) from error

    finally:
        temp_path.unlink(missing_ok=True)

    sha_values = {candidate.sha256 for candidate in candidates}

    if expected_sha256 not in sha_values:
        raise ContactDocumentPhotoError(
            "The replaced photo did not round-trip back out of the "
            "updated document - no change was saved."
        )

    if previous_sha256 in sha_values:
        raise ContactDocumentPhotoError(
            "The previous photo is still present after replacement - "
            "no change was saved."
        )


def _validate_removal(
    new_zip_bytes: bytes,
    *,
    removed_sha256: str,
) -> None:
    temp_path = _write_temp_docx(new_zip_bytes)

    try:
        candidates = extract_contact_photo_candidates(temp_path)

    except ContactPhotoExtractionError as error:
        raise ContactDocumentPhotoError(
            f"The updated document could not be re-parsed for "
            f"contact photos - no change was saved: {error}"
        ) from error

    finally:
        temp_path.unlink(missing_ok=True)

    if removed_sha256 in {candidate.sha256 for candidate in candidates}:
        raise ContactDocumentPhotoError(
            "The removed photo is still present after removal - no "
            "change was saved."
        )


def _validate_addition(
    new_zip_bytes: bytes,
    *,
    expected_sha256: str,
) -> None:
    temp_path = _write_temp_docx(new_zip_bytes)

    try:
        candidates = extract_contact_photo_candidates(temp_path)

    except ContactPhotoExtractionError as error:
        raise ContactDocumentPhotoError(
            f"The updated document could not be re-parsed for "
            f"contact photos - no change was saved: {error}"
        ) from error

    finally:
        temp_path.unlink(missing_ok=True)

    if expected_sha256 not in {candidate.sha256 for candidate in candidates}:
        raise ContactDocumentPhotoError(
            "The added photo did not round-trip back out of the "
            "updated document as an accepted contact photo - no "
            "change was saved."
        )
