import unittest

from app.core.country_registry import (
    COUNTRIES,
    CountryDefinition,
    CountryRegistryConfigurationError,
    _build_country_indexes,
    canonical_country_name,
    country_code_from_name,
)


class CountryRegistryTests(
    unittest.TestCase
):
    def test_all_registered_tokens_resolve_to_country(
        self,
    ) -> None:
        for country in COUNTRIES:
            tokens = (
                country.code,
                country.display_name,
                *country.aliases,
            )

            for token in tokens:
                with self.subTest(
                    code=country.code,
                    token=token,
                ):
                    self.assertEqual(
                        country_code_from_name(
                            token
                        ),
                        country.code,
                    )

    def test_uk_aliases_resolve_to_gb(
        self,
    ) -> None:
        aliases = (
            "UK",
            "Great Britain",
            "Britain",
            "the United Kingdom",
            "United Kingdom",
        )

        for alias in aliases:
            with self.subTest(
                alias=alias
            ):
                self.assertEqual(
                    country_code_from_name(
                        alias
                    ),
                    "GB",
                )

        self.assertEqual(
            canonical_country_name(
                "GB"
            ),
            "United Kingdom",
        )

    def test_rejects_alias_collision(
        self,
    ) -> None:
        countries = (
            CountryDefinition(
                code="AA",
                display_name="Country Alpha",
                aliases=(
                    "Shared Alias",
                ),
            ),
            CountryDefinition(
                code="BB",
                display_name="Country Beta",
                aliases=(
                    "Shared Alias",
                ),
            ),
        )

        with self.assertRaisesRegex(
            CountryRegistryConfigurationError,
            "Country alias collision",
        ):
            _build_country_indexes(
                countries
            )

    def test_rejects_duplicate_country_code(
        self,
    ) -> None:
        countries = (
            CountryDefinition(
                code="AA",
                display_name="Country Alpha",
            ),
            CountryDefinition(
                code="AA",
                display_name="Country Beta",
            ),
        )

        with self.assertRaisesRegex(
            CountryRegistryConfigurationError,
            "Duplicate country code",
        ):
            _build_country_indexes(
                countries
            )

    def test_rejects_invalid_country_code(
        self,
    ) -> None:
        invalid_codes = (
            "gb",
            "GBR",
            "G1",
            "",
        )

        for invalid_code in invalid_codes:
            with self.subTest(
                code=invalid_code
            ):
                with self.assertRaisesRegex(
                    CountryRegistryConfigurationError,
                    "Invalid country code",
                ):
                    _build_country_indexes(
                        (
                            CountryDefinition(
                                code=invalid_code,
                                display_name=(
                                    "Invalid Country"
                                ),
                            ),
                        )
                    )

    def test_rejects_empty_alias(
        self,
    ) -> None:
        countries = (
            CountryDefinition(
                code="AA",
                display_name="Country Alpha",
                aliases=(
                    "   ",
                ),
            ),
        )

        with self.assertRaisesRegex(
            CountryRegistryConfigurationError,
            "alias must not be empty",
        ):
            _build_country_indexes(
                countries
            )


if __name__ == "__main__":
    unittest.main()