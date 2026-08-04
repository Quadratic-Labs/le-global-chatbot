import hashlib
import re
import zipfile
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from docx import Document

from app.services.section_splitter import (
    split_parsed_sections,
)
from app.core.country_registry import (
    resolve_country,
)
from app.core.country_registry import (
    UnknownCountryNameError,
)
from app.core.legal_taxonomy import (
    get_canonical_legal_topic,
    is_overview_section,
)
from app.core.subsection_taxonomy import (
    get_subsection_topic_override,
)
from app.models.document import DocumentChunk
from app.services.docx_parser import (
    ParsedSection,
    build_contact_chunk_content,
    extract_contacts_from_docx,
    parse_docx_sections,
)


CONTACT_SUBSECTION: Final[str] = "Contact"

# The one document family/type this pipeline currently supports - see
# metadata_from_content's own docstring. A constant, not a taxonomy:
# document identity/storage keys off it precisely so a future second
# family would need one deliberate new constant here, never a
# per-document guess. Public (no leading underscore) because the
# admin upload response also surfaces it for traceability.
DOCUMENT_FAMILY: Final[str] = "employment-law-overview"

# Title/cover structures this pipeline recognizes as *this* document
# family - reused from the filename-naming convention this same
# product has always used, now matched against the document's own
# content instead of its filename (mission "CONTINUATION PATCH
# 0.4.3"): the filename itself no longer determines country, year,
# document type, or replacement - see metadata_from_content.
_TITLE_LINE_PATTERNS: Final[
    tuple[re.Pattern[str], ...]
] = (
    re.compile(
        r"^Labour and Employment Law in\s+"
        r"(?P<country>.+?)"
        r"(?:\s+(?P<year>(?:19|20)\d{2}))?$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^Employment Law Overview"
        r"(?:\s*-\s*|\s+)"
        r"(?P<country>.+?)"
        r"(?:\s+(?P<year>(?:19|20)\d{2}))?$",
        re.IGNORECASE,
    ),
)

# The bare document-family heading, with no country yet - the first
# line of a two-line cover ("Labour and Employment Law" \n "Canada",
# or "... / Canada" on one line) that _match_title_line's two-line
# fallback pairs with the following line.
_BARE_FAMILY_HEADING_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^(?:Labour and Employment Law|Employment Law Overview)\s*/?\s*$",
    re.IGNORECASE,
)

_TITLE_SCAN_LIMIT: Final[int] = 20


class UnknownLegalTopicError(ValueError):
    """Raised when a section is outside the approved taxonomy."""


class InvalidDocxFormatError(ValueError):
    """Raised when a file is not a genuinely valid, parseable DOCX."""


class UndeterminableDocumentCountryError(ValueError):
    """
    Raised when no supported country can be identified from the
    document's own title/cover content - never from its filename.
    """


class AmbiguousDocumentCountryError(ValueError):
    """
    Raised when more than one distinct country is found in the
    document's title/cover content, with no clear primary one.
    """


@dataclass(frozen=True, slots=True)
class DocumentMetadata:
    """Metadata shared by all chunks from one source document."""

    country: str
    country_code: str
    reference_year: int | None
    language: str
    source_filename: str
    source_format: str = "docx"


def _sha256(
    value: str,
) -> str:
    """Return a deterministic SHA-256 hexadecimal digest."""

    return hashlib.sha256(
        value.encode("utf-8")
    ).hexdigest()


def storage_filename_for_country(
    country_code: str,
) -> str:
    """
    The internal, collision-free on-disk storage name for one
    country's active document under this pipeline's one document
    family - never the user-supplied filename.

    Keying storage by this instead of the original filename (mission
    "CONTINUATION PATCH 0.4.3", section 10) is what makes two
    completely unrelated uploads that happen to share a filename
    (e.g. both named "final.docx") safe: each country still gets its
    own, stable storage path, so neither can silently overwrite the
    other's stored source file on disk. The user's own original
    filename is preserved separately, purely as display/audit
    metadata (DocumentChunk.source_filename) - never as a path.
    """

    return f"{country_code.strip().upper()}.docx"


def validate_docx_format(
    file_path: Path,
) -> None:
    """
    Confirm a file is a genuinely valid, parseable DOCX archive -
    the .docx extension alone proves nothing (mission "CONTINUATION
    PATCH 0.4.3", section 6). Never proceeds to content parsing,
    OpenSearch, or any storage change when this fails.
    """

    if not zipfile.is_zipfile(file_path):
        raise InvalidDocxFormatError(
            "The uploaded file is not a valid DOCX document."
        )

    try:
        with zipfile.ZipFile(file_path) as archive:
            names = set(archive.namelist())

    except zipfile.BadZipFile as error:
        raise InvalidDocxFormatError(
            "The uploaded file is not a valid DOCX document."
        ) from error

    if (
        "[Content_Types].xml" not in names
        or "word/document.xml" not in names
    ):
        raise InvalidDocxFormatError(
            "The uploaded file is not a valid DOCX document."
        )

    try:
        Document(file_path)

    except Exception as error:
        raise InvalidDocxFormatError(
            "The uploaded file is not a valid DOCX document."
        ) from error


def _leading_paragraph_texts(
    file_path: Path,
    limit: int = _TITLE_SCAN_LIMIT,
) -> list[str]:
    """
    The first `limit` non-empty paragraph texts, in document order -
    the title/cover area a reader would actually see first, never
    the full document body (mission section 7: legal documents may
    name many other countries deep in their own text, which must
    never be mistaken for the document's own country).
    """

    document = Document(file_path)
    texts: list[str] = []

    for paragraph in document.paragraphs:
        text = paragraph.text.strip()

        if not text:
            continue

        texts.append(text)

        if len(texts) >= limit:
            break

    return texts


def _match_title_line(
    line: str,
    next_line: str | None,
) -> tuple[str, int | None] | None:
    """
    One candidate (raw country token, optional year) from a single
    title/cover line, or from that line paired with the one right
    after it - covers both a one-line cover ("Employment Law
    Overview Canada 2026", "Labour and Employment Law / Canada") and
    a two-line cover (a bare "Labour and Employment Law" heading
    immediately followed by a "Canada" line).
    """

    for pattern in _TITLE_LINE_PATTERNS:
        match = pattern.fullmatch(line)

        if match is None:
            continue

        year_value = match.group("year")

        return (
            match.group("country").strip(),
            (int(year_value) if year_value is not None else None),
        )

    if "/" in line:
        prefix, _, suffix = line.rpartition("/")

        if _BARE_FAMILY_HEADING_PATTERN.fullmatch(
            f"{prefix.strip()} /"
        ):
            candidate = suffix.strip()

            if candidate:
                return candidate, None

    if (
        _BARE_FAMILY_HEADING_PATTERN.fullmatch(line)
        and next_line
        and not _BARE_FAMILY_HEADING_PATTERN.fullmatch(next_line)
    ):
        return next_line.strip(), None

    return None


def _detect_country_and_year_from_content(
    file_path: Path,
    country_code: str | None = None,
) -> tuple[str, str, int | None]:
    """
    Resolve (country, country_code, reference_year) from the
    document's own title/cover content - never from its filename.

    Only the leading title/cover area is scanned (see
    _leading_paragraph_texts). Candidates are deduplicated by
    resolved country *code*, not raw text, so trivially different
    phrasings of the same country never look ambiguous. More than
    one distinct country code found there - a genuinely ambiguous
    cover - is refused rather than guessed at (section 7).
    """

    lines = _leading_paragraph_texts(file_path)

    resolved: list[tuple[str, str, int | None]] = []

    for index, line in enumerate(lines):
        next_line = (
            lines[index + 1] if index + 1 < len(lines) else None
        )
        candidate = _match_title_line(line, next_line)

        if candidate is None:
            continue

        raw_country, year = candidate

        try:
            country, resolved_code = resolve_country(
                raw_country=raw_country,
                country_code=country_code,
            )

        except UnknownCountryNameError:
            # This title-shaped line simply isn't a real country
            # name - keep scanning the rest of the title area. A
            # CountryMetadataMismatchError, in contrast, means a real
            # country *was* found in the content but conflicts with
            # the caller's explicit country_code - that is always a
            # genuine, actionable error, never silently skipped.
            continue

        resolved.append((country, resolved_code, year))

    if not resolved:
        raise UndeterminableDocumentCountryError(
            "Unable to identify a supported country from the "
            "document content."
        )

    distinct_codes = {code for _, code, _ in resolved}

    if len(distinct_codes) > 1:
        raise AmbiguousDocumentCountryError(
            "Unable to determine a unique document country from "
            "the document content."
        )

    country, resolved_code, _ = resolved[0]

    reference_year = next(
        (year for _, _, year in resolved if year is not None),
        None,
    )

    return country, resolved_code, reference_year


def _validate_metadata(
    metadata: DocumentMetadata,
) -> None:
    """Validate metadata before creating chunks."""

    if not metadata.country.strip():
        raise ValueError(
            "country must not be empty"
        )

    country_code = (
        metadata.country_code
        .strip()
    )

    if (
        len(country_code) != 2
        or not country_code.isalpha()
    ):
        raise ValueError(
            "country_code must contain exactly "
            "two alphabetical characters"
        )

    if not metadata.language.strip():
        raise ValueError(
            "language must not be empty"
        )

    if not metadata.source_filename.strip():
        raise ValueError(
            "source_filename must not be empty"
        )

    if not metadata.source_format.strip():
        raise ValueError(
            "source_format must not be empty"
        )

    if (
        metadata.reference_year is not None
        and not 1900
        <= metadata.reference_year
        <= 2100
    ):
        raise ValueError(
            "reference_year must be between "
            "1900 and 2100"
        )


def _build_document_id(
    metadata: DocumentMetadata,
) -> str:
    """
    Build a stable identifier for one source document.

    Identity is country_code + document family + language only -
    never reference_year, never source_filename (mission
    "CONTINUATION PATCH 0.4.3", section 11). A new upload for a
    country that is already active, of any year and under any
    filename, resolves to this exact same document_id, so it
    replaces the previous version through the existing replace
    mechanism instead of creating a second, parallel document.
    reference_year and source_filename remain per-chunk *version*
    metadata (see DocumentChunk) - they simply never affect which
    document a chunk belongs to.
    """

    identity = "\x1f".join(
        (
            "document-v2",
            DOCUMENT_FAMILY,
            metadata.country_code.strip().upper(),
            metadata.language.strip().casefold(),
        )
    )

    return (
        f"doc_{_sha256(identity)}"
    )


def _build_chunk_id(
    document_id: str,
    document_type: str,
    legal_topic: str | None,
    section: str,
    subsection: str | None,
    occurrence: int,
) -> str:
    """Build a stable chunk identifier from its structural path."""

    identity = "\x1f".join(
        (
            "chunk-v1",
            document_id,
            document_type,
            legal_topic or "",
            section.casefold(),
            (
                subsection
                or ""
            ).casefold(),
            str(occurrence),
        )
    )

    return (
        f"chunk_{_sha256(identity)}"
    )


def metadata_from_content(
    file_path: Path,
    country_code: str | None = None,
    language: str = "en",
) -> DocumentMetadata:
    """
    Extract canonical metadata from the document's own content - the
    filename is never consulted for country, year, or document type
    (mission "CONTINUATION PATCH 0.4.3", section 5): it is preserved
    only as source_filename, for display/audit, never as a source of
    truth for anything else.

    The country token is resolved through the central country
    registry, exactly as before - only *where* it is read from has
    changed, from the filename to the document's title/cover content.

    An explicit country code is optional. When supplied, it must
    correspond to the country found in the document's own content.
    """

    country, resolved_country_code, reference_year = (
        _detect_country_and_year_from_content(
            file_path,
            country_code=country_code,
        )
    )

    source_format = (
        file_path.suffix
        .lstrip(".")
        .lower()
    )

    return DocumentMetadata(
        country=country,
        country_code=resolved_country_code,
        reference_year=reference_year,
        language=(
            language
            .strip()
            .lower()
        ),
        source_filename=file_path.name,
        source_format=(
            source_format
            or "docx"
        ),
    )


def build_document_chunks(
    parsed_sections: Sequence[
        ParsedSection
    ],
    metadata: DocumentMetadata,
) -> list[DocumentChunk]:
    """Enrich parsed sections into indexable legal chunks."""

    _validate_metadata(
        metadata
    )

    country = (
        metadata.country
        .strip()
    )

    country_code = (
        metadata.country_code
        .strip()
        .upper()
    )

    language = (
        metadata.language
        .strip()
        .lower()
    )

    source_filename = (
        metadata.source_filename
        .strip()
    )

    source_format = (
        metadata.source_format
        .strip()
        .lower()
    )

    document_id = _build_document_id(
        metadata
    )

    path_occurrences: defaultdict[
        tuple[str, str, str, str],
        int,
    ] = defaultdict(
        int
    )

    chunks: list[
        DocumentChunk
    ] = []

    for parsed_section in parsed_sections:
        section = (
            parsed_section.section
            .strip()
        )

        subsection = (
            parsed_section.subsection.strip()
            if parsed_section.subsection
            else None
        )

        content = (
            parsed_section.content
            .strip()
        )

        if not content:
            continue

        if is_overview_section(
            section=section,
            country=country,
        ):
            document_type = "overview"
            legal_topic = None

        else:
            document_type = "comparator"

            legal_topic = (
                get_canonical_legal_topic(
                    section=section,
                    country=country,
                )
                or get_subsection_topic_override(
                    section
                )
            )

            if legal_topic is None:
                raise UnknownLegalTopicError(
                    "Unknown legal topic detected. "
                    f"Section: {section!r}. "
                    "The document was not indexed because "
                    "the topic is outside the approved taxonomy."
                )

        path_key = (
            document_type,
            legal_topic or "",
            section.casefold(),
            (
                subsection
                or ""
            ).casefold(),
        )

        path_occurrences[
            path_key
        ] += 1

        occurrence = (
            path_occurrences[
                path_key
            ]
        )

        chunk_id = _build_chunk_id(
            document_id=document_id,
            document_type=document_type,
            legal_topic=legal_topic,
            section=section,
            subsection=subsection,
            occurrence=occurrence,
        )

        chunks.append(
            DocumentChunk(
                document_id=document_id,
                chunk_id=chunk_id,
                country=country,
                country_code=country_code,
                legal_topic=legal_topic,
                document_type=document_type,
                language=language,
                section=section,
                subsection=subsection,
                content=content,
                source_filename=source_filename,
                source_format=source_format,
                content_hash=_sha256(
                    content
                ),
                reference_year=(
                    metadata.reference_year
                ),
            )
        )

    return chunks


def _build_contact_chunk(
    file_path: Path,
    metadata: DocumentMetadata,
) -> DocumentChunk | None:
    """
    Build one Contact-subsection chunk from a source DOCX, if it has
    a validated contact card.

    Reuses the same document_id/chunk_id scheme as every other chunk
    of this document, so it lives in the same OpenSearch mapping with
    no new field. Returns None when no contact could be extracted,
    rather than indexing an empty placeholder.
    """

    contacts = extract_contacts_from_docx(
        file_path,
        country=metadata.country,
    )

    if not contacts:
        return None

    content = build_contact_chunk_content(
        contacts
    )

    if not content:
        return None

    country = metadata.country.strip()
    country_code = metadata.country_code.strip().upper()

    document_id = _build_document_id(
        metadata
    )

    section = (
        f"Employment Law Overview {country}"
    )

    chunk_id = _build_chunk_id(
        document_id=document_id,
        document_type="overview",
        legal_topic=None,
        section=section,
        subsection=CONTACT_SUBSECTION,
        occurrence=1,
    )

    return DocumentChunk(
        document_id=document_id,
        chunk_id=chunk_id,
        country=country,
        country_code=country_code,
        legal_topic=None,
        document_type="overview",
        language=metadata.language.strip().lower(),
        section=section,
        subsection=CONTACT_SUBSECTION,
        content=content,
        source_filename=metadata.source_filename.strip(),
        source_format=metadata.source_format.strip().lower(),
        content_hash=_sha256(
            content
        ),
        reference_year=metadata.reference_year,
    )


def build_document_chunks_from_docx(
    file_path: Path,
    country_code: str | None = None,
    language: str = "en",
) -> list[DocumentChunk]:
    """Parse and enrich one L&E DOCX document."""

    validate_docx_format(file_path)

    metadata = metadata_from_content(
        file_path=file_path,
        country_code=country_code,
        language=language,
    )

    parsed_sections = split_parsed_sections(
        parsed_sections=parse_docx_sections(
            file_path=file_path,
            country=metadata.country,
        ),
        max_chars=6000,
    )

    chunks = build_document_chunks(
        parsed_sections=parsed_sections,
        metadata=metadata,
    )

    contact_chunk = _build_contact_chunk(
        file_path=file_path,
        metadata=metadata,
    )

    if contact_chunk is not None:
        chunks.append(
            contact_chunk
        )

    return chunks