"""
DOCX-native persistence for an Admin-selected country.

Mission "ORDER 8E-A1", sections 10/37: when country detection from a
document's own content fails, an Admin may select the country manually
instead of being hard-blocked. That choice must survive Download,
Reindex, and any future re-upload without any external state
(database row, `.admin-state` file, or filename convention) - the
DOCX itself is the only source of truth, exactly like every other
piece of this pipeline's metadata.

The marker is a standard OOXML custom document property
(docProps/custom.xml) - the same mechanism Word's own "File > Info >
Properties > Advanced Properties > Custom" exposes. It is invisible to
anyone reading the document's own visible content (never a fake
paragraph in the legal text) and is read back by the exact same
mechanism regardless of which application last saved the file.

python-docx 1.2.0 has no built-in support for custom properties (only
core properties - title/author/etc via Document.core_properties), so
this module manipulates docProps/custom.xml, [Content_Types].xml, and
_rels/.rels directly as a raw zip archive - mirroring the same
try/find-or-create pattern python-docx's own OpcPackage uses
internally for core properties (see OpcPackage._core_properties_part),
since no CustomPropertiesPart equivalent exists to reuse.

Writing is fully deterministic: the same source document content plus
the same target country always produces byte-identical output,
regardless of when or how many times it runs. This matters for
"already up to date" comparisons elsewhere in the upload pipeline
(see admin_document_replacement.py) - they must compare the
*normalized* candidate (the file this module produced), never raw
upload bytes, against the stored source.
"""

from __future__ import annotations

import zipfile
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree as ET

from app.core.country_registry import (
    UnknownCountryCodeError,
    canonical_country_name,
    normalize_country_code,
)


CUSTOM_PROPERTIES_PARTNAME = "docProps/custom.xml"
_CONTENT_TYPES_PARTNAME = "[Content_Types].xml"
_ROOT_RELS_PARTNAME = "_rels/.rels"

CUSTOM_PROPERTIES_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.custom-properties+xml"
)
CUSTOM_PROPERTIES_RELATIONSHIP_TYPE = (
    "http://schemas.openxmlformats.org/officeDocument/2006/"
    "relationships/custom-properties"
)

_CUSTOM_PROPS_NS = (
    "http://schemas.openxmlformats.org/officeDocument/2006/custom-properties"
)
_VT_NS = "http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes"
_CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
_RELS_NS = "http://schemas.openxmlformats.org/package/2006/relationships"

# The well-known "custom properties" format id every Office application
# writes for this property class - not a per-property identifier, the
# same constant is shared by every custom property in the part.
_CUSTOM_PROPERTY_FMTID = "{D5CDD505-2E9C-101B-9397-08002B2CF9AE}"

# pid 0 and 1 are reserved by convention (ECMA-376 Part 1, 22.3);
# the first real custom property starts at 2.
_FIRST_CUSTOM_PROPERTY_PID = 2

COUNTRY_CODE_PROPERTY_NAME = "LE Global Country Code"
COUNTRY_NAME_PROPERTY_NAME = "LE Global Country Name"

# A fixed, arbitrary point in time (the classic zip epoch) used for
# every archive entry this module writes, so two calls with the same
# logical input always produce a byte-identical archive - real wall-
# clock timestamps would otherwise make "already up to date" byte
# comparisons fail for no semantic reason.
_DETERMINISTIC_ZIP_DATE_TIME = (1980, 1, 1, 0, 0, 0)

for _prefix, _uri in (
    ("", _CUSTOM_PROPS_NS),
    ("vt", _VT_NS),
):
    ET.register_namespace(_prefix, _uri)


@dataclass(frozen=True, slots=True)
class CountryMarker:
    """One validated, DOCX-native country marker."""

    country_code: str
    country_name: str


class InvalidCountryMarkerValueError(ValueError):
    """Raised when a caller asks to write an unrecognized country code."""


def _property_element(
    parent: ET.Element,
    *,
    pid: int,
    name: str,
    value: str,
) -> ET.Element:
    element = ET.SubElement(
        parent,
        f"{{{_CUSTOM_PROPS_NS}}}property",
        {
            "fmtid": _CUSTOM_PROPERTY_FMTID,
            "pid": str(pid),
            "name": name,
        },
    )
    lpwstr = ET.SubElement(element, f"{{{_VT_NS}}}lpwstr")
    lpwstr.text = value
    return element


def _parse_existing_custom_properties(
    custom_xml: bytes | None,
) -> list[tuple[int, str, str]]:
    """
    Return every existing custom property as (pid, name, value.

    Deliberately preserves properties this module does not own (any
    custom property a Word user may have already added) - only the two
    LE Global-owned property names are ever touched by _upsert below.
    Malformed or unreadable existing content.xml is treated as absent
    rather than raised, so a document with a foreign/corrupt custom
    properties part still safely gets a fresh, well-formed one.
    """

    if not custom_xml:
        return []

    try:
        root = ET.fromstring(custom_xml)

    except ET.ParseError:
        return []

    properties: list[tuple[int, str, str]] = []

    for element in root.findall(f"{{{_CUSTOM_PROPS_NS}}}property"):
        name = element.get("name")
        pid_raw = element.get("pid")

        if not name or pid_raw is None:
            continue

        try:
            pid = int(pid_raw)

        except ValueError:
            continue

        value_element = element.find(f"{{{_VT_NS}}}lpwstr")
        value = (
            value_element.text
            if value_element is not None and value_element.text is not None
            else ""
        )

        properties.append((pid, name, value))

    return properties


def _serialize_custom_properties(
    properties: list[tuple[int, str, str]],
) -> bytes:
    """Build docProps/custom.xml deterministically from a property list."""

    root = ET.Element(f"{{{_CUSTOM_PROPS_NS}}}Properties")

    for pid, name, value in sorted(properties, key=lambda item: item[0]):
        _property_element(root, pid=pid, name=name, value=value)

    body = ET.tostring(root, encoding="unicode")

    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n'
        f"{body}"
    ).encode("utf-8")


def _upsert_custom_properties(
    custom_xml: bytes | None,
    updates: dict[str, str],
) -> bytes:
    """
    Return docProps/custom.xml content with `updates` applied.

    An existing LE Global property (matched by name) keeps its pid and
    only has its value replaced, so re-writing the same marker never
    disturbs any other property's identity. A brand-new property is
    assigned the next unused pid, in the fixed iteration order of
    `updates` - so writing the same set of updates to an empty part
    always assigns the same pids.
    """

    existing = _parse_existing_custom_properties(custom_xml)

    by_name = {name: (pid, value) for pid, name, value in existing}
    other_properties = [
        (pid, name, value)
        for pid, name, value in existing
        if name not in updates
    ]

    used_pids = {pid for pid, _, _ in existing}
    next_pid = max(used_pids, default=_FIRST_CUSTOM_PROPERTY_PID - 1) + 1
    next_pid = max(next_pid, _FIRST_CUSTOM_PROPERTY_PID)

    owned_properties: list[tuple[int, str, str]] = []

    for name, value in updates.items():
        existing_entry = by_name.get(name)

        if existing_entry is not None:
            pid, _ = existing_entry

        else:
            pid = next_pid
            next_pid += 1

        owned_properties.append((pid, name, value))

    return _serialize_custom_properties(other_properties + owned_properties)


def _ensure_content_type_override(content_types_xml: bytes) -> bytes:
    """Add the custom-properties Override, if not already declared."""

    root = ET.fromstring(content_types_xml)

    for override in root.findall(f"{{{_CT_NS}}}Override"):
        if override.get("PartName") == f"/{CUSTOM_PROPERTIES_PARTNAME}":
            return content_types_xml

    ET.SubElement(
        root,
        f"{{{_CT_NS}}}Override",
        {
            "PartName": f"/{CUSTOM_PROPERTIES_PARTNAME}",
            "ContentType": CUSTOM_PROPERTIES_CONTENT_TYPE,
        },
    )

    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n'
        + ET.tostring(root, encoding="unicode")
    ).encode("utf-8")


def _ensure_root_relationship(rels_xml: bytes) -> bytes:
    """Add the custom-properties relationship, if not already present."""

    root = ET.fromstring(rels_xml)

    existing_ids: set[int] = set()

    for relationship in root.findall(f"{{{_RELS_NS}}}Relationship"):
        if relationship.get("Type") == CUSTOM_PROPERTIES_RELATIONSHIP_TYPE:
            return rels_xml

        raw_id = relationship.get("Id", "")

        if raw_id.startswith("rId") and raw_id[3:].isdigit():
            existing_ids.add(int(raw_id[3:]))

    next_id = max(existing_ids, default=0) + 1

    ET.SubElement(
        root,
        f"{{{_RELS_NS}}}Relationship",
        {
            "Id": f"rId{next_id}",
            "Type": CUSTOM_PROPERTIES_RELATIONSHIP_TYPE,
            "Target": CUSTOM_PROPERTIES_PARTNAME,
        },
    )

    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n'
        + ET.tostring(root, encoding="unicode")
    ).encode("utf-8")


def read_country_marker(
    document_path: Path,
) -> CountryMarker | None:
    """
    Read and validate one DOCX-native country marker, or None.

    Returns None - never raises - for a document with no marker part,
    an unreadable/malformed one, or one whose country code does not
    resolve through the country registry: an invalid or unrecognized
    marker is always safely ignored, exactly like a document with no
    marker at all (mission section 11) - never trusted as arbitrary
    text, never a bypass of the confirmation this marker only persists.
    """

    try:
        with zipfile.ZipFile(document_path) as archive:
            if CUSTOM_PROPERTIES_PARTNAME not in archive.namelist():
                return None

            custom_xml = archive.read(CUSTOM_PROPERTIES_PARTNAME)

    except (OSError, zipfile.BadZipFile, KeyError):
        return None

    properties = {
        name: value
        for _, name, value in _parse_existing_custom_properties(custom_xml)
    }

    raw_code = properties.get(COUNTRY_CODE_PROPERTY_NAME)

    if not raw_code or not raw_code.strip():
        return None

    try:
        normalized_code = normalize_country_code(raw_code)

    except UnknownCountryCodeError:
        return None

    country_name = (
        properties.get(COUNTRY_NAME_PROPERTY_NAME) or ""
    ).strip() or canonical_country_name(normalized_code)

    return CountryMarker(
        country_code=normalized_code,
        country_name=country_name,
    )


def write_country_marker(
    source_path: Path,
    destination_path: Path,
    *,
    country_code: str,
    country_name: str,
) -> None:
    """
    Write `source_path`'s content to `destination_path`, with a DOCX-
    native country marker for (country_code, country_name) upserted.

    Every other part - body, styles, headers/footers, media, any
    unrelated custom property or relationship - is preserved unchanged.
    Deterministic: the same (source bytes, country_code, country_name)
    always produces the same destination bytes, so repeated writes (or
    a later independent re-parse) never disagree with each other.

    Raises InvalidCountryMarkerValueError for a country_code the
    registry does not recognize - this function never persists an
    unvalidated value.
    """

    try:
        normalized_code = normalize_country_code(country_code)

    except UnknownCountryCodeError as error:
        raise InvalidCountryMarkerValueError(
            f"Cannot persist an unrecognized country code: {country_code!r}."
        ) from error

    normalized_name = country_name.strip()

    if not normalized_name:
        raise InvalidCountryMarkerValueError(
            "country_name must not be empty."
        )

    with zipfile.ZipFile(source_path) as source_zip:
        names = [info.filename for info in source_zip.infolist()]
        contents = {name: source_zip.read(name) for name in names}

    contents[CUSTOM_PROPERTIES_PARTNAME] = _upsert_custom_properties(
        contents.get(CUSTOM_PROPERTIES_PARTNAME),
        {
            COUNTRY_CODE_PROPERTY_NAME: normalized_code,
            COUNTRY_NAME_PROPERTY_NAME: normalized_name,
        },
    )

    contents[_CONTENT_TYPES_PARTNAME] = _ensure_content_type_override(
        contents[_CONTENT_TYPES_PARTNAME]
    )
    contents[_ROOT_RELS_PARTNAME] = _ensure_root_relationship(
        contents[_ROOT_RELS_PARTNAME]
    )

    ordered_names = list(names)

    for extra_name in (
        CUSTOM_PROPERTIES_PARTNAME,
        _CONTENT_TYPES_PARTNAME,
        _ROOT_RELS_PARTNAME,
    ):
        if extra_name not in ordered_names:
            ordered_names.append(extra_name)

    destination_path.parent.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(
        destination_path, "w", zipfile.ZIP_DEFLATED
    ) as destination_zip:
        for name in ordered_names:
            info = zipfile.ZipInfo(
                filename=name,
                date_time=_DETERMINISTIC_ZIP_DATE_TIME,
            )
            info.compress_type = zipfile.ZIP_DEFLATED
            destination_zip.writestr(info, contents[name])
