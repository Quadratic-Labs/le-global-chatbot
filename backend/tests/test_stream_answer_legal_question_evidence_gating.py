"""
GATE S3B (chat-streaming initiative): parity tests proving
stream_answer_legal_question() supports the full evidence-gating /
multi-spec input surface answer_legal_question() does - action_specs,
subject_text, search_concepts, evidence_mode, known_excluded_country_codes.

Fixtures are taken directly from test_rag_answer_evidence_gating.py's
own established, PROVEN-correct scenarios (real hit content, real
concept terms, real expected evidence_status_by_country outcomes) -
not invented content that happens to look plausible.
"""

from __future__ import annotations

import asyncio
import unittest

from app.clients.openai_responses_stream import StreamEvent, StreamEventType
from app.models.chat import LegalChatRequest
from app.models.conversation_state import ConversationSearchConcept
from app.models.search import LegalSearchHit, LegalSearchResponse
from app.services.rag_answer import (
    EXCLUDED_COUNTRY_HEADING_INSTRUCTION_TEMPLATE,
    LegalActionEvidenceSpec,
    StreamAnswerEventType,
    answer_legal_question,
    stream_answer_legal_question,
)

from tests.support.rag_fixtures import _build_metrics
from tests.test_rag_answer_evidence_gating import (
    _build_hit,
    _make_country_scoped_search_function,
    _make_search_function,
    _remote_work_concept,
)
from tests.support.stream_fixtures import (
    FakeStreamGenerationClient,
    _RepairOnlyClient,
    _delta_events,
)


SHARED_MODEL = "shared-test-model"


class _EquivalenceGenerationClient:
    """Sync generation client - used as answer_legal_question()'s ONLY
    client, and as stream_answer_legal_question()'s repair-only
    client (never called in these direct-pass scenarios) - always
    returns the same configured answer, records every call."""

    model = SHARED_MODEL

    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.calls: list[tuple[str, str]] = []

    def generate(self, instructions: str, input_text: str):
        from app.clients.openai_responses import GeneratedText

        self.calls.append((instructions, input_text))
        return GeneratedText(text=self.answer, model=self.model)


def _run_stream(coro_factory) -> list:
    async def collect():
        events = []
        async for event in coro_factory():
            events.append(event)
        return events

    return asyncio.run(collect())


def _recording_search_function(hits_by_country: dict[str, list[LegalSearchHit]]):
    """Like _make_country_scoped_search_function, but also records the
    exact sequence of (country_codes, query) issued - for the
    retrieval-call-equivalence assertions."""

    calls: list[tuple[tuple[str, ...], str]] = []

    def fake_search(request) -> LegalSearchResponse:
        calls.append((tuple(request.country_codes), request.query))
        hits = [
            hit
            for code in request.country_codes
            for hit in hits_by_country.get(code, [])
        ]
        return LegalSearchResponse(
            query=request.query, total=len(hits), limit=request.limit,
            offset=0, took_ms=1, hits=hits,
        )

    return fake_search, calls


class StreamingEvidenceGatingParityTests(unittest.TestCase):

    # -- A. multiple action specs -----------------------------------------

    def test_multiple_action_specs_both_direct(self) -> None:
        hits_by_country = {
            "GB": [
                _build_hit(
                    country="United Kingdom", country_code="GB",
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
                    chunk_id="chunk-es", country="Spain", country_code="ES",
                    subsection="Overtime",
                    content=(
                        "Overtime hours are capped at 80 hours per "
                        "year in Spain."
                    ),
                )
            ],
        }
        answer = (
            "United Kingdom\n- Fixed-term contracts convert "
            "after four years. [1]\n\nSpain\n- Overtime is capped "
            "at 80 hours per year. [2]"
        )
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

        stream_client = FakeStreamGenerationClient(_delta_events(answer))
        sync_client = _RepairOnlyClient(answer="should never be used")
        metrics = _build_metrics("stream-two-legal-disjoint")

        events = _run_stream(
            lambda: stream_answer_legal_question(
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
                generation_client=sync_client,
                stream_generation_client=stream_client,
                action_specs=specs,
                metrics=metrics,
            )
        )

        finalized = events[-1]
        self.assertEqual(StreamAnswerEventType.FINALIZED, finalized.type)
        self.assertTrue(finalized.result.grounded)
        self.assertEqual(
            {"GB": "direct", "ES": "direct"},
            metrics.evidence_status_by_country,
        )
        # Direct pass: repair (sync) client must never be called.
        self.assertEqual(0, len(sync_client.calls))

    # -- B. subject_text influencing the query/context ---------------------

    def test_subject_text_direct_hit_proceeds_to_generation(self) -> None:
        hits = [
            _build_hit(
                country="United Kingdom", country_code="GB",
                subsection="Remote Work",
                content=(
                    "Employees may telework subject to written "
                    "agreement with their employer."
                ),
            ),
        ]
        answer = (
            "United Kingdom\n- Telework is permitted subject to "
            "agreement. [1]"
        )

        stream_client = FakeStreamGenerationClient(_delta_events(answer))
        sync_client = _RepairOnlyClient(answer="should never be used")
        metrics = _build_metrics("stream-direct-hit")

        events = _run_stream(
            lambda: stream_answer_legal_question(
                request=LegalChatRequest(
                    question="Can employees work remotely?",
                    country_codes=["GB"],
                ),
                search_function=_make_search_function(hits),
                generation_client=sync_client,
                stream_generation_client=stream_client,
                subject_text="remote work",
                search_concepts=[_remote_work_concept()],
                evidence_mode="direct_topic",
                metrics=metrics,
            )
        )

        finalized = events[-1]
        self.assertEqual(StreamAnswerEventType.FINALIZED, finalized.type)
        self.assertTrue(finalized.result.grounded)
        self.assertEqual(
            {"GB": "direct"}, metrics.evidence_status_by_country,
        )

    # -- C. explicit search_concepts (insufficient -> early exit) ---------

    def test_all_countries_insufficient_finalizes_with_no_generation(
        self,
    ) -> None:
        hits = [
            _build_hit(
                country="United Kingdom", country_code="GB",
                section="Working Conditions", subsection="Working Hours",
                content="Standard working hours are 9am to 5pm.",
            ),
        ]

        stream_client = FakeStreamGenerationClient(
            [StreamEvent(type=StreamEventType.COMPLETED)]
        )
        sync_client = _RepairOnlyClient(answer="should never be used")
        metrics = _build_metrics("stream-all-insufficient")

        events = _run_stream(
            lambda: stream_answer_legal_question(
                request=LegalChatRequest(
                    question="Can employees work remotely?",
                    country_codes=["GB"],
                ),
                search_function=_make_search_function(hits),
                generation_client=sync_client,
                stream_generation_client=stream_client,
                subject_text="remote work",
                search_concepts=[_remote_work_concept()],
                evidence_mode="direct_topic",
                metrics=metrics,
            )
        )

        # No generation attempted at all: FINALIZED is the only event.
        self.assertEqual(1, len(events))
        self.assertEqual(StreamAnswerEventType.FINALIZED, events[0].type)
        self.assertFalse(events[0].result.grounded)
        self.assertEqual(events[0].result.sources, [])
        self.assertIn("remote work", events[0].result.answer)
        self.assertIn("United Kingdom", events[0].result.answer)
        self.assertEqual(
            {"GB": "insufficient"}, metrics.evidence_status_by_country,
        )
        self.assertEqual(0, len(stream_client.calls))
        self.assertEqual(0, len(sync_client.calls))

    # -- D. evidence_mode (mixed: one direct, one insufficient) -----------

    def test_mixed_insufficient_and_direct_countries(self) -> None:
        hits = [
            _build_hit(
                country="United Kingdom", country_code="GB",
                subsection="Remote Work",
                content="Employees may telework by written agreement.",
            ),
        ]
        answer = (
            "United Kingdom\n- Telework is permitted subject to "
            "agreement. [1]"
        )

        stream_client = FakeStreamGenerationClient(_delta_events(answer))
        sync_client = _RepairOnlyClient(answer="should never be used")
        metrics = _build_metrics("stream-mixed")

        events = _run_stream(
            lambda: stream_answer_legal_question(
                request=LegalChatRequest(
                    question="Can employees work remotely?",
                    country_codes=["GB", "PE"],
                ),
                search_function=_make_search_function(hits),
                generation_client=sync_client,
                stream_generation_client=stream_client,
                subject_text="remote work",
                search_concepts=[_remote_work_concept()],
                evidence_mode="direct_topic",
                metrics=metrics,
            )
        )

        finalized = events[-1]
        self.assertEqual(StreamAnswerEventType.FINALIZED, finalized.type)
        self.assertTrue(finalized.result.grounded)
        self.assertEqual(
            {"GB": "direct", "PE": "insufficient"},
            metrics.evidence_status_by_country,
        )
        self.assertIn("Peru", finalized.result.answer)
        self.assertIn("remote work", finalized.result.answer)
        self.assertIn("Telework is permitted", finalized.result.answer)

        cited_countries = {s.country for s in finalized.result.sources}
        self.assertNotIn("Peru", cited_countries)

    # -- E. known_excluded_country_codes -----------------------------------

    def test_known_excluded_country_codes_folds_into_instructions(
        self,
    ) -> None:
        hits_by_country = {
            "BR": [
                _build_hit(
                    country="Brazil", country_code="BR",
                    subsection="Notice",
                    content=(
                        "The statutory notice period is proportional "
                        "to length of service in Brazil."
                    ),
                )
            ],
            "MX": [
                _build_hit(
                    chunk_id="chunk-mx", country="Mexico",
                    country_code="MX", subsection="Notice",
                    content=(
                        "There is no statutory notice period under "
                        "the Federal Labor Law in Mexico."
                    ),
                )
            ],
        }
        answer = (
            "Brazil\n- The statutory notice period is "
            "proportional to length of service. [1]\n\nMexico\n"
            "- There is no statutory notice period under the "
            "Federal Labor Law. [2]"
        )

        stream_client = FakeStreamGenerationClient(_delta_events(answer))
        sync_client = _RepairOnlyClient(answer="should never be used")

        events = _run_stream(
            lambda: stream_answer_legal_question(
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
                generation_client=sync_client,
                stream_generation_client=stream_client,
                known_excluded_country_codes=["CL"],
            )
        )

        finalized = events[-1]
        self.assertEqual(StreamAnswerEventType.FINALIZED, finalized.type)
        self.assertTrue(finalized.result.grounded)
        self.assertEqual(1, len(stream_client.calls))

        instructions_used = stream_client.calls[0][0]
        self.assertIn(
            EXCLUDED_COUNTRY_HEADING_INSTRUCTION_TEMPLATE.format(
                countries="Brazil, Mexico"
            ),
            instructions_used,
        )

    # -- F. combined: multi-spec + known_excluded_country_codes -----------

    def test_combined_action_specs_and_known_excluded_country_codes(
        self,
    ) -> None:
        hits_by_country = {
            "ES": [
                _build_hit(
                    country="Spain", country_code="ES",
                    subsection="Dismissal",
                    content=(
                        "Dismissal without just cause requires "
                        "severance pay in Spain."
                    ),
                )
            ],
            "AU": [
                _build_hit(
                    chunk_id="chunk-au", country="Australia",
                    country_code="AU", subsection="Dismissal",
                    content=(
                        "Unfair dismissal claims require showing "
                        "the dismissal was harsh in Australia."
                    ),
                )
            ],
        }
        answer = (
            "Spain\n- Dismissal without just cause requires "
            "severance pay. [1]\n\nAustralia\n- Unfair dismissal "
            "claims require showing harshness. [2]"
        )
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
        ]

        stream_client = FakeStreamGenerationClient(_delta_events(answer))
        sync_client = _RepairOnlyClient(answer="should never be used")
        metrics = _build_metrics("stream-combined")

        events = _run_stream(
            lambda: stream_answer_legal_question(
                request=LegalChatRequest(
                    question="Compare dismissal rules in Spain and Australia.",
                    country_codes=["ES", "AU"],
                ),
                search_function=_make_country_scoped_search_function(
                    hits_by_country
                ),
                generation_client=sync_client,
                stream_generation_client=stream_client,
                action_specs=specs,
                known_excluded_country_codes=["CL"],
                metrics=metrics,
            )
        )

        finalized = events[-1]
        self.assertEqual(StreamAnswerEventType.FINALIZED, finalized.type)
        self.assertTrue(finalized.result.grounded)
        self.assertEqual(
            {"ES": "direct", "AU": "direct"},
            metrics.evidence_status_by_country,
        )

        instructions_used = stream_client.calls[0][0]
        self.assertIn(
            EXCLUDED_COUNTRY_HEADING_INSTRUCTION_TEMPLATE.format(
                countries="Spain, Australia"
            ),
            instructions_used,
        )


class StrongEquivalenceTests(unittest.TestCase):
    """
    Section 5: for identical deterministic provider output and
    identical search results, answer_legal_question() and
    stream_answer_legal_question() must produce equivalent final
    results across every business-relevant field - proven per case,
    never assumed "by construction" alone.
    """

    def _compare(
        self, *, request_kwargs: dict, extra_kwargs: dict, answer: str,
    ) -> None:
        non_streaming_client = _EquivalenceGenerationClient(answer=answer)

        non_streaming_result = answer_legal_question(
            request=LegalChatRequest(**request_kwargs),
            generation_client=non_streaming_client,
            **extra_kwargs,
        )

        stream_client = FakeStreamGenerationClient(_delta_events(answer))
        stream_client.model = SHARED_MODEL
        streaming_sync_client = _EquivalenceGenerationClient(
            answer="should never be used (no repair expected)"
        )

        events = _run_stream(
            lambda: stream_answer_legal_question(
                request=LegalChatRequest(**request_kwargs),
                generation_client=streaming_sync_client,
                stream_generation_client=stream_client,
                **extra_kwargs,
            )
        )

        streaming_result = events[-1].result

        self.assertEqual(non_streaming_result.answer, streaming_result.answer)
        self.assertEqual(
            non_streaming_result.grounded, streaming_result.grounded,
        )
        self.assertEqual(non_streaming_result.model, streaming_result.model)
        self.assertEqual(
            non_streaming_result.retrieval_total,
            streaming_result.retrieval_total,
        )
        self.assertEqual(
            len(non_streaming_result.sources), len(streaming_result.sources),
        )

        for expected_source, actual_source in zip(
            non_streaming_result.sources, streaming_result.sources,
        ):
            self.assertEqual(expected_source.citation, actual_source.citation)
            self.assertEqual(
                expected_source.chunk_id, actual_source.chunk_id,
            )
            self.assertEqual(
                expected_source.country_code, actual_source.country_code,
            )

        # No repair attempted on either path for a clean answer.
        self.assertEqual(0, len(streaming_sync_client.calls))

    def test_equivalence_single_country(self) -> None:
        hits = [_build_hit()]
        self._compare(
            request_kwargs=dict(
                question="What notice period applies?",
                country_codes=["GB"],
            ),
            extra_kwargs=dict(
                search_function=_make_search_function(hits),
            ),
            answer=(
                "United Kingdom\n- The minimum notice is one week "
                "in the stated circumstances [1]."
            ),
        )

    def test_equivalence_evidence_gated_direct_hit(self) -> None:
        hits = [
            _build_hit(
                country="United Kingdom", country_code="GB",
                subsection="Remote Work",
                content=(
                    "Employees may telework subject to written "
                    "agreement with their employer."
                ),
            ),
        ]
        self._compare(
            request_kwargs=dict(
                question="Can employees work remotely?",
                country_codes=["GB"],
            ),
            extra_kwargs=dict(
                search_function=_make_search_function(hits),
                subject_text="remote work",
                search_concepts=[_remote_work_concept()],
                evidence_mode="direct_topic",
            ),
            answer=(
                "United Kingdom\n- Telework is permitted subject to "
                "agreement. [1]"
            ),
        )

    def test_equivalence_multi_spec(self) -> None:
        hits_by_country = {
            "GB": [
                _build_hit(
                    country="United Kingdom", country_code="GB",
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
                    chunk_id="chunk-es", country="Spain", country_code="ES",
                    subsection="Overtime",
                    content=(
                        "Overtime hours are capped at 80 hours per "
                        "year in Spain."
                    ),
                )
            ],
        }
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
        self._compare(
            request_kwargs=dict(
                question=(
                    "Explain fixed-term contracts in the UK and "
                    "overtime rules in Spain."
                ),
                country_codes=["GB", "ES"],
            ),
            extra_kwargs=dict(
                search_function=_make_country_scoped_search_function(
                    hits_by_country
                ),
                action_specs=specs,
            ),
            answer=(
                "United Kingdom\n- Fixed-term contracts convert "
                "after four years. [1]\n\nSpain\n- Overtime is capped "
                "at 80 hours per year. [2]"
            ),
        )


class RetrievalCallEquivalenceTests(unittest.TestCase):
    """Section 6: streaming must not execute extra/duplicate/omitted
    retrievals compared to the stable path, for the same multi-spec
    input."""

    def test_retrieval_call_sequence_identical_for_multi_spec(self) -> None:
        hits_by_country = {
            "GB": [
                _build_hit(
                    country="United Kingdom", country_code="GB",
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
                    chunk_id="chunk-es", country="Spain", country_code="ES",
                    subsection="Overtime",
                    content=(
                        "Overtime hours are capped at 80 hours per "
                        "year in Spain."
                    ),
                )
            ],
        }
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
        answer = (
            "United Kingdom\n- Fixed-term contracts convert "
            "after four years. [1]\n\nSpain\n- Overtime is capped "
            "at 80 hours per year. [2]"
        )

        non_streaming_search, non_streaming_calls = (
            _recording_search_function(hits_by_country)
        )
        answer_legal_question(
            request=LegalChatRequest(
                question=(
                    "Explain fixed-term contracts in the UK and "
                    "overtime rules in Spain."
                ),
                country_codes=["GB", "ES"],
            ),
            search_function=non_streaming_search,
            generation_client=_EquivalenceGenerationClient(answer=answer),
            action_specs=specs,
        )

        streaming_search, streaming_calls = _recording_search_function(
            hits_by_country
        )
        _run_stream(
            lambda: stream_answer_legal_question(
                request=LegalChatRequest(
                    question=(
                        "Explain fixed-term contracts in the UK and "
                        "overtime rules in Spain."
                    ),
                    country_codes=["GB", "ES"],
                ),
                search_function=streaming_search,
                generation_client=_RepairOnlyClient(
                    answer="should never be used"
                ),
                stream_generation_client=FakeStreamGenerationClient(
                    _delta_events(answer)
                ),
                action_specs=specs,
            )
        )

        self.assertEqual(2, len(non_streaming_calls))
        self.assertEqual(non_streaming_calls, streaming_calls)


if __name__ == "__main__":
    unittest.main()
