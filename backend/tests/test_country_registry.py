import unittest

from app.core.country_registry import (
    COUNTRIES,
    CountryDefinition,
    CountryRegistryConfigurationError,
    UnknownCountryCodeError,
    UnknownCountryNameError,
    _build_country_indexes,
    canonical_country_name,
    country_code_from_name,
    country_name_and_aliases,
    normalize_country_code,
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

    def test_leading_definite_article_is_stripped_as_fallback(
        self,
    ) -> None:
        # Mission "ORDER 2": Czech Republic's real front matter reads
        # "...in the Czech Republic" - the registry had no "the Czech
        # Republic" alias (unlike Netherlands/Philippines/UK/USA,
        # which already had one), so this raised
        # UnknownCountryNameError, masked by the reindex router into a
        # generic 502. The fix is generic (a fallback in
        # country_code_from_name), not a new per-country alias.
        with_article = (
            "the Czech Republic",
            "The Czech Republic",
            "THE CZECH REPUBLIC",
        )

        for token in with_article:
            with self.subTest(
                token=token
            ):
                self.assertEqual(
                    country_code_from_name(
                        token
                    ),
                    "CZ",
                )

    def test_leading_definite_article_fallback_does_not_invent_countries(
        self,
    ) -> None:
        # The article-stripping fallback (curated aliases and the
        # generic pycountry fallback alike - mission "ORDER 5C-GEO")
        # must never invent a country that genuinely does not exist
        # anywhere, curated or not. "the Gambia" is now a real,
        # correctly-resolving example of the pycountry fallback (see
        # test_pycountry_fallback_resolves_any_world_country below),
        # not of this refusal - a wholly fictional name is what
        # actually proves the fallback still refuses to guess.
        with self.assertRaisesRegex(
            UnknownCountryNameError,
            "Unknown country name or alias",
        ):
            country_code_from_name(
                "the Freedonia"
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


class WorldCountryFallbackTests(unittest.TestCase):
    """
    Mission "ORDER 5C-GEO", section 5: country_registry.py stays a
    small, curated, product-specific list - world-country recognition
    for everything else comes from the generic pycountry fallback,
    never a hand-added CountryDefinition simulating the whole world.
    """

    def test_curated_registry_is_exactly_the_34_allowlist_codes(
        self,
    ) -> None:
        # Mission "ORDER 5C" registered several countries (France,
        # Germany, Tunisia, Egypt, Morocco, Austria, Denmark among
        # them) purely to make the allowlist-rejection distinction
        # work before this fallback existed. Now that any world
        # country resolves generically, those detection-only entries
        # are redundant and have been removed - the curated registry
        # holds only genuinely product-specific aliases/history, which
        # today happens to line up exactly with the 34-country ADMIN
        # allowlist (app.core.admin_country_policy) - a coincidence of
        # today's product scope, never an assumed equivalence the code
        # itself relies on (see test_admin_country_policy.py's own,
        # completely independent 34-code assertion).
        self.assertEqual(len(COUNTRIES), 34)

    def test_pycountry_fallback_resolves_any_world_country(
        self,
    ) -> None:
        # Deliberately countries with NO curated CountryDefinition at
        # all - the whole point of this fallback.
        cases = {
            "Algeria": "DZ",
            "Tunisia": "TN",
            "Egypt": "EG",
            "Morocco": "MA",
            "Austria": "AT",
            "Denmark": "DK",
            "Kenya": "KE",
            "the Gambia": "GM",
        }

        for name, expected_code in cases.items():
            with self.subTest(name=name):
                self.assertEqual(
                    country_code_from_name(name),
                    expected_code,
                )

    def test_curated_alias_still_wins_over_pycountry_for_same_country(
        self,
    ) -> None:
        # Curated aliases (Turkiye/Turkey, UK/USA, Czechia) must keep
        # resolving exactly as before - the fallback only ever runs
        # once those already fail.
        self.assertEqual(country_code_from_name("Turkey"), "TR")
        self.assertEqual(country_code_from_name("UK"), "GB")
        self.assertEqual(country_code_from_name("USA"), "US")
        self.assertEqual(
            country_code_from_name("Czechia"), "CZ"
        )

    def test_fallback_code_to_name_direction_also_works(
        self,
    ) -> None:
        # A code the registry does not curate (resolved only through
        # the fallback) must still produce a display name and a
        # normalize_country_code pass - the exact gap that, before
        # being fixed, made a disallowed-but-detected country's own
        # admin-upload chunk-building crash instead of cleanly
        # reaching the allowlist rejection (see
        # test_admin_document_replacement.py's Tunisia-shaped test).
        self.assertEqual(canonical_country_name("DZ"), "Algeria")
        self.assertEqual(
            country_name_and_aliases("DZ"), ("Algeria",)
        )
        self.assertEqual(normalize_country_code("dz"), "DZ")

    def test_unknown_code_and_name_are_still_rejected(
        self,
    ) -> None:
        with self.assertRaises(UnknownCountryCodeError):
            normalize_country_code("ZZ")

        with self.assertRaises(UnknownCountryCodeError):
            canonical_country_name("ZZ")

        with self.assertRaises(UnknownCountryNameError):
            country_code_from_name("Freedonia")


if __name__ == "__main__":
    unittest.main()