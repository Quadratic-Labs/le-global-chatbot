from dataclasses import dataclass
import re
import unicodedata
from typing import Final, Iterable

import pycountry


class CountryRegistryError(ValueError):
    """Base error raised by the country registry."""


class CountryRegistryConfigurationError(
    CountryRegistryError
):
    """Raised when the registry contains invalid configuration."""


class UnknownCountryCodeError(
    CountryRegistryError
):
    """Raised when an ISO country code is not registered."""


class UnknownCountryNameError(
    CountryRegistryError
):
    """Raised when a country name or alias is not registered."""


class CountryMetadataMismatchError(
    CountryRegistryError
):
    """Raised when a filename country conflicts with a code."""


@dataclass(frozen=True, slots=True)
class CountryDefinition:
    """Canonical metadata for one supported country."""

    code: str
    display_name: str
    aliases: tuple[str, ...] = ()


COUNTRIES: Final[
    tuple[CountryDefinition, ...]
] = (
    CountryDefinition(
        code="AR",
        display_name="Argentina",
    ),
    CountryDefinition(
        code="AU",
        display_name="Australia",
    ),
    CountryDefinition(
        code="BE",
        display_name="Belgium",
    ),
    CountryDefinition(
        code="BR",
        display_name="Brazil",
    ),
    CountryDefinition(
        code="CA",
        display_name="Canada",
        aliases=(
            "Canadian",
        ),
    ),
    CountryDefinition(
        code="CL",
        display_name="Chile",
        aliases=(
            "Chilean",
        ),
    ),
    CountryDefinition(
        code="CN",
        display_name="China",
        aliases=(
            "Chinese",
        ),
    ),
    CountryDefinition(
        code="CO",
        display_name="Colombia",
        aliases=(
            "Colombian",
        ),
    ),
    CountryDefinition(
        code="CZ",
        display_name="Czech Republic",
        aliases=(
            "Czechia",
        ),
    ),
    CountryDefinition(
        code="FR",
        display_name="France",
    ),
    CountryDefinition(
        code="DE",
        display_name="Germany",
    ),
    CountryDefinition(
        code="GR",
        display_name="Greece",
    ),
    CountryDefinition(
        code="IN",
        display_name="India",
    ),
    CountryDefinition(
        code="ID",
        display_name="Indonesia",
    ),
    CountryDefinition(
        code="IE",
        display_name="Ireland",
        aliases=(
            "Irish",
        ),
    ),
    CountryDefinition(
        code="IT",
        display_name="Italy",
    ),
    CountryDefinition(
        code="JP",
        display_name="Japan",
    ),
    CountryDefinition(
        code="MX",
        display_name="Mexico",
    ),
    CountryDefinition(
        code="NL",
        display_name="Netherlands",
        aliases=(
            "the Netherlands",
            "Dutch",
        ),
    ),
    CountryDefinition(
        code="NO",
        display_name="Norway",
    ),
    CountryDefinition(
        code="PE",
        display_name="Peru",
    ),
    CountryDefinition(
        code="PH",
        display_name="Philippines",
        aliases=(
            "the Philippines",
            "Philippine",
        ),
    ),
    CountryDefinition(
        code="PL",
        display_name="Poland",
    ),
    CountryDefinition(
        code="PT",
        display_name="Portugal",
    ),
    CountryDefinition(
        code="RO",
        display_name="Romania",
    ),
    CountryDefinition(
        code="SG",
        display_name="Singapore",
    ),
    CountryDefinition(
        code="SK",
        display_name="Slovakia",
    ),
    CountryDefinition(
        code="ES",
        display_name="Spain",
    ),
    CountryDefinition(
        code="SE",
        display_name="Sweden",
    ),
    CountryDefinition(
        code="CH",
        display_name="Switzerland",
    ),
    CountryDefinition(
        code="TW",
        display_name="Taiwan",
        aliases=(
            "Taiwanese",
        ),
    ),
    CountryDefinition(
        code="TR",
        display_name="Türkiye",
        aliases=(
            "Turkey",
            "Turkish",
        ),
    ),
    CountryDefinition(
        code="GB",
        display_name="United Kingdom",
        aliases=(
            "UK",
            "Great Britain",
            "Britain",
            "the United Kingdom",
        ),
    ),
    CountryDefinition(
        code="US",
        display_name="United States",
        aliases=(
            "USA",
            "U.S.",
            "U.S.A.",
            "the United States",
            "United States of America",
        ),
    ),
)


_COUNTRY_CODE_PATTERN: Final[
    re.Pattern[str]
] = re.compile(
    r"^[A-Z]{2}$"
)


# A document's own front matter may or may not spell a country with
# its English definite article ("the Czech Republic" vs "Czech
# Republic") independently of which countries already happen to carry
# an explicit "the X" alias below (mission "ORDER 2": Czech Republic's
# real front matter used the article, had no such alias, and
# country_code_from_name raised UnknownCountryNameError, masked by the
# reindex router into a generic 502). Rather than requiring every
# country whose common English name can take "the" to remember to
# register that exact alias (Netherlands, Philippines, United Kingdom,
# and United States already do; Czech Republic did not), the lookup
# below tries the token as given first, then - only if that fails -
# once more with a leading "the " stripped. Applied AFTER normalization
# (already casefolded), so this is case-insensitive by construction.
_LEADING_DEFINITE_ARTICLE_PATTERN: Final[
    re.Pattern[str]
] = re.compile(
    r"^the\s+"
)


def _normalize_country_token(
    value: str,
) -> str:
    """
    Normalize a country code, name, alias, or filename token.

    Underscores are converted to spaces to support filenames such as:

        Labour_and_Employment_Law_in_Spain_2026.docx

    Unicode NFKC normalization is applied first so a name typed with
    combining diacritics (for example a decomposed "Turkiye") matches
    the same registered token as its precomposed form ("Türkiye").
    """

    return " ".join(
        unicodedata.normalize("NFKC", value)
        .replace("_", " ")
        .replace("\xa0", " ")
        .split()
    ).casefold()


def _build_country_indexes(
    countries: Iterable[
        CountryDefinition
    ],
) -> tuple[
    dict[str, CountryDefinition],
    dict[str, str],
]:
    """
    Build country indexes and fail immediately on invalid metadata.

    The validation prevents silent failures caused by:

    - duplicate country codes;
    - invalid ISO codes;
    - empty names or aliases;
    - one alias assigned to multiple countries.
    """

    countries_by_code: dict[
        str,
        CountryDefinition,
    ] = {}

    country_codes_by_alias: dict[
        str,
        str,
    ] = {}

    for country in countries:
        code = country.code

        if not _COUNTRY_CODE_PATTERN.fullmatch(
            code
        ):
            raise CountryRegistryConfigurationError(
                "Invalid country code in registry: "
                f"{code!r}. "
                "Country codes must contain exactly "
                "two uppercase letters."
            )

        if code in countries_by_code:
            raise CountryRegistryConfigurationError(
                "Duplicate country code in registry: "
                f"{code!r}."
            )

        normalized_display_name = (
            _normalize_country_token(
                country.display_name
            )
        )

        if not normalized_display_name:
            raise CountryRegistryConfigurationError(
                "Country display name must not be empty "
                f"for code {code!r}."
            )

        countries_by_code[
            code
        ] = country

        registered_tokens = (
            code,
            country.display_name,
            *country.aliases,
        )

        for token in registered_tokens:
            normalized_token = (
                _normalize_country_token(
                    token
                )
            )

            if not normalized_token:
                raise CountryRegistryConfigurationError(
                    "Country alias must not be empty "
                    f"for code {code!r}."
                )

            existing_code = (
                country_codes_by_alias.get(
                    normalized_token
                )
            )

            if (
                existing_code is not None
                and existing_code != code
            ):
                raise CountryRegistryConfigurationError(
                    "Country alias collision: "
                    f"{token!r} resolves to both "
                    f"{existing_code!r} and {code!r}. "
                    "Aliases must be unique across "
                    "the country registry."
                )

            country_codes_by_alias[
                normalized_token
            ] = code

    return (
        countries_by_code,
        country_codes_by_alias,
    )


(
    _COUNTRIES_BY_CODE,
    _COUNTRY_CODES_BY_ALIAS,
) = _build_country_indexes(
    COUNTRIES
)


def _pycountry_code_from_name(
    name: str,
) -> str | None:
    """
    Generic ISO 3166-1 fallback for any world country this curated
    registry does not specifically know about.

    Mission "ORDER 5C-GEO", section 5: country_registry.py stays a
    small, product-curated list (aliases, display names, historical
    compatibility) - never an artisanal simulation of the whole
    world's countries. That broader recognition already exists in
    pycountry's own ISO 3166-1 dataset (the project's existing
    dependency - see app/services/country_detection.py's own
    worldwide phrase map), so a name this registry has no specific
    entry for is resolved through it instead of a hand-added
    CountryDefinition.
    """

    try:
        country = pycountry.countries.lookup(name)

    except LookupError:
        return None

    return country.alpha_2.upper()


def _pycountry_display_name(
    code: str,
) -> str | None:
    """The pycountry fallback's own display name for a country code
    this registry has no curated CountryDefinition for."""

    country = pycountry.countries.get(
        alpha_2=code.upper()
    )

    if country is None:
        return None

    return country.name


def normalize_country_code(
    country_code: str,
) -> str:
    """Validate and normalize an ISO alpha-2 country code."""

    normalized_code = (
        country_code
        .strip()
        .upper()
    )

    if (
        normalized_code not in _COUNTRIES_BY_CODE
        and _pycountry_display_name(normalized_code) is None
    ):
        raise UnknownCountryCodeError(
            "Unknown country code: "
            f"{country_code!r}. "
            "Add the country to "
            "app/core/country_registry.py "
            "before ingesting its documents."
        )

    return normalized_code


def country_code_from_name(
    country_name: str,
) -> str:
    """
    Resolve a country name, alias, or filename token.

    Falls back to stripping one leading definite article ("the ") when
    the token does not otherwise match - a generic grammatical
    normalization, not a per-country special case: it never widens
    what counts as a known country, it only stops a real, already-
    registered country's name from being missed merely because a
    document happened to spell it with "the" and no exact alias for
    that phrasing was registered. Only once both of those still fail
    does the generic pycountry fallback run (_pycountry_code_from_name)
    - so a curated alias always wins first for the countries this
    product specifically knows about.
    """

    normalized_name = (
        _normalize_country_token(
            country_name
        )
    )

    country_code = (
        _COUNTRY_CODES_BY_ALIAS.get(
            normalized_name
        )
    )

    name_without_article = (
        _LEADING_DEFINITE_ARTICLE_PATTERN.sub(
            "",
            normalized_name,
        )
    )

    if country_code is None and name_without_article != normalized_name:
        country_code = (
            _COUNTRY_CODES_BY_ALIAS.get(
                name_without_article
            )
        )

    if country_code is None:
        # The generic pycountry fallback gets the same "the " stripping
        # a curated alias already benefits from above - a real world
        # country whose common name is casually written with a leading
        # article ("the Gambia", "the Bahamas") must resolve exactly
        # like "the Czech Republic" already does for a curated one,
        # never only for the ~34 countries this registry curates.
        country_code = _pycountry_code_from_name(
            name_without_article
        ) or _pycountry_code_from_name(
            normalized_name
        )

    if country_code is None:
        raise UnknownCountryNameError(
            "Unknown country name or alias: "
            f"{country_name!r}. "
            "Add the country or alias to "
            "app/core/country_registry.py "
            "before ingesting the document."
        )

    return country_code


def canonical_country_name(
    country_code: str,
) -> str:
    """Return the canonical display name for a country code."""

    normalized_code = normalize_country_code(
        country_code
    )

    definition = _COUNTRIES_BY_CODE.get(
        normalized_code
    )

    if definition is not None:
        return definition.display_name

    # normalize_country_code above already proved this code resolves
    # through the generic pycountry fallback when it is not one of
    # the curated entries.
    fallback_display_name = _pycountry_display_name(
        normalized_code
    )

    assert fallback_display_name is not None

    return fallback_display_name


def country_name_and_aliases(
    country_code: str,
) -> tuple[str, ...]:
    """
    Return the display name and every registered alias for one
    country code - the single, safe source other modules (for example
    legal_taxonomy's jurisdiction-suffix stripping) must reuse instead
    of ever keeping a second, independent list of country names. A
    code outside the curated registry (resolved through the generic
    pycountry fallback) has no curated aliases, so only its display
    name is returned.
    """

    normalized_code = normalize_country_code(
        country_code
    )

    definition = _COUNTRIES_BY_CODE.get(
        normalized_code
    )

    if definition is not None:
        return (
            definition.display_name,
            *definition.aliases,
        )

    return (
        canonical_country_name(
            normalized_code
        ),
    )


def resolve_country(
    raw_country: str,
    country_code: str | None = None,
) -> tuple[str, str]:
    """
    Resolve filename metadata to a canonical country name and code.

    When an explicit country code is supplied, it must correspond to
    the country detected in the filename.
    """

    filename_country_code = (
        country_code_from_name(
            raw_country
        )
    )

    if country_code is None:
        resolved_code = (
            filename_country_code
        )

    else:
        resolved_code = (
            normalize_country_code(
                country_code
            )
        )

        if (
            filename_country_code
            != resolved_code
        ):
            raise CountryMetadataMismatchError(
                "Country metadata mismatch: "
                f"filename token {raw_country!r} "
                f"resolves to "
                f"{filename_country_code!r}, "
                "but the supplied country code is "
                f"{resolved_code!r}."
            )

    return (
        canonical_country_name(
            resolved_code
        ),
        resolved_code,
    )