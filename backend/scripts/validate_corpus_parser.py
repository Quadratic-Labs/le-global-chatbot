from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Final

from app.core.legal_taxonomy import (
    LEGAL_TOPICS,
    get_canonical_legal_topic,
    is_overview_section,
)
from app.services.docx_parser import (
    ParsedSection,
    parse_docx_sections,
)


_FILENAME_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(
        r"^Labour and Employment Law in\s+"
        r"(?P<country>.+?)"
        r"(?:\s+(?P<year>(?:19|20)\d{2}))?$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^Employment Law Overview(?:\s*-\s*)?\s+"
        r"(?P<country>.+?)"
        r"(?:\s+(?P<year>(?:19|20)\d{2}))?$",
        re.IGNORECASE,
    ),
)

_COPY_SUFFIX_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\s*\(\d+\)$"
)

_COUNTRY_ALIASES: Final[dict[str, str]] = {
    "uk": "United Kingdom",
    "united kingdom": "United Kingdom",
}


def _normalize_text(value: str) -> str:
    """Normalize whitespace in metadata values."""

    return " ".join(
        value
        .replace("\xa0", " ")
        .split()
    )


def _country_from_filename(
    file_path: Path,
) -> str:
    """Extract and normalize the country from an L&E filename."""

    original_stem = file_path.stem.strip()

    cleaned_stem = _COPY_SUFFIX_PATTERN.sub(
        "",
        original_stem,
    ).strip()

    for pattern in _FILENAME_PATTERNS:
        match = pattern.fullmatch(
            cleaned_stem
        )

        if match is None:
            continue

        raw_country = _normalize_text(
            match.group("country")
        ).strip(" -_")

        return _COUNTRY_ALIASES.get(
            raw_country.casefold(),
            raw_country,
        )

    raise ValueError(
        "Unable to detect country from filename: "
        f"{file_path.name}"
    )


def _topics_from_sections(
    sections: list[ParsedSection],
    country: str,
) -> set[str]:
    """Return all canonical legal topics reached by the parser."""

    topics: set[str] = set()

    for section in sections:
        legal_topic = get_canonical_legal_topic(
            section=section.section,
            country=country,
        )

        if legal_topic is not None:
            topics.add(
                legal_topic
            )

    return topics


def _unknown_sections(
    sections: list[ParsedSection],
    country: str,
) -> list[str]:
    """Return main sections outside the taxonomy and overview."""

    unknown: set[str] = set()

    for section in sections:
        if get_canonical_legal_topic(
            section=section.section,
            country=country,
        ) is not None:
            continue

        if is_overview_section(
            section=section.section,
            country=country,
        ):
            continue

        unknown.add(
            section.section
        )

    return sorted(
        unknown
    )


def _validate_file(
    file_path: Path,
) -> dict[str, object]:
    """Run the strict L&E parser against one real DOCX."""

    country = _country_from_filename(
        file_path
    )

    sections = parse_docx_sections(
        file_path=file_path,
        country=country,
    )

    topics = _topics_from_sections(
        sections=sections,
        country=country,
    )

    missing_topics = [
        topic
        for topic in LEGAL_TOPICS
        if topic not in topics
    ]

    unknown_sections = _unknown_sections(
        sections=sections,
        country=country,
    )

    section_paths = [
        (
            section.section,
            section.subsection,
        )
        for section in sections
    ]

    duplicate_paths = sorted(
        {
            path
            for path in section_paths
            if section_paths.count(path) > 1
        }
    )

    max_content_length = max(
        (
            len(section.content)
            for section in sections
        ),
        default=0,
    )

    return {
        "filename": file_path.name,
        "country": country,
        "chunks": len(sections),
        "topics_found": len(topics),
        "missing_topics": missing_topics,
        "unknown_sections": unknown_sections,
        "duplicate_paths": duplicate_paths,
        "max_content_length": max_content_length,
    }


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit(
            "Usage: python -m scripts.validate_corpus_parser "
            "<source-directory>"
        )

    source_directory = Path(
        sys.argv[1]
    ).resolve()

    if not source_directory.is_dir():
        raise NotADirectoryError(
            "Source directory not found: "
            f"{source_directory}"
        )

    files = sorted(
        source_directory.glob(
            "*.docx"
        ),
        key=lambda path: path.name.casefold(),
    )

    if not files:
        raise FileNotFoundError(
            "No DOCX files found in: "
            f"{source_directory}"
        )

    results: list[dict[str, object]] = []

    failed_files = 0

    print(
        "L&E strict parser validation"
    )
    print(
        "=" * 80
    )

    for file_path in files:
        try:
            result = _validate_file(
                file_path
            )

        except Exception as error:
            failed_files += 1

            print()
            print(
                f"[ERROR] {file_path.name}"
            )
            print(
                f"  {type(error).__name__}: {error}"
            )
            continue

        results.append(
            result
        )

        missing_topics = result[
            "missing_topics"
        ]

        unknown_sections = result[
            "unknown_sections"
        ]

        duplicate_paths = result[
            "duplicate_paths"
        ]

        is_valid = (
            result["topics_found"] == 11
            and not missing_topics
            and not unknown_sections
        )

        status = (
            "OK"
            if is_valid
            else "CHECK"
        )

        if not is_valid:
            failed_files += 1

        print()
        print(
            f"[{status}] {result['filename']}"
        )
        print(
            f"  Country: {result['country']}"
        )
        print(
            f"  Parsed chunks: {result['chunks']}"
        )
        print(
            "  Legal topic coverage: "
            f"{result['topics_found']}/11"
        )
        print(
            "  Max content length: "
            f"{result['max_content_length']}"
        )

        if missing_topics:
            print(
                "  Missing topics: "
                + ", ".join(
                    missing_topics
                )
            )

        if unknown_sections:
            print(
                "  Unknown sections: "
                + ", ".join(
                    unknown_sections
                )
            )

        if duplicate_paths:
            print(
                "  Repeated section paths: "
                f"{len(duplicate_paths)}"
            )

    print()
    print(
        "=" * 80
    )
    print(
        f"Files analysed: {len(files)}"
    )
    print(
        f"Files successfully parsed: {len(results)}"
    )
    print(
        f"Files requiring attention: {failed_files}"
    )

    if failed_files:
        raise SystemExit(
            1
        )


if __name__ == "__main__":
    main()