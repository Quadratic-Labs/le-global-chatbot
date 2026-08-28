"""
Tests for stream_answer_legal_question() (chat-streaming initiative,
GATE S3).

answer_legal_question() and its own test suite (test_rag_answer.py)
are completely unaffected - the full backend suite already discovers
and runs test_rag_answer.py independently to prove it.

Fixtures (_build_hit, _build_metrics, FakeGenerationClient, and the
exact initial/repaired answer text pairs used below) are taken
directly from test_rag_answer.py's own established, PROVEN-correct
fixtures - real text known to trigger the real validators the way the
test names claim, not invented text that happens to look right.
"""

from __future__ import annotations

import asyncio
import unittest
from typing import Any

from app.clients.openai_responses import GeneratedText, OpenAIResponseError
from app.clients.openai_responses_stream import StreamEvent, StreamEventType
from app.models.chat import LegalChatRequest
from app.models.search import LegalSearchHit, LegalSearchResponse
from app.services.chat_metrics import LegalChatMetrics
from app.services.rag_answer import (
    RagAnswerError,
    StreamAnswerEvent,
    StreamAnswerEventType,
    StreamAnswerTimings,
    stream_answer_legal_question,
)

from tests.test_rag_answer import (
    FakeGenerationClient,
    _build_hit,
    _build_metrics,
    _make_search_function,
)


class _RepairOnlyClient:
    """
    Sync generation client double used ONLY for the hidden repair
    call in the streaming architecture.

    Deliberately NOT FakeGenerationClient (test_rag_answer.py): that
    fake's "second call returns repair_answer" logic assumes ONE
    client serves both the first AND the repair generate() call - true
    for answer_legal_question(), but never true here, where the first
    generation always goes through a separate FakeStreamGenerationClient
    and this sync client's own generate() is called for repair only
    (its first-ever call IS the repair call) - so it always returns
    the one configured answer, no call-count logic needed.
    """

    model = "test-model"

    def __init__(self, answer: str, *, raise_error: bool = False) -> None:
        self.answer = answer
        self.raise_error = raise_error
        self.calls: list[tuple[str, str]] = []

    def generate(self, instructions: str, input_text: str) -> GeneratedText:
        self.calls.append((instructions, input_text))

        if self.raise_error:
            raise OpenAIResponseError("boom")

        return GeneratedText(text=self.answer, model=self.model)


class FakeStreamGenerationClient:
    """Scripted streaming test double for the final-answer generation
    stage only - satisfies TextStreamGenerationClient (model + async
    stream())."""

    model = "test-stream-model"

    def __init__(self, events: list[StreamEvent]) -> None:
        self._events = events
        self.calls: list[tuple[str, str]] = []

    async def stream(self, instructions: str, input_text: str):
        self.calls.append((instructions, input_text))

        for event in self._events:
            yield event


def _delta_events(text: str, *, chunk_size: int = 1) -> list[StreamEvent]:
    """Split `text` into `chunk_size`-character DELTA events, then a
    COMPLETED event - simulating a real provider dribbling out an
    answer token by token."""

    events = [
        StreamEvent(type=StreamEventType.DELTA, text=text[i:i + chunk_size])
        for i in range(0, len(text), chunk_size)
    ]
    events.append(StreamEvent(type=StreamEventType.COMPLETED))
    return events


def _run_stream(coro_factory) -> list[StreamAnswerEvent]:
    async def collect():
        events = []
        async for event in coro_factory():
            events.append(event)
        return events

    return asyncio.run(collect())


def _assert_valid_event_sequence(
    test_case: unittest.TestCase,
    events: list[StreamAnswerEvent],
) -> None:
    """
    Stream lifecycle invariants (mission section 13) - applied to
    EVERY scenario below, not relied upon only by convention.
    """

    test_case.assertTrue(events, "a stream must yield at least one event")

    terminal_types = {
        StreamAnswerEventType.FINALIZED, StreamAnswerEventType.ERROR,
    }
    terminal_indices = [
        index
        for index, event in enumerate(events)
        if event.type in terminal_types
    ]

    test_case.assertEqual(
        1, len(terminal_indices),
        f"exactly one terminal event required, got: {[e.type for e in events]}",
    )
    test_case.assertEqual(
        len(events) - 1, terminal_indices[0],
        "no event may follow the terminal event",
    )

    validating_indices = [
        index for index, event in enumerate(events)
        if event.type is StreamAnswerEventType.VALIDATING
    ]
    test_case.assertLessEqual(
        len(validating_indices), 1, "VALIDATING may occur at most once",
    )

    if validating_indices:
        validating_index = validating_indices[0]

        for index, event in enumerate(events):
            if event.type is StreamAnswerEventType.ANSWER_DELTA:
                test_case.assertLess(
                    index, validating_index,
                    "no ANSWER_DELTA may occur after VALIDATING",
                )



class StreamAnswerLegalQuestionTests(unittest.TestCase):

    CLEAN_ANSWER = (
        "United Kingdom\n"
        "- The minimum notice is one week "
        "in the stated circumstances [1]."
    )

    def _default_request(self, **overrides: Any) -> LegalChatRequest:
        fields = dict(
            question="What notice period applies?",
            country_codes=["GB"],
        )
        fields.update(overrides)
        return LegalChatRequest(**fields)

    # -- A. direct validation pass -------------------------------------

    def test_direct_pass_yields_deltas_then_validating_then_finalized(
        self,
    ) -> None:
        stream_client = FakeStreamGenerationClient(
            _delta_events(self.CLEAN_ANSWER, chunk_size=5)
        )
        sync_client = FakeGenerationClient(answer=self.CLEAN_ANSWER)
        metrics = _build_metrics("test-direct-pass")

        events = _run_stream(
            lambda: stream_answer_legal_question(
                request=self._default_request(),
                search_function=_make_search_function(),
                generation_client=sync_client,
                stream_generation_client=stream_client,
                metrics=metrics,
            )
        )

        _assert_valid_event_sequence(self, events)

        types = [event.type for event in events]
        self.assertIn(StreamAnswerEventType.ANSWER_DELTA, types)
        self.assertIn(StreamAnswerEventType.VALIDATING, types)
        self.assertNotIn(StreamAnswerEventType.DISCARD, types)
        self.assertNotIn(StreamAnswerEventType.REPLACEMENT, types)
        self.assertEqual(StreamAnswerEventType.FINALIZED, types[-1])

        finalized = events[-1]
        self.assertTrue(finalized.result.grounded)
        self.assertEqual(self.CLEAN_ANSWER, finalized.result.answer)

        # Direct pass: repair generation must never have been called.
        self.assertEqual(0, len(sync_client.calls))

        self.assertIs(metrics.repair_triggered, False)
        self.assertEqual(1, metrics.generation_attempts)

    def test_concatenated_deltas_reconstruct_exact_answer_text(
        self,
    ) -> None:
        stream_client = FakeStreamGenerationClient(
            _delta_events(self.CLEAN_ANSWER, chunk_size=3)
        )
        sync_client = FakeGenerationClient(answer=self.CLEAN_ANSWER)

        events = _run_stream(
            lambda: stream_answer_legal_question(
                request=self._default_request(),
                search_function=_make_search_function(),
                generation_client=sync_client,
                stream_generation_client=stream_client,
            )
        )

        deltas = "".join(
            event.delta_text
            for event in events
            if event.type is StreamAnswerEventType.ANSWER_DELTA
        )
        self.assertEqual(self.CLEAN_ANSWER, deltas)

    # -- B. validation failure + successful repair ----------------------

    def test_hard_error_repair_success_yields_replacement_then_finalized(
        self,
    ) -> None:
        initial_answer = (
            "United Kingdom\n"
            "- Employees are entitled to unpaid "
            "leave for family reasons [1]."
        )
        repaired_answer = (
            "United Kingdom\n"
            "- Employees are entitled to paid "
            "parental leave for four weeks [1]."
        )

        stream_client = FakeStreamGenerationClient(
            _delta_events(initial_answer)
        )
        sync_client = _RepairOnlyClient(answer=repaired_answer)
        metrics = _build_metrics("test-repair-success")

        events = _run_stream(
            lambda: stream_answer_legal_question(
                request=self._default_request(
                    question="What is the paid leave entitlement in the UK?",
                ),
                search_function=_make_search_function(),
                generation_client=sync_client,
                stream_generation_client=stream_client,
                metrics=metrics,
            )
        )

        _assert_valid_event_sequence(self, events)

        types = [event.type for event in events]
        self.assertIn(StreamAnswerEventType.ANSWER_DELTA, types)
        self.assertIn(StreamAnswerEventType.VALIDATING, types)
        self.assertIn(StreamAnswerEventType.DISCARD, types)
        self.assertIn(StreamAnswerEventType.REPLACEMENT, types)
        self.assertEqual(StreamAnswerEventType.FINALIZED, types[-1])

        replacement = next(
            e for e in events if e.type is StreamAnswerEventType.REPLACEMENT
        )
        self.assertEqual(repaired_answer, replacement.replacement_text)

        finalized = events[-1]
        self.assertEqual(repaired_answer, finalized.result.answer)

        # The provisional (initial) text must NEVER be exposed as
        # replacement/final content - only the accepted repair.
        self.assertNotEqual(initial_answer, finalized.result.answer)

        # sync_client here serves ONLY the hidden repair call (the
        # first generation went through stream_client instead) - one
        # call, not two, unlike answer_legal_question's own single-
        # client convention (see _RepairOnlyClient's own docstring).
        self.assertEqual(1, len(sync_client.calls))
        self.assertEqual(1, len(stream_client.calls))
        self.assertIs(metrics.repair_triggered, True)
        self.assertIs(metrics.repair_success, True)
        self.assertIs(metrics.repair_answer_returned, True)
        self.assertEqual(2, metrics.generation_attempts)

    def test_repair_degraded_answer_reverts_to_first_via_replacement(
        self,
    ) -> None:
        """When a soft-only repair degrades an otherwise legally
        valid initial answer, the first answer wins. Because the
        initial answer had no hard error, it remains visible during
        repair and the winning text is finalized through REPLACEMENT
        without a preceding DISCARD."""

        # A soft-error-only initial answer (structure) whose "repair"
        # instead introduces a hard error (unknown citation) - the
        # first answer must win.
        initial_answer = (
            "United Kingdom\n"
            "- Bullet one covering notice periods [1].\n"
            "- Bullet two covering notice periods [1].\n"
            "- Bullet three covering notice periods [1].\n"
            "- Bullet four covering notice periods [1].\n"
            "- Bullet five covering notice periods [1].\n"
            "- Bullet six covering notice periods [1].\n"
            "- Bullet seven covering notice periods [1]."
        )
        degraded_repair_answer = "This citation does not exist [2]."

        stream_client = FakeStreamGenerationClient(
            _delta_events(initial_answer)
        )
        sync_client = _RepairOnlyClient(answer=degraded_repair_answer)

        events = _run_stream(
            lambda: stream_answer_legal_question(
                request=self._default_request(),
                search_function=_make_search_function(),
                generation_client=sync_client,
                stream_generation_client=stream_client,
            )
        )

        _assert_valid_event_sequence(self, events)

        types = [event.type for event in events]
        self.assertNotIn(
            StreamAnswerEventType.DISCARD,
            types,
        )
        self.assertIn(
            StreamAnswerEventType.REPLACEMENT,
            types,
        )

        finalized = events[-1]
        self.assertEqual(StreamAnswerEventType.FINALIZED, finalized.type)
        self.assertEqual(initial_answer, finalized.result.answer)

        replacement = next(
            e for e in events if e.type is StreamAnswerEventType.REPLACEMENT
        )
        self.assertEqual(initial_answer, replacement.replacement_text)

    # -- C. validation failure + failed repair ---------------------------

    def test_repair_failure_yields_discard_then_error(self) -> None:
        # No repair_answer override: the SAME bad text comes back from
        # the "repaired" call too - repair cannot possibly succeed.
        bad_answer = "This citation does not exist [2]."

        stream_client = FakeStreamGenerationClient(
            _delta_events(bad_answer)
        )
        sync_client = FakeGenerationClient(answer=bad_answer)
        metrics = _build_metrics("test-repair-failure")

        events = _run_stream(
            lambda: stream_answer_legal_question(
                request=self._default_request(),
                search_function=_make_search_function(),
                generation_client=sync_client,
                stream_generation_client=stream_client,
                metrics=metrics,
            )
        )

        _assert_valid_event_sequence(self, events)

        types = [event.type for event in events]
        self.assertIn(StreamAnswerEventType.DISCARD, types)
        self.assertNotIn(StreamAnswerEventType.REPLACEMENT, types)
        self.assertEqual(StreamAnswerEventType.ERROR, types[-1])

        self.assertIs(metrics.repair_triggered, True)
        self.assertIs(metrics.repair_success, False)

    # -- D/F. provider error before first delta (also covers "timeout") -

    def test_provider_error_before_first_delta_yields_error_only(
        self,
    ) -> None:
        stream_client = FakeStreamGenerationClient(
            [
                StreamEvent(
                    type=StreamEventType.ERROR,
                    error_message="OpenAI could not be reached in time.",
                    retryable=True,
                )
            ]
        )
        sync_client = FakeGenerationClient()

        events = _run_stream(
            lambda: stream_answer_legal_question(
                request=self._default_request(),
                search_function=_make_search_function(),
                generation_client=sync_client,
                stream_generation_client=stream_client,
            )
        )

        _assert_valid_event_sequence(self, events)
        self.assertEqual(1, len(events))
        self.assertEqual(StreamAnswerEventType.ERROR, events[0].type)
        self.assertTrue(events[0].retryable)

        # A repair attempt requires an initial answer to validate -
        # a provider failure before any text exists must never reach it.
        self.assertEqual(0, len(sync_client.calls))

    # -- E/G. provider error after several deltas (also covers "timeout") -

    def test_provider_error_after_deltas_yields_discard_then_error(
        self,
    ) -> None:
        events_from_provider = _delta_events("Partial answer text")[:-1]
        events_from_provider.append(
            StreamEvent(
                type=StreamEventType.ERROR,
                error_message="OpenAI streaming exceeded the maximum "
                              "allowed duration.",
                retryable=False,
            )
        )

        stream_client = FakeStreamGenerationClient(events_from_provider)
        sync_client = FakeGenerationClient()

        events = _run_stream(
            lambda: stream_answer_legal_question(
                request=self._default_request(),
                search_function=_make_search_function(),
                generation_client=sync_client,
                stream_generation_client=stream_client,
            )
        )

        _assert_valid_event_sequence(self, events)

        types = [event.type for event in events]
        self.assertIn(StreamAnswerEventType.ANSWER_DELTA, types)
        self.assertEqual(
            StreamAnswerEventType.DISCARD, types[-2],
        )
        self.assertEqual(StreamAnswerEventType.ERROR, types[-1])

        # No validation/repair ever runs for a provider-level failure -
        # there is no complete text to validate.
        self.assertEqual(0, len(sync_client.calls))

    # -- H. empty/invalid final generated answer -------------------------

    def test_empty_generation_still_follows_repair_semantics(
        self,
    ) -> None:
        """An empty accumulated answer must be treated exactly like any
        other quality failure - triggering the same repair path, never
        special-cased or silently accepted."""

        repaired_answer = self.CLEAN_ANSWER

        stream_client = FakeStreamGenerationClient(
            [StreamEvent(type=StreamEventType.COMPLETED)]
        )
        sync_client = _RepairOnlyClient(answer=repaired_answer)

        events = _run_stream(
            lambda: stream_answer_legal_question(
                request=self._default_request(),
                search_function=_make_search_function(),
                generation_client=sync_client,
                stream_generation_client=stream_client,
            )
        )

        _assert_valid_event_sequence(self, events)

        finalized = events[-1]
        self.assertEqual(StreamAnswerEventType.FINALIZED, finalized.type)
        self.assertEqual(repaired_answer, finalized.result.answer)

    # -- I. Unicode/citations across fragmented deltas --------------------

    def test_unicode_and_citations_survive_single_character_fragmentation(
        self,
    ) -> None:
        answer = (
            "France\n"
            "- L'employeur doit respecter un préavis "
            "de deux mois [1]. Café et bureau à Paris [1]."
        )

        stream_client = FakeStreamGenerationClient(
            _delta_events(answer, chunk_size=1)
        )
        sync_client = FakeGenerationClient(answer=answer)

        events = _run_stream(
            lambda: stream_answer_legal_question(
                request=self._default_request(
                    question="What is the notice period in France?",
                    country_codes=["FR"],
                ),
                search_function=_make_search_function(
                    hits=[
                        _build_hit(
                            country="France", country_code="FR",
                        )
                    ]
                ),
                generation_client=sync_client,
                stream_generation_client=stream_client,
            )
        )

        _assert_valid_event_sequence(self, events)

        deltas = "".join(
            event.delta_text
            for event in events
            if event.type is StreamAnswerEventType.ANSWER_DELTA
        )
        self.assertEqual(answer, deltas)

        finalized = events[-1]
        self.assertEqual(StreamAnswerEventType.FINALIZED, finalized.type)
        self.assertIn("préavis", finalized.result.answer)
        self.assertIn("Café", finalized.result.answer)

    # -- Missing country / empty retrieval: no generation at all ----------

    def test_missing_country_finalizes_immediately_with_no_deltas(
        self,
    ) -> None:
        stream_client = FakeStreamGenerationClient(
            [StreamEvent(type=StreamEventType.COMPLETED)]
        )
        sync_client = FakeGenerationClient()

        events = _run_stream(
            lambda: stream_answer_legal_question(
                request=self._default_request(country_codes=[]),
                search_function=_make_search_function(),
                generation_client=sync_client,
                stream_generation_client=stream_client,
            )
        )

        self.assertEqual(1, len(events))
        self.assertEqual(StreamAnswerEventType.FINALIZED, events[0].type)
        self.assertFalse(events[0].result.grounded)
        self.assertEqual(0, len(stream_client.calls))

    def test_empty_retrieval_finalizes_immediately_with_no_deltas(
        self,
    ) -> None:
        stream_client = FakeStreamGenerationClient(
            [StreamEvent(type=StreamEventType.COMPLETED)]
        )
        sync_client = FakeGenerationClient()

        def empty_search(request: Any) -> LegalSearchResponse:
            return LegalSearchResponse(
                query=request.query, total=0, limit=request.limit,
                offset=0, took_ms=1, hits=[],
            )

        events = _run_stream(
            lambda: stream_answer_legal_question(
                request=self._default_request(),
                search_function=empty_search,
                generation_client=sync_client,
                stream_generation_client=stream_client,
            )
        )

        self.assertEqual(1, len(events))
        self.assertEqual(StreamAnswerEventType.FINALIZED, events[0].type)
        self.assertFalse(events[0].result.grounded)
        self.assertEqual(0, len(stream_client.calls))

    # -- Prompt/retrieval/rerank equivalence -------------------------------

    def test_streaming_generation_receives_identical_instructions_and_input(
        self,
    ) -> None:
        """Section 9: streaming must receive the SAME effective system
        instructions/model input as the non-streaming path - proven by
        capturing what the non-streaming call actually sends, via the
        SAME sync client used as both the non-streaming client AND (in
        a separate call) the streaming path's repair client, over an
        IDENTICAL request/hits."""

        from app.services.rag_answer import answer_legal_question

        recording_client = FakeGenerationClient(answer=self.CLEAN_ANSWER)

        answer_legal_question(
            request=self._default_request(),
            search_function=_make_search_function(),
            generation_client=recording_client,
        )

        non_streaming_instructions, non_streaming_input = (
            recording_client.calls[0]
        )

        stream_client = FakeStreamGenerationClient(
            _delta_events(self.CLEAN_ANSWER)
        )
        sync_client_for_streaming = FakeGenerationClient(
            answer=self.CLEAN_ANSWER,
        )

        _run_stream(
            lambda: stream_answer_legal_question(
                request=self._default_request(),
                search_function=_make_search_function(),
                generation_client=sync_client_for_streaming,
                stream_generation_client=stream_client,
            )
        )

        streaming_instructions, streaming_input = stream_client.calls[0]

        self.assertEqual(non_streaming_instructions, streaming_instructions)
        self.assertEqual(non_streaming_input, streaming_input)

    def test_retrieval_executes_exactly_once(self) -> None:
        call_count = {"count": 0}

        def counting_search(request: Any) -> LegalSearchResponse:
            call_count["count"] += 1
            return LegalSearchResponse(
                query=request.query, total=1, limit=request.limit,
                offset=0, took_ms=1, hits=[_build_hit()],
            )

        stream_client = FakeStreamGenerationClient(
            _delta_events(self.CLEAN_ANSWER)
        )
        sync_client = FakeGenerationClient(answer=self.CLEAN_ANSWER)

        _run_stream(
            lambda: stream_answer_legal_question(
                request=self._default_request(),
                search_function=counting_search,
                generation_client=sync_client,
                stream_generation_client=stream_client,
            )
        )

        self.assertEqual(1, call_count["count"])

    # -- Comparison coverage -----------------------------------------------

    def test_two_country_comparison(self) -> None:
        stream_client = FakeStreamGenerationClient(
            _delta_events(
                "France\n- Notice is one month [1].\n\n"
                "Germany\n- Notice is one month [2]."
            )
        )
        sync_client = FakeGenerationClient(
            answer=(
                "France\n- Notice is one month [1].\n\n"
                "Germany\n- Notice is one month [2]."
            )
        )

        events = _run_stream(
            lambda: stream_answer_legal_question(
                request=self._default_request(
                    question="Compare notice periods",
                    country_codes=["FR", "DE"],
                ),
                search_function=_make_search_function(
                    hits=[
                        _build_hit(
                            chunk_id="fr-1", country="France",
                            country_code="FR",
                        ),
                        _build_hit(
                            chunk_id="de-1", country="Germany",
                            country_code="DE",
                        ),
                    ]
                ),
                generation_client=sync_client,
                stream_generation_client=stream_client,
            )
        )

        _assert_valid_event_sequence(self, events)
        finalized = events[-1]
        self.assertEqual(StreamAnswerEventType.FINALIZED, finalized.type)
        self.assertTrue(finalized.result.grounded)

    def test_three_country_comparison(self) -> None:
        answer = (
            "France\n- Notice is one month [1].\n\n"
            "Germany\n- Notice is one month [2].\n\n"
            "Belgium\n- Notice is one month [3]."
        )
        stream_client = FakeStreamGenerationClient(_delta_events(answer))
        sync_client = FakeGenerationClient(answer=answer)

        events = _run_stream(
            lambda: stream_answer_legal_question(
                request=self._default_request(
                    question="Compare notice periods",
                    country_codes=["FR", "DE", "BE"],
                ),
                search_function=_make_search_function(
                    hits=[
                        _build_hit(
                            chunk_id="fr-1", country="France",
                            country_code="FR",
                        ),
                        _build_hit(
                            chunk_id="de-1", country="Germany",
                            country_code="DE",
                        ),
                        _build_hit(
                            chunk_id="be-1", country="Belgium",
                            country_code="BE",
                        ),
                    ]
                ),
                generation_client=sync_client,
                stream_generation_client=stream_client,
            )
        )

        _assert_valid_event_sequence(self, events)
        finalized = events[-1]
        self.assertEqual(StreamAnswerEventType.FINALIZED, finalized.type)
        self.assertTrue(finalized.result.grounded)

    # -- Timing hooks --------------------------------------------------------

    def test_timings_are_populated_in_order(self) -> None:
        stream_client = FakeStreamGenerationClient(
            _delta_events(self.CLEAN_ANSWER)
        )
        sync_client = FakeGenerationClient(answer=self.CLEAN_ANSWER)
        timings = StreamAnswerTimings()

        _run_stream(
            lambda: stream_answer_legal_question(
                request=self._default_request(),
                search_function=_make_search_function(),
                generation_client=sync_client,
                stream_generation_client=stream_client,
                timings=timings,
            )
        )

        self.assertIsNotNone(timings.retrieval_and_rerank_complete)
        self.assertIsNotNone(timings.generation_start)
        self.assertIsNotNone(timings.first_provider_delta)
        self.assertIsNotNone(timings.provider_completion)
        self.assertIsNotNone(timings.validation_start)
        self.assertIsNotNone(timings.validation_end)
        self.assertIsNone(timings.repair_start)
        self.assertIsNone(timings.repair_end)
        self.assertIsNotNone(timings.finalization)

        self.assertLessEqual(
            timings.retrieval_and_rerank_complete, timings.generation_start,
        )
        self.assertLessEqual(
            timings.generation_start, timings.first_provider_delta,
        )
        self.assertLessEqual(
            timings.first_provider_delta, timings.provider_completion,
        )
        self.assertLessEqual(
            timings.provider_completion, timings.validation_start,
        )
        self.assertLessEqual(
            timings.validation_start, timings.validation_end,
        )
        self.assertLessEqual(
            timings.validation_end, timings.finalization,
        )

    def test_repair_timings_populated_when_repair_triggered(self) -> None:
        initial_answer = (
            "United Kingdom\n"
            "- Employees are entitled to unpaid "
            "leave for family reasons [1]."
        )
        repaired_answer = (
            "United Kingdom\n"
            "- Employees are entitled to paid "
            "parental leave for four weeks [1]."
        )
        stream_client = FakeStreamGenerationClient(
            _delta_events(initial_answer)
        )
        sync_client = _RepairOnlyClient(answer=repaired_answer)
        timings = StreamAnswerTimings()

        _run_stream(
            lambda: stream_answer_legal_question(
                request=self._default_request(
                    question="What is the paid leave entitlement in the UK?",
                ),
                search_function=_make_search_function(),
                generation_client=sync_client,
                stream_generation_client=stream_client,
                timings=timings,
            )
        )

        self.assertIsNotNone(timings.repair_start)
        self.assertIsNotNone(timings.repair_end)
        self.assertLessEqual(timings.repair_start, timings.repair_end)
        self.assertLessEqual(timings.repair_end, timings.finalization)


if __name__ == "__main__":
    unittest.main()
