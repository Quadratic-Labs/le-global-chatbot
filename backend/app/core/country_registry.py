from dataclasses import dataclass
import re
from typing import Final, Iterable


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
    ),
    CountryDefinition(
        code="CZ",
        display_name="Czech Republic",
        aliases=(
            "Czechia",
        ),
    ),
    CountryDefinition(
        code="GR",
        display_name="Greece",
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
        code="PE",
        display_name="Peru",
    ),
    CountryDefinition(
        code="PL",
        display_name="Poland",
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
        code="GB",
        display_name="United Kingdom",
        aliases=(
            "UK",
            "Great Britain",
            "Britain",
            "the United Kingdom",
        ),
    ),
)


_COUNTRY_CODE_PATTERN: Final[
    re.Pattern[str]
] = re.compile(
    r"^[A-Z]{2}$"
)


def _normalize_country_token(
    value: str,
) -> str:
    """
    Normalize a country code, name, alias, or filename token.

    Underscores are converted to spaces to support filenames such as:

        Labour_and_Employment_Law_in_Spain_2026.docx
    """

    return " ".join(
        value
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


def normalize_country_code(
    country_code: str,
) -> str:
    """Validate and normalize an ISO alpha-2 country code."""

    normalized_code = (
        country_code
        .strip()
        .upper()
    )

    if normalized_code not in _COUNTRIES_BY_CODE:
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
    """Resolve a country name, alias, or filename token."""

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

    return _COUNTRIES_BY_CODE[
        normalized_code
    ].display_name


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