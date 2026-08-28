import re
from typing import Final

from app.core.country_registry import (
    UnknownCountryNameError,
    country_code_from_name,
    country_name_and_aliases,
)


LEGAL_TOPICS: Final[tuple[str, ...]] = (
    "Hiring Practices",
    "Employment Contracts",
    "Working Conditions",
    "Anti-Discrimination Laws",
    "Pay Equity Laws",
    "Social Media and Data Privacy",
    "Termination of Employment Contracts",
    "Restrictive Covenants",
    "Transfer of Undertakings",
    "Trade Unions and Employers Associations",
    "Employee Benefits",
)


_TOPIC_ALIASES: Final[dict[str, tuple[str, ...]]] = {
    "Hiring Practices": (
        "hiring practices",
        "hiring practice",
    ),
    "Employment Contracts": (
        "employment contracts",
        "employment contract",
        "employment contract law",
    ),
    "Working Conditions": (
        "working conditions",
        "wages and work hours",
    ),
    "Anti-Discrimination Laws": (
        "anti-discrimination laws",
        "anti discrimination laws",
    ),
    "Pay Equity Laws": (
        "pay equity laws",
    ),
    "Social Media and Data Privacy": (
        "social media and data privacy",
    ),
    "Termination of Employment Contracts": (
        "termination of employment contracts",
        "termination of employment contract",
        "termination of employment",
    ),
    "Restrictive Covenants": (
        "restrictive covenants",
    ),
    "Transfer of Undertakings": (
        "transfer of undertakings",
        "transfer of undertaking",
    ),
    "Trade Unions and Employers Associations": (
        "trade unions and employers associations",
        "trade unions and employer associations",
        "trade unions and employers' associations",
        "trade unions and employer's associations",
    ),
    "Employee Benefits": (
        "employee benefits",
    ),
}


_TOPIC_PREFIX_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^\s*"
    r"(?:[|¦=]+\s*)?"
    r"(?:(?:\d{1,2}|[IVX]{1,6})\s*[.)]\s*)?",
    re.IGNORECASE,
)

_TRAILING_DECORATION_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\s*[|¦=]+\s*$"
)

_TRAILING_ANNOTATION_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\s*\([^()]*\)\s*$"
)


def _normalize_text(value: str) -> str:
    """Normalize whitespace and punctuation used in source labels."""

    return " ".join(
        value
        .replace("\xa0", " ")
        .replace("’", "'")
        .replace("–", "-")
        .replace("—", "-")
        .split()
    )


def _clean_label(value: str) -> str:
    """Remove numbering and decorative separators from a label."""

    normalized = _normalize_text(value)

    without_prefix = _TOPIC_PREFIX_PATTERN.sub(
        "",
        normalized,
    )

    without_trailing_decoration = (
        _TRAILING_DECORATION_PATTERN.sub(
            "",
            without_prefix,
        )
    )

    without_trailing_annotation = (
        _strip_trailing_annotation(
            without_trailing_decoration
        )
    )

    return without_trailing_annotation.strip()


def _strip_trailing_annotation(value: str) -> str:
    """
    Drop one harmless trailing "(...)" editorial annotation (for
    example " (NEW SECTION)") so a canonical heading carrying it is
    still recognized. Never applied if it would erase the entire
    label - only a heading whose non-parenthetical text stands on its
    own is affected.
    """

    stripped = _TRAILING_ANNOTATION_PATTERN.sub(
        "",
        value,
    )

    return stripped if stripped.strip() else value


def _with_the_variant(
    name: str,
) -> tuple[str, ...]:
    """A name plus its "the <name>" / stripped-"the " counterpart."""

    if name.casefold().startswith("the "):
        return (name, name[4:].strip())

    return (name, f"the {name}")


def _country_name_variants(
    country: str | None,
) -> tuple[str, ...]:
    """
    Return every safe label variant a heading may use for one
    country - its own raw label, plus, when it resolves to a
    registered country, every alias the country registry itself
    already knows (for example "USA", "U.S.", "U.S.A." for "United
    States") - never a second, independent list of country names
    (mission "HOTFIX 0.4.4", final targeted correction).
    """

    if country is None:
        return ()

    normalized_country = _normalize_text(
        country
    ).strip()

    if not normalized_country:
        return ()

    known_names = (normalized_country,)

    try:
        resolved_code = country_code_from_name(
            normalized_country
        )

    except UnknownCountryNameError:
        pass

    else:
        known_names = country_name_and_aliases(
            resolved_code
        )

    variants: set[str] = set()

    for name in known_names:
        variants.update(
            _with_the_variant(
                _normalize_text(name)
            )
        )

    return tuple(
        sorted(
            variants,
            key=len,
            reverse=True,
        )
    )


def _remove_country_suffix(
    label: str,
    country: str | None,
) -> str:
    """Remove a final 'in <country>' suffix when explicitly known."""

    for country_variant in _country_name_variants(
        country
    ):
        suffix = f" in {country_variant}"

        if label.casefold().endswith(
            suffix.casefold()
        ):
            return label[
                : -len(suffix)
            ].strip()

    return label


def normalize_topic(
    section: str,
    country: str | None = None,
) -> str:
    """
    Normalize a source section into a potential topic label.

    Examples:
        "01. Hiring Practices"
        becomes:
        "Hiring Practices"

        "| 05. Pay Equity Laws"
        becomes:
        "Pay Equity Laws"

        "Hiring practices in Australia"
        becomes:
        "Hiring practices"

        "Restrictive Covenants in Australia|"
        becomes:
        "Restrictive Covenants"
    """

    cleaned_label = _clean_label(
        section
    )

    return _remove_country_suffix(
        label=cleaned_label,
        country=country,
    )


def get_canonical_legal_topic(
    section: str,
    country: str | None = None,
) -> str | None:
    """
    Return the canonical L&E topic for a source section.

    Matching is deliberately exact after normalization. A body sentence
    beginning with a topic name must not accidentally become a section.
    """

    normalized_topic = normalize_topic(
        section=section,
        country=country,
    ).casefold()

    for canonical_topic, aliases in (
        _TOPIC_ALIASES.items()
    ):
        if normalized_topic in {
            alias.casefold()
            for alias in aliases
        }:
            return canonical_topic

    return None


def is_overview_section(
    section: str,
    country: str | None = None,
) -> bool:
    """Return whether a section belongs to the document overview."""

    normalized_section = _clean_label(
        section
    ).casefold()

    if normalized_section == "general":
        return True

    overview_bases = (
        "employment law overview",
        "labour and employment law overview",
    )

    if normalized_section in overview_bases:
        return True

    for country_variant in _country_name_variants(
        country
    ):
        normalized_country = (
            country_variant.casefold()
        )

        accepted_labels = {
            (
                "employment law overview "
                f"{normalized_country}"
            ),
            (
                "employment law overview in "
                f"{normalized_country}"
            ),
            (
                "labour and employment law overview "
                f"{normalized_country}"
            ),
            (
                "labour and employment law overview in "
                f"{normalized_country}"
            ),
            (
                "labour and employment law in "
                f"{normalized_country}"
            ),
        }

        if normalized_section in accepted_labels:
            return True

    return False