"""
Tests for city resolution (app/services/jurisdiction_resolution.py)
and the combined country-then-city primitive
(country_detection.resolve_jurisdiction) - mission "ORDER 5C-GEO" and
its corrective gate (context-aware resolution, longest-match-first,
population as ranking only).
"""

from __future__ import annotations

import unittest

from app.services.country_detection import (
    JurisdictionResolutionStatus,
    resolve_jurisdiction,
)
from app.services.jurisdiction_resolution import (
    resolve_city_country_codes,
)


class UnambiguousCityResolutionTests(unittest.TestCase):
    """Mission section 12 - real, demonstrably dominant cities."""

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
                    resolution.status,
                    JurisdictionResolutionStatus.RESOLVED,
                )
                self.assertEqual(
                    resolution.country_code, expected_code
                )


class LongestMatchFirstTests(unittest.TestCase):
    """
    Corrective gate, section 3/6 - a real multi-word city name must
    never be destroyed because one of its own component words
    separately matches a different, unrelated city. Never a New-York-
    specific hardcode: this is the same longest-span-first scan used
    for every city name in the index.
    """

    def test_new_york_resolves_to_the_united_states(self) -> None:
        # "York" alone (England, population 156,135) would otherwise
        # match and wrongly resolve the whole question to the United
        # Kingdom - the real regression this corrective gate exists to
        # fix. geonamescache lists "New York" as a genuine alternate
        # name of "New York City" (population 8,804,190) - indexed
        # here, not hardcoded.
        resolution = resolve_jurisdiction(
            "What are the labor laws in New York?"
        )

        self.assertEqual(
            resolution.status,
            JurisdictionResolutionStatus.RESOLVED,
        )
        self.assertEqual(resolution.country_code, "US")

    def test_new_york_with_explicit_country_still_resolves_to_us(
        self,
    ) -> None:
        resolution = resolve_jurisdiction(
            "employment law in New York, United States"
        )

        self.assertEqual(
            resolution.status,
            JurisdictionResolutionStatus.RESOLVED,
        )
        self.assertEqual(resolution.country_code, "US")

    def test_bare_york_alone_stays_ambiguous(self) -> None:
        # Without "New" in front, "York" really is ambiguous on this
        # dataset (England vs. a same-named, much smaller US town) -
        # the longest available match is just the single word itself.
        resolution = resolve_jurisdiction("employment law in York")

        self.assertEqual(
            resolution.status,
            JurisdictionResolutionStatus.AMBIGUOUS,
        )

    def test_los_angeles_multi_word_name_resolves(self) -> None:
        # A second, independently-verified multi-word city (not just
        # New York) - proves the longest-match support is generic.
        resolution = resolve_jurisdiction("rules in Los Angeles")

        self.assertEqual(
            resolution.status,
            JurisdictionResolutionStatus.RESOLVED,
        )
        self.assertEqual(resolution.country_code, "US")


class CommonWordLocationContextTests(unittest.TestCase):
    """
    Corrective gate, sections 5/8 - the exact same lexical string can
    be a common English word or a real location, and only context
    (never population) tells the two apart.
    """

    def test_male_without_context_is_not_a_location(self) -> None:
        resolution = resolve_jurisdiction(
            "Is there a male employee quota?"
        )

        self.assertEqual(
            resolution.status,
            JurisdictionResolutionStatus.NOT_FOUND,
        )

    def test_male_with_context_resolves_to_maldives(self) -> None:
        resolution = resolve_jurisdiction(
            "What is employment law in Male?"
        )

        self.assertEqual(
            resolution.status,
            JurisdictionResolutionStatus.RESOLVED,
        )
        self.assertEqual(resolution.country_code, "MV")

    def test_reading_as_a_verb_is_not_a_location(self) -> None:
        resolution = resolve_jurisdiction(
            "What are the rules about reading employment contracts?"
        )

        self.assertEqual(
            resolution.status,
            JurisdictionResolutionStatus.NOT_FOUND,
        )

    def test_reading_with_context_is_a_real_but_ambiguous_city(
        self,
    ) -> None:
        # Reading, England (318,014) and Reading, Pennsylvania
        # (87,879) are both real, comparably-scaled cities sharing the
        # name - under 10x apart, so this stays a genuine ambiguity
        # (never guessed) rather than NOT_FOUND (mission section 8:
        # "cette conclusion dépend du contexte" - context changes the
        # outcome from "not a location" to "a real, if ambiguous,
        # one", never to a silent guess).
        resolution = resolve_jurisdiction(
            "What are employment laws in Reading?"
        )

        self.assertEqual(
            resolution.status,
            JurisdictionResolutionStatus.AMBIGUOUS,
        )
        self.assertIn("GB", resolution.candidate_country_codes)
        self.assertIn("US", resolution.candidate_country_codes)

    def test_bath_with_context_resolves_to_the_uk(self) -> None:
        resolution = resolve_jurisdiction(
            "What are the employment rules in Bath?"
        )

        self.assertEqual(
            resolution.status,
            JurisdictionResolutionStatus.RESOLVED,
        )
        self.assertEqual(resolution.country_code, "GB")

    def test_join_a_union_is_not_falsely_resolved_to_the_us(
        self,
    ) -> None:
        # The original regression this whole guard exists for -
        # "union" has no location context here at all.
        resolution = resolve_jurisdiction(
            "What rights do employees have to join a union?"
        )

        self.assertEqual(
            resolution.status,
            JurisdictionResolutionStatus.NOT_FOUND,
        )

    def test_union_with_context_is_a_real_location(self) -> None:
        # The same word, with genuine location context this time -
        # context is what changed, never a population threshold (this
        # city's population, ~56,800, is well under the 400,000
        # absolute floor a previous revision of this module used to
        # enforce and this corrective gate removed).
        resolution = resolve_jurisdiction(
            "What is employment law in Union?"
        )

        self.assertEqual(
            resolution.status,
            JurisdictionResolutionStatus.RESOLVED,
        )

    def test_bare_city_name_alone_has_implicit_context(self) -> None:
        # A message that is nothing but the place name is itself an
        # obviously intentional location question - no preposition
        # needed when there is nothing else in the message at all.
        resolution = resolve_jurisdiction("Lisbon")

        self.assertEqual(
            resolution.status,
            JurisdictionResolutionStatus.RESOLVED,
        )
        self.assertEqual(resolution.country_code, "PT")


class SubstantialSmallCityResolutionTests(unittest.TestCase):
    """
    Corrective gate, section 7 - population must never gate
    recognition outright. Every city below is a real national
    capital, chosen after inspecting the live geonamescache dataset
    (not assumed), each at a small fraction of the 400,000 absolute
    floor a previous revision enforced and this corrective gate
    removed entirely.
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
                    resolution.status,
                    JurisdictionResolutionStatus.RESOLVED,
                )
                self.assertEqual(
                    resolution.country_code, expected_code
                )


class AmbiguousCityResolutionTests(unittest.TestCase):
    """
    Mission section 11 - a city name genuinely shared by more than
    one comparably-sized real city must never be silently guessed,
    Barcelona included, and never via a Barcelona-specific check.
    """

    def test_barcelona_alone_is_ambiguous(self) -> None:
        resolution = resolve_jurisdiction(
            "social media rules at work in Barcelona"
        )

        self.assertEqual(
            resolution.status,
            JurisdictionResolutionStatus.AMBIGUOUS,
        )
        self.assertIn("ES", resolution.candidate_country_codes)
        self.assertIn("VE", resolution.candidate_country_codes)

    def test_other_real_ties_are_also_ambiguous(self) -> None:
        for question in (
            "rules in Valencia",
            "rules in Cambridge",
        ):
            with self.subTest(question=question):
                resolution = resolve_jurisdiction(question)

                self.assertEqual(
                    resolution.status,
                    JurisdictionResolutionStatus.AMBIGUOUS,
                )
                self.assertGreaterEqual(
                    len(resolution.candidate_country_codes), 2
                )

    def test_explicit_country_dominates_an_ambiguous_city_name(
        self,
    ) -> None:
        # Mission section 10 - "Barcelona, Spain" must never be
        # flagged ambiguous by "Barcelona" alone.
        resolution = resolve_jurisdiction("Barcelona, Spain")

        self.assertEqual(
            resolution.status,
            JurisdictionResolutionStatus.RESOLVED,
        )
        self.assertEqual(resolution.country_code, "ES")


class DominancePolicyBoundaryTests(unittest.TestCase):
    """
    Corrective gate, section 10 - the population-ratio tie-break is
    audited against the real, current dataset (3,649 city names with
    2+ country candidates), not re-picked on intuition. A handful of
    real cities sitting close to the ratio-10 boundary, in both
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
                    resolution.status,
                    JurisdictionResolutionStatus.RESOLVED,
                )
                self.assertEqual(
                    resolution.country_code, expected_code
                )

    def test_cities_just_below_the_ratio_stay_ambiguous(self) -> None:
        # Geneva, CH (201,741) / Geneva, US (21,806): ~9.25x - a real
        # same-named US city exists, so this stays a genuine
        # ambiguity under "never guess", even though most people
        # asking about "Geneva" mean Switzerland.
        resolution = resolve_jurisdiction("employment law in Geneva")

        self.assertEqual(
            resolution.status,
            JurisdictionResolutionStatus.AMBIGUOUS,
        )


class UnknownLocalityTests(unittest.TestCase):
    """
    Corrective gate, section 11 - a question that clearly names a
    place this dataset does not recognize must never fabricate a
    country, and is a genuinely different situation from "no location
    mentioned at all".
    """

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
                self.assertEqual(
                    resolution.candidate_country_codes, ()
                )

    def test_matched_location_keeps_original_casing(self) -> None:
        resolution = resolve_jurisdiction(
            "employment law in Ruritania"
        )

        self.assertEqual(
            resolution.matched_location, "Ruritania"
        )

    def test_no_capitalized_phrase_after_preposition_is_not_found(
        self,
    ) -> None:
        # No preposition-led capitalized phrase at all - genuinely
        # nothing to ask the user about, unlike the cases above.
        resolution = resolve_jurisdiction(
            "What are the rules on termination?"
        )

        self.assertEqual(
            resolution.status,
            JurisdictionResolutionStatus.NOT_FOUND,
        )


class NotFoundAndFalsePositiveTests(unittest.TestCase):
    """Mission section 13 - a conservative resolver, never a naive
    world-city scan of every sentence."""

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
                    resolution.status,
                    JurisdictionResolutionStatus.NOT_FOUND,
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
    """Mission section 14 - comparisons must never regress."""

    def test_two_explicit_countries_is_ambiguous_for_this_primitive(
        self,
    ) -> None:
        # resolve_jurisdiction answers ONE jurisdiction; a real
        # multi-country comparison must keep using
        # detect_mentioned_country_codes directly (see
        # country_detection.resolve_country_availability, which does
        # exactly that and is regression-tested separately in
        # test_chat.py/test_country_detection.py).
        resolution = resolve_jurisdiction(
            "Compare France and Germany"
        )

        self.assertEqual(
            resolution.status,
            JurisdictionResolutionStatus.AMBIGUOUS,
        )
        self.assertEqual(
            set(resolution.candidate_country_codes),
            {"FR", "DE"},
        )


class ResolveCityCountryCodesTests(unittest.TestCase):
    """Direct tests of the lower-level, city-only primitive."""

    def test_multi_word_city_names_are_matched_intact(self) -> None:
        # Corrective gate, section 3 - multi-word names are no longer
        # out of scope; this is the primitive-level counterpart to
        # LongestMatchFirstTests above.
        codes, matched = resolve_city_country_codes(
            "rules in Los Angeles"
        )

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
    Adversarial-review finding, corrective gate: "St." was indexed
    with its period kept but real text always tokenizes it without
    one, splitting the period and period-free spellings of the same
    real-world place into two disjoint, inconsistent candidate sets.
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
                    resolution.status,
                    JurisdictionResolutionStatus.RESOLVED,
                )
                self.assertEqual(resolution.country_code, "RU")

    def test_st_johns_and_saint_johns_agree(self) -> None:
        # Real-world data: Antigua and Barbuda's capital (51,737) vs.
        # Canada's St. John's, Newfoundland (110,525) - genuinely
        # close (~2.1x), so both spellings must agree it is a real,
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
                    resolution.status,
                    JurisdictionResolutionStatus.AMBIGUOUS,
                )
                self.assertEqual(
                    set(resolution.candidate_country_codes),
                    {"AG", "CA"},
                )


class CountryAggregateNicknameTests(unittest.TestCase):
    """
    Adversarial-review finding, corrective gate: geonamescache's
    country/territory-aggregate city entries (name identical to their
    own country's name, e.g. "Hong Kong", "Singapore") list historical
    or touristic nicknames as alternate names ("Victoria", "Garden
    City"); those nicknames must never inherit the whole territory's
    population and silently outrank every real, unrelated city of the
    same name elsewhere - a real city, never a country, is what
    "Victoria"/"Garden City" alone should ever be weighed as.
    """

    def test_victoria_no_longer_forces_hong_kong(self) -> None:
        resolution = resolve_jurisdiction(
            "What is the minimum notice period for termination "
            "in Victoria?"
        )

        # Real Victoria/Canada (289,625), Victoria/Mexico (332,100),
        # and Victoria HK's own real settlement (956,800, once no
        # longer inflated to the whole territory's 7,396,076) are
        # all within a real, un-dominated spread - a genuine tie,
        # never silently forced to Hong Kong.
        self.assertEqual(
            resolution.status,
            JurisdictionResolutionStatus.AMBIGUOUS,
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
            resolution.status,
            JurisdictionResolutionStatus.AMBIGUOUS,
        )
        self.assertNotEqual(resolution.country_code, "SG")
        self.assertIn("US", resolution.candidate_country_codes)

    def test_hong_kong_and_singapore_own_names_still_resolve(
        self,
    ) -> None:
        # Only the NICKNAME alternates are excluded - the aggregate
        # entry's own primary name is unaffected.
        self.assertEqual(
            resolve_jurisdiction(
                "employment law in Hong Kong"
            ).country_code,
            "HK",
        )
        self.assertEqual(
            resolve_jurisdiction(
                "employment law in Singapore"
            ).country_code,
            "SG",
        )


class CompoundPlaceNameFallbackTests(unittest.TestCase):
    """
    Adversarial-review finding, corrective gate: a real, qualified
    place name geonamescache never recorded as a multi-word alternate
    ("Kingston upon Thames") must never silently fall back to
    resolving as if only its bare, unrelated, ambiguous head word
    ("Kingston") had been said - the correct country is not even
    among that word's own candidates, so offering it would be worse
    than admitting no match. A structurally identical name the
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
            resolution.status,
            JurisdictionResolutionStatus.AMBIGUOUS,
        )
        self.assertNotIn("GB", resolution.candidate_country_codes)

    def test_bare_kingston_is_still_genuinely_ambiguous(self) -> None:
        # Unaffected by the fix - a genuinely bare, unqualified
        # mention still surfaces its own real ambiguity.
        resolution = resolve_jurisdiction("employment law in Kingston")

        self.assertEqual(
            resolution.status,
            JurisdictionResolutionStatus.AMBIGUOUS,
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
                    resolution.status,
                    JurisdictionResolutionStatus.RESOLVED,
                )
                self.assertEqual(
                    resolution.country_code, expected_code
                )


class DistinctCityMentionsNeverContaminateTests(unittest.TestCase):
    """
    Adversarial-review finding, corrective gate: two individually
    unambiguous cities named together must never merge into a
    fabricated ambiguity that mixes in an unrelated, non-dominant
    candidate from the OTHER city - each matched city is narrowed to
    its own real answer first, independently, before two or more
    distinct matches are ever combined.
    """

    def test_paris_and_berlin_together_offer_exactly_those_countries(
        self,
    ) -> None:
        resolution = resolve_jurisdiction(
            "Compare termination rules in Paris and in Berlin"
        )

        self.assertEqual(
            resolution.status,
            JurisdictionResolutionStatus.AMBIGUOUS,
        )
        self.assertEqual(
            set(resolution.candidate_country_codes),
            {"FR", "DE"},
        )

    def test_matched_location_names_every_matched_city(self) -> None:
        resolution = resolve_jurisdiction(
            "Compare termination rules in Paris and in Berlin"
        )

        self.assertIn("paris", resolution.matched_location)
        self.assertIn("berlin", resolution.matched_location)


if __name__ == "__main__":
    unittest.main()
