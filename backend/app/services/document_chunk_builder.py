import hashlib
import re
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final
from app.services.section_splitter import (
    split_parsed_sections,
)
from app.core.country_registry import (
    resolve_country,
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
    parse_docx_sections,
)


_FILENAME_PATTERNS: Final[
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


_COPY_SUFFIX_PATTERN: Final[
    re.Pattern[str]
] = re.compile(
    r"\s*\(\d+\)$"
)


class UnknownLegalTopicError(ValueError):
    """Raised when a section is outside the approved taxonomy."""


class InvalidSourceFilenameError(ValueError):
    """Raised when a DOCX filename has an unsupported format."""


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


def _normalize_filename_stem(
    file_path: Path,
) -> str:
    """
    Normalize supported filename variations.

    Supported variations include:

    - spaces;
    - underscores;
    - final copy suffixes such as "(1)".
    """

    stem_without_copy_suffix = (
        _COPY_SUFFIX_PATTERN.sub(
            "",
            file_path.stem.strip(),
        )
    )

    return " ".join(
        stem_without_copy_suffix
        .replace("_", " ")
        .split()
    )


def _extract_filename_metadata(
    file_path: Path,
) -> tuple[str, int | None]:
    """Return the raw country token and optional year."""

    normalized_stem = _normalize_filename_stem(
        file_path
    )

    for pattern in _FILENAME_PATTERNS:
        match = pattern.fullmatch(
            normalized_stem
        )

        if match is None:
            continue

        year_value = match.group(
            "year"
        )

        return (
            match.group(
                "country"
            ).strip(),
            (
                int(year_value)
                if year_value is not None
                else None
            ),
        )

    raise InvalidSourceFilenameError(
        "Unexpected DOCX filename. "
        "Supported formats are: "
        "'Labour and Employment Law in "
        "<Country> [Year].docx' and "
        "'Employment Law Overview [-] "
        "<Country> [Year].docx'. "
        "Spaces, underscores, and a final copy suffix "
        "such as '(1)' are accepted. "
        f"Received: {file_path.name}"
    )


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
    """Build a stable identifier for one source document."""

    identity = "\x1f".join(
        (
            "document-v1",
            metadata.country.strip().casefold(),
            metadata.country_code.strip().upper(),
            str(
                metadata.reference_year
                or ""
            ),
            metadata.language.strip().casefold(),
            metadata.source_filename.strip().casefold(),
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


def metadata_from_filename(
    file_path: Path,
    country_code: str | None = None,
    language: str = "en",
) -> DocumentMetadata:
    """
    Extract canonical metadata from a supported filename.

    The country token is resolved through the central country
    registry.

    An explicit country code is optional. When supplied, it must
    correspond to the country found in the filename.
    """

    raw_country, reference_year = (
        _extract_filename_metadata(
            file_path
        )
    )

    country, resolved_country_code = (
        resolve_country(
            raw_country=raw_country,
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


def build_document_chunks_from_docx(
    file_path: Path,
    country_code: str | None = None,
    language: str = "en",
) -> list[DocumentChunk]:
    """Parse and enrich one L&E DOCX document."""

    metadata = metadata_from_filename(
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

    return build_document_chunks(
        parsed_sections=parsed_sections,
        metadata=metadata,
    )