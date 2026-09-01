"""
Tests for the jurisdiction-resolution domain: the canonical country
registry (app/core/country_registry.py), worldwide country-name/alias
detection in free text, and city-to-country resolution
(app/services/country_detection.py and
app/services/jurisdiction_resolution.py) - plus the router-level wiring
in app/routers/chat.py that consumes them for direct-contact requests.

These three layers are one domain: the registry is the curated,
product-specific country list; country_detection.py adds a worldwide
(pycountry-backed) name/alias/demonym scan on top of it, plus splitting
mentioned countries by corpus availability; jurisdiction_resolution.py
adds the one piece neither of those covers - resolving a bare city
name ("Lisbon") to a country. All three are consulted, directly or
transitively, whenever a request needs to know which country a
question is about.

Resolution contract, end to end: an explicit country name/alias/
demonym anywhere in the text always wins outright; only once none is
found does a city-name match get considered, and only when it resolves
to exactly one real country - a genuinely ambiguous city name (shared
by more than one comparably-sized real city) or no match at all is
never guessed at, and is treated as if nothing had been mentioned by
the availability layer, or reported as a distinct AMBIGUOUS/
UNKNOWN_LOCALITY outcome by the lower-level resolve_jurisdiction
primitive. Population is a ranking signal between multiple real
candidates for the same matched name only - never an existence/
recognition gate: real national capitals indexed at only a few
thousand people (Vaduz, Valletta) resolve normally, while two
comparably-sized same-named cities (Barcelona ES/VE) stay ambiguous
regardless of size.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

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
from app.models.catalog import (
    LegalCatalogCountry,
    LegalCatalogResponse,
)
from app.models.chat import LegalChatRequest
from app.routers.chat import (
    _build_deterministic_hints,
    _resolve_current_country_scope,
)
from app.services.country_detection import (
    CountryDetectionError,
    JurisdictionResolutionStatus,
    detect_mentioned_country_codes,
    is_country_only_followup,
    resolve_country_availability,
    resolve_country_display_name,
    resolve_jurisdiction,
)
from app.services.country_detection import (
    resolve_city_country_codes,
)
from app.services.legal_catalog import LegalCatalogError


def _build_catalog() -> LegalCatalogResponse:
    """The small, fixed country catalog most tests below check
    availability against."""

    return LegalCatalogResponse(
        countries=[
            LegalCatalogCountry(
                country_code="GB", country="United Kingdom", chunk_count=41
            ),
            LegalCatalogCountry(
                country_code="ES", country="Spain", chunk_count=49
            ),
            LegalCatalogCountry(
                country_code="IT", country="Italy", chunk_count=63
            ),
            LegalCatalogCountry(
                country_code="CZ", country="Czech Republic", chunk_count=54
            ),
        ],
        legal_topics=[],
        subsections=[],
    )


def _catalog_provider() -> LegalCatalogResponse:
    return _build_catalog()


def _document_topic_provider(
    country_codes: list[str],
) -> dict[str, list[str]]:
    """Fake DocumentLegalTopicsProvider - no live document-topic data
    for any country, which none of the tests below concern."""

    del country_codes

    return {}


# ---------------------------------------------------------------------
# Canonical country registry (app/core/country_registry.py)
# ---------------------------------------------------------------------


class CountryRegistryTests(unittest.TestCase):
    """The curated registry's own validation and code/name lookups."""

    def test_all_registered_tokens_resolve_to_country(self) -> None:
        for country in COUNTRIES:
            tokens = (country.code, country.display_name, *country.aliases)

            for token in tokens:
                with self.subTest(code=country.code, token=token):
                    self.assertEqual(
                        country_code_from_name(token), country.code
                    )

    def test_canonical_name_reverse_lookup_for_gb(self) -> None:
        # The forward direction (every alias, GB's included, resolving
        # to its code) is already proven exhaustively above - this
        # covers the reverse direction the loop above does not touch.
        self.assertEqual(canonical_country_name("GB"), "United Kingdom")

    def test_rejects_alias_collision(self) -> None:
        countries = (
            CountryDefinition(
                code="AA",
                display_name="Country Alpha",
                aliases=("Shared Alias",),
            ),
            CountryDefinition(
                code="BB",
                display_name="Country Beta",
                aliases=("Shared Alias",),
            ),
        )

        with self.assertRaisesRegex(
            CountryRegistryConfigurationError, "Country alias collision"
        ):
            _build_country_indexes(countries)

    def test_rejects_duplicate_country_code(self) -> None:
        countries = (
            CountryDefinition(code="AA", display_name="Country Alpha"),
            CountryDefinition(code="AA", display_name="Country Beta"),
        )

        with self.assertRaisesRegex(
            CountryRegistryConfigurationError, "Duplicate country code"
        ):
            _build_country_indexes(countries)

    def test_rejects_invalid_country_code(self) -> None:
        invalid_codes = ("gb", "GBR", "G1", "")

        for invalid_code in invalid_codes:
            with self.subTest(code=invalid_code):
                with self.assertRaisesRegex(
                    CountryRegistryConfigurationError, "Invalid country code"
                ):
                    _build_country_indexes(
                        (
                            CountryDefinition(
                                code=invalid_code,
                                display_name="Invalid Country",
                            ),
                        )
                    )

    def test_leading_definite_article_is_stripped_as_fallback(self) -> None:
        # A country's real-world front matter may spell its name with
        # a leading English definite article ("the Czech Republic")
        # even when no curated alias exists for that exact phrasing.
        # country_code_from_name strips one leading "the " generically
        # as a fallback, rather than requiring every such country to
        # register an explicit "the X" alias (Netherlands, Philippines,
        # UK and USA already do; this is for the ones that don't).
        with_article = (
            "the Czech Republic",
            "The Czech Republic",
            "THE CZECH REPUBLIC",
        )

        for token in with_article:
            with self.subTest(token=token):
                self.assertEqual(country_code_from_name(token), "CZ")

    def test_leading_definite_article_fallback_does_not_invent_countries(
        self,
    ) -> None:
        # The article-stripping fallback (curated aliases and the
        # generic pycountry fallback alike) must never invent a
        # country that genuinely does not exist anywhere. "the Gambia"
        # is a real, correctly-resolving pycountry-fallback example
        # (see test_pycountry_fallback_resolves_any_world_country
        # below), not of this refusal - a wholly fictional name is
        # what actually proves the fallback still refuses to guess.
        with self.assertRaisesRegex(
            UnknownCountryNameError, "Unknown country name or alias"
        ):
            country_code_from_name("the Freedonia")

    def test_rejects_empty_alias(self) -> None:
        countries = (
            CountryDefinition(
                code="AA", display_name="Country Alpha", aliases=("   ",)
            ),
        )

        with self.assertRaisesRegex(
            CountryRegistryConfigurationError, "alias must not be empty"
        ):
            _build_country_indexes(countries)


class WorldCountryFallbackTests(unittest.TestCase):
    """
    country_registry.py stays a small, curated, product-specific list;
    world-country recognition for everything else comes from the
    generic pycountry fallback rather than a hand-added
    CountryDefinition simulating the whole world.
    """

    def test_curated_registry_is_exactly_the_34_allowlist_codes(
        self,
    ) -> None:
        # The curated registry holds only genuinely product-specific
        # aliases/history; today that happens to line up exactly with
        # the 34-country ADMIN allowlist (app.core.admin_country_
        # policy) - a coincidence of today's product scope, never an
        # assumed equivalence the code itself relies on (see
        # test_admin_country_policy.py's own, independent 34-code
        # assertion).
        self.assertEqual(len(COUNTRIES), 34)

    def test_pycountry_fallback_resolves_any_world_country(self) -> None:
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
                self.assertEqual(country_code_from_name(name), expected_code)

    def test_curated_alias_still_wins_over_pycountry_for_same_country(
        self,
    ) -> None:
        # Curated aliases (Turkiye/Turkey, UK/USA, Czechia) must keep
        # resolving exactly as before - the fallback only ever runs
        # once those already fail.
        self.assertEqual(country_code_from_name("Turkey"), "TR")
        self.assertEqual(country_code_from_name("UK"), "GB")
        self.assertEqual(country_code_from_name("USA"), "US")
        self.assertEqual(country_code_from_name("Czechia"), "CZ")

    def test_fallback_code_to_name_direction_also_works(self) -> None:
        # A code the registry does not curate (resolved only through
        # the fallback) must still produce a display name and pass
        # normalize_country_code, the exact gap that used to make a
        # disallowed-but-detected country's admin-upload chunk-
        # building crash instead of cleanly reaching the allowlist
        # rejection (see test_admin_document_replacement.py's
        # Tunisia-shaped test).
        self.assertEqual(canonical_country_name("DZ"), "Algeria")
        self.assertEqual(country_name_and_aliases("DZ"), ("Algeria",))
        self.assertEqual(normalize_country_code("dz"), "DZ")

    def test_unknown_code_and_name_are_still_rejected(self) -> None:
        with self.assertRaises(UnknownCountryCodeError):
            normalize_country_code("ZZ")

        with self.assertRaises(UnknownCountryCodeError):
            canonical_country_name("ZZ")

        with self.assertRaises(UnknownCountryNameError):
            country_code_from_name("Freedonia")


# ---------------------------------------------------------------------
# Worldwide country-name/alias detection in free text
# (app/services/country_detection.py)
# ---------------------------------------------------------------------


class CountryNameDetectionTests(unittest.TestCase):
    """detect_mentioned_country_codes: recognizing any world country
    (not only ones indexed in the corpus) named in a question."""

    def test_detects_alias_and_country_name(self) -> None:
        detected_codes = detect_mentioned_country_codes(
            "Compare notice periods in the UK and Spain."
        )

        self.assertEqual(detected_codes, ["GB", "ES"])

    def test_bare_uppercase_code_in_free_text_is_not_detected(self) -> None:
        # Bare two-letter codes are intentionally not scanned for in
        # free text, since common words can collide with real ISO
        # codes (for example "IN").
        detected_codes = detect_mentioned_country_codes(
            "What is the notice period in IT?"
        )

        self.assertEqual(detected_codes, [])

    def test_lowercase_word_is_not_country_code(self) -> None:
        detected_codes = detect_mentioned_country_codes(
            "Can it be terminated immediately?"
        )

        self.assertEqual(detected_codes, [])

    def test_detects_country_alias(self) -> None:
        detected_codes = detect_mentioned_country_codes(
            "What rules apply in Czechia?"
        )

        self.assertEqual(detected_codes, ["CZ"])

    def test_detects_country_outside_the_corpus(self) -> None:
        detected_codes = detect_mentioned_country_codes(
            "What are the overtime rules in Canada?"
        )

        self.assertEqual(detected_codes, ["CA"])


class CountryOnlyFollowupTests(unittest.TestCase):
    """
    is_country_only_followup: deterministically distinguishing a bare
    country-only follow-up ("Peru?", "What about Peru?") from a message
    that also carries its own legal subject ("Overtime in Peru?") - the
    distinction that decides whether a stored legal topic/subsection
    should be carried over to answer it. Deliberately parameterized:
    both groups exercise the same connector-word-stripping logic, only
    the phrasing (and expected outcome) differs per case.
    """

    def test_bare_or_connector_phrased_country_mentions_return_codes(
        self,
    ) -> None:
        cases = {
            "Peru?": ["PE"],
            "Australia": ["AU"],
            "What about Peru?": ["PE"],
            "And Peru?": ["PE"],
            "And Australia?": ["AU"],
            "How about the United Kingdom?": ["GB"],
            "For Spain?": ["ES"],
        }

        for question, expected_codes in cases.items():
            with self.subTest(question=question):
                self.assertEqual(
                    is_country_only_followup(question),
                    expected_codes,
                    f"expected {expected_codes!r} for {question!r}",
                )

    def test_questions_with_their_own_legal_subject_are_not_country_only(
        self,
    ) -> None:
        questions = (
            "Overtime in Peru?",
            "What about sick leave in Peru?",
            "Contacts in Spain",
            "Compare Spain and Peru",
            "Peru working conditions",
            "Dismissal in Australia",
            "Tell me about overtime rules.",
            # The critical "must keep working" case: a genuine new
            # question that happens to name a country must never be
            # misclassified as a bare country-only follow-up.
            "Tell me about working conditions in Peru.",
        )

        for question in questions:
            with self.subTest(question=question):
                self.assertIsNone(
                    is_country_only_followup(question),
                    f"expected None for {question!r}",
                )


class CountryAvailabilityTests(unittest.TestCase):
    """
    resolve_country_availability: splitting mentioned countries by
    corpus availability. Explicit country_codes always take priority
    over free-text detection, which itself takes priority over a
    city-name fallback - this is the availability-layer counterpart to
    resolve_jurisdiction below (same city-resolution primitive, but
    behind the "available/unavailable, never guessed" contract chat
    routing actually consumes, rather than resolve_jurisdiction's own
    RESOLVED/AMBIGUOUS/UNKNOWN_LOCALITY/NOT_FOUND outcomes).
    """

    def test_available_country_is_reported_as_available(self) -> None:
        availability = resolve_country_availability(
            request=LegalChatRequest(
                question="Compare notice periods in the UK and Spain."
            ),
            catalog_provider=_catalog_provider,
        )

        self.assertEqual(availability.available_codes, ["GB", "ES"])
        self.assertEqual(availability.unavailable_codes, [])

    def test_unavailable_country_is_reported_not_ignored(self) -> None:
        availability = resolve_country_availability(
            request=LegalChatRequest(question="What is the law in France?"),
            catalog_provider=_catalog_provider,
        )

        self.assertEqual(availability.available_codes, [])
        self.assertEqual(availability.unavailable_codes, ["FR"])

    def test_mixed_available_and_unavailable_countries(self) -> None:
        availability = resolve_country_availability(
            request=LegalChatRequest(
                question="Compare overtime in Spain and Canada."
            ),
            catalog_provider=_catalog_provider,
        )

        self.assertEqual(availability.available_codes, ["ES"])
        self.assertEqual(availability.unavailable_codes, ["CA"])

    def test_no_country_mentioned_is_empty_scope(self) -> None:
        availability = resolve_country_availability(
            request=LegalChatRequest(
                question="What is the statutory notice period?"
            ),
            catalog_provider=_catalog_provider,
        )

        self.assertEqual(availability.available_codes, [])
        self.assertEqual(availability.unavailable_codes, [])

    def test_explicit_codes_are_checked_against_the_catalog(self) -> None:
        catalog_called = False

        def catalog_provider() -> LegalCatalogResponse:
            nonlocal catalog_called
            catalog_called = True

            return _build_catalog()

        availability = resolve_country_availability(
            request=LegalChatRequest(
                question="Compare the UK and Spain.",
                country_codes=[" it "],
            ),
            catalog_provider=catalog_provider,
        )

        self.assertTrue(catalog_called)
        self.assertEqual(availability.available_codes, ["IT"])

    def test_explicit_unavailable_code_is_reported(self) -> None:
        availability = resolve_country_availability(
            request=LegalChatRequest(
                question="What is the law here?",
                country_codes=["ca"],
            ),
            catalog_provider=_catalog_provider,
        )

        self.assertEqual(availability.available_codes, [])
        self.assertEqual(availability.unavailable_codes, ["CA"])

    def test_catalog_error_is_wrapped(self) -> None:
        def failing_catalog_provider() -> LegalCatalogResponse:
            raise LegalCatalogError("unavailable")

        with self.assertRaises(CountryDetectionError):
            resolve_country_availability(
                request=LegalChatRequest(
                    question="What is the law in Canada?"
                ),
                catalog_provider=failing_catalog_provider,
            )

    def test_city_only_question_resolves_to_its_country(self) -> None:
        # A question naming only a city, with the city's country
        # genuinely indexed, is treated exactly as if that country had
        # been named outright.
        availability = resolve_country_availability(
            request=LegalChatRequest(question="What is the law in Madrid?"),
            catalog_provider=_catalog_provider,
        )

        self.assertEqual(availability.available_codes, ["ES"])
        self.assertEqual(availability.unavailable_codes, [])

    def test_city_only_question_for_an_unindexed_country(self) -> None:
        availability = resolve_country_availability(
            request=LegalChatRequest(question="What is the law in Lisbon?"),
            catalog_provider=_catalog_provider,
        )

        self.assertEqual(availability.available_codes, [])
        self.assertEqual(availability.unavailable_codes, ["PT"])

    def test_ambiguous_city_alone_contributes_nothing(self) -> None:
        # Barcelona alone (no explicit country) must never be guessed
        # - exactly as if no location had been mentioned at all, never
        # a silently-picked candidate.
        availability = resolve_country_availability(
            request=LegalChatRequest(
                question="What is the law in Barcelona?"
            ),
            catalog_provider=_catalog_provider,
        )

        self.assertEqual(availability.available_codes, [])
        self.assertEqual(availability.unavailable_codes, [])

    def test_explicit_country_beats_an_unrelated_city_mention(self) -> None:
        # An explicit country name already answers the question in
        # full - the city fallback must never even run, let alone add
        # anything.
        availability = resolve_country_availability(
            request=LegalChatRequest(
                question=(
                    "Compare notice periods in Spain and Canada, "
                    "for a client based in Barcelona."
                )
            ),
            catalog_provider=_catalog_provider,
        )

        self.assertEqual(availability.available_codes, ["ES"])
        self.assertEqual(availability.unavailable_codes, ["CA"])

    def test_legacy_country_indexed_outside_the_admin_allowlist_is_visible(
        self,
    ) -> None:
        # The ADMIN upload allowlist must never hide a country the
        # real catalog already has content for, even one this registry
        # only resolves through the generic pycountry fallback
        # (Algeria is not curated, and is not part of
        # ADMIN_ALLOWED_COUNTRY_CODES either).
        def catalog_with_legacy_algeria() -> LegalCatalogResponse:
            return LegalCatalogResponse(
                countries=[
                    LegalCatalogCountry(
                        country_code="DZ", country="Algeria", chunk_count=12
                    ),
                ],
                legal_topics=[],
                subsections=[],
            )

        availability = resolve_country_availability(
            request=LegalChatRequest(question="What is the law in Algeria?"),
            catalog_provider=catalog_with_legacy_algeria,
        )

        self.assertEqual(availability.available_codes, ["DZ"])
        self.assertEqual(availability.unavailable_codes, [])

    def test_slovakia_without_an_indexed_document_is_unavailable(
        self,
    ) -> None:
        # Slovakia is recognized and admin-upload-allowed, but with no
        # indexed document (not in this test's catalog), availability
        # must say so - and this must never be conflated with the
        # separate Czech contact-mapping fallback (see
        # CONTACT_COUNTRY_FALLBACK_CODES in app/routers/chat.py).
        availability = resolve_country_availability(
            request=LegalChatRequest(
                question="What is the law in Slovakia?"
            ),
            catalog_provider=_catalog_provider,
        )

        self.assertEqual(availability.available_codes, [])
        self.assertEqual(availability.unavailable_codes, ["SK"])


class CountryDisplayNameTests(unittest.TestCase):
    """resolve_country_display_name: readable names from ISO codes."""

    def test_resolves_known_code(self) -> None:
        self.assertEqual(resolve_country_display_name("CA"), "Canada")
        self.assertEqual(
            resolve_country_display_name("gb"), "United Kingdom"
        )

    def test_unknown_code_falls_back_to_the_code(self) -> None:
        self.assertEqual(resolve_country_display_name("ZZ"), "ZZ")


# ---------------------------------------------------------------------
# City-to-country resolution
# (app/services/jurisdiction_resolution.py,
#  app/services/country_detection.resolve_jurisdiction)
# ---------------------------------------------------------------------


class UnambiguousCityResolutionTests(unittest.TestCase):
    """Real, demonstrably dominant cities resolve to their real
    country via resolve_jurisdiction."""

    def test_clear_cities_resolve_to_their_real_dominant_country(
        self,
    ) -> None:
        cases = {
            "employment law in Lisbon": "PT",
            "termination rules in Madrid": "ES",
            "what are the labor laws in Tokyo?": "JP",
            "employment contracts in Tunis": "TN",
            "contact rules in Paris": "FR",
            "labour rules in London": "GB",
        }

        for question, expected_code in cases.items():
            with self.subTest(question=question):
                resolution = resolve_jurisdiction(question)

                self.assertEqual(
                    resolution.status, JurisdictionResolutionStatus.RESOLVED
                )
                self.assertEqual(resolution.country_code, expected_code)


class LongestMatchFirstTests(unittest.TestCase):
    """
    A real multi-word city name must never be destroyed because one of
    its own component words separately matches a different, unrelated
    city - this is the same longest-span-first scan used for every
    city name in the index, never a New-York-specific hardcode.
    """

    def test_new_york_resolves_to_the_united_states(self) -> None:
        # "York" alone (England, population 156,135) would otherwise
        # match and wrongly resolve the whole question to the United
        # Kingdom. geonamescache lists "New York" as a genuine
        # alternate name of "New York City" (population 8,804,190) -
        # indexed here, not hardcoded.
        resolution = resolve_jurisdiction(
            "What are the labor laws in New York?"
        )

        self.assertEqual(
            resolution.status, JurisdictionResolutionStatus.RESOLVED
        )
        self.assertEqual(resolution.country_code, "US")

    def test_new_york_with_explicit_country_still_resolves_to_us(
        self,
    ) -> None:
        resolution = resolve_jurisdiction(
            "employment law in New York, United States"
        )

        self.assertEqual(
            resolution.status, JurisdictionResolutionStatus.RESOLVED
        )
        self.assertEqual(resolution.country_code, "US")

    def test_bare_york_alone_stays_ambiguous(self) -> None:
        # Without "New" in front, "York" really is ambiguous on this
        # dataset (England vs. a same-named, much smaller US town) -
        # the longest available match is just the single word itself.
        resolution = resolve_jurisdiction("employment law in York")

        self.assertEqual(
            resolution.status, JurisdictionResolutionStatus.AMBIGUOUS
        )

    def test_los_angeles_multi_word_name_resolves(self) -> None:
        # A second, independently-verified multi-word city, proving
        # the longest-match support is generic, not New-York-specific.
        resolution = resolve_jurisdiction("rules in Los Angeles")

        self.assertEqual(
            resolution.status, JurisdictionResolutionStatus.RESOLVED
        )
        self.assertEqual(resolution.country_code, "US")


class CommonWordLocationContextTests(unittest.TestCase):
    """
    The exact same lexical string can be a common English word or a
    real location, and only context (never population) tells the two
    apart. Each pair below deliberately shows the same word without
    and with location context, so the outcome flip is visible.
    """

    def test_male_without_context_is_not_a_location(self) -> None:
        resolution = resolve_jurisdiction("Is there a male employee quota?")

        self.assertEqual(
            resolution.status, JurisdictionResolutionStatus.NOT_FOUND
        )

    def test_male_with_context_resolves_to_maldives(self) -> None:
        resolution = resolve_jurisdiction("What is employment law in Male?")

        self.assertEqual(
            resolution.status, JurisdictionResolutionStatus.RESOLVED
        )
        self.assertEqual(resolution.country_code, "MV")

    def test_reading_as_a_verb_is_not_a_location(self) -> None:
        resolution = resolve_jurisdiction(
            "What are the rules about reading employment contracts?"
        )

        self.assertEqual(
            resolution.status, JurisdictionResolutionStatus.NOT_FOUND
        )

    def test_reading_with_context_is_a_real_but_ambiguous_city(
        self,
    ) -> None:
        # Reading, England (318,014) and Reading, Pennsylvania (87,879)
        # are both real, comparably-scaled cities sharing the name -
        # under 10x apart, so this stays a genuine ambiguity (never
        # guessed) rather than NOT_FOUND: context changes the outcome
        # from "not a location" to "a real, if ambiguous, one", never
        # to a silent guess.
        resolution = resolve_jurisdiction(
            "What are employment laws in Reading?"
        )

        self.assertEqual(
            resolution.status, JurisdictionResolutionStatus.AMBIGUOUS
        )
        self.assertIn("GB", resolution.candidate_country_codes)
        self.assertIn("US", resolution.candidate_country_codes)

    def test_bath_with_context_resolves_to_the_uk(self) -> None:
        resolution = resolve_jurisdiction(
            "What are the employment rules in Bath?"
        )

        self.assertEqual(
            resolution.status, JurisdictionResolutionStatus.RESOLVED
        )
        self.assertEqual(resolution.country_code, "GB")

    def test_join_a_union_is_not_falsely_resolved_to_the_us(self) -> None:
        # The original regression this whole guard exists for -
        # "union" has no location context here at all.
        resolution = resolve_jurisdiction(
            "What rights do employees have to join a union?"
        )

        self.assertEqual(
            resolution.status, JurisdictionResolutionStatus.NOT_FOUND
        )

    def test_union_with_context_is_a_real_location(self) -> None:
        # The same word, with genuine location context this time -
        # context is what changed, never a population threshold (this
        # city's population, ~56,800, is well under the 400,000
        # absolute floor an earlier revision of this module enforced
        # and no longer does).
        resolution = resolve_jurisdiction("What is employment law in Union?")

        self.assertEqual(
            resolution.status, JurisdictionResolutionStatus.RESOLVED
        )

    def test_bare_city_name_alone_has_implicit_context(self) -> None:
        # A message that is nothing but the place name is itself an
        # obviously intentional location question - no preposition
        # needed when there is nothing else in the message at all.
        resolution = resolve_jurisdiction("Lisbon")

        self.assertEqual(
            resolution.status, JurisdictionResolutionStatus.RESOLVED
        )
        self.assertEqual(resolution.country_code, "PT")


class SubstantialSmallCityResolutionTests(unittest.TestCase):
    """
    Population must never gate recognition outright. Every city below
    is a real national capital, chosen after inspecting the live
    geonamescache dataset, each at a small fraction of the 400,000
    absolute floor an earlier revision enforced and no longer does.
    """

    def test_small_national_capitals_resolve(self) -> None:
        cases = {
            "employment law in Vaduz": "LI",  # population ~5,197
            "employment law in Valletta": "MT",  # population ~6,794
            "employment law in Apia": "WS",  # population ~40,407
            "employment law in Majuro": "MH",  # population ~25,400
            "employment law in Castries": "LC",  # population ~20,000
        }

        for question, expected_code in cases.items():
            with self.subTest(question=question):
                resolution = resolve_jurisdiction(question)

                self.assertEqual(
                    resolution.status, JurisdictionResolutionStatus.RESOLVED
                )
                self.assertEqual(resolution.country_code, expected_code)


class AmbiguousCityResolutionTests(unittest.TestCase):
    """A city name genuinely shared by more than one comparably-sized
    real city must never be silently guessed, Barcelona included, and
    never via a Barcelona-specific check."""

    def test_barcelona_alone_is_ambiguous(self) -> None:
        resolution = resolve_jurisdiction(
            "social media rules at work in Barcelona"
        )

        self.assertEqual(
            resolution.status, JurisdictionResolutionStatus.AMBIGUOUS
        )
        self.assertIn("ES", resolution.candidate_country_codes)
        self.assertIn("VE", resolution.candidate_country_codes)

    def test_other_real_ties_are_also_ambiguous(self) -> None:
        for question in ("rules in Valencia", "rules in Cambridge"):
            with self.subTest(question=question):
                resolution = resolve_jurisdiction(question)

                self.assertEqual(
                    resolution.status, JurisdictionResolutionStatus.AMBIGUOUS
                )
                self.assertGreaterEqual(
                    len(resolution.candidate_country_codes), 2
                )

    def test_explicit_country_dominates_an_ambiguous_city_name(
        self,
    ) -> None:
        # "Barcelona, Spain" must never be flagged ambiguous by
        # "Barcelona" alone.
        resolution = resolve_jurisdiction("Barcelona, Spain")

        self.assertEqual(
            resolution.status, JurisdictionResolutionStatus.RESOLVED
        )
        self.assertEqual(resolution.country_code, "ES")


class DominancePolicyBoundaryTests(unittest.TestCase):
    """
    The population-ratio tie-break (_DOMINANT_POPULATION_RATIO = 10)
    is audited against the real, current dataset (3,649 city names
    with 2+ country candidates), not re-picked on intuition. A handful
    of real cities sitting close to the ratio-10 boundary, in both
    directions, pinned down as regression tests.
    """

    def test_cities_just_above_the_ratio_resolve(self) -> None:
        cases = {
            # Washington, US (689,545) / Washington, GB (67,085):
            # ~10.28x.
            "employment law in Washington": "US",
            # Panama City, PA (408,168) / Panama City, US (38,286):
            # ~10.66x.
            "employment law in Panama": "PA",
        }

        for question, expected_code in cases.items():
            with self.subTest(question=question):
                resolution = resolve_jurisdiction(question)

                self.assertEqual(
                    resolution.status, JurisdictionResolutionStatus.RESOLVED
                )
                self.assertEqual(resolution.country_code, expected_code)

    def test_cities_just_below_the_ratio_stay_ambiguous(self) -> None:
        # Geneva, CH (201,741) / Geneva, US (21,806): ~9.25x - a real
        # same-named US city exists, so this stays a genuine ambiguity
        # under "never guess", even though most people asking about
        # "Geneva" mean Switzerland.
        resolution = resolve_jurisdiction("employment law in Geneva")

        self.assertEqual(
            resolution.status, JurisdictionResolutionStatus.AMBIGUOUS
        )


class UnknownLocalityTests(unittest.TestCase):
    """A question that clearly names a place this dataset does not
    recognize must never fabricate a country - a genuinely different
    situation from "no location mentioned at all"."""

    def test_unrecognized_capitalized_place_is_unknown_locality(
        self,
    ) -> None:
        for question in (
            "employment law in Ruritania",
            "What are the labor laws in Freedonia?",
        ):
            with self.subTest(question=question):
                resolution = resolve_jurisdiction(question)

                self.assertEqual(
                    resolution.status,
                    JurisdictionResolutionStatus.UNKNOWN_LOCALITY,
                )
                self.assertIsNotNone(resolution.matched_location)
                self.assertIsNone(resolution.country_code)
                self.assertEqual(resolution.candidate_country_codes, ())

    def test_matched_location_keeps_original_casing(self) -> None:
        resolution = resolve_jurisdiction("employment law in Ruritania")

        self.assertEqual(resolution.matched_location, "Ruritania")

    def test_no_capitalized_phrase_after_preposition_is_not_found(
        self,
    ) -> None:
        # No preposition-led capitalized phrase at all - genuinely
        # nothing to ask the user about, unlike the cases above.
        resolution = resolve_jurisdiction("What are the rules on termination?")

        self.assertEqual(
            resolution.status, JurisdictionResolutionStatus.NOT_FOUND
        )


class NotFoundAndFalsePositiveTests(unittest.TestCase):
    """A conservative resolver, never a naive world-city scan of every
    sentence."""

    def test_no_location_returns_not_found(self) -> None:
        questions = (
            "What are the rules on termination?",
            "What are the social media rules at work?",
            "How long can probation last?",
            "What rules apply to employee monitoring?",
        )

        for question in questions:
            with self.subTest(question=question):
                resolution = resolve_jurisdiction(question)

                self.assertEqual(
                    resolution.status, JurisdictionResolutionStatus.NOT_FOUND
                )

    def test_legal_topic_words_never_match_as_cities(self) -> None:
        # None of these has any location-introducing preposition in
        # front of it, so none should even be considered a candidate,
        # whether or not the word happens to also be a place name
        # somewhere in the world.
        codes, _ = resolve_city_country_codes(
            "overtime social media monitoring probation dismissal "
            "termination contract benefits union"
        )

        self.assertEqual(codes, frozenset())


class MultiJurisdictionRegressionTests(unittest.TestCase):
    """A country comparison must never regress into a single-country
    resolution."""

    def test_two_explicit_countries_is_ambiguous_for_this_primitive(
        self,
    ) -> None:
        # resolve_jurisdiction answers ONE jurisdiction; a real
        # multi-country comparison must keep using
        # detect_mentioned_country_codes directly (see
        # resolve_country_availability above, which does exactly that).
        resolution = resolve_jurisdiction("Compare France and Germany")

        self.assertEqual(
            resolution.status, JurisdictionResolutionStatus.AMBIGUOUS
        )
        self.assertEqual(
            set(resolution.candidate_country_codes), {"FR", "DE"}
        )


class ResolveCityCountryCodesTests(unittest.TestCase):
    """Direct tests of the lower-level, city-only primitive."""

    def test_multi_word_city_names_are_matched_intact(self) -> None:
        # The primitive-level counterpart to LongestMatchFirstTests
        # above: multi-word names are matched whole.
        codes, matched = resolve_city_country_codes("rules in Los Angeles")

        self.assertEqual(codes, frozenset({"US"}))
        self.assertEqual(matched, "los angeles")

    def test_short_words_are_never_matched(self) -> None:
        # Below _MINIMUM_CITY_NAME_LENGTH - guards against short,
        # common-word collisions with tiny place names.
        codes, _ = resolve_city_country_codes("Are you sure?")

        self.assertEqual(codes, frozenset())

    def test_without_location_context_nothing_matches(self) -> None:
        # "Lisbon" is a real, otherwise-unambiguous city, but with no
        # preposition in front of it and not standing alone as the
        # whole message, it is not treated as a location reference.
        codes, matched = resolve_city_country_codes(
            "Lisbon is a beautiful place to talk about"
        )

        self.assertEqual(codes, frozenset())
        self.assertIsNone(matched)


class PeriodAbbreviatedNameTests(unittest.TestCase):
    """
    "St." was once indexed with its period kept, but real text always
    tokenizes it without one, splitting the period and period-free
    spellings of the same real-world place into two disjoint,
    inconsistent candidate sets. Normalization now strips periods on
    both the indexing and the scanning side.
    """

    def test_st_petersburg_and_saint_petersburg_agree(self) -> None:
        # Real-world data: Russia's Saint Petersburg (5,351,935) vs.
        # the US St. Petersburg, Florida (257,083) - a genuine ~20.8x
        # dominance, so both spellings must resolve to the same
        # dominant answer, never silently disagree with each other.
        for question in (
            "I run a small business in St. Petersburg, Florida.",
            "What is the process for incorporation in St Petersburg?",
            "What is the process for incorporation in Saint Petersburg?",
        ):
            with self.subTest(question=question):
                resolution = resolve_jurisdiction(question)

                self.assertEqual(
                    resolution.status, JurisdictionResolutionStatus.RESOLVED
                )
                self.assertEqual(resolution.country_code, "RU")

    def test_st_johns_and_saint_johns_agree(self) -> None:
        # Real-world data: Antigua and Barbuda's capital (51,737) vs.
        # Canada's St. John's, Newfoundland (110,525) - genuinely close
        # (~2.1x), so both spellings must agree it is a real,
        # unresolved tie between exactly these two, never silently
        # drop one of them depending on how the period was written.
        for question in (
            "What is the minimum wage in St. John's?",
            "What is the minimum wage in St John's?",
            "What is the minimum wage in Saint John's?",
        ):
            with self.subTest(question=question):
                resolution = resolve_jurisdiction(question)

                self.assertEqual(
                    resolution.status, JurisdictionResolutionStatus.AMBIGUOUS
                )
                self.assertEqual(
                    set(resolution.candidate_country_codes), {"AG", "CA"}
                )


class CountryAggregateNicknameTests(unittest.TestCase):
    """
    geonamescache's country/territory-aggregate city entries (name
    identical to their own country's name, e.g. "Hong Kong",
    "Singapore") list historical or touristic nicknames as alternate
    names ("Victoria", "Garden City"); those nicknames must never
    inherit the whole territory's population and silently outrank
    every real, unrelated city of the same name elsewhere - a real
    city, never a country, is what "Victoria"/"Garden City" alone
    should ever be weighed as.
    """

    def test_victoria_no_longer_forces_hong_kong(self) -> None:
        resolution = resolve_jurisdiction(
            "What is the minimum notice period for termination "
            "in Victoria?"
        )

        # Real Victoria/Canada (289,625), Victoria/Mexico (332,100),
        # and Victoria HK's own real settlement (956,800, once no
        # longer inflated to the whole territory's 7,396,076) are all
        # within a real, un-dominated spread - a genuine tie, never
        # silently forced to Hong Kong.
        self.assertEqual(
            resolution.status, JurisdictionResolutionStatus.AMBIGUOUS
        )
        self.assertNotEqual(resolution.country_code, "HK")
        self.assertIn("CA", resolution.candidate_country_codes)

    def test_garden_city_no_longer_forces_singapore(self) -> None:
        resolution = resolve_jurisdiction(
            "What are the labor laws for small businesses in "
            "Garden City?"
        )

        # Real Garden City entries in Egypt/GB/US remain, once
        # Singapore's own nickname-inflated population is removed.
        self.assertEqual(
            resolution.status, JurisdictionResolutionStatus.AMBIGUOUS
        )
        self.assertNotEqual(resolution.country_code, "SG")
        self.assertIn("US", resolution.candidate_country_codes)

    def test_hong_kong_and_singapore_own_names_still_resolve(self) -> None:
        # Only the NICKNAME alternates are excluded - the aggregate
        # entry's own primary name is unaffected.
        self.assertEqual(
            resolve_jurisdiction("employment law in Hong Kong").country_code,
            "HK",
        )
        self.assertEqual(
            resolve_jurisdiction("employment law in Singapore").country_code,
            "SG",
        )


class CompoundPlaceNameFallbackTests(unittest.TestCase):
    """
    A real, qualified place name geonamescache never recorded as a
    multi-word alternate ("Kingston upon Thames") must never silently
    fall back to resolving as if only its bare, unrelated, ambiguous
    head word ("Kingston") had been said - the correct country is not
    even among that word's own candidates, so offering it would be
    worse than admitting no match. A structurally identical name the
    dataset DOES record ("Newcastle upon Tyne", "Kingston upon Hull")
    must keep resolving normally - the fix only ever suppresses the
    single-word fallback, never a real multi-word match.
    """

    def test_kingston_upon_thames_never_offers_the_wrong_countries(
        self,
    ) -> None:
        resolution = resolve_jurisdiction(
            "employment law in Kingston upon Thames"
        )

        self.assertNotEqual(
            resolution.status, JurisdictionResolutionStatus.AMBIGUOUS
        )
        self.assertNotIn("GB", resolution.candidate_country_codes)

    def test_bare_kingston_is_still_genuinely_ambiguous(self) -> None:
        # Unaffected by the fix - a genuinely bare, unqualified mention
        # still surfaces its own real ambiguity.
        resolution = resolve_jurisdiction("employment law in Kingston")

        self.assertEqual(
            resolution.status, JurisdictionResolutionStatus.AMBIGUOUS
        )

    def test_newcastle_upon_tyne_and_kingston_upon_hull_still_resolve(
        self,
    ) -> None:
        for question, expected_code in (
            ("employment law in Newcastle upon Tyne", "GB"),
            ("employment law in Kingston upon Hull", "GB"),
        ):
            with self.subTest(question=question):
                resolution = resolve_jurisdiction(question)

                self.assertEqual(
                    resolution.status, JurisdictionResolutionStatus.RESOLVED
                )
                self.assertEqual(resolution.country_code, expected_code)


class DistinctCityMentionsNeverContaminateTests(unittest.TestCase):
    """
    Two individually unambiguous cities named together must never
    merge into a fabricated ambiguity that mixes in an unrelated,
    non-dominant candidate from the OTHER city - each matched city is
    narrowed to its own real answer first, independently, before two
    or more distinct matches are ever combined.
    """

    def test_paris_and_berlin_together_offer_exactly_those_countries(
        self,
    ) -> None:
        resolution = resolve_jurisdiction(
            "Compare termination rules in Paris and in Berlin"
        )

        self.assertEqual(
            resolution.status, JurisdictionResolutionStatus.AMBIGUOUS
        )
        self.assertEqual(
            set(resolution.candidate_country_codes), {"FR", "DE"}
        )

    def test_matched_location_names_every_matched_city(self) -> None:
        resolution = resolve_jurisdiction(
            "Compare termination rules in Paris and in Berlin"
        )

        self.assertIn("paris", resolution.matched_location)
        self.assertIn("berlin", resolution.matched_location)


# ---------------------------------------------------------------------
# Router-level wiring (app/routers/chat.py)
#
# These exercise chat.py's own combination logic on top of the
# primitives above - contact-intent detection, the city fallback, and
# capital-preference among several supported candidates - rather than
# the primitives directly. Different boundary from every test above:
# kept separate rather than folded into CountryAvailabilityTests.
# ---------------------------------------------------------------------


def _fake_country_resolution(
    *, request: LegalChatRequest, catalog_provider
):
    """Stand in for resolve_country_availability, echoing back
    whatever country_codes the caller asked about as available - lets
    a test isolate _resolve_current_country_scope's own city-to-
    country wiring from real catalog matching (covered directly by
    CountryAvailabilityTests above)."""

    del catalog_provider

    return SimpleNamespace(
        available_codes=list(request.country_codes),
        unavailable_codes=[],
    )


class DirectContactCityToCountryWiringTests(unittest.TestCase):
    """_resolve_current_country_scope: for a direct-contact question
    naming only a city, resolving that city to a country (or correctly
    declining to, when ambiguous) before contact routing proceeds."""

    def test_direct_contact_for_paris_resolves_france(self) -> None:
        with mock.patch(
            "app.routers.chat.resolve_country_availability",
            side_effect=_fake_country_resolution,
        ):
            scope = _resolve_current_country_scope(
                LegalChatRequest(
                    question="Can I have the contact details for Paris?"
                ),
                lambda: None,
            )

        self.assertEqual(scope.available_codes, ["FR"])

    def test_ambiguous_milan_is_not_guessed(self) -> None:
        # Milan (IT/US, ~9.51x - below the dominance ratio) has no
        # single dominant country and is not a national capital of
        # either candidate, so the router must decline to guess rather
        # than picking one.
        with mock.patch(
            "app.routers.chat.resolve_country_availability",
            side_effect=_fake_country_resolution,
        ):
            scope = _resolve_current_country_scope(
                LegalChatRequest(
                    question="Can I have the contact details for Milan?"
                ),
                lambda: None,
            )

        self.assertEqual(scope.available_codes, [])
        self.assertEqual(scope.unavailable_codes, [])


class DeterministicHintsContactSignalTests(unittest.TestCase):
    """
    _build_deterministic_hints's strong_contact_signal is computed
    directly from the question text, independently of whichever
    country/city scope _resolve_current_country_scope resolves -
    mocking the latter here isolates that independence: the same
    contact-intent phrasing must be detected whether it names a
    country or a city, and the resolved scope it is paired with must
    still be the one returned by _resolve_current_country_scope
    unchanged.
    """

    def test_final_direct_contact_signal_for_country(self) -> None:
        fake_scope = SimpleNamespace(
            available_codes=["IT"], unavailable_codes=[]
        )

        with mock.patch(
            "app.routers.chat._resolve_current_country_scope",
            return_value=fake_scope,
        ):
            hints, scope, _ = _build_deterministic_hints(
                request=LegalChatRequest(
                    question="Can I have the contact details for Italy?"
                ),
                catalog_provider=_catalog_provider,
                document_topic_provider=_document_topic_provider,
            )

        self.assertTrue(hints.strong_contact_signal)
        self.assertEqual(scope.available_codes, ["IT"])

    def test_final_direct_contact_signal_for_city(self) -> None:
        fake_scope = SimpleNamespace(
            available_codes=["FR"], unavailable_codes=[]
        )

        with mock.patch(
            "app.routers.chat._resolve_current_country_scope",
            return_value=fake_scope,
        ):
            hints, scope, _ = _build_deterministic_hints(
                request=LegalChatRequest(
                    question="Can I have the contact details for Paris?"
                ),
                catalog_provider=_catalog_provider,
                document_topic_provider=_document_topic_provider,
            )

        self.assertTrue(hints.strong_contact_signal)
        self.assertEqual(scope.available_codes, ["FR"])


if __name__ == "__main__":
    unittest.main()
