from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final

from app.core.legal_taxonomy import LEGAL_TOPICS


class SubsectionTaxonomyError(ValueError):
    """Base exception raised by the subsection taxonomy."""


class SubsectionTaxonomyConfigurationError(
    SubsectionTaxonomyError
):
    """Raised when the subsection registry is inconsistent."""


@dataclass(frozen=True, slots=True)
class SubsectionDefinition:
    """
    Canonical subsection accepted under one legal topic.

    parent_topic=None represents an overview subsection.
    """

    parent_topic: str | None
    canonical_name: str
    aliases: tuple[str, ...] = ()


_LEADING_ALPHA_PREFIX_PATTERN: Final[
    re.Pattern[str]
] = re.compile(
    r"^\s*[a-z]\s*[.)]\s*",
    re.IGNORECASE,
)


_TRAILING_SEPARATOR_PATTERN: Final[
    re.Pattern[str]
] = re.compile(
    r"\s*[:|¦=]+\s*$"
)


SUBSECTION_DEFINITIONS: Final[
    tuple[SubsectionDefinition, ...]
] = (
    # ------------------------------------------------------------------
    # Overview
    # ------------------------------------------------------------------
    SubsectionDefinition(
        parent_topic=None,
        canonical_name="Introduction",
    ),
    SubsectionDefinition(
        parent_topic=None,
        canonical_name="Key Points",
    ),
    SubsectionDefinition(
        parent_topic=None,
        canonical_name="Legal Framework",
        aliases=(
            "Legal framework",
            "Legal framework (selection)",
        ),
    ),
    SubsectionDefinition(
        parent_topic=None,
        canonical_name="New Developments",
    ),

    # ------------------------------------------------------------------
    # 01. Hiring Practices
    # ------------------------------------------------------------------
    SubsectionDefinition(
        parent_topic="Hiring Practices",
        canonical_name=(
            "Requirement for Foreign Employees to Work"
        ),
    ),
    SubsectionDefinition(
        parent_topic="Hiring Practices",
        canonical_name=(
            "Does a Foreign Employer need to Establish "
            "or Work through a Local Entity to Hire "
            "an Employee?"
        ),
    ),
    SubsectionDefinition(
        parent_topic="Hiring Practices",
        canonical_name="Limitations on Background Checks",
    ),
    SubsectionDefinition(
        parent_topic="Hiring Practices",
        canonical_name=(
            "Restrictions on Application/Interview Questions"
        ),
        aliases=(
            "Restrictions on Application / Interview Questions",
        ),
    ),

    # ------------------------------------------------------------------
    # 02. Employment Contracts
    # ------------------------------------------------------------------
    SubsectionDefinition(
        parent_topic="Employment Contracts",
        canonical_name="Minimum Requirements",
        aliases=(
            "Minimum requirements",
        ),
    ),
    SubsectionDefinition(
        parent_topic="Employment Contracts",
        canonical_name="Fixed-term/Open-ended Contracts",
    ),
    SubsectionDefinition(
        parent_topic="Employment Contracts",
        canonical_name="Trial Period",
        aliases=(
            "Probationary Period",
        ),
    ),
    SubsectionDefinition(
        parent_topic="Employment Contracts",
        canonical_name="Notice Period",
    ),
    SubsectionDefinition(
        parent_topic="Employment Contracts",
        canonical_name="Teleworking Contract",
    ),

    # ------------------------------------------------------------------
    # 03. Working Conditions
    # ------------------------------------------------------------------
    SubsectionDefinition(
        parent_topic="Working Conditions",
        canonical_name="Minimum Working Conditions",
    ),
    SubsectionDefinition(
        parent_topic="Working Conditions",
        canonical_name="Salary",
    ),
    SubsectionDefinition(
        parent_topic="Working Conditions",
        canonical_name="Maximum Working Week",
    ),
    SubsectionDefinition(
        parent_topic="Working Conditions",
        canonical_name="Overtime",
    ),
    SubsectionDefinition(
        parent_topic="Working Conditions",
        canonical_name="Work Hours Record",
        aliases=(
            "Work hours record",
            "Working hours record",
            "Working time record",
            "Work time record",
        ),
    ),
    SubsectionDefinition(
        parent_topic="Working Conditions",
        canonical_name="Paid Leave",
        aliases=(
            "Paid leave",
        ),
    ),
    SubsectionDefinition(
        parent_topic="Working Conditions",
        canonical_name=(
            "Employer's Obligation to Provide "
            "a Healthy and Safe Workplace"
        ),
    ),
    SubsectionDefinition(
        parent_topic="Working Conditions",
        canonical_name="Complaint Procedures",
    ),

    # ------------------------------------------------------------------
    # 04. Anti-Discrimination Laws
    # ------------------------------------------------------------------
    SubsectionDefinition(
        parent_topic="Anti-Discrimination Laws",
        canonical_name="Summary",
    ),
    SubsectionDefinition(
        parent_topic="Anti-Discrimination Laws",
        canonical_name="Extent of Protection",
    ),
    SubsectionDefinition(
        parent_topic="Anti-Discrimination Laws",
        canonical_name="Protections Against Harassment",
    ),
    SubsectionDefinition(
        parent_topic="Anti-Discrimination Laws",
        canonical_name=(
            "Employer's Obligation to Provide "
            "Reasonable Accommodations"
        ),
    ),
    SubsectionDefinition(
        parent_topic="Anti-Discrimination Laws",
        canonical_name="Remedies",
    ),
    SubsectionDefinition(
        parent_topic="Anti-Discrimination Laws",
        canonical_name="Other Requirements",
    ),

    # ------------------------------------------------------------------
    # 05. Pay Equity Laws
    # ------------------------------------------------------------------
    SubsectionDefinition(
        parent_topic="Pay Equity Laws",
        canonical_name="Extent of Protection",
    ),
    SubsectionDefinition(
        parent_topic="Pay Equity Laws",
        canonical_name="Remedies",
    ),
    SubsectionDefinition(
        parent_topic="Pay Equity Laws",
        canonical_name="Enforcement/Litigation",
    ),
    SubsectionDefinition(
        parent_topic="Pay Equity Laws",
        canonical_name="Other Requirements",
    ),

    # ------------------------------------------------------------------
    # 06. Social Media and Data Privacy
    # ------------------------------------------------------------------
    SubsectionDefinition(
        parent_topic="Social Media and Data Privacy",
        canonical_name="Restrictions in the Workplace",
    ),
    SubsectionDefinition(
        parent_topic="Social Media and Data Privacy",
        canonical_name=(
            "Can the employer monitor, access, review "
            "the employee's electronic communications?"
        ),
    ),
    SubsectionDefinition(
        parent_topic="Social Media and Data Privacy",
        canonical_name=(
            "Employee's Use of Social Media to Disparage "
            "the Employer or Divulge Confidential Information"
        ),
    ),

    # ------------------------------------------------------------------
    # 07. Termination of Employment Contracts
    # ------------------------------------------------------------------
    SubsectionDefinition(
        parent_topic="Termination of Employment Contracts",
        canonical_name="Grounds for Termination",
    ),
    SubsectionDefinition(
        parent_topic="Termination of Employment Contracts",
        canonical_name="Collective Dismissals",
    ),
    SubsectionDefinition(
        parent_topic="Termination of Employment Contracts",
        canonical_name="Individual Dismissals",
    ),
    SubsectionDefinition(
        parent_topic="Termination of Employment Contracts",
        canonical_name="Is Severance Pay Required?",
    ),
    SubsectionDefinition(
        parent_topic="Termination of Employment Contracts",
        canonical_name="Separation Agreements",
    ),
    SubsectionDefinition(
        parent_topic="Termination of Employment Contracts",
        canonical_name=(
            "Remedies for Employee Seeking to Challenge "
            "Wrongful Termination"
        ),
    ),
    SubsectionDefinition(
        parent_topic="Termination of Employment Contracts",
        canonical_name="Whistleblower Laws",
        aliases=(
            "Whitsleblower Laws",
        ),
    ),

    # ------------------------------------------------------------------
    # 08. Restrictive Covenants
    # ------------------------------------------------------------------
    SubsectionDefinition(
        parent_topic="Restrictive Covenants",
        canonical_name=(
            "Definition and Types of Restrictive Covenants"
        ),
    ),
    SubsectionDefinition(
        parent_topic="Restrictive Covenants",
        canonical_name="Types of Restrictive Covenants",
    ),
    SubsectionDefinition(
        parent_topic="Restrictive Covenants",
        canonical_name=(
            "Enforcement of Restrictive Covenants "
            "- Process and Remedies"
        ),
    ),
    SubsectionDefinition(
        parent_topic="Restrictive Covenants",
        canonical_name=(
            "Use and Limitations of Garden Leave"
        ),
        aliases=(
            "Use and Limitation of Garden Leave",
        ),
    ),

    # ------------------------------------------------------------------
    # 09. Transfer of Undertakings
    # ------------------------------------------------------------------
    SubsectionDefinition(
        parent_topic="Transfer of Undertakings",
        canonical_name=(
            "Employees' Rights in Case of "
            "a Transfer of Undertaking"
        ),
    ),
    SubsectionDefinition(
        parent_topic="Transfer of Undertakings",
        canonical_name=(
            "Requirements for Predecessor "
            "and Successor Parties"
        ),
    ),

    # ------------------------------------------------------------------
    # 10. Trade Unions and Employers Associations
    # ------------------------------------------------------------------
    SubsectionDefinition(
        parent_topic=(
            "Trade Unions and Employers Associations"
        ),
        canonical_name=(
            "Brief Description of Employees' "
            "and Employers' Associations"
        ),
    ),
    SubsectionDefinition(
        parent_topic=(
            "Trade Unions and Employers Associations"
        ),
        canonical_name=(
            "Rights and Importance of Trade Unions"
        ),
    ),
    SubsectionDefinition(
        parent_topic=(
            "Trade Unions and Employers Associations"
        ),
        canonical_name="Types of Representation",
    ),
    SubsectionDefinition(
        parent_topic=(
            "Trade Unions and Employers Associations"
        ),
        canonical_name=(
            "Tasks and Obligations of Representatives"
        ),
        aliases=(
            "Tasks and Obligations of Representation",
        ),
    ),
    SubsectionDefinition(
        parent_topic=(
            "Trade Unions and Employers Associations"
        ),
        canonical_name=(
            "Employees' Representation in Management"
        ),
        aliases=(
            "Employees´ Representation in Management",
        ),
    ),
    SubsectionDefinition(
        parent_topic=(
            "Trade Unions and Employers Associations"
        ),
        canonical_name=(
            "Other Types of Employee Representative Bodies"
        ),
    ),
    SubsectionDefinition(
        parent_topic=(
            "Trade Unions and Employers Associations"
        ),
        canonical_name=(
            "Health and Safety Representatives "
            "under Work Health and Safety Laws"
        ),
    ),

    # ------------------------------------------------------------------
    # 11. Employee Benefits
    # ------------------------------------------------------------------
    SubsectionDefinition(
        parent_topic="Employee Benefits",
        canonical_name="Social Security",
    ),
    SubsectionDefinition(
        parent_topic="Employee Benefits",
        canonical_name="Healthcare and Insurances",
    ),
    SubsectionDefinition(
        parent_topic="Employee Benefits",
        canonical_name="Required Leave",
    ),
    SubsectionDefinition(
        parent_topic="Employee Benefits",
        canonical_name=(
            "Pensions: Mandatory and Typically Provided"
        ),
    ),
)


def normalize_subsection_label(
    value: str,
) -> str:
    """
    Normalize a subsection label for controlled matching.

    The normalizer handles:

    - non-breaking spaces;
    - curly apostrophes;
    - acute-accent apostrophes;
    - long dashes;
    - alphabetical prefixes such as "a.";
    - spaces around slashes;
    - trailing decorative separators.
    """

    normalized = (
        value
        .replace("\xa0", " ")
        .replace("’", "'")
        .replace("´", "'")
        .replace("`", "'")
        .replace("–", "-")
        .replace("—", "-")
    )

    normalized = _LEADING_ALPHA_PREFIX_PATTERN.sub(
        "",
        normalized,
    )

    normalized = _TRAILING_SEPARATOR_PATTERN.sub(
        "",
        normalized,
    )

    normalized = re.sub(
        r"\s*/\s*",
        "/",
        normalized,
    )

    normalized = re.sub(
        r"\s*-\s*",
        " - ",
        normalized,
    )

    return " ".join(
        normalized.split()
    ).casefold()


def _build_subsection_index(
    definitions: Iterable[
        SubsectionDefinition
    ],
) -> dict[
    tuple[str | None, str],
    str,
]:
    """
    Build the subsection index and detect configuration errors.

    Identical labels may exist under different legal topics, such as
    "Remedies". They may not resolve to two different canonical names
    under the same parent topic.
    """

    index: dict[
        tuple[str | None, str],
        str,
    ] = {}

    legal_topics = set(
        LEGAL_TOPICS
    )

    for definition in definitions:
        parent_topic = (
            definition.parent_topic
        )

        if (
            parent_topic is not None
            and parent_topic not in legal_topics
        ):
            raise SubsectionTaxonomyConfigurationError(
                "Unknown parent legal topic in subsection "
                "taxonomy: "
                f"{parent_topic!r}."
            )

        canonical_name = (
            definition.canonical_name.strip()
        )

        if not canonical_name:
            raise SubsectionTaxonomyConfigurationError(
                "Canonical subsection name must not be empty."
            )

        tokens = (
            canonical_name,
            *definition.aliases,
        )

        for token in tokens:
            normalized_token = normalize_subsection_label(
                token
            )

            if not normalized_token:
                raise SubsectionTaxonomyConfigurationError(
                    "Subsection alias must not be empty for "
                    f"{canonical_name!r}."
                )

            index_key = (
                parent_topic,
                normalized_token,
            )

            existing_name = index.get(
                index_key
            )

            if (
                existing_name is not None
                and existing_name != canonical_name
            ):
                raise SubsectionTaxonomyConfigurationError(
                    "Subsection alias collision under "
                    f"{parent_topic!r}: {token!r} resolves "
                    f"to both {existing_name!r} and "
                    f"{canonical_name!r}."
                )

            index[
                index_key
            ] = canonical_name

    return index


_SUBSECTION_INDEX: Final[
    dict[tuple[str | None, str], str]
] = _build_subsection_index(
    SUBSECTION_DEFINITIONS
)


def get_canonical_subsection(
    parent_topic: str | None,
    subsection: str,
) -> str | None:
    """
    Return the canonical subsection for a legal topic.

    parent_topic=None is used for document overview sections.
    """

    normalized_subsection = normalize_subsection_label(
        subsection
    )

    if not normalized_subsection:
        return None

    return _SUBSECTION_INDEX.get(
        (
            parent_topic,
            normalized_subsection,
        )
    )


# Some source documents embed a heading whose content belongs to a
# different legal topic than the section it is physically placed in
# (for example, Australia presents "Notice of Termination and
# Redundancy Pay" as a bold "Normal"-style paragraph inside "Working
# Conditions", identical in DOCX structure to ordinary bold emphasis
# elsewhere in the same document). This table lets the parser start a
# distinct chunk under the correct topic for that one heading only,
# without permanently changing the enclosing section's topic, so
# subsequent subsections of the enclosing section continue to resolve
# normally.
SUBSECTION_TOPIC_OVERRIDES: Final[
    dict[str, str]
] = {
    normalize_subsection_label(
        "Notice of Termination and Redundancy Pay"
    ): "Termination of Employment Contracts",
}


def get_subsection_topic_override(
    subsection: str,
) -> str | None:
    """Return a one-off legal-topic override for a specific heading."""

    return SUBSECTION_TOPIC_OVERRIDES.get(
        normalize_subsection_label(
            subsection
        )
    )