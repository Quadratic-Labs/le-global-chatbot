"""
Tests for rag_answer.py's 0.4.2 evidence-fidelity additions:
subject_text/search_concepts/evidence_mode gating in answer_legal_
question, the subject_drift quality check, and adjacent-citation
deduplication (defect H). evaluate_evidence_status/answer_mentions_
concepts's own coverage-classification rules are tested exhaustively
in test_evidence_coverage.py - these tests cover the integration wiring
around that engine instead: what answer_legal_question actually does
with each status, and never a duplicated re-test of the classification
rules themselves.
"""

from __future__ import annotations

import unittest

from app.models.chat import LegalChatRequest
from app.models.conversation_state import ConversationSearchConcept
from app.models.search import LegalSearchHit, LegalSearchResponse
from app.services.chat_metrics import LegalChatMetrics
from app.services.rag_answer import (
    EXCLUDED_COUNTRY_HEADING_INSTRUCTION_TEMPLATE,
    INSUFFICIENT_EVIDENCE_ANSWER_TEMPLATE,
    PARTIAL_EVIDENCE_INSTRUCTION_TEMPLATE,
    SYSTEM_INSTRUCTIONS,
    LegalActionEvidenceSpec,
    QualityError,
    RagAnswerError,
    _build_retrieval_query,
    _deduplicate_adjacent_citations,
    _validate_no_subject_drift,
    answer_legal_question,
)


def _build_hit(
    *,
    chunk_id: str = "chunk-1",
    country: str = "United Kingdom",
    country_code: str = "GB",
    section: str = "Working Conditions",
    subsection: str = "General",
    content: str = "General working conditions information.",
    legal_topic: str = "Working Conditions",
    score: float = 12.5,
) -> LegalSearchHit:
    return LegalSearchHit(
        score=score,
        document_id=f"document-{country_code.lower()}",
        chunk_id=chunk_id,
        country=country,
        country_code=country_code,
        legal_topic=legal_topic,
        document_type="comparator",
        language="en",
        section=section,
        subsection=subsection,
        content=content,
        source_filename=(
            f"Labour and Employment Law in {country} 2026.docx"
        ),
        source_format="docx",
        reference_year=2026,
    )


def _build_metrics(request_id: str) -> LegalChatMetrics:
    return LegalChatMetrics(
        request_id=request_id,
        question_characters=10,
        max_sources=6,
        rerank_enabled=False,
    )


def _make_search_function(hits: list[LegalSearchHit]):
    def fake_search(request: object) -> LegalSearchResponse:
        return LegalSearchResponse(
            query=request.query,
            total=len(hits),
            limit=request.limit,
            offset=0,
            took_ms=1,
            hits=hits,
        )

    return fake_search


class FakeGenerationClient:
    """Records the instructions/input it was called with."""

    model = "test-model"

    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.calls: list[tuple[str, str]] = []

    def generate(self, instructions: str, input_text: str):
        from app.clients.openai_responses import GeneratedText

        self.calls.append((instructions, input_text))

        return GeneratedText(text=self.answer, model=self.model)

    @property
    def called(self) -> bool:
        return bool(self.calls)


def _remote_work_concept() -> ConversationSearchConcept:
    return ConversationSearchConcept(
        terms=["remote work", "telework", "teleworking"]
    )


def _make_country_scoped_search_function(
    hits_by_country: dict[str, list[LegalSearchHit]],
):
    """
    Returns each requested country's own hits - never another
    country's - so a per-action retrieval test can prove one action's
    query never sees another action's content.
    """

    def fake_search(request: object) -> LegalSearchResponse:
        hits = [
            hit
            for code in request.country_codes
            for hit in hits_by_country.get(code, [])
        ]

        return LegalSearchResponse(
            query=request.query,
            total=len(hits),
            limit=request.limit,
            offset=0,
            took_ms=1,
            hits=hits,
        )

    return fake_search


class SequencedFakeGenerationClient:
    """Returns a different answer on each successive call - the first
    answer for the initial generation attempt, the second for the
    repair attempt (if triggered) - and fails the test outright if
    called a third time, since one generation plus at most one repair
    is the whole budget."""

    model = "test-model"

    def __init__(self, answers: list[str]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, str]] = []

    def generate(self, instructions: str, input_text: str):
        from app.clients.openai_responses import GeneratedText

        self.calls.append((instructions, input_text))

        if len(self.calls) > len(self.answers):
            raise AssertionError(
                "Generation must never be called more times than "
                "one generation plus one repair allows."
            )

        return GeneratedText(
            text=self.answers[len(self.calls) - 1],
            model=self.model,
        )


class BuildRetrievalQueryTests(unittest.TestCase):
    def test_search_concepts_append_their_terms_to_the_query(
        self,
    ) -> None:
        query = _build_retrieval_query(
            "Can employees work from home?",
            [],
            [_remote_work_concept()],
        )

        self.assertIn("remote work", query)
        self.assertIn("telework", query)
        self.assertIn("teleworking", query)
        self.assertIn("Can", query)

    def test_no_search_concepts_leaves_the_query_unchanged(
        self,
    ) -> None:
        with_none = _build_retrieval_query(
            "Can employees work from home?", [], None
        )
        with_empty = _build_retrieval_query(
            "Can employees work from home?", [], []
        )

        self.assertEqual(with_none, with_empty)
        self.assertNotIn("telework", with_none)


class SubjectDriftValidationTests(unittest.TestCase):
    def test_broad_topic_never_flags_drift(self) -> None:
        errors = _validate_no_subject_drift(
            answer="Something entirely unrelated.",
            search_concepts=[_remote_work_concept()],
            evidence_mode="broad_topic",
        )

        self.assertEqual(errors, [])

    def test_direct_topic_passes_when_any_concept_is_mentioned(
        self,
    ) -> None:
        errors = _validate_no_subject_drift(
            answer="Employees may telework subject to agreement.",
            search_concepts=[_remote_work_concept()],
            evidence_mode="direct_topic",
        )

        self.assertEqual(errors, [])

    def test_direct_topic_flags_drift_when_no_concept_is_mentioned(
        self,
    ) -> None:
        errors = _validate_no_subject_drift(
            answer="General working conditions apply.",
            search_concepts=[_remote_work_concept()],
            evidence_mode="direct_topic",
        )

        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].error_type, "subject_drift")

    def test_relation_required_needs_every_concept_group_mentioned(
        self,
    ) -> None:
        dismissal = ConversationSearchConcept(
            terms=["dismissal", "termination"]
        )
        sick_leave = ConversationSearchConcept(
            terms=["sick leave", "medical leave"]
        )

        only_one_group = _validate_no_subject_drift(
            answer="Termination requires notice.",
            search_concepts=[dismissal, sick_leave],
            evidence_mode="relation_required",
        )

        self.assertEqual(len(only_one_group), 1)

        both_groups = _validate_no_subject_drift(
            answer=(
                "Termination during sick leave requires special "
                "protection."
            ),
            search_concepts=[dismissal, sick_leave],
            evidence_mode="relation_required",
        )

        self.assertEqual(both_groups, [])


class DeduplicateAdjacentCitationsTests(unittest.TestCase):
    def test_collapses_a_simple_adjacent_repeat(self) -> None:
        self.assertEqual(
            _deduplicate_adjacent_citations(
                "Notice is one week [1, 2]. [1, 2] Then more text."
            ),
            "Notice is one week [1, 2]. Then more text.",
        )

    def test_collapses_three_or_more_repeats(self) -> None:
        self.assertEqual(
            _deduplicate_adjacent_citations(
                "Notice is one week [1]. [1]. [1] Then more text."
            ),
            "Notice is one week [1]. Then more text.",
        )

    def test_collapses_a_repeat_with_no_punctuation_between(
        self,
    ) -> None:
        self.assertEqual(
            _deduplicate_adjacent_citations("See [3] [3] for detail."),
            "See [3] for detail.",
        )

    def test_never_touches_a_non_adjacent_reappearance(self) -> None:
        text = (
            "Notice is one week [1]. Some unrelated sentence here. "
            "[1] applies again."
        )

        self.assertEqual(_deduplicate_adjacent_citations(text), text)

    def test_never_touches_two_different_citation_groups(self) -> None:
        text = "Notice is one week [1]. [2] covers termination."

        self.assertEqual(_deduplicate_adjacent_citations(text), text)

    def test_never_renumbers_anything(self) -> None:
        self.assertEqual(
            _deduplicate_adjacent_citations("[2, 5]. [2, 5] repeated."),
            "[2, 5]. repeated.",
        )


class AnswerLegalQuestionEvidenceGatingTests(unittest.TestCase):
    """
    answer_legal_question's own evidence-mode gating - omitted params
    behave exactly as before (see the first test); given params gate
    generation on whether the evidence actually supports the precise
    subject (defect C: "remote work" retrieval surfacing only
    adjacent content).
    """

    def test_omitting_evidence_params_behaves_exactly_as_before(
        self,
    ) -> None:
        hit = _build_hit(content="Some general legal content. [1]")
        client = FakeGenerationClient(
            answer="United Kingdom\n- Some general legal content. [1]"
        )
        metrics = _build_metrics("baseline")

        response = answer_legal_question(
            request=LegalChatRequest(
                question="What are the rules?",
                country_codes=["GB"],
            ),
            search_function=_make_search_function([hit]),
            generation_client=client,
            metrics=metrics,
        )

        self.assertTrue(client.called)
        self.assertTrue(response.grounded)
        self.assertEqual(metrics.evidence_status_by_country, {})

    def test_all_countries_insufficient_skips_generation_entirely(
        self,
    ) -> None:
        # defect C's own regression: hits exist (topically adjacent -
        # "Working Conditions"/general content) but never mention the
        # precise remote-work subject at all.
        hits = [
            _build_hit(
                country="United Kingdom",
                country_code="GB",
                section="Working Conditions",
                subsection="Working Hours",
                content="Standard working hours are 9am to 5pm.",
            ),
        ]

        client = FakeGenerationClient(answer="Should never be used.")
        metrics = _build_metrics("all-insufficient")

        response = answer_legal_question(
            request=LegalChatRequest(
                question="Can employees work remotely?",
                country_codes=["GB"],
            ),
            search_function=_make_search_function(hits),
            generation_client=client,
            metrics=metrics,
            subject_text="remote work",
            search_concepts=[_remote_work_concept()],
            evidence_mode="direct_topic",
        )

        self.assertFalse(client.called)
        self.assertFalse(response.grounded)
        self.assertEqual(response.sources, [])
        self.assertIn("remote work", response.answer)
        self.assertIn("United Kingdom", response.answer)
        self.assertEqual(
            metrics.evidence_status_by_country,
            {"GB": "insufficient"},
        )
        self.assertEqual(metrics.outcome, "insufficient_evidence")

    def test_a_direct_hit_is_never_blocked(self) -> None:
        hits = [
            _build_hit(
                country="United Kingdom",
                country_code="GB",
                section="Working Conditions",
                subsection="Remote Work",
                content=(
                    "Employees may telework subject to written "
                    "agreement with their employer."
                ),
            ),
        ]

        client = FakeGenerationClient(
            answer=(
                "United Kingdom\n- Telework is permitted subject to "
                "agreement. [1]"
            )
        )
        metrics = _build_metrics("direct-hit")

        response = answer_legal_question(
            request=LegalChatRequest(
                question="Can employees work remotely?",
                country_codes=["GB"],
            ),
            search_function=_make_search_function(hits),
            generation_client=client,
            metrics=metrics,
            subject_text="remote work",
            search_concepts=[_remote_work_concept()],
            evidence_mode="direct_topic",
        )

        self.assertTrue(client.called)
        self.assertTrue(response.grounded)
        self.assertEqual(
            metrics.evidence_status_by_country,
            {"GB": "direct"},
        )

    def test_mixed_insufficient_and_direct_countries(self) -> None:
        # GB has on-subject evidence; PE has none at all (no hits
        # returned for it) - the insufficient country must never
        # block the country that does have evidence, and must never
        # silently appear to have been answered either.
        hits = [
            _build_hit(
                country="United Kingdom",
                country_code="GB",
                subsection="Remote Work",
                content="Employees may telework by written agreement.",
            ),
        ]

        client = FakeGenerationClient(
            answer=(
                "United Kingdom\n- Telework is permitted subject to "
                "agreement. [1]"
            )
        )
        metrics = _build_metrics("mixed")

        response = answer_legal_question(
            request=LegalChatRequest(
                question="Can employees work remotely?",
                country_codes=["GB", "PE"],
            ),
            search_function=_make_search_function(hits),
            generation_client=client,
            metrics=metrics,
            subject_text="remote work",
            search_concepts=[_remote_work_concept()],
            evidence_mode="direct_topic",
        )

        self.assertTrue(client.called)
        self.assertTrue(response.grounded)
        self.assertEqual(
            metrics.evidence_status_by_country,
            {"GB": "direct", "PE": "insufficient"},
        )
        self.assertIn("Peru", response.answer)
        self.assertIn("remote work", response.answer)
        self.assertIn("Telework is permitted", response.answer)

        cited_countries = {
            source.country for source in response.sources
        }
        self.assertNotIn("Peru", cited_countries)

    def test_a_partial_country_gets_the_partial_instruction_injected(
        self,
    ) -> None:
        # Not directly on-subject but at least one concept group is
        # covered somewhere in the candidate set - a genuine, if
        # imperfect, partial answer (never silently promoted to
        # direct - see evidence_coverage.evaluate_evidence_status).
        hits = [
            _build_hit(
                country="United Kingdom",
                country_code="GB",
                subsection="Notice",
                content=(
                    "Teleworking arrangements are permitted for some "
                    "roles."
                ),
                score=5.0,
            ),
            _build_hit(
                chunk_id="chunk-2",
                country="United Kingdom",
                country_code="GB",
                subsection="Equipment",
                content="Equipment costs are reimbursed by the employer.",
                score=4.0,
            ),
        ]

        two_concepts = [
            ConversationSearchConcept(terms=["teleworking"]),
            ConversationSearchConcept(terms=["equipment allowance"]),
        ]

        client = FakeGenerationClient(
            answer="United Kingdom\n- Some partial content. [1]"
        )
        metrics = _build_metrics("partial")

        response = answer_legal_question(
            request=LegalChatRequest(
                question="What are the remote work equipment rules?",
                country_codes=["GB"],
            ),
            search_function=_make_search_function(hits),
            generation_client=client,
            metrics=metrics,
            subject_text="remote work equipment allowance",
            search_concepts=two_concepts,
            evidence_mode="relation_required",
        )

        self.assertTrue(client.called)
        self.assertEqual(
            metrics.evidence_status_by_country,
            {"GB": "partial"},
        )

        instructions_used = client.calls[0][0]
        self.assertIn(SYSTEM_INSTRUCTIONS, instructions_used)
        self.assertIn(
            PARTIAL_EVIDENCE_INSTRUCTION_TEMPLATE.format(
                subject="remote work equipment allowance",
                country="United Kingdom",
            ),
            instructions_used,
        )

    def test_insufficient_evidence_message_names_the_exact_subject(
        self,
    ) -> None:
        expected = INSUFFICIENT_EVIDENCE_ANSWER_TEMPLATE.format(
            subject="dismissal while on sick leave",
            country="Peru",
        )

        client = FakeGenerationClient(answer="unused")

        response = answer_legal_question(
            request=LegalChatRequest(
                question="Can I be dismissed while on sick leave?",
                country_codes=["PE"],
            ),
            search_function=_make_search_function(
                [
                    _build_hit(
                        country="Peru",
                        country_code="PE",
                        content="Unrelated general content.",
                    )
                ]
            ),
            generation_client=client,
            metrics=_build_metrics("named-subject"),
            subject_text="dismissal while on sick leave",
            search_concepts=[
                ConversationSearchConcept(
                    terms=["dismissal", "termination"]
                ),
                ConversationSearchConcept(
                    terms=["sick leave", "medical leave"]
                ),
            ],
            evidence_mode="relation_required",
        )

        self.assertFalse(client.called)
        self.assertEqual(response.answer, expected)


class PerActionEvidenceGatingTests(unittest.TestCase):
    """
    Phase 4 hardening: a mixed request naming more than one legal-type
    action must never let one action's source or evidence status
    satisfy another's - each LegalActionEvidenceSpec is retrieved and
    graded independently, even when two specs share a country, while
    generation itself stays exactly one combined OpenAI call.
    """

    def test_comparison_dismissal_plus_legal_overtime_disjoint_countries(
        self,
    ) -> None:
        # The mission's own mandated example: a Comparison (ES/AU,
        # dismissal) plus a Legal action (PE, overtime) - a dismissal
        # source must never cover overtime, and vice versa.
        hits_by_country = {
            "ES": [
                _build_hit(
                    country="Spain",
                    country_code="ES",
                    subsection="Dismissal",
                    content=(
                        "Dismissal without just cause requires "
                        "severance pay in Spain."
                    ),
                )
            ],
            "AU": [
                _build_hit(
                    chunk_id="chunk-au",
                    country="Australia",
                    country_code="AU",
                    subsection="Dismissal",
                    content=(
                        "Unfair dismissal claims require showing "
                        "the dismissal was harsh in Australia."
                    ),
                )
            ],
            "PE": [
                _build_hit(
                    chunk_id="chunk-pe",
                    country="Peru",
                    country_code="PE",
                    subsection="Annual Leave",
                    content=(
                        "Employees are entitled to 30 days paid "
                        "annual leave in Peru."
                    ),
                )
            ],
        }

        client = FakeGenerationClient(
            answer=(
                "Spain\n- Dismissal without just cause requires "
                "severance pay. [1]\n\nAustralia\n- Unfair dismissal "
                "claims require showing harshness. [2]\n\nComparison\n"
                "- Spain requires severance pay while Australia "
                "requires showing harshness. [1, 2]"
            )
        )
        metrics = _build_metrics("per-action-mandated-example")

        specs = [
            LegalActionEvidenceSpec(
                country_codes=["ES", "AU"],
                legal_topics=["Termination of Employment Contracts"],
                subject_text="dismissal grounds",
                search_concepts=[
                    ConversationSearchConcept(
                        terms=["dismissal", "termination"]
                    )
                ],
                evidence_mode="direct_topic",
            ),
            LegalActionEvidenceSpec(
                country_codes=["PE"],
                legal_topics=["Working Conditions"],
                subject_text="overtime rules",
                search_concepts=[
                    ConversationSearchConcept(
                        terms=["overtime", "extra hours"]
                    )
                ],
                evidence_mode="direct_topic",
            ),
        ]

        response = answer_legal_question(
            request=LegalChatRequest(
                question=(
                    "Compare dismissal rules in Spain and Australia, "
                    "and explain overtime rules in Peru."
                ),
                country_codes=["ES", "AU", "PE"],
            ),
            search_function=_make_country_scoped_search_function(
                hits_by_country
            ),
            generation_client=client,
            metrics=metrics,
            action_specs=specs,
        )

        self.assertEqual(len(client.calls), 1)
        self.assertTrue(response.grounded)
        self.assertEqual(
            metrics.evidence_status_by_country,
            {"ES": "direct", "AU": "direct", "PE": "insufficient"},
        )
        self.assertIn("Peru", response.answer)
        self.assertIn("overtime rules", response.answer)
        self.assertEqual(
            {source.country_code for source in response.sources},
            {"ES", "AU"},
        )

    def test_two_legal_actions_disjoint_countries_both_direct(
        self,
    ) -> None:
        hits_by_country = {
            "GB": [
                _build_hit(
                    country="United Kingdom",
                    country_code="GB",
                    subsection="Fixed-Term Contracts",
                    content=(
                        "Fixed-term contracts automatically convert "
                        "after four years of continuous service in "
                        "the UK."
                    ),
                )
            ],
            "ES": [
                _build_hit(
                    chunk_id="chunk-es",
                    country="Spain",
                    country_code="ES",
                    subsection="Overtime",
                    content=(
                        "Overtime hours are capped at 80 hours per "
                        "year in Spain."
                    ),
                )
            ],
        }

        client = FakeGenerationClient(
            answer=(
                "United Kingdom\n- Fixed-term contracts convert "
                "after four years. [1]\n\nSpain\n- Overtime is capped "
                "at 80 hours per year. [2]"
            )
        )
        metrics = _build_metrics("two-legal-disjoint")

        specs = [
            LegalActionEvidenceSpec(
                country_codes=["GB"],
                legal_topics=["Employment Contracts"],
                subject_text="fixed-term contract conversion",
                search_concepts=[
                    ConversationSearchConcept(
                        terms=["fixed-term", "fixed term contract"]
                    )
                ],
                evidence_mode="direct_topic",
            ),
            LegalActionEvidenceSpec(
                country_codes=["ES"],
                legal_topics=["Working Conditions"],
                subject_text="overtime cap",
                search_concepts=[
                    ConversationSearchConcept(terms=["overtime"])
                ],
                evidence_mode="direct_topic",
            ),
        ]

        response = answer_legal_question(
            request=LegalChatRequest(
                question=(
                    "Explain fixed-term contracts in the UK and "
                    "overtime rules in Spain."
                ),
                country_codes=["GB", "ES"],
            ),
            search_function=_make_country_scoped_search_function(
                hits_by_country
            ),
            generation_client=client,
            metrics=metrics,
            action_specs=specs,
        )

        self.assertEqual(len(client.calls), 1)
        self.assertTrue(response.grounded)
        self.assertEqual(
            metrics.evidence_status_by_country,
            {"GB": "direct", "ES": "direct"},
        )

    def test_one_legal_action_direct_one_insufficient(self) -> None:
        hits_by_country = {
            "GB": [
                _build_hit(
                    country="United Kingdom",
                    country_code="GB",
                    subsection="Fixed-Term Contracts",
                    content=(
                        "Fixed-term contracts automatically convert "
                        "after four years of continuous service."
                    ),
                )
            ],
            "PE": [
                _build_hit(
                    chunk_id="chunk-pe",
                    country="Peru",
                    country_code="PE",
                    subsection="Health and Safety",
                    content=(
                        "Employers must provide a safe workplace "
                        "under Peruvian law."
                    ),
                )
            ],
        }

        client = FakeGenerationClient(
            answer=(
                "United Kingdom\n- Fixed-term contracts convert "
                "after four years. [1]"
            )
        )
        metrics = _build_metrics("one-direct-one-insufficient")

        specs = [
            LegalActionEvidenceSpec(
                country_codes=["GB"],
                legal_topics=["Employment Contracts"],
                subject_text="fixed-term contract conversion",
                search_concepts=[
                    ConversationSearchConcept(terms=["fixed-term"])
                ],
                evidence_mode="direct_topic",
            ),
            LegalActionEvidenceSpec(
                country_codes=["PE"],
                legal_topics=["Working Conditions"],
                subject_text="overtime rules",
                search_concepts=[
                    ConversationSearchConcept(terms=["overtime"])
                ],
                evidence_mode="direct_topic",
            ),
        ]

        response = answer_legal_question(
            request=LegalChatRequest(
                question=(
                    "Explain fixed-term contracts in the UK and "
                    "overtime rules in Peru."
                ),
                country_codes=["GB", "PE"],
            ),
            search_function=_make_country_scoped_search_function(
                hits_by_country
            ),
            generation_client=client,
            metrics=metrics,
            action_specs=specs,
        )

        self.assertTrue(response.grounded)
        self.assertEqual(
            metrics.evidence_status_by_country,
            {"GB": "direct", "PE": "insufficient"},
        )
        self.assertIn("overtime rules", response.answer)
        self.assertNotIn(
            "PE", {s.country_code for s in response.sources}
        )

    def test_comparison_partial_plus_legal_direct(self) -> None:
        # Each comparison country gets two hits that each cover only
        # one of the two required concept groups (the same proven
        # pattern as test_a_partial_country_gets_the_partial_
        # instruction_injected above) - no single hit ever establishes
        # the full relation, so ES and AU both land on "partial",
        # never silently promoted to "direct".
        hits_by_country = {
            "ES": [
                _build_hit(
                    chunk_id="chunk-es-1",
                    country="Spain",
                    country_code="ES",
                    subsection="Notice",
                    content="The notice period depends on length of service.",
                ),
                _build_hit(
                    chunk_id="chunk-es-2",
                    country="Spain",
                    country_code="ES",
                    subsection="Severance",
                    content="Severance is paid according to a statutory formula.",
                ),
            ],
            "AU": [
                _build_hit(
                    chunk_id="chunk-au-1",
                    country="Australia",
                    country_code="AU",
                    subsection="Notice",
                    content="The notice period depends on length of service.",
                ),
                _build_hit(
                    chunk_id="chunk-au-2",
                    country="Australia",
                    country_code="AU",
                    subsection="Severance",
                    content="Severance is calculated separately from notice.",
                ),
            ],
            "GB": [
                _build_hit(
                    chunk_id="chunk-gb",
                    country="United Kingdom",
                    country_code="GB",
                    subsection="Redundancy",
                    content=(
                        "Redundancy payments depend on age and "
                        "length of service in the UK."
                    ),
                )
            ],
        }

        client = FakeGenerationClient(
            answer=(
                "Spain\n- The notice period depends on length of "
                "service. [1]\n- Severance is paid according to a "
                "statutory formula. [2]\n\nAustralia\n- The notice "
                "period depends on length of service. [3]\n- "
                "Severance is calculated separately from notice. "
                "[4]\n\nComparison\n- Both countries link notice and "
                "severance obligations to length of service. [1, 3]"
                "\n\nUnited Kingdom\n- Redundancy payments depend on "
                "age and length of service. [5]"
            )
        )
        metrics = _build_metrics("comparison-partial-plus-legal-direct")

        specs = [
            LegalActionEvidenceSpec(
                country_codes=["ES", "AU"],
                legal_topics=["Termination of Employment Contracts"],
                subject_text="notice and severance relationship",
                search_concepts=[
                    ConversationSearchConcept(terms=["notice period"]),
                    ConversationSearchConcept(terms=["severance"]),
                ],
                evidence_mode="relation_required",
            ),
            LegalActionEvidenceSpec(
                country_codes=["GB"],
                legal_topics=["Termination of Employment Contracts"],
                subject_text="redundancy pay",
                search_concepts=[
                    ConversationSearchConcept(terms=["redundancy"])
                ],
                evidence_mode="direct_topic",
            ),
        ]

        response = answer_legal_question(
            request=LegalChatRequest(
                question=(
                    "Compare notice and severance in Spain and "
                    "Australia, and explain redundancy pay in the UK."
                ),
                country_codes=["ES", "AU", "GB"],
            ),
            search_function=_make_country_scoped_search_function(
                hits_by_country
            ),
            generation_client=client,
            metrics=metrics,
            action_specs=specs,
        )

        self.assertTrue(response.grounded)
        self.assertEqual(
            metrics.evidence_status_by_country,
            {"ES": "partial", "AU": "partial", "GB": "direct"},
        )

    def test_two_actions_sharing_a_country_different_subjects(
        self,
    ) -> None:
        def fake_search(request: object) -> LegalSearchResponse:
            if "redundancy" in request.query:
                hits = [
                    _build_hit(
                        country="United Kingdom",
                        country_code="GB",
                        subsection="Redundancy",
                        content=(
                            "Redundancy payments are calculated "
                            "based on age and length of service."
                        ),
                    )
                ]
            else:
                hits = [
                    _build_hit(
                        chunk_id="chunk-gb-leave",
                        country="United Kingdom",
                        country_code="GB",
                        subsection="Annual Leave",
                        content=(
                            "Employees are entitled to 28 days paid "
                            "annual leave."
                        ),
                    )
                ]

            return LegalSearchResponse(
                query=request.query,
                total=len(hits),
                limit=request.limit,
                offset=0,
                took_ms=1,
                hits=hits,
            )

        client = FakeGenerationClient(
            answer=(
                "United Kingdom\n- Redundancy payments are "
                "calculated based on age and length of service. [1]"
            )
        )
        metrics = _build_metrics("shared-country-different-subjects")

        specs = [
            LegalActionEvidenceSpec(
                country_codes=["GB"],
                legal_topics=["Termination of Employment Contracts"],
                subject_text="redundancy",
                search_concepts=[
                    ConversationSearchConcept(terms=["redundancy"])
                ],
                evidence_mode="direct_topic",
            ),
            LegalActionEvidenceSpec(
                country_codes=["GB"],
                legal_topics=["Working Conditions"],
                subject_text="overtime",
                search_concepts=[
                    ConversationSearchConcept(terms=["overtime"])
                ],
                evidence_mode="direct_topic",
            ),
        ]

        response = answer_legal_question(
            request=LegalChatRequest(
                question="Explain redundancy and overtime rules in the UK.",
                country_codes=["GB"],
            ),
            search_function=fake_search,
            generation_client=client,
            metrics=metrics,
            action_specs=specs,
        )

        self.assertEqual(
            len(client.calls),
            1,
            "the overtime spec's own insufficiency must never force "
            "a spurious repair of the (already valid) redundancy "
            "content",
        )
        self.assertTrue(response.grounded)
        self.assertEqual(
            metrics.evidence_status_by_country,
            {"GB#0": "direct", "GB#1": "insufficient"},
        )
        self.assertIn("Redundancy payments", response.answer)
        self.assertIn("overtime", response.answer)

    def test_a_leaky_search_function_never_lets_one_action_borrow_another(
        self,
    ) -> None:
        # A deliberately non-compliant search_function that ignores
        # country_codes and always returns every country's hits -
        # simulating a hypothetical retrieval bug - still must not let
        # PE's dismissal concept be satisfied by an ES hit, or
        # vice versa: evidence-status grouping is by the hit's own
        # country_code, never by which spec's query fetched it.
        all_hits = [
            _build_hit(
                country="Spain",
                country_code="ES",
                subsection="Dismissal",
                content="Dismissal without just cause requires severance.",
            ),
            _build_hit(
                chunk_id="chunk-pe",
                country="Peru",
                country_code="PE",
                subsection="Annual Leave",
                content="Employees are entitled to 30 days paid leave.",
            ),
        ]

        def leaky_search(request: object) -> LegalSearchResponse:
            return LegalSearchResponse(
                query=request.query,
                total=len(all_hits),
                limit=request.limit,
                offset=0,
                took_ms=1,
                hits=list(all_hits),
            )

        client = FakeGenerationClient(
            answer=(
                "Spain\n- Dismissal without just cause requires "
                "severance. [1]"
            )
        )
        metrics = _build_metrics("leaky-search-no-cross-contamination")

        specs = [
            LegalActionEvidenceSpec(
                country_codes=["ES"],
                legal_topics=["Termination of Employment Contracts"],
                subject_text="dismissal grounds",
                search_concepts=[
                    ConversationSearchConcept(terms=["dismissal"])
                ],
                evidence_mode="direct_topic",
            ),
            LegalActionEvidenceSpec(
                country_codes=["PE"],
                legal_topics=["Working Conditions"],
                subject_text="overtime rules",
                search_concepts=[
                    ConversationSearchConcept(terms=["overtime"])
                ],
                evidence_mode="direct_topic",
            ),
        ]

        response = answer_legal_question(
            request=LegalChatRequest(
                question=(
                    "Explain dismissal rules in Spain and overtime "
                    "rules in Peru."
                ),
                country_codes=["ES", "PE"],
            ),
            search_function=leaky_search,
            generation_client=client,
            metrics=metrics,
            action_specs=specs,
        )

        # PE's own hit (about annual leave, not overtime) never
        # satisfies the overtime concept just because the leaky
        # search handed it back - PE is correctly insufficient.
        self.assertEqual(
            metrics.evidence_status_by_country,
            {"ES": "direct", "PE": "insufficient"},
        )
        self.assertTrue(response.grounded)
        self.assertIn("overtime rules", response.answer)


class RepairPipelineHardeningTests(unittest.TestCase):
    """
    Phase 3 hardening: the exact structural failure diagnosed live
    (an insufficient country filtered from generation while the
    question still names it verbatim, so the model tries to address
    it anyway, producing a heading the structure validator does not
    recognize) plus the general repair-skeleton guarantees - never a
    second repair, never a generic legal fallback, never a hard error
    silently downgraded to succeed.
    """

    def test_excluded_country_instruction_is_sent_when_a_country_is_dropped(
        self,
    ) -> None:
        hits_by_country = {
            "BR": [
                _build_hit(
                    country="Brazil",
                    country_code="BR",
                    subsection="Notice",
                    content=(
                        "The statutory notice period is proportional "
                        "to length of service in Brazil."
                    ),
                )
            ],
            "MX": [
                _build_hit(
                    chunk_id="chunk-mx",
                    country="Mexico",
                    country_code="MX",
                    subsection="Onboarding",
                    content=(
                        "New hires must complete registration "
                        "paperwork in Mexico."
                    ),
                )
            ],
        }

        client = FakeGenerationClient(
            answer=(
                "Brazil\n- The statutory notice period is "
                "proportional to length of service. [1]"
            )
        )
        metrics = _build_metrics("excluded-country-instruction")

        specs = [
            LegalActionEvidenceSpec(
                country_codes=["BR", "MX"],
                legal_topics=["Termination of Employment Contracts"],
                subject_text="statutory notice periods",
                search_concepts=[
                    ConversationSearchConcept(terms=["notice period"])
                ],
                evidence_mode="direct_topic",
            ),
        ]

        answer_legal_question(
            request=LegalChatRequest(
                question="Compare notice periods in Brazil and Mexico.",
                country_codes=["BR", "MX"],
            ),
            search_function=_make_country_scoped_search_function(
                hits_by_country
            ),
            generation_client=client,
            metrics=metrics,
            action_specs=specs,
        )

        self.assertGreaterEqual(len(client.calls), 1)
        instructions_used = client.calls[0][0]
        self.assertIn(
            EXCLUDED_COUNTRY_HEADING_INSTRUCTION_TEMPLATE.format(
                countries="Brazil"
            ),
            instructions_used,
        )

    def test_reproduced_excluded_country_heading_violation_then_repaired(
        self,
    ) -> None:
        # Reproduces, deterministically, the exact live failure: the
        # first attempt adds a heading for Mexico even though Mexico
        # was dropped as insufficient - the repair then complies.
        hits_by_country = {
            "BR": [
                _build_hit(
                    country="Brazil",
                    country_code="BR",
                    subsection="Notice",
                    content=(
                        "The statutory notice period is proportional "
                        "to length of service in Brazil."
                    ),
                )
            ],
            "MX": [
                _build_hit(
                    chunk_id="chunk-mx",
                    country="Mexico",
                    country_code="MX",
                    subsection="Onboarding",
                    content=(
                        "New hires must complete registration "
                        "paperwork in Mexico."
                    ),
                )
            ],
        }

        client = SequencedFakeGenerationClient(
            answers=[
                (
                    "Mexico\n- A definitive answer on notice periods "
                    "in Mexico cannot be provided from the supplied "
                    "sources.\n\nBrazil\n- The statutory notice "
                    "period is proportional to length of service. [1]"
                ),
                (
                    "Brazil\n- The statutory notice period is "
                    "proportional to length of service. [1]"
                ),
            ]
        )
        metrics = _build_metrics("reproduced-then-repaired")

        specs = [
            LegalActionEvidenceSpec(
                country_codes=["BR", "MX"],
                legal_topics=["Termination of Employment Contracts"],
                subject_text="statutory notice periods",
                search_concepts=[
                    ConversationSearchConcept(terms=["notice period"])
                ],
                evidence_mode="direct_topic",
            ),
        ]

        response = answer_legal_question(
            request=LegalChatRequest(
                question="Compare notice periods in Brazil and Mexico.",
                country_codes=["BR", "MX"],
            ),
            search_function=_make_country_scoped_search_function(
                hits_by_country
            ),
            generation_client=client,
            metrics=metrics,
            action_specs=specs,
        )

        self.assertEqual(len(client.calls), 2)
        self.assertTrue(response.grounded)
        self.assertEqual(metrics.generation_attempts, 2)
        self.assertTrue(metrics.repair_triggered)
        self.assertEqual(metrics.final_hard_error_types, [])

    def test_a_country_excluded_upstream_for_corpus_unavailability_gets_the_same_instruction(
        self,
    ) -> None:
        # Reproduces, deterministically, a second real cause of the
        # excluded-country-heading class of 502 - found live via the
        # 0.4.2 durcissement mission's own reinforced validation
        # (Phase 9): a country named in the question alongside
        # supported ones, but excluded upstream in chat.py for being
        # outside the supported corpus entirely (not for insufficient
        # evidence) - previously only handled by a post-hoc "Note: X
        # is not covered" appended after generation, with nothing
        # telling the generation model to ignore it. This is the exact
        # live-reproduced Brazil/Mexico/Argentina + Chile case (Chile
        # is not one of the corpus's supported countries at all), only
        # the request-understanding call was healthy this time so
        # chat.py's own resolve_country_availability - not the
        # evidence gate - is what excluded Chile before this call.
        hits_by_country = {
            "BR": [
                _build_hit(
                    country="Brazil",
                    country_code="BR",
                    subsection="Notice",
                    content=(
                        "The statutory notice period is proportional "
                        "to length of service in Brazil."
                    ),
                )
            ],
            "MX": [
                _build_hit(
                    chunk_id="chunk-mx",
                    country="Mexico",
                    country_code="MX",
                    subsection="Notice",
                    content=(
                        "There is no statutory notice period under "
                        "the Federal Labor Law in Mexico."
                    ),
                )
            ],
        }

        client = FakeGenerationClient(
            answer=(
                "Brazil\n- The statutory notice period is "
                "proportional to length of service. [1]\n\nMexico\n"
                "- There is no statutory notice period under the "
                "Federal Labor Law. [2]"
            )
        )
        metrics = _build_metrics("known-excluded-upstream")

        response = answer_legal_question(
            request=LegalChatRequest(
                question=(
                    "Compare termination notice periods in Brazil, "
                    "Mexico, and Chile."
                ),
                country_codes=["BR", "MX"],
            ),
            search_function=_make_country_scoped_search_function(
                hits_by_country
            ),
            generation_client=client,
            metrics=metrics,
            known_excluded_country_codes=["CL"],
        )

        self.assertTrue(response.grounded)
        self.assertEqual(len(client.calls), 1)
        instructions_used = client.calls[0][0]
        self.assertIn(
            EXCLUDED_COUNTRY_HEADING_INSTRUCTION_TEMPLATE.format(
                countries="Brazil, Mexico"
            ),
            instructions_used,
        )

    def test_still_invalid_after_repair_is_a_safe_failure_not_a_loop(
        self,
    ) -> None:
        hit = _build_hit(
            country="Brazil",
            country_code="BR",
            subsection="Notice",
            content="Prior notice is proportional to length of service.",
        )

        # Both attempts repeat the exact same free-text-before-bullets
        # violation - the repair must not be retried a second time,
        # and the request must fail safely rather than return an
        # invalid answer.
        violating_answer = (
            "Brazil\n"
            "Prior notice is proportional to length of service, "
            "without a leading bullet at all. [1]"
        )
        client = SequencedFakeGenerationClient(
            answers=[violating_answer, violating_answer]
        )
        metrics = _build_metrics("still-invalid-after-repair")

        with self.assertRaises(RagAnswerError):
            answer_legal_question(
                request=LegalChatRequest(
                    question="Explain notice periods in Brazil.",
                    country_codes=["BR"],
                ),
                search_function=_make_search_function([hit]),
                generation_client=client,
                metrics=metrics,
            )

        self.assertEqual(
            len(client.calls),
            2,
            "exactly one generation plus one repair - never a loop",
        )
        self.assertEqual(
            metrics.final_hard_error_types,
            ["invalid_grounding_structure"],
        )

    def test_free_text_before_bullets_is_invalid_grounding_structure(
        self,
    ) -> None:
        hit = _build_hit(
            country="Brazil",
            country_code="BR",
            content="Prior notice is proportional to length of service.",
        )
        client = SequencedFakeGenerationClient(
            answers=[
                (
                    "Brazil\n"
                    "Prior notice is proportional to length of "
                    "service, stated as a plain sentence. [1]"
                ),
                (
                    "Brazil\n- Prior notice is proportional to "
                    "length of service. [1]"
                ),
            ]
        )
        metrics = _build_metrics("free-text-before-bullets")

        response = answer_legal_question(
            request=LegalChatRequest(
                question="Explain notice periods in Brazil.",
                country_codes=["BR"],
            ),
            search_function=_make_search_function([hit]),
            generation_client=client,
            metrics=metrics,
        )

        self.assertEqual(
            metrics.initial_hard_error_types,
            ["invalid_grounding_structure"],
        )
        self.assertTrue(response.grounded)

    def test_a_missing_country_section_is_invalid_grounding_structure(
        self,
    ) -> None:
        hits = [
            _build_hit(
                country="Brazil",
                country_code="BR",
                content="Prior notice is proportional to length of service.",
            ),
            _build_hit(
                chunk_id="chunk-ar",
                country="Argentina",
                country_code="AR",
                content="Prior notice is one or two months depending on tenure.",
            ),
        ]
        client = SequencedFakeGenerationClient(
            answers=[
                (
                    # Argentina is requested, and named in prose, but
                    # never gets its own dedicated section - isolating
                    # this to the structural defect alone, distinct
                    # from the separate missing_requested_country
                    # (country name never mentioned anywhere) check.
                    "Brazil\n- Prior notice is proportional to "
                    "length of service, unlike in Argentina. [1]"
                ),
                (
                    "Brazil\n- Prior notice is proportional to "
                    "length of service. [1]\n\nArgentina\n- Prior "
                    "notice is one or two months depending on "
                    "tenure. [2]"
                ),
            ]
        )
        metrics = _build_metrics("missing-country-section")

        response = answer_legal_question(
            request=LegalChatRequest(
                question="Explain notice periods in Brazil and Argentina.",
                country_codes=["BR", "AR"],
            ),
            search_function=_make_search_function(hits),
            generation_client=client,
            metrics=metrics,
        )

        self.assertEqual(
            metrics.initial_hard_error_types,
            ["invalid_grounding_structure"],
        )
        self.assertTrue(response.grounded)

    def test_an_unrecognized_heading_is_invalid_grounding_structure(
        self,
    ) -> None:
        hit = _build_hit(
            country="Brazil",
            country_code="BR",
            content="Prior notice is proportional to length of service.",
        )
        client = SequencedFakeGenerationClient(
            answers=[
                (
                    # "Overview" is not Brazil's canonical heading and
                    # not "Comparison" - Brazil is still named in
                    # prose so this isolates the heading defect alone.
                    "Overview\n- Prior notice in Brazil is "
                    "proportional to length of service. [1]"
                ),
                (
                    "Brazil\n- Prior notice is proportional to "
                    "length of service. [1]"
                ),
            ]
        )
        metrics = _build_metrics("unrecognized-heading")

        response = answer_legal_question(
            request=LegalChatRequest(
                question="Explain notice periods in Brazil.",
                country_codes=["BR"],
            ),
            search_function=_make_search_function([hit]),
            generation_client=client,
            metrics=metrics,
        )

        self.assertEqual(
            metrics.initial_hard_error_types,
            ["invalid_grounding_structure"],
        )
        self.assertTrue(response.grounded)

    def test_section_order_is_never_itself_a_structure_violation(
        self,
    ) -> None:
        # Sections in a different order than the question named them
        # is not a documented structural rule - only presence, bullet-
        # only content, and no stray headings are checked.
        hits = [
            _build_hit(
                country="Brazil",
                country_code="BR",
                content="Prior notice is proportional to length of service.",
            ),
            _build_hit(
                chunk_id="chunk-ar",
                country="Argentina",
                country_code="AR",
                content="Prior notice is one or two months depending on tenure.",
            ),
        ]
        client = FakeGenerationClient(
            answer=(
                "Argentina\n- Prior notice is one or two months "
                "depending on tenure. [2]\n\nBrazil\n- Prior notice "
                "is proportional to length of service. [1]"
            )
        )
        metrics = _build_metrics("section-order-not-checked")

        response = answer_legal_question(
            request=LegalChatRequest(
                question="Explain notice periods in Brazil and Argentina.",
                country_codes=["BR", "AR"],
            ),
            search_function=_make_search_function(hits),
            generation_client=client,
            metrics=metrics,
        )

        self.assertTrue(response.grounded)
        self.assertEqual(metrics.initial_hard_error_types, [])

    def test_duplicated_adjacent_citations_are_collapsed_not_repaired(
        self,
    ) -> None:
        # Citation dedup is a deterministic post-processing pass
        # (_deduplicate_adjacent_citations), never a repair-triggering
        # validation error - confirmed here end to end.
        hit = _build_hit(
            country="Brazil",
            country_code="BR",
            content="Prior notice is proportional to length of service.",
        )
        client = FakeGenerationClient(
            answer=(
                "Brazil\n- Prior notice is proportional to length "
                "of service [1]. [1]"
            )
        )
        metrics = _build_metrics("duplicated-adjacent-citations")

        response = answer_legal_question(
            request=LegalChatRequest(
                question="Explain notice periods in Brazil.",
                country_codes=["BR"],
            ),
            search_function=_make_search_function([hit]),
            generation_client=client,
            metrics=metrics,
        )

        self.assertEqual(len(client.calls), 1)
        self.assertTrue(response.grounded)
        self.assertNotIn("[1]. [1]", response.answer)


if __name__ == "__main__":
    unittest.main()
