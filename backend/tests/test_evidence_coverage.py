"""
Tests for the deterministic, local evidence-coverage engine.

These are the exact regression fixtures from the 0.4.2 mission's
Phase 21/rectificatif section K - a real regression this mission
fixes (defect C: "remote work" retrieval surfacing only adjacent
work-hours/health-and-safety content) and a real defect this engine
must never reintroduce (two independent chunks, one per concept,
never counted as direct proof of a relation between them).
"""

from __future__ import annotations

import unittest

from app.models.search import LegalSearchHit
from app.services.evidence_coverage import (
    RELATION_PROXIMITY_MAX_TOKENS,
    answer_mentions_concepts,
    evaluate_evidence_status,
    normalize_for_matching,
)


class _Concept:
    """Minimal SearchConceptLike stand-in - avoids depending on the
    Pydantic ConversationSearchConcept model for these pure-function
    tests."""

    def __init__(self, terms: list[str]) -> None:
        self.terms = terms


def _hit(
    content: str,
    *,
    section: str = "Working Conditions",
    subsection: str = "General",
    country_code: str = "ES",
) -> LegalSearchHit:
    return LegalSearchHit(
        score=10.0,
        document_id="doc",
        chunk_id=f"chunk-{hash((content, section, subsection))}",
        country="Spain",
        country_code=country_code,
        legal_topic="Working Conditions",
        document_type="comparator",
        language="en",
        section=section,
        subsection=subsection,
        content=content,
        source_filename="x.docx",
        source_format="docx",
        reference_year=2026,
    )


class NormalizeForMatchingTests(unittest.TestCase):
    def test_casefolds(self) -> None:
        self.assertEqual(
            normalize_for_matching("Non-Compete"),
            normalize_for_matching("non compete"),
        )

    def test_normalizes_dashes(self) -> None:
        self.assertEqual(
            normalize_for_matching("non‐compete"),
            normalize_for_matching("non compete"),
        )

    def test_collapses_whitespace(self) -> None:
        self.assertEqual(
            normalize_for_matching("remote   work"),
            normalize_for_matching("remote work"),
        )


class BroadTopicEvidenceTests(unittest.TestCase):
    def test_any_hit_is_direct_regardless_of_concepts(self) -> None:
        hit = _hit("Anything at all, unrelated to any concept group.")

        self.assertEqual(
            evaluate_evidence_status(
                [hit],
                [_Concept(["remote work"])],
                "broad_topic",
            ),
            "direct",
        )

    def test_no_hits_is_insufficient(self) -> None:
        self.assertEqual(
            evaluate_evidence_status(
                [], [_Concept(["remote work"])], "broad_topic"
            ),
            "insufficient",
        )


class SubjectTextFallbackEvidenceTests(unittest.TestCase):
    """
    "No search_concepts were supplied" must never be treated as
    automatic proof for direct_topic/relation_required - a general
    chunk on working hours or health-and-safety must not count as
    direct evidence for a precise question just because the action
    carried no search_concepts (mission "MISSION EXPRESS BLOQUANTE
    0.4.2", section 4). subject_text, when given, is the fallback
    direct concept instead - still requiring an actual match.
    """

    def test_empty_concepts_and_no_subject_text_is_insufficient(
        self,
    ) -> None:
        hit = _hit("Anything at all.")

        self.assertEqual(
            evaluate_evidence_status([hit], [], "direct_topic"),
            "insufficient",
        )

    def test_subject_text_fallback_matches_a_direct_hit(self) -> None:
        hit = _hit(
            "The overtime rules require payment at 1.25 times the "
            "ordinary hourly rate for the first two hours.",
            subsection="Overtime",
        )

        self.assertEqual(
            evaluate_evidence_status(
                [hit],
                [],
                "direct_topic",
                subject_text="overtime rules",
            ),
            "direct",
        )

    def test_subject_text_fallback_rejects_an_adjacent_hit(self) -> None:
        hit = _hit(
            "Employers must assess workplace risks and provide "
            "personal protective equipment where required.",
            subsection="Health and Safety",
        )

        self.assertEqual(
            evaluate_evidence_status(
                [hit],
                [],
                "direct_topic",
                subject_text="overtime rules",
            ),
            "insufficient",
        )


class DirectTopicEvidenceTests(unittest.TestCase):
    """The exact remote-work regression (defect C)."""

    def setUp(self) -> None:
        self.concepts = [
            _Concept(["remote work", "telework", "working from home"])
        ]

    def test_direct_telework_chunk_is_direct(self) -> None:
        hit = _hit(
            "A telework agreement must specify the "
            "employer-provided equipment and reimbursable "
            "home-office expenses.",
            subsection="Remote Work",
        )

        self.assertEqual(
            evaluate_evidence_status(
                [hit], self.concepts, "direct_topic"
            ),
            "direct",
        )

    def test_work_hours_record_chunk_is_insufficient(self) -> None:
        hit = _hit(
            "Employers must maintain a daily record of actual "
            "start and end working hours for each employee.",
            subsection="Working Time Records",
        )

        self.assertEqual(
            evaluate_evidence_status(
                [hit], self.concepts, "direct_topic"
            ),
            "insufficient",
        )

    def test_health_and_safety_chunk_is_insufficient(self) -> None:
        hit = _hit(
            "Employers must assess workplace risks and provide "
            "personal protective equipment where required.",
            subsection="Health and Safety",
        )

        self.assertEqual(
            evaluate_evidence_status(
                [hit], self.concepts, "direct_topic"
            ),
            "insufficient",
        )

    def test_working_from_home_synonym_is_direct(self) -> None:
        hit = _hit(
            "Employees working from home retain the same "
            "entitlements as those working on employer premises.",
        )

        self.assertEqual(
            evaluate_evidence_status(
                [hit], self.concepts, "direct_topic"
            ),
            "direct",
        )

    def test_section_label_alone_never_counts(self) -> None:
        """
        A hit whose broad `section` happens to share wording with a
        concept, but whose own subsection/content never mentions it,
        must never count as coverage - only subsection/content do.
        """

        hit = _hit(
            "General overtime pay rates and rest-period rules.",
            section="Remote Work and Flexible Arrangements",
            subsection="Overtime",
        )

        self.assertEqual(
            evaluate_evidence_status(
                [hit], self.concepts, "direct_topic"
            ),
            "insufficient",
        )


class RelationRequiredEvidenceTests(unittest.TestCase):
    """The exact sick-leave-dismissal regression (defect B)."""

    def setUp(self) -> None:
        self.concepts = [
            _Concept(["dismissal", "dismiss", "termination"]),
            _Concept(
                ["sick leave", "medical leave", "illness absence"]
            ),
        ]

    def test_same_chunk_covering_both_concepts_is_direct(self) -> None:
        hit = _hit(
            "An employer may dismiss an employee during sick "
            "leave only for objective grounds unrelated to the "
            "illness.",
            subsection="Dismissal During Sick Leave",
        )

        self.assertEqual(
            evaluate_evidence_status(
                [hit], self.concepts, "relation_required"
            ),
            "direct",
        )

    def test_two_independent_chunks_one_per_concept_is_never_direct(
        self,
    ) -> None:
        dismissal_hit = _hit(
            "General dismissal requires just cause and written "
            "notice to the employee.",
            section="Termination",
            subsection="General Grounds",
        )
        sick_leave_hit = _hit(
            "Employees are entitled to paid sick leave with "
            "certified medical leave for illness absence.",
            section="Employee Benefits",
            subsection="Sick Leave",
        )

        status = evaluate_evidence_status(
            [dismissal_hit, sick_leave_hit],
            self.concepts,
            "relation_required",
        )

        self.assertIn(status, ("partial", "insufficient"))
        self.assertNotEqual(status, "direct")

    def test_only_dismissal_concept_present_is_partial(self) -> None:
        hit = _hit(
            "General dismissal requires just cause and written "
            "notice to the employee.",
            subsection="General Grounds",
        )

        self.assertEqual(
            evaluate_evidence_status(
                [hit], self.concepts, "relation_required"
            ),
            "partial",
        )

    def test_neither_concept_present_is_insufficient(self) -> None:
        hit = _hit(
            "Employees are entitled to annual paid vacation of "
            "22 days per year.",
            subsection="Annual Leave",
        )

        self.assertEqual(
            evaluate_evidence_status(
                [hit], self.concepts, "relation_required"
            ),
            "insufficient",
        )

    def test_two_adjacent_chunks_never_combined_across_hits(
        self,
    ) -> None:
        """
        Coverage is evaluated per hit, never by concatenating two
        different hits' text - even when passed in adjacent list
        order, two separate LegalSearchHit objects must never be
        treated as one textual unit unless a reranker has explicitly
        confirmed the relation (reranked_direct_chunk_ids).
        """

        dismissal_hit = _hit(
            "General dismissal requires just cause.",
            subsection="General Grounds",
        )
        sick_leave_hit = _hit(
            "Sick leave requires certified medical leave.",
            subsection="Sick Leave",
        )

        status = evaluate_evidence_status(
            [dismissal_hit, sick_leave_hit],
            self.concepts,
            "relation_required",
        )

        self.assertNotEqual(status, "direct")

    def test_reranker_confirmation_allows_direct_without_reproximity(
        self,
    ) -> None:
        """
        When the (optional, disabled-by-default) LLM reranker has
        already confirmed a specific chunk answers the full relation,
        this local check trusts that confirmation rather than
        re-deriving it - but only for the confirmed chunk_id.
        """

        hit = _hit(
            "See the cross-referenced table for full details.",
            subsection="Cross-Reference",
        )

        without_confirmation = evaluate_evidence_status(
            [hit], self.concepts, "relation_required"
        )
        with_confirmation = evaluate_evidence_status(
            [hit],
            self.concepts,
            "relation_required",
            reranked_direct_chunk_ids=frozenset({hit.chunk_id}),
        )

        self.assertEqual(without_confirmation, "insufficient")
        self.assertEqual(with_confirmation, "direct")

    def test_proximity_threshold_is_respected(self) -> None:
        """
        Two concept matches inside one hit, but far apart (beyond
        RELATION_PROXIMITY_MAX_TOKENS), must not count as the same
        relation - filler tokens deliberately separate them.
        """

        filler = " filler" * (RELATION_PROXIMITY_MAX_TOKENS + 20)
        hit = _hit(f"Dismissal grounds are listed here.{filler} "
                   "Sick leave entitlements are listed separately.")

        self.assertEqual(
            evaluate_evidence_status(
                [hit], self.concepts, "relation_required"
            ),
            "partial",
        )


class SubjectTextOverlapFallbackTests(unittest.TestCase):
    """
    Mission "HOTFIX 0.4.4" - chat capabilities and evidence stability:
    the exact Swiss non-compete regression. Four reasonable
    rephrasings of the same question ("enforceable", "rules",
    "conditions", "summarise") must all retain at least one source
    once retrieval already found the right country/topic hits - even
    when a given phrasing's own model-generated search_concepts happen
    not to literally appear in the hit text.
    """

    def setUp(self) -> None:
        self.hit = _hit(
            "A post-employment non-compete clause is enforceable "
            "only if it is limited in duration, geographic scope, "
            "and type of prohibited activity, and if the employee "
            "received adequate compensation for the restriction.",
            section="Restrictive Covenants",
            subsection="Non-Compete Clauses",
            country_code="CH",
        )
        self.subject_text = (
            "post-employment non-compete clause conditions"
        )

    def test_enforceable_phrasing_matches_directly(self) -> None:
        status = evaluate_evidence_status(
            [self.hit],
            [_Concept(["non-compete clause", "enforceable"])],
            "direct_topic",
            subject_text=self.subject_text,
        )

        self.assertEqual(status, "direct")

    def test_rules_phrasing_matches_directly(self) -> None:
        status = evaluate_evidence_status(
            [self.hit],
            [_Concept(["non-compete clause"])],
            "direct_topic",
            subject_text=self.subject_text,
        )

        self.assertEqual(status, "direct")

    def test_conditions_phrasing_falls_back_to_partial(self) -> None:
        # The model's own synonym generation for this phrasing does
        # not literally appear in the hit ("validity requirements" is
        # nowhere in the text) - the exact-phrase check alone would
        # wrongly return "insufficient" despite the identical hit that
        # the "enforceable"/"rules" phrasings above correctly matched.
        status = evaluate_evidence_status(
            [self.hit],
            [_Concept(["validity requirements", "formal conditions"])],
            "direct_topic",
            subject_text=self.subject_text,
        )

        self.assertIn(status, ("direct", "partial"))
        self.assertNotEqual(status, "insufficient")

    def test_summarise_phrasing_falls_back_to_partial(self) -> None:
        status = evaluate_evidence_status(
            [self.hit],
            [_Concept(["overview", "summary of rules"])],
            "direct_topic",
            subject_text=self.subject_text,
        )

        self.assertIn(status, ("direct", "partial"))
        self.assertNotEqual(status, "insufficient")

    def test_genuinely_unrelated_hit_still_insufficient(self) -> None:
        # The counter-example the fallback must never paper over: a
        # hit that is truly about something else entirely, sharing no
        # meaningful vocabulary with the resolved subject at all.
        unrelated_hit = _hit(
            "Employees are entitled to twenty paid vacation days "
            "per calendar year, prorated for partial years of "
            "service.",
            section="Employee Benefits",
            subsection="Annual Leave",
            country_code="CH",
        )

        status = evaluate_evidence_status(
            [unrelated_hit],
            [_Concept(["validity requirements", "formal conditions"])],
            "direct_topic",
            subject_text=self.subject_text,
        )

        self.assertEqual(status, "insufficient")

    def test_no_subject_text_never_triggers_fallback(self) -> None:
        # Without a resolved subject_text at all, behavior is exactly
        # as before this fix - never a new source of false positives.
        status = evaluate_evidence_status(
            [self.hit],
            [_Concept(["validity requirements", "formal conditions"])],
            "direct_topic",
        )

        self.assertEqual(status, "insufficient")

    def test_notice_followup_after_country_only_reply_keeps_a_source(
        self,
    ) -> None:
        # The exact "Tell me about notice." / "Spain" regression: the
        # inherited search_concepts from the first turn ("termination
        # notice period") do not literally appear in the Spanish
        # content's own wording, but the resolved subject_text
        # ("notice") clearly does.
        hit = _hit(
            "An employer must give an employee at least fifteen "
            "days' advance warning before ending the employment "
            "relationship, extended by collective agreement for "
            "longer-serving staff.",
            section="Termination of Employment Contracts",
            subsection="Notice Requirements",
            country_code="ES",
        )

        status = evaluate_evidence_status(
            [hit],
            [_Concept(["termination notice period"])],
            "direct_topic",
            subject_text="notice",
        )

        self.assertNotEqual(status, "insufficient")

    def test_relation_required_also_gets_the_fallback(self) -> None:
        concepts = [
            _Concept(["validity requirements"]),
            _Concept(["formal conditions"]),
        ]

        status = evaluate_evidence_status(
            [self.hit],
            concepts,
            "relation_required",
            subject_text=self.subject_text,
        )

        self.assertIn(status, ("direct", "partial"))
        self.assertNotEqual(status, "insufficient")


class SubjectTextOverlapFallbackCounterExampleTests(unittest.TestCase):
    """
    Mission "HOTFIX 0.4.4" follow-up - counter-examples proving the
    word-overlap fallback (SubjectTextOverlapFallbackTests above)
    cannot itself be turned into a grounding bypass. Wrong-country and
    wrong-topic rejection are proven at the rag_answer.py integration
    level (test_rag_answer_evidence_gating.py's
    WrongCountryAndWrongTopicNeverLaunderedTests): evaluate_evidence_
    status has no country/topic parameter at all - country_code is
    already used to bucket hits per country before this function ever
    sees them (rag_answer.py's spec_hits_by_country), and legal_topic
    is already a hard OpenSearch `terms` filter (test_legal_search.py's
    test_build_query_with_filters) - so a hit for the wrong country or
    topic never reaches this function in the first place. These tests
    cover what genuinely is this function's own responsibility: a
    subject_text built only from generic framing words, an
    insufficient (minority) token overlap, and a right-country/right-
    topic hit that simply does not support the specific detail asked.
    """

    def test_generic_only_subject_text_never_triggers_fallback_alone(
        self,
    ) -> None:
        # "rules"/"conditions"/"information" describe the shape of the
        # question, never a specific legal subject - a subject_text
        # built only from such words must leave zero significant
        # tokens, so the fallback can never fire from them alone, even
        # though the hit happens to contain those exact words too.
        hit = _hit(
            "General information about workplace rules and "
            "conditions applicable to all employees.",
            section="Working Conditions",
            subsection="General",
            country_code="CH",
        )

        status = evaluate_evidence_status(
            [hit],
            [_Concept(["non-compete clause"])],
            "direct_topic",
            subject_text="What are the rules, conditions, and information?",
        )

        self.assertEqual(status, "insufficient")

    def test_minority_token_overlap_stays_insufficient(self) -> None:
        # Only one of the subject's several distinct significant
        # tokens ("duration") appears in the hit - a genuine minority,
        # never the majority the fallback requires.
        subject_text = (
            "post-employment non-compete clause geographic scope "
            "limitation duration"
        )
        hit = _hit(
            "Employees are entitled to a fixed duration of paid "
            "annual leave each calendar year.",
            section="Employee Benefits",
            subsection="Annual Leave",
            country_code="CH",
        )

        status = evaluate_evidence_status(
            [hit],
            [_Concept(["validity requirements"])],
            "direct_topic",
            subject_text=subject_text,
        )

        self.assertEqual(status, "insufficient")

    def test_right_country_and_topic_but_absent_specific_detail(
        self,
    ) -> None:
        # A hit genuinely from the right country and the right broad
        # topic (Restrictive Covenants) - but about a different
        # specific clause (non-solicitation) than the one actually
        # asked about (garden leave compensation). The fallback must
        # not manufacture support for a detail the hit never discusses,
        # rather than a real absence of evidence being papered over.
        hit = _hit(
            "A non-solicitation clause prevents a former employee "
            "from soliciting clients or colleagues for a defined "
            "period after leaving the company.",
            section="Restrictive Covenants",
            subsection="Non-Solicitation Clauses",
            country_code="CH",
        )

        status = evaluate_evidence_status(
            [hit],
            [_Concept(["garden leave pay", "continued salary"])],
            "direct_topic",
            subject_text="garden leave compensation entitlement",
        )

        self.assertEqual(status, "insufficient")



class QualifiedLegalTermMatchingRegressionTests(
    unittest.TestCase
):
    """
    Leading legal qualifiers may be omitted only when a meaningful
    multi-word phrase remains. Never degrade to a generic one-word
    concept match.
    """

    def test_statutory_severance_pay_matches_severance_pay(
        self,
    ) -> None:
        hit = _hit(
            (
                "The severance pay resulting from an objective "
                "dismissal is twenty days salary per year of service."
            )
        )

        self.assertEqual(
            evaluate_evidence_status(
                [hit],
                [_Concept(["statutory severance pay"])],
                "direct_topic",
            ),
            "direct",
        )

    def test_smoke_severance_synonyms_match_severance_pay(
        self,
    ) -> None:
        hit = _hit(
            (
                "A severance payment will only be required in "
                "certain cases. Severance pay resulting from "
                "objective dismissal is twenty days salary per "
                "year of service."
            )
        )

        concepts = [
            _Concept(
                [
                    "statutory severance",
                    "statutory redundancy payment",
                    "mandatory severance",
                ]
            )
        ]

        self.assertEqual(
            evaluate_evidence_status(
                [hit],
                concepts,
                "direct_topic",
            ),
            "direct",
        )

    def test_statutory_severance_does_not_reduce_to_one_word(
        self,
    ) -> None:
        hit = _hit(
            "The employee may receive severance."
        )

        self.assertEqual(
            evaluate_evidence_status(
                [hit],
                [_Concept(["statutory severance"])],
                "direct_topic",
            ),
            "insufficient",
        )



class AnswerMentionsConceptsTests(unittest.TestCase):
    """Used only for subject_drift detection on the generated text."""

    def test_broad_topic_always_true(self) -> None:
        self.assertTrue(
            answer_mentions_concepts(
                "Some unrelated answer text.",
                [_Concept(["remote work"])],
                "broad_topic",
            )
        )

    def test_direct_topic_true_when_concept_present(self) -> None:
        self.assertTrue(
            answer_mentions_concepts(
                "Employees working from home retain full rights.",
                [_Concept(["remote work", "working from home"])],
                "direct_topic",
            )
        )

    def test_direct_topic_false_when_concept_absent(self) -> None:
        self.assertFalse(
            answer_mentions_concepts(
                "General termination requires just cause.",
                [_Concept(["remote work", "telework"])],
                "direct_topic",
            )
        )

    def test_relation_required_needs_every_group(self) -> None:
        concepts = [
            _Concept(["dismissal", "termination"]),
            _Concept(["sick leave", "medical leave"]),
        ]

        self.assertFalse(
            answer_mentions_concepts(
                "General termination requires just cause.",
                concepts,
                "relation_required",
            )
        )
        self.assertTrue(
            answer_mentions_concepts(
                "A dismissal of an employee even during sick "
                "leave is permitted for unrelated cause.",
                concepts,
                "relation_required",
            )
        )


if __name__ == "__main__":
    unittest.main()
