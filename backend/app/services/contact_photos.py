"""Deterministic contact-photo extraction from L&E Global DOCX files.

The extractor deliberately uses Word document structure and display
geometry. It never uses OCR, facial recognition, an LLM, or country-
specific exceptions.

A missing/ambiguous photo is safer than associating the wrong image
with a legal contact.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import mimetypes
import posixpath
from pathlib import Path
import zipfile

from lxml import etree
from docx.image.image import Image


_W_NS = (
    "http://schemas.openxmlformats.org/"
    "wordprocessingml/2006/main"
)
_A_NS = (
    "http://schemas.openxmlformats.org/"
    "drawingml/2006/main"
)
_R_NS = (
    "http://schemas.openxmlformats.org/"
    "officeDocument/2006/relationships"
)
_WP_NS = (
    "http://schemas.openxmlformats.org/"
    "drawingml/2006/wordprocessingDrawing"
)
_PR_NS = (
    "http://schemas.openxmlformats.org/"
    "package/2006/relationships"
)

_NS = {
    "w": _W_NS,
    "a": _A_NS,
    "r": _R_NS,
    "wp": _WP_NS,
    "pr": _PR_NS,
}

# Empirically validated against the current 33-document corpus.
# This deliberately describes DISPLAY geometry, not source-pixel ratio.
_MIN_PORTRAIT_DISPLAY_RATIO = 0.50
_MAX_PORTRAIT_DISPLAY_RATIO = 1.25
_MIN_CONTACT_ZONE_OVERLAP = 0.50

# Word may expose compatibility copies of the same floating textbox
# with very small coordinate differences.
_CONTACT_ZONE_DEDUPE_TOLERANCE_EMU = 60_000

# Explicit descriptions Word/Office may attach to decorative content.
# Do not reject descriptions that explicitly describe a person.
_DECORATIVE_DESCRIPTION_TERMS = (
    "logo",
    "pagoda",
    "banner",
    "icon",
    "flag",
    "chart",
    "diagram",
    "map",
    "background",
    "pattern",
)

_HUMAN_DESCRIPTION_TERMS = (
    "person",
    "people",
    "man",
    "woman",
    "portrait",
    "headshot",
    "face",
    "lawyer",
    "attorney",
)


class ContactPhotoExtractionError(RuntimeError):
    """The DOCX could not be inspected safely for contact photos."""


@dataclass(frozen=True, slots=True)
class ContactPhotoCandidate:
    """One deterministically accepted contact-photo image.

    relationship_id/media_path are the image's own OPC identity within
    the source package - internal extraction detail, never surfaced
    through the Admin API (mirroring ContactRecord.photo_filename's own
    "never exposed" rule); they exist here so a later mutation
    (contact_document_photos.py) can locate the SAME accepted image
    this extractor already found, without re-deriving the geometry/
    zone rules a second time.
    """

    source_filename: str
    content_type: str
    data: bytes
    sha256: str
    reason: str
    relationship_id: str = ""
    media_path: str = ""


@dataclass(frozen=True, slots=True)
class _Geometry:
    x: float
    width: float
    height: float
    relative_h: str | None

    @property
    def x2(self) -> float:
        return self.x + self.width

    @property
    def display_ratio(self) -> float | None:
        if self.height <= 0:
            return None
        return self.width / self.height


@dataclass(frozen=True, slots=True)
class _ImageCandidate:
    relationship_id: str
    media_path: str
    geometry: _Geometry
    description: str | None
    name: str | None
    behind_document: bool
    geometry_match: bool


def _safe_xml(raw: bytes) -> etree._Element:
    parser = etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        recover=False,
    )
    return etree.fromstring(raw, parser=parser)


def _number(value: str | None) -> float | None:
    if value is None:
        return None

    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _wp_child_text(
    parent: etree._Element | None,
    tag: str,
) -> str | None:
    if parent is None:
        return None

    child = parent.find(
        f"{{{_WP_NS}}}{tag}"
    )

    return (
        child.text
        if child is not None
        else None
    )


def _anchor_geometry(
    anchor: etree._Element,
) -> _Geometry | None:
    position_h = anchor.find(
        f"{{{_WP_NS}}}positionH"
    )
    extent = anchor.find(
        f"{{{_WP_NS}}}extent"
    )

    if position_h is None or extent is None:
        return None

    x = _number(
        _wp_child_text(
            position_h,
            "posOffset",
        )
    )
    width = _number(
        extent.get("cx")
    )
    height = _number(
        extent.get("cy")
    )

    if (
        x is None
        or width is None
        or height is None
        or width <= 0
        or height <= 0
    ):
        return None

    return _Geometry(
        x=x,
        width=width,
        height=height,
        relative_h=position_h.get(
            "relativeFrom"
        ),
    )


def _node_text(node: etree._Element) -> str:
    return " ".join(
        value.strip()
        for value in node.xpath(
            ".//w:t/text()",
            namespaces=_NS,
        )
        if value.strip()
    )


def _is_contact_textbox(
    textbox: etree._Element,
) -> bool:
    normalized = " ".join(
        _node_text(textbox)
        .upper()
        .split()
    )

    # Handles both CONTACT PERSON and CONTACT PERSONS as well as
    # documents whose XML splits the final "S" into a separate run.
    return "CONTACT PERSON" in normalized


def _contact_zones(
    document_root: etree._Element,
) -> list[_Geometry]:
    zones: list[_Geometry] = []

    for textbox in document_root.xpath(
        "//w:txbxContent",
        namespaces=_NS,
    ):
        if not _is_contact_textbox(
            textbox
        ):
            continue

        anchors = textbox.xpath(
            "ancestor::wp:anchor[1]",
            namespaces=_NS,
        )

        if not anchors:
            continue

        geometry = _anchor_geometry(
            anchors[0]
        )

        if geometry is None:
            continue

        duplicate = any(
            (
                abs(
                    geometry.x
                    - existing.x
                )
                <= _CONTACT_ZONE_DEDUPE_TOLERANCE_EMU
                and abs(
                    geometry.width
                    - existing.width
                )
                <= _CONTACT_ZONE_DEDUPE_TOLERANCE_EMU
                and geometry.relative_h
                == existing.relative_h
            )
            for existing in zones
        )

        if not duplicate:
            zones.append(
                geometry
            )

    return zones


def _portrait_like(
    geometry: _Geometry,
) -> bool:
    ratio = geometry.display_ratio

    return bool(
        ratio is not None
        and _MIN_PORTRAIT_DISPLAY_RATIO
        <= ratio
        <= _MAX_PORTRAIT_DISPLAY_RATIO
    )


def _description_is_decorative(
    description: str | None,
) -> bool:
    if not description:
        return False

    normalized = description.casefold()

    if any(
        term in normalized
        for term in _HUMAN_DESCRIPTION_TERMS
    ):
        return False

    return any(
        term in normalized
        for term in _DECORATIVE_DESCRIPTION_TERMS
    )


def _geometry_matches_contact_zone(
    image: _Geometry,
    zone: _Geometry,
) -> bool:
    # Coordinates from different Word reference systems (for example
    # page vs column) cannot be compared directly.
    if (
        image.relative_h
        != zone.relative_h
    ):
        return False

    overlap = max(
        0.0,
        min(
            image.x2,
            zone.x2,
        )
        - max(
            image.x,
            zone.x,
        ),
    )

    overlap_ratio = (
        overlap / image.width
        if image.width
        else 0.0
    )

    center = (
        image.x
        + image.width / 2.0
    )

    center_inside = (
        zone.x
        <= center
        <= zone.x2
    )

    return bool(
        center_inside
        or overlap_ratio
        >= _MIN_CONTACT_ZONE_OVERLAP
    )


def _resolve_media_path(
    target: str,
) -> str | None:
    if not target:
        return None

    # document.xml lives under word/, so relationship targets are
    # resolved relative to that directory.
    normalized = posixpath.normpath(
        posixpath.join(
            "word",
            target.lstrip("/"),
        )
    )

    # A relationship must never escape the DOCX package.
    if (
        normalized == ".."
        or normalized.startswith("../")
    ):
        return None

    return normalized


def _content_type(
    blob: bytes,
    source_filename: str,
) -> str:
    try:
        image = Image.from_blob(
            blob
        )
        content_type = image.content_type

        if (
            isinstance(
                content_type,
                str,
            )
            and content_type.startswith(
                "image/"
            )
        ):
            return content_type
    except Exception:
        pass

    guessed, _ = mimetypes.guess_type(
        source_filename
    )

    if (
        guessed
        and guessed.startswith("image/")
    ):
        return guessed

    return "application/octet-stream"


def _relationship_map(
    archive: zipfile.ZipFile,
    names: set[str],
) -> dict[str, tuple[str, bool]]:
    rel_path = (
        "word/_rels/document.xml.rels"
    )

    if rel_path not in names:
        return {}

    root = _safe_xml(
        archive.read(rel_path)
    )

    relationships: dict[
        str,
        tuple[str, bool],
    ] = {}

    for relation in root.xpath(
        "./pr:Relationship",
        namespaces=_NS,
    ):
        relationship_id = relation.get(
            "Id"
        )
        relationship_type = relation.get(
            "Type",
            "",
        )
        target = relation.get(
            "Target",
            "",
        )
        external = (
            relation.get(
                "TargetMode",
                "",
            ).casefold()
            == "external"
        )

        if (
            relationship_id
            and relationship_type.endswith(
                "/image"
            )
        ):
            relationships[
                relationship_id
            ] = (
                target,
                external,
            )

    return relationships


def _collect_image_candidates(
    document_root: etree._Element,
    relationships: dict[
        str,
        tuple[str, bool],
    ],
    zones: list[_Geometry],
) -> list[_ImageCandidate]:
    candidates: list[
        _ImageCandidate
    ] = []

    for blip in document_root.xpath(
        "//a:blip[@r:embed]",
        namespaces=_NS,
    ):
        relationship_id = blip.get(
            f"{{{_R_NS}}}embed"
        )

        if not relationship_id:
            continue

        relation = relationships.get(
            relationship_id
        )

        if relation is None:
            continue

        target, external = relation

        if external:
            continue

        media_path = _resolve_media_path(
            target
        )

        if media_path is None:
            continue

        anchors = blip.xpath(
            "ancestor::wp:anchor[1]",
            namespaces=_NS,
        )

        # Current validated corpus contact portraits are floating
        # DrawingML objects. Inline/body graphics are deliberately not
        # guessed as contacts.
        if not anchors:
            continue

        anchor = anchors[0]
        geometry = _anchor_geometry(
            anchor
        )

        if geometry is None:
            continue

        docpr = anchor.find(
            f"{{{_WP_NS}}}docPr"
        )

        description = (
            docpr.get("descr")
            if docpr is not None
            else None
        )
        name = (
            docpr.get("name")
            if docpr is not None
            else None
        )

        behind_document = (
            anchor.get(
                "behindDoc",
                "",
            ).casefold()
            in {
                "1",
                "true",
            }
        )

        if (
            not _portrait_like(
                geometry
            )
            or behind_document
            or _description_is_decorative(
                description
            )
        ):
            continue

        geometry_match = any(
            _geometry_matches_contact_zone(
                geometry,
                zone,
            )
            for zone in zones
        )

        candidates.append(
            _ImageCandidate(
                relationship_id=(
                    relationship_id
                ),
                media_path=media_path,
                geometry=geometry,
                description=description,
                name=name,
                behind_document=(
                    behind_document
                ),
                geometry_match=(
                    geometry_match
                ),
            )
        )

    return candidates


def _extract_canonical_table_photo_candidates(
    file_path: Path,
) -> list[ContactPhotoCandidate] | None:
    """
    Read contact photos directly from the Admin-managed canonical
    contact table (see docx_parser.CONTACT_TABLE_HIDDEN_MARKER) - one
    ordinary INLINE picture per row's own right cell, in row order.

    Returns None (never an empty list) when no such table exists, so
    extract_contact_photo_candidates() falls back to the legacy
    floating-shape geometry heuristics below - an empty list means
    "the table exists and genuinely has no photos yet" (every row is
    photo-less).
    """

    from docx import Document as WordDocument

    from app.services.docx_parser import CONTACT_TABLE_HIDDEN_MARKER

    try:
        document = WordDocument(file_path)
    except Exception:
        return None

    marker_table = None

    for table in document.tables:
        if not table.rows:
            continue

        if CONTACT_TABLE_HIDDEN_MARKER in table.rows[0].cells[0].text:
            marker_table = table
            break

    if marker_table is None:
        return None

    result: list[ContactPhotoCandidate] = []
    seen_relationship_ids: set[str] = set()

    for row in marker_table.rows:
        if len(row.cells) < 2:
            continue

        blip = row.cells[1]._tc.find(f".//{{{_A_NS}}}blip")

        if blip is None:
            continue

        relationship_id = blip.get(f"{{{_R_NS}}}embed")

        if not relationship_id or relationship_id in seen_relationship_ids:
            continue

        seen_relationship_ids.add(relationship_id)

        try:
            image_part = document.part.related_parts[relationship_id]
        except KeyError:
            continue

        blob = image_part.blob
        source_filename = posixpath.basename(str(image_part.partname))

        result.append(
            ContactPhotoCandidate(
                source_filename=source_filename,
                content_type=_content_type(blob, source_filename),
                data=blob,
                sha256=hashlib.sha256(blob).hexdigest(),
                reason="CANONICAL_TABLE",
                relationship_id=relationship_id,
                media_path=str(image_part.partname).lstrip("/"),
            )
        )

    return result


def extract_contact_photo_candidates(
    file_path: Path,
) -> list[ContactPhotoCandidate]:
    """Return deterministically accepted contact photos in person order.

    Rules validated against the current 33-document corpus:

    * inspect body DrawingML images only;
    * require portrait-like Word display geometry;
    * reject explicitly decorative images;
    * prefer images geometrically associated with the CONTACT PERSON(S)
      zone when coordinate systems are compatible;
    * when there is no geometric match because coordinate systems differ,
      accept exactly one remaining plausible portrait;
    * never guess when multiple unmatched plausible portraits remain.

    The returned order is deterministic, left-to-right by Word display X.
    """

    path = Path(file_path)

    if not path.is_file():
        raise ContactPhotoExtractionError(
            f"DOCX file does not exist: {path}"
        )

    canonical_table_candidates = _extract_canonical_table_photo_candidates(
        path
    )

    if canonical_table_candidates is not None:
        return canonical_table_candidates

    try:
        with zipfile.ZipFile(
            path,
            "r",
        ) as archive:
            names = set(
                archive.namelist()
            )

            document_part = (
                "word/document.xml"
            )

            if document_part not in names:
                raise ContactPhotoExtractionError(
                    "DOCX has no word/document.xml part."
                )

            document_root = _safe_xml(
                archive.read(
                    document_part
                )
            )

            relationships = (
                _relationship_map(
                    archive,
                    names,
                )
            )

            zones = _contact_zones(
                document_root
            )

            plausible = (
                _collect_image_candidates(
                    document_root,
                    relationships,
                    zones,
                )
            )

            strong = [
                candidate
                for candidate in plausible
                if candidate.geometry_match
            ]

            if strong:
                selected = strong
                reason = "GEOMETRY"

            elif len(plausible) == 1:
                selected = plausible
                reason = "UNIQUE_PORTRAIT"

            else:
                # Zero candidates or an unresolved multi-image case:
                # safety wins over guessing.
                return []

            selected = sorted(
                selected,
                key=lambda candidate: (
                    candidate.geometry.x,
                    candidate.media_path,
                    candidate.relationship_id,
                ),
            )

            result: list[
                ContactPhotoCandidate
            ] = []

            seen: set[
                tuple[str, str]
            ] = set()

            for candidate in selected:
                if candidate.media_path not in names:
                    continue

                blob = archive.read(
                    candidate.media_path
                )

                digest = hashlib.sha256(
                    blob
                ).hexdigest()

                dedupe_key = (
                    candidate.media_path,
                    digest,
                )

                if dedupe_key in seen:
                    continue

                seen.add(
                    dedupe_key
                )

                source_filename = (
                    posixpath.basename(
                        candidate.media_path
                    )
                )

                result.append(
                    ContactPhotoCandidate(
                        source_filename=(
                            source_filename
                        ),
                        content_type=_content_type(
                            blob,
                            source_filename,
                        ),
                        data=blob,
                        sha256=digest,
                        reason=reason,
                        relationship_id=(
                            candidate.relationship_id
                        ),
                        media_path=candidate.media_path,
                    )
                )

            return result

    except ContactPhotoExtractionError:
        raise

    except (
        OSError,
        zipfile.BadZipFile,
        etree.XMLSyntaxError,
        KeyError,
    ) as exc:
        raise ContactPhotoExtractionError(
            f"Could not inspect contact photos in {path.name}."
        ) from exc
