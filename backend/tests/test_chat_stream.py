"""
Tests for POST /api/v1/chat/stream (chat-streaming initiative, GATE
S4).

Testing strategy, matching this codebase's own established convention
for router-level tests (see test_chat.py's FriendlyInvalidRequestHttpTests,
which calls legal_chat(...) directly and mocks
app.routers.chat.resolve_legal_chat_response rather than going through
a real HTTP server/TestClient):

- Route-mechanism tests mock app.routers.chat_stream.
  resolve_legal_chat_response with a small fake that either (a) raises
  directly, simulating a pre-generation failure/deterministic early
  response, or (b) calls the REAL legal_answer_generation_fn it
  receives - exercising the REAL bridge -> REAL
  stream_answer_legal_question() -> a FAKE stream client (patched via
  app.routers.chat_stream.get_openai_answer_stream_client), proving
  the actual cross-thread relay mechanism end to end, not just a
  mocked stand-in for it.
- No real network call anywhere in this file.
- POST /api/v1/chat itself is untouched - its own 166-test suite
  (test_chat.py et al.) is re-run separately (see GATE S4 report) to
  prove zero regression, not duplicated here.
"""

from __future__ import annotations

import asyncio
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fastapi import HTTPException, Response

from app.clients.openai_responses import OpenAIConfigurationError
from app.clients.openai_responses_stream import StreamEvent, StreamEventType
from app.models.chat import LegalChatRequest, LegalChatResponse
from app.services.conversation_transition import ConversationTransitionError
from app.services.country_detection import CountryDetectionError
from app.services.rag_answer import (
    InvalidLegalChatRequestError,
    RagAnswerError,
)

from app.routers import chat_stream as chat_stream_module
from app.routers.chat_stream import (
    _drain_stream_events,
    _map_stream_answer_event,
    _metadata_record,
    _serialize_ndjson_record,
    legal_chat_stream,
)
from app.routers.chat import (
    CLARIFICATION_UNSUPPORTED_REQUEST_ANSWER,
    legal_chat as real_legal_chat,
    resolve_legal_chat_response as real_resolve_legal_chat_response,
)
from app.models.search import LegalSearchResponse
from app.services.contact_state import ContactRecord, ContactState

from tests.test_rag_answer import (
    FakeGenerationClient,
    _build_hit,
    _make_search_function,
)
from tests.test_stream_answer_legal_question import (
    FakeStreamGenerationClient,
    _RepairOnlyClient,
)
from tests.test_chat import (
    FakeGenerationClient as ChatFakeGenerationClient,
    FakeUnderstandingClient,
    NoCallGenerationClient,
    NoCallUnderstandingClient,
    _FailingUnderstandingClient,
    _build_contact_hit,
    _catalog_provider,
    _document_topic_provider,
    _understanding_action,
    _understanding_result,
    _unexpected_search,
)


def _delta_events(text: str, *, chunk_size: int = 3) -> list[StreamEvent]:
    events = [
        StreamEvent(type=StreamEventType.DELTA, text=text[i:i + chunk_size])
        for i in range(0, len(text), chunk_size)
    ]
    events.append(StreamEvent(type=StreamEventType.COMPLETED))
    return events


def _fake_settings() -> SimpleNamespace:
    return SimpleNamespace(
        rerank_enabled=False,
        rerank_pool_multiplier=1,
        rag_max_context_characters=12000,
        rag_max_source_characters=6000,
    )


class _RaisingPipeline:
    """Simulates resolve_legal_chat_response failing BEFORE ever
    calling legal_answer_generation_fn - i.e. a pre-generation
    failure or an early-return path that itself raises."""

    def __init__(self, error: Exception) -> None:
        self.error = error

    def __call__(self, request, **kwargs) -> LegalChatResponse:
        raise self.error


class _DeterministicPipeline:
    """Simulates a path that resolves WITHOUT ever needing generation
    (conversation-meta/help/clarification/contact/fallback) -
    legal_answer_generation_fn is never called."""

    def __init__(self, response: LegalChatResponse) -> None:
        self.response = response

    def __call__(self, request, **kwargs) -> LegalChatResponse:
        return self.response


class _GeneratingPipeline:
    """
    Simulates _execute_resolved_plan's own real call shape: calls the
    REAL legal_answer_generation_fn it receives (the bridge under
    test), exactly as _execute_resolved_plan's line ~2119 does, then
    optionally appends extra text (simulating an unavailable-countries
    note appended AFTER the streamed answer) or raises AFTER the
    bridge call (simulating an in-stream failure), matching how a real
    RagAnswerError from deep inside the bridge would propagate.
    """

    def __init__(
        self,
        *,
        generation_request: LegalChatRequest,
        search_function,
        generation_client,
        append_text: str = "",
        raise_after: Exception | None = None,
    ) -> None:
        self.generation_request = generation_request
        self.search_function = search_function
        self.generation_client = generation_client
        self.append_text = append_text
        self.raise_after = raise_after

    def __call__(
        self, request, *, legal_answer_generation_fn, **kwargs,
    ) -> LegalChatResponse:
        metrics = SimpleNamespace(
            outcome="unknown",
            openai_ms=0.0,
            answer_generation_openai_ms=0.0,
            retrieval_total=0,
            selected_sources=0,
            model=None,
            generation_attempts=0,
            repair_triggered=False,
            repair_success=False,
            repair_answer_returned=False,
            initial_hard_error_types=[],
            initial_soft_error_types=[],
            final_hard_error_types=[],
            final_soft_error_types=[],
            insufficient_country_duplication_detected=False,
            add_opensearch_seconds=lambda seconds: None,
            add_rerank_seconds=lambda seconds: None,
        )

        legal_response = legal_answer_generation_fn(
            self.generation_request,
            search_function=self.search_function,
            generation_client=self.generation_client,
            rerank_enabled=kwargs["rerank_enabled"],
            rerank_pool_multiplier=kwargs["rerank_pool_multiplier"],
            max_context_characters=kwargs["max_context_characters"],
            max_source_characters=kwargs["max_source_characters"],
            metrics=metrics,
            current_user_question=None,
            action_specs=None,
            known_excluded_country_codes=None,
        )

        if self.raise_after is not None:
            raise self.raise_after

        final_answer = legal_response.answer + self.append_text

        return LegalChatResponse(
            question=request.question.strip(),
            answer=final_answer,
            grounded=legal_response.grounded,
            model=legal_response.model,
            retrieval_total=legal_response.retrieval_total,
            sources=legal_response.sources,
            contacts=[],
            conversation_state=None,
        )


def _run(coro):
    return asyncio.run(coro)


async def _consume_ndjson(streaming_response) -> list[dict]:
    records = []
    async for chunk in streaming_response.body_iterator:
        for line in chunk.decode("utf-8").splitlines():
            if line:
                records.append(json.loads(line))
    return records


async def _call_and_consume(coro) -> tuple[object, list[dict]]:
    """Runs legal_chat_stream(...) AND drains its NDJSON body within
    the SAME asyncio.run() / event loop - required because
    legal_chat_stream creates an asyncio.Task (pipeline_task) tied to
    the currently-running loop; consuming the response in a SEPARATE
    asyncio.run() call would orphan that task in an already-closed
    loop (observed directly: asyncio.CancelledError on `await
    pipeline_task` once the first loop closed)."""

    response = await coro
    records = await _consume_ndjson(response)
    return response, records


def _reconstruct_response_from_ndjson(
    records: list[dict],
) -> LegalChatResponse:
    """
    Reconstructs the logical LegalChatResponse a real browser client
    would build by applying protocol-v1 NDJSON records in order (GATE
    S4B item 6) - the same rules a frontend implementation must
    follow:

        DELTA       -> append to the provisional answer
        DISCARD     -> clear the provisional answer
        REPLACEMENT -> replace the provisional answer outright
        METADATA    -> every other LegalChatResponse field
        DONE        -> terminal success
        ERROR       -> terminal failure (raises - there is no
                       logical response to reconstruct)

    Used to prove /chat/stream's reconstructed response is equivalent
    to the real /chat route's own LegalChatResponse for identical
    inputs - never a second, independent definition of correctness.
    """

    provisional_answer = ""
    metadata_payload: dict | None = None
    terminal: str | None = None

    for record in records:
        record_type = record["type"]

        if record_type == "delta":
            provisional_answer += record["text"]
        elif record_type == "discard":
            provisional_answer = ""
        elif record_type == "replacement":
            provisional_answer = record["text"]
        elif record_type == "metadata":
            metadata_payload = {
                key: value for key, value in record.items() if key != "type"
            }
        elif record_type == "done":
            terminal = "done"
        elif record_type == "error":
            terminal = "error"

    if terminal == "error":
        raise AssertionError(
            "Cannot reconstruct a LegalChatResponse from a stream "
            "that terminated in an error record."
        )

    if terminal != "done":
        raise AssertionError(
            "NDJSON stream never reached a terminal done record."
        )

    if metadata_payload is None:
        raise AssertionError(
            "NDJSON stream reached done without ever sending metadata."
        )

    return LegalChatResponse(answer=provisional_answer, **metadata_payload)


def _real_orchestration_pipeline(
    *,
    catalog_provider,
    document_topic_provider,
    search_function,
    generation_client=None,
    understanding_client=None,
):
    """
    GATE S4B items 4/5/6: a pipeline stand-in with the SAME call
    signature legal_chat_stream() uses when invoking
    resolve_legal_chat_response (positional request, keyword
    request_id/rerank_enabled/rerank_pool_multiplier/
    max_context_characters/max_source_characters/
    legal_answer_generation_fn) - but forwards into the REAL,
    unmodified resolve_legal_chat_response (real_resolve_legal_chat_
    response, imported directly from app.routers.chat rather than
    through the app.routers.chat_stream name this module patches),
    with the given fakes injected only for ITS OWN catalog_provider/
    document_topic_provider/search_function/generation_client/
    understanding_client parameters.

    This exercises the REAL request-understanding/dispatch logic
    (clarification, contact, assistant-help, insufficient-evidence,
    comparison, fallback) end to end - unlike _DeterministicPipeline/
    _GeneratingPipeline (which bypass that dispatch entirely), this is
    "only a generic deterministic helper" no longer.
    """

    def _run(request, **kwargs):
        return real_resolve_legal_chat_response(
            request,
            catalog_provider=catalog_provider,
            document_topic_provider=document_topic_provider,
            search_function=search_function,
            generation_client=generation_client,
            understanding_client=understanding_client,
            **kwargs,
        )

    return _run


def _run_real_chat_route(
    request: LegalChatRequest,
    *,
    catalog_provider,
    document_topic_provider,
    search_function,
    generation_client=None,
    understanding_client=None,
    settings=None,
) -> LegalChatResponse:
    """
    Calls the REAL, unpatched POST /api/v1/chat route function
    directly (mirroring test_chat.py's FriendlyInvalidRequestHttpTests
    convention), with resolve_legal_chat_response itself replaced by
    _real_orchestration_pipeline's forwarding wrapper.

    resolve_legal_chat_response's own catalog_provider/
    document_topic_provider/search_function parameters default to
    get_legal_catalog/get_document_legal_topics_by_country/
    search_legal_documents - but as ORDINARY PYTHON DEFAULT ARGUMENTS,
    bound once at function-DEFINITION time. Patching those names in
    app.routers.chat's module namespace afterward has no effect on an
    already-bound default; only intercepting the
    resolve_legal_chat_response CALL itself (a live global-name lookup
    inside legal_chat()'s own body, resolved at call time) can inject
    fakes here. Used only to build the "stable /chat" side of the
    STABLE_VS_STREAM_EQUIVALENCE_MATRIX (GATE S4B item 6) - with the
    SAME _real_orchestration_pipeline construction /chat/stream's own
    equivalent test uses, so both sides get byte-identical fake
    wiring.
    """

    pipeline = _real_orchestration_pipeline(
        catalog_provider=catalog_provider,
        document_topic_provider=document_topic_provider,
        search_function=search_function,
        generation_client=generation_client,
        understanding_client=understanding_client,
    )

    with mock.patch(
        "app.routers.chat.resolve_legal_chat_response", side_effect=pipeline,
    ), mock.patch(
        "app.routers.chat.get_settings",
        return_value=settings if settings is not None else _fake_settings(),
    ):
        return real_legal_chat(
            request=request,
            response=Response(),
            x_request_id="equivalence-baseline",
        )


def _make_comparison_fixture(
    codes: list[str],
    names: dict[str, str],
    *,
    max_sources: int,
    legal_topics: list[str] | None = None,
):
    """
    GATE S4B items 5/6: a real N-country comparison, dispatched
    through the ACTUAL RequestUnderstanding "comparison" action type
    and real multi-country retrieval/generation - not a mocked stand-
    in for resolve_legal_chat_response itself.

    Each country's fake search hit is given an EXPLICIT, distinct
    chunk_id (test_rag_answer.py's _build_hit defaults chunk_id to the
    fixed literal "chunk-1" for every call - reusing that default
    across >1 country silently dedupes every country but one out of
    selected_hits during merge, which then fails grounding validation
    with a confusing "citation out of range"-shaped error; the S3
    streaming tests already avoid this by passing distinct chunk_ids
    explicitly - see test_stream_answer_legal_question.py's own
    test_two_country_comparison).

    Returns (request, pipeline, stream_client, expected_answer).
    """

    def fake_search(request: LegalSearchResponse) -> LegalSearchResponse:
        code = request.country_codes[0]
        return LegalSearchResponse(
            query=request.query,
            total=1,
            limit=request.limit,
            offset=0,
            took_ms=1,
            hits=[
                _build_hit(
                    country_code=code,
                    country=names[code],
                    chunk_id=f"chunk-{code.lower()}-1",
                )
            ],
        )

    answer = "\n\n".join(
        f"{names[c]}\n- Supported by [{i}]."
        for i, c in enumerate(codes, start=1)
    )

    understanding_client = FakeUnderstandingClient(
        payload=_understanding_result(
            actions=[
                _understanding_action(
                    "comparison",
                    country_codes=codes,
                    legal_topics=legal_topics or ["Working Conditions"],
                )
            ],
        )
    )

    pipeline = _real_orchestration_pipeline(
        catalog_provider=_catalog_provider,
        document_topic_provider=_document_topic_provider,
        search_function=fake_search,
        generation_client=ChatFakeGenerationClient(answer=answer),
        understanding_client=understanding_client,
    )

    stream_client = FakeStreamGenerationClient(_delta_events(answer))

    request = LegalChatRequest(
        question="Compare notice periods across these countries.",
        country_codes=codes,
        max_sources=max_sources,
    )

    return request, pipeline, stream_client, answer


class SerializationSafetyTests(unittest.TestCase):

    def _decode_text_field(self, payload: dict, field: str) -> str:
        raw = _serialize_ndjson_record(payload)
        self.assertTrue(raw.endswith(b"\n"))
        self.assertEqual(1, raw.count(b"\n"), "exactly one newline (the terminator)")
        decoded = json.loads(raw.decode("utf-8"))
        return decoded[field]

    def test_quotes_are_escaped_and_survive_round_trip(self) -> None:
        text = 'He said "notice periods vary".'
        self.assertEqual(
            text, self._decode_text_field({"type": "delta", "text": text}, "text"),
        )

    def test_backslashes_survive_round_trip(self) -> None:
        text = "C:\\path\\to\\file and a\\b"
        self.assertEqual(
            text, self._decode_text_field({"type": "delta", "text": text}, "text"),
        )

    def test_embedded_newline_does_not_split_the_record(self) -> None:
        text = "Line one.\nLine two.\nLine three."
        raw = _serialize_ndjson_record({"type": "delta", "text": text})
        # Exactly one newline in the whole record: the terminator. The
        # newlines INSIDE `text` must be JSON-escaped (\n), not raw.
        self.assertEqual(1, raw.count(b"\n"))
        self.assertIn(b"\\n", raw)
        decoded = json.loads(raw.decode("utf-8"))
        self.assertEqual(text, decoded["text"])

    def test_accents_and_apostrophes_survive_round_trip(self) -> None:
        text = "L'employeur doit respecter un préavis de deux mois."
        self.assertEqual(
            text, self._decode_text_field({"type": "delta", "text": text}, "text"),
        )

    def test_non_ascii_unicode_survives_round_trip(self) -> None:
        text = "日本の労働法 — café, Müller, naïve"
        self.assertEqual(
            text, self._decode_text_field({"type": "delta", "text": text}, "text"),
        )

    def test_nan_and_infinity_are_rejected_not_silently_emitted(self) -> None:
        with self.assertRaises(ValueError):
            _serialize_ndjson_record({"type": "delta", "score": float("nan")})

        with self.assertRaises(ValueError):
            _serialize_ndjson_record({"type": "delta", "score": float("inf")})


class MetadataSchemaTests(unittest.TestCase):

    def test_metadata_excludes_answer_and_includes_everything_else(
        self,
    ) -> None:
        hit_source = _build_hit()
        response = LegalChatResponse(
            question="What notice period applies?",
            answer="This must not appear in metadata.",
            grounded=True,
            model="gpt-5-mini",
            retrieval_total=3,
            sources=[],
            contacts=[],
            conversation_state=None,
        )

        record = _metadata_record(response)

        self.assertEqual("metadata", record["type"])
        self.assertNotIn("answer", record)
        self.assertEqual("What notice period applies?", record["question"])
        self.assertTrue(record["grounded"])
        self.assertEqual("gpt-5-mini", record["model"])
        self.assertEqual(3, record["retrieval_total"])
        self.assertIn("sources", record)
        self.assertIn("contacts", record)


class EventMappingTests(unittest.TestCase):

    def test_delta_maps_correctly(self) -> None:
        from app.services.rag_answer import StreamAnswerEvent, StreamAnswerEventType

        record = _map_stream_answer_event(
            StreamAnswerEvent(
                type=StreamAnswerEventType.ANSWER_DELTA, delta_text="hi",
            )
        )
        self.assertEqual({"type": "delta", "text": "hi"}, record)

    def test_validating_discard_map_to_bare_type_records(self) -> None:
        from app.services.rag_answer import StreamAnswerEvent, StreamAnswerEventType

        self.assertEqual(
            {"type": "validating"},
            _map_stream_answer_event(
                StreamAnswerEvent(type=StreamAnswerEventType.VALIDATING)
            ),
        )
        self.assertEqual(
            {"type": "discard"},
            _map_stream_answer_event(
                StreamAnswerEvent(type=StreamAnswerEventType.DISCARD)
            ),
        )

    def test_replacement_maps_correctly(self) -> None:
        from app.services.rag_answer import StreamAnswerEvent, StreamAnswerEventType

        record = _map_stream_answer_event(
            StreamAnswerEvent(
                type=StreamAnswerEventType.REPLACEMENT,
                replacement_text="corrected",
            )
        )
        self.assertEqual({"type": "replacement", "text": "corrected"}, record)

    def test_finalized_and_error_are_not_directly_mapped(self) -> None:
        """These require caller context (final response / stream
        termination) - a caller forgetting to special-case them must
        get None, never a wrong/empty record."""

        from app.services.rag_answer import StreamAnswerEvent, StreamAnswerEventType

        self.assertIsNone(
            _map_stream_answer_event(
                StreamAnswerEvent(type=StreamAnswerEventType.FINALIZED)
            )
        )
        self.assertIsNone(
            _map_stream_answer_event(
                StreamAnswerEvent(type=StreamAnswerEventType.ERROR)
            )
        )


def _patch_chat_stream(*, pipeline, stream_client=None):
    """Shared patch triple for exercising legal_chat_stream() through
    a fake resolve_legal_chat_response - reused by every test class
    that drives the route function directly."""

    return (
        mock.patch(
            "app.routers.chat_stream.get_settings",
            return_value=_fake_settings(),
        ),
        mock.patch(
            "app.routers.chat_stream.resolve_legal_chat_response",
            side_effect=pipeline,
        ),
        mock.patch(
            "app.routers.chat_stream.get_openai_answer_stream_client",
            return_value=stream_client,
        ),
    )


class RouteMechanismTests(unittest.TestCase):

    def _patch(self, *, pipeline, stream_client=None):
        return _patch_chat_stream(pipeline=pipeline, stream_client=stream_client)

    # -- PRE_STREAM_ERROR boundary -----------------------------------

    def test_openai_configuration_error_before_any_pipeline_call_is_503(
        self,
    ) -> None:
        with (
            mock.patch(
                "app.routers.chat_stream.get_settings",
                return_value=_fake_settings(),
            ),
            mock.patch(
                "app.routers.chat_stream.get_openai_answer_stream_client",
                side_effect=OpenAIConfigurationError("no key"),
            ),
        ):
            with self.assertRaises(HTTPException) as ctx:
                _run(
                    legal_chat_stream(
                        request=LegalChatRequest(question="What applies?", country_codes=["GB"]),
                        x_request_id="pre-1",
                    )
                )
        self.assertEqual(503, ctx.exception.status_code)

    def test_invalid_request_error_before_generation_is_422(self) -> None:
        p1, p2, p3 = self._patch(
            pipeline=_RaisingPipeline(InvalidLegalChatRequestError("bad request")),
        )
        with p1, p2, p3:
            with self.assertRaises(HTTPException) as ctx:
                _run(
                    legal_chat_stream(
                        request=LegalChatRequest(question="A valid-length question."),
                        x_request_id="pre-2",
                    )
                )
        self.assertEqual(422, ctx.exception.status_code)

    def test_country_detection_error_before_generation_is_502(self) -> None:
        p1, p2, p3 = self._patch(
            pipeline=_RaisingPipeline(CountryDetectionError("boom")),
        )
        with p1, p2, p3:
            with self.assertRaises(HTTPException) as ctx:
                _run(
                    legal_chat_stream(
                        request=LegalChatRequest(question="A valid-length question."),
                        x_request_id="pre-3",
                    )
                )
        self.assertEqual(502, ctx.exception.status_code)

    def test_rag_answer_error_before_generation_is_502(self) -> None:
        p1, p2, p3 = self._patch(
            pipeline=_RaisingPipeline(RagAnswerError("boom")),
        )
        with p1, p2, p3:
            with self.assertRaises(HTTPException) as ctx:
                _run(
                    legal_chat_stream(
                        request=LegalChatRequest(question="A valid-length question."),
                        x_request_id="pre-4",
                    )
                )
        self.assertEqual(502, ctx.exception.status_code)

    def test_conversation_transition_error_before_generation_is_502(
        self,
    ) -> None:
        p1, p2, p3 = self._patch(
            pipeline=_RaisingPipeline(ConversationTransitionError("boom")),
        )
        with p1, p2, p3:
            with self.assertRaises(HTTPException) as ctx:
                _run(
                    legal_chat_stream(
                        request=LegalChatRequest(question="A valid-length question."),
                        x_request_id="pre-5",
                    )
                )
        self.assertEqual(502, ctx.exception.status_code)

    def test_comparison_source_budget_is_a_friendly_ndjson_stream_not_422(
        self,
    ) -> None:
        error = InvalidLegalChatRequestError(
            "too many countries",
            code="comparison_source_budget",
            details={"country_count": 9},
        )
        p1, p2, p3 = self._patch(pipeline=_RaisingPipeline(error))
        with p1, p2, p3:
            http_response, records = _run(
                _call_and_consume(
                    legal_chat_stream(
                        request=LegalChatRequest(
                            question="Compare all countries.", max_sources=6,
                        ),
                        x_request_id="pre-6",
                    )
                )
            )
        self.assertEqual("start", records[0]["type"])
        self.assertEqual("delta", records[1]["type"])
        self.assertIn("9 countries", records[1]["text"])
        self.assertEqual("metadata", records[2]["type"])
        self.assertEqual("done", records[3]["type"])

    # -- Early finalized responses (section 12) ------------------------

    def test_deterministic_response_streams_as_single_delta_then_metadata_done(
        self,
    ) -> None:
        response = LegalChatResponse(
            question="hello",
            answer="I can help with employment-law questions for supported countries.",
            grounded=False, model=None, retrieval_total=0, sources=[],
        )
        p1, p2, p3 = self._patch(pipeline=_DeterministicPipeline(response))
        with p1, p2, p3:
            http_response, records = _run(
                _call_and_consume(
                    legal_chat_stream(
                        request=LegalChatRequest(question="hello"),
                        x_request_id="early-1",
                    )
                )
            )
        types = [r["type"] for r in records]
        self.assertEqual(["start", "delta", "metadata", "done"], types)
        self.assertEqual(response.answer, records[1]["text"])
        self.assertEqual("early-1", records[0]["request_id"])
        self.assertEqual("early-1", records[3]["request_id"])

    def test_content_type_and_headers(self) -> None:
        response = LegalChatResponse(
            question="hello", answer="Hi there.", grounded=False,
            model=None, retrieval_total=0, sources=[],
        )
        p1, p2, p3 = self._patch(pipeline=_DeterministicPipeline(response))
        with p1, p2, p3:
            http_response, records = _run(
                _call_and_consume(
                    legal_chat_stream(
                        request=LegalChatRequest(question="hello"),
                        x_request_id="headers-1",
                    )
                )
            )
        self.assertEqual(
            "application/x-ndjson; charset=utf-8", http_response.media_type,
        )
        self.assertEqual(
            "no-cache, no-transform", http_response.headers["Cache-Control"],
        )
        self.assertEqual("no", http_response.headers["X-Accel-Buffering"])
        self.assertEqual("headers-1", http_response.headers["X-Request-ID"])
        self.assertNotIn("Content-Length", http_response.headers)

    # -- Real streaming mechanism (bridge -> stream_answer_legal_question) -

    def test_direct_success_real_streaming_sequence(self) -> None:
        answer = (
            "United Kingdom\n- The minimum notice is one week "
            "in the stated circumstances [1]."
        )
        stream_client = FakeStreamGenerationClient(_delta_events(answer))
        generation_request = LegalChatRequest(
            question="What notice period applies?", country_codes=["GB"],
        )
        pipeline = _GeneratingPipeline(
            generation_request=generation_request,
            search_function=_make_search_function(hits=[_build_hit()]),
            generation_client=FakeGenerationClient(answer=answer),
        )
        p1, p2, p3 = self._patch(pipeline=pipeline, stream_client=stream_client)
        with p1, p2, p3:
            http_response, records = _run(
                _call_and_consume(
                    legal_chat_stream(request=generation_request, x_request_id="s-1"),
                )
            )
        types = [r["type"] for r in records]

        self.assertEqual("start", types[0])
        self.assertIn("delta", types)
        self.assertIn("validating", types)
        self.assertNotIn("discard", types)
        self.assertNotIn("replacement", types)
        self.assertEqual("metadata", types[-2])
        self.assertEqual("done", types[-1])

        deltas = "".join(r["text"] for r, t in zip(records, types) if t == "delta")
        self.assertEqual(answer, deltas)
        self.assertEqual(
            "What notice period applies?", records[-2]["question"],
        )

    def test_repair_success_real_streaming_sequence(self) -> None:
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
        stream_client = FakeStreamGenerationClient(_delta_events(initial_answer))
        generation_request = LegalChatRequest(
            question="What is the paid leave entitlement in the UK?",
            country_codes=["GB"],
        )
        pipeline = _GeneratingPipeline(
            generation_request=generation_request,
            search_function=_make_search_function(hits=[_build_hit()]),
            generation_client=_RepairOnlyClient(answer=repaired_answer),
        )
        p1, p2, p3 = self._patch(pipeline=pipeline, stream_client=stream_client)
        with p1, p2, p3:
            http_response, records = _run(
                _call_and_consume(
                    legal_chat_stream(request=generation_request, x_request_id="s-2"),
                )
            )
        types = [r["type"] for r in records]

        self.assertIn("delta", types)
        self.assertIn("validating", types)
        self.assertIn("discard", types)
        self.assertIn("replacement", types)
        self.assertEqual("metadata", types[-2])
        self.assertEqual("done", types[-1])

        replacement = next(r for r, t in zip(records, types) if t == "replacement")
        self.assertEqual(repaired_answer, replacement["text"])

    def test_provider_error_before_delta_in_stream(self) -> None:
        stream_client = FakeStreamGenerationClient(
            [StreamEvent(type=StreamEventType.ERROR, error_message="down", retryable=True)]
        )
        generation_request = LegalChatRequest(
            question="What notice period applies?", country_codes=["GB"],
        )
        pipeline = _GeneratingPipeline(
            generation_request=generation_request,
            search_function=_make_search_function(hits=[_build_hit()]),
            generation_client=FakeGenerationClient(),
            raise_after=RagAnswerError("Streaming generation failed."),
        )
        p1, p2, p3 = self._patch(pipeline=pipeline, stream_client=stream_client)
        with p1, p2, p3:
            http_response, records = _run(
                _call_and_consume(
                    legal_chat_stream(request=generation_request, x_request_id="s-3"),
                )
            )
        types = [r["type"] for r in records]
        self.assertEqual(["start", "error"], types)
        self.assertTrue(records[-1]["retryable"])

    def test_provider_error_after_delta_discards_then_errors(self) -> None:
        events = _delta_events("Partial answer text")[:-1]
        events.append(
            StreamEvent(type=StreamEventType.ERROR, error_message="boom")
        )
        stream_client = FakeStreamGenerationClient(events)
        generation_request = LegalChatRequest(
            question="What notice period applies?", country_codes=["GB"],
        )
        pipeline = _GeneratingPipeline(
            generation_request=generation_request,
            search_function=_make_search_function(hits=[_build_hit()]),
            generation_client=FakeGenerationClient(),
            raise_after=RagAnswerError("boom"),
        )
        p1, p2, p3 = self._patch(pipeline=pipeline, stream_client=stream_client)
        with p1, p2, p3:
            http_response, records = _run(
                _call_and_consume(
                    legal_chat_stream(request=generation_request, x_request_id="s-4"),
                )
            )
        types = [r["type"] for r in records]
        self.assertIn("delta", types)
        self.assertIn("discard", types)
        self.assertEqual(types[-1], "error")
        self.assertNotIn("metadata", types)
        self.assertNotIn("done", types)

    # -- Reconciliation: post-generation text appended after streaming --

    def test_appended_note_after_grounded_answer_triggers_reconciling_replacement(
        self,
    ) -> None:
        answer = "United Kingdom\n- Notice is one week [1]."
        stream_client = FakeStreamGenerationClient(_delta_events(answer))
        generation_request = LegalChatRequest(
            question="What notice period applies?", country_codes=["GB"],
        )
        pipeline = _GeneratingPipeline(
            generation_request=generation_request,
            search_function=_make_search_function(hits=[_build_hit()]),
            generation_client=FakeGenerationClient(answer=answer),
            append_text="\n\nNote: some requested countries are not covered.",
        )
        p1, p2, p3 = self._patch(pipeline=pipeline, stream_client=stream_client)
        with p1, p2, p3:
            http_response, records = _run(
                _call_and_consume(
                    legal_chat_stream(request=generation_request, x_request_id="s-5"),
                )
            )
        types = [r["type"] for r in records]

        # A reconciling replacement must appear (streamed text != final answer).
        self.assertIn("replacement", types)
        final_metadata_index = types.index("metadata")
        replacement_records = [
            r for r, t in zip(records, types) if t == "replacement"
        ]
        self.assertIn(
            "Note: some requested countries are not covered.",
            replacement_records[-1]["text"],
        )

    def test_clean_answer_with_no_appended_text_has_no_extra_replacement(
        self,
    ) -> None:
        answer = "United Kingdom\n- Notice is one week [1]."
        stream_client = FakeStreamGenerationClient(_delta_events(answer))
        generation_request = LegalChatRequest(
            question="What notice period applies?", country_codes=["GB"],
        )
        pipeline = _GeneratingPipeline(
            generation_request=generation_request,
            search_function=_make_search_function(hits=[_build_hit()]),
            generation_client=FakeGenerationClient(answer=answer),
        )
        p1, p2, p3 = self._patch(pipeline=pipeline, stream_client=stream_client)
        with p1, p2, p3:
            http_response, records = _run(
                _call_and_consume(
                    legal_chat_stream(request=generation_request, x_request_id="s-6"),
                )
            )
        types = [r["type"] for r in records]
        self.assertNotIn("replacement", types)

    # -- post_generation_failure (GATE S4B item 7) ----------------------

    def test_post_generation_failure_after_finalized_discards_then_errors(
        self,
    ) -> None:
        """
        Generation completes cleanly (FINALIZED fires, no provider
        ERROR) but _execute_resolved_plan's OWN post-processing then
        raises before ever returning a LegalChatResponse - the
        post_generation_failure branch (chat_stream.py). Whatever text
        the client already received is no longer trustworthy (the
        request as a whole failed, so it was never truly finalized),
        so a DISCARD must precede the terminal error, and neither
        metadata nor done may ever be emitted.
        """

        answer = (
            "United Kingdom\n- The minimum notice is one week "
            "in the stated circumstances [1]."
        )
        stream_client = FakeStreamGenerationClient(_delta_events(answer))
        generation_request = LegalChatRequest(
            question="What notice period applies?", country_codes=["GB"],
        )
        pipeline = _GeneratingPipeline(
            generation_request=generation_request,
            search_function=_make_search_function(hits=[_build_hit()]),
            generation_client=FakeGenerationClient(answer=answer),
            raise_after=RuntimeError("post-processing failed"),
        )
        p1, p2, p3 = self._patch(pipeline=pipeline, stream_client=stream_client)
        with p1, p2, p3:
            http_response, records = _run(
                _call_and_consume(
                    legal_chat_stream(request=generation_request, x_request_id="s-7"),
                )
            )
        types = [r["type"] for r in records]

        self.assertEqual("start", types[0])
        self.assertIn("delta", types)
        self.assertIn("discard", types)
        self.assertEqual(types[-1], "error")
        self.assertEqual(1, types.count("error"))
        self.assertEqual(
            "post_generation_failure", records[-1]["code"],
        )
        self.assertNotIn("metadata", types)
        self.assertNotIn("done", types)

    # -- Unknown pre-stream exception (GATE S4B item 8) ------------------

    def test_unknown_pre_stream_exception_follows_unhandled_fastapi_path(
        self,
    ) -> None:
        """
        _map_pre_stream_exception_to_http only classifies 5 known
        exception types; anything else must propagate unchanged - the
        same "no HTTPException, no NDJSON error, just let FastAPI's
        own default unhandled-exception path take over" behavior /chat
        itself already has for an exception outside its own 5-type
        except clause. Never silently downgraded to HTTP 200 + NDJSON
        error merely because generation never started.
        """

        class _UnmappedError(Exception):
            pass

        p1, p2, p3 = self._patch(
            pipeline=_RaisingPipeline(_UnmappedError("unexpected")),
        )
        with p1, p2, p3:
            with self.assertRaises(_UnmappedError):
                _run(
                    legal_chat_stream(
                        request=LegalChatRequest(question="A valid-length question."),
                        x_request_id="pre-7",
                    )
                )


class StreamMetricsTests(unittest.TestCase):
    """
    Tests for the "chat_stream_performance" structured log event
    (GATE S4B items 2/3/9/17) - additive to, and never a replacement
    for, the existing "legal_chat_performance" line.
    """

    LOGGER_NAME = "app.routers.chat_stream"

    def _stream_metric_payloads(self, log_context) -> list[dict]:
        """
        Filters this logger's captured records down to only the
        JSON "chat_stream_performance" lines - the SAME logger also
        carries logger.exception(...) traceback text for
        post_generation_failure, which is not JSON and must not be
        mistaken for a second/duplicate metric record.
        """

        payloads = []
        for record in log_context.records:
            try:
                payload = json.loads(record.getMessage())
            except (json.JSONDecodeError, TypeError):
                continue
            if payload.get("event") == "chat_stream_performance":
                payloads.append(payload)
        return payloads

    def _single_stream_metric(self, log_context) -> dict:
        """Asserts exactly ONE terminal metric record was emitted -
        never a duplicate - and returns its payload."""

        payloads = self._stream_metric_payloads(log_context)
        self.assertEqual(1, len(payloads))
        return payloads[0]

    def test_direct_completion_emits_exactly_one_stream_completed_metric(
        self,
    ) -> None:
        answer = (
            "United Kingdom\n- The minimum notice is one week "
            "in the stated circumstances [1]."
        )
        stream_client = FakeStreamGenerationClient(_delta_events(answer))
        generation_request = LegalChatRequest(
            question="What notice period applies?", country_codes=["GB"],
        )
        pipeline = _GeneratingPipeline(
            generation_request=generation_request,
            search_function=_make_search_function(hits=[_build_hit()]),
            generation_client=FakeGenerationClient(answer=answer),
        )
        p1, p2, p3 = _patch_chat_stream(pipeline=pipeline, stream_client=stream_client)
        with p1, p2, p3:
            with self.assertLogs(self.LOGGER_NAME, level="INFO") as log_context:
                http_response, records = _run(
                    _call_and_consume(
                        legal_chat_stream(
                            request=generation_request, x_request_id="metrics-1",
                        ),
                    )
                )

        payload = self._single_stream_metric(log_context)

        self.assertEqual("stream_completed", payload["outcome"])
        self.assertEqual("metrics-1", payload["request_id"])
        self.assertIsNone(payload["error_code"])
        self.assertFalse(payload["repair_triggered"])
        self.assertIsNone(payload["repair_success"])
        self.assertIsNone(payload["t1_understanding_complete"])
        self.assertIsNone(payload["t3_rerank_complete"])
        self.assertIsNotNone(payload["t2_retrieval_complete"])
        self.assertIsNotNone(payload["t4_generation_start"])
        self.assertIsNotNone(payload["t5_first_provider_delta"])
        self.assertIsNotNone(payload["t6_first_fastapi_delta"])
        self.assertIsNotNone(payload["t8_done"])
        self.assertIsNotNone(payload["total_ms"])
        self.assertNotIn("question", payload)
        self.assertNotIn("answer", payload)

    def test_repair_emits_repair_triggered_with_final_completion(
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
        stream_client = FakeStreamGenerationClient(_delta_events(initial_answer))
        generation_request = LegalChatRequest(
            question="What is the paid leave entitlement in the UK?",
            country_codes=["GB"],
        )
        pipeline = _GeneratingPipeline(
            generation_request=generation_request,
            search_function=_make_search_function(hits=[_build_hit()]),
            generation_client=_RepairOnlyClient(answer=repaired_answer),
        )
        p1, p2, p3 = _patch_chat_stream(pipeline=pipeline, stream_client=stream_client)
        with p1, p2, p3:
            with self.assertLogs(self.LOGGER_NAME, level="INFO") as log_context:
                http_response, records = _run(
                    _call_and_consume(
                        legal_chat_stream(
                            request=generation_request, x_request_id="metrics-2",
                        ),
                    )
                )

        payload = self._single_stream_metric(log_context)

        self.assertEqual("stream_completed", payload["outcome"])
        self.assertTrue(payload["repair_triggered"])
        self.assertIsNotNone(payload["repair_start"])
        self.assertIsNotNone(payload["repair_end"])
        self.assertIsNotNone(payload["validation_start"])
        self.assertIsNotNone(payload["validation_end"])

    def test_provider_in_stream_error_emits_stream_error_metric(
        self,
    ) -> None:
        events = _delta_events("Partial answer text")[:-1]
        events.append(
            StreamEvent(type=StreamEventType.ERROR, error_message="boom")
        )
        stream_client = FakeStreamGenerationClient(events)
        generation_request = LegalChatRequest(
            question="What notice period applies?", country_codes=["GB"],
        )
        pipeline = _GeneratingPipeline(
            generation_request=generation_request,
            search_function=_make_search_function(hits=[_build_hit()]),
            generation_client=FakeGenerationClient(),
        )
        p1, p2, p3 = _patch_chat_stream(pipeline=pipeline, stream_client=stream_client)
        with p1, p2, p3:
            with self.assertLogs(self.LOGGER_NAME, level="INFO") as log_context:
                http_response, records = _run(
                    _call_and_consume(
                        legal_chat_stream(
                            request=generation_request, x_request_id="metrics-3",
                        ),
                    )
                )

        payload = self._single_stream_metric(log_context)

        self.assertEqual("stream_error", payload["outcome"])
        self.assertEqual("stream_generation_failed", payload["error_code"])
        self.assertIsNone(payload["t8_done"])

    def test_known_pre_stream_exception_emits_pre_stream_failure_metric(
        self,
    ) -> None:
        p1, p2, p3 = _patch_chat_stream(
            pipeline=_RaisingPipeline(InvalidLegalChatRequestError("bad request")),
        )
        with p1, p2, p3:
            with self.assertLogs(self.LOGGER_NAME, level="INFO") as log_context:
                with self.assertRaises(HTTPException):
                    _run(
                        legal_chat_stream(
                            request=LegalChatRequest(
                                question="A valid-length question.",
                            ),
                            x_request_id="metrics-4",
                        )
                    )

        payload = self._single_stream_metric(log_context)

        self.assertEqual("pre_stream_failure", payload["outcome"])
        self.assertEqual(
            "InvalidLegalChatRequestError", payload["error_code"],
        )
        self.assertIsNone(payload["t2_retrieval_complete"])
        self.assertIsNone(payload["t4_generation_start"])
        self.assertIsNone(payload["t6_first_fastapi_delta"])
        self.assertIsNone(payload["t8_done"])

    def test_post_generation_failure_emits_post_generation_failure_metric(
        self,
    ) -> None:
        answer = (
            "United Kingdom\n- The minimum notice is one week "
            "in the stated circumstances [1]."
        )
        stream_client = FakeStreamGenerationClient(_delta_events(answer))
        generation_request = LegalChatRequest(
            question="What notice period applies?", country_codes=["GB"],
        )
        pipeline = _GeneratingPipeline(
            generation_request=generation_request,
            search_function=_make_search_function(hits=[_build_hit()]),
            generation_client=FakeGenerationClient(answer=answer),
            raise_after=RuntimeError("post-processing failed"),
        )
        p1, p2, p3 = _patch_chat_stream(pipeline=pipeline, stream_client=stream_client)
        with p1, p2, p3:
            with self.assertLogs(self.LOGGER_NAME, level="INFO") as log_context:
                http_response, records = _run(
                    _call_and_consume(
                        legal_chat_stream(
                            request=generation_request, x_request_id="metrics-5",
                        ),
                    )
                )

        payload = self._single_stream_metric(log_context)

        self.assertEqual("post_generation_failure", payload["outcome"])
        self.assertEqual("RuntimeError", payload["error_code"])
        self.assertIsNone(payload["t8_done"])


class NamedEarlyResponseRouteTests(unittest.TestCase):
    """
    GATE S4B item 4: E/F/G/H named early-response scenarios, each
    driven through the REAL resolve_legal_chat_response dispatch
    (_real_orchestration_pipeline) rather than a generic deterministic
    stand-in - proving /chat/stream's early-finalized NDJSON shape
    (start -> delta(full answer) -> metadata -> done) for the actual
    production code paths that produce it, not just a hand-built
    LegalChatResponse.
    """

    # -- E: clarification -----------------------------------------------

    def test_clarification_unsupported_request_streams_as_early_finalized(
        self,
    ) -> None:
        """
        Mirrors test_chat.py's ChatMetricsTests.
        test_tax_question_records_unsupported_request_clarification -
        a real out-of-scope question (not employment law at all) makes
        RequestUnderstanding return status="unsupported", which
        resolve_legal_chat_response returns as a canned clarification
        BEFORE ever calling legal_answer_generation_fn or search.
        """

        understanding_client = FakeUnderstandingClient(
            payload=_understanding_result(
                status="unsupported", clarification_reason="unsupported_request",
            )
        )
        pipeline = _real_orchestration_pipeline(
            catalog_provider=_catalog_provider,
            document_topic_provider=_document_topic_provider,
            search_function=_unexpected_search,
            generation_client=NoCallGenerationClient(),
            understanding_client=understanding_client,
        )
        p1, p2, p3 = _patch_chat_stream(pipeline=pipeline)
        with p1, p2, p3:
            http_response, records = _run(
                _call_and_consume(
                    legal_chat_stream(
                        request=LegalChatRequest(
                            question=(
                                "What are the corporate income tax "
                                "rules in Spain?"
                            ),
                            country_codes=["ES"],
                        ),
                        x_request_id="clarify-1",
                    )
                )
            )

        types = [r["type"] for r in records]
        self.assertEqual(["start", "delta", "metadata", "done"], types)
        self.assertEqual(
            CLARIFICATION_UNSUPPORTED_REQUEST_ANSWER, records[1]["text"],
        )
        metadata = records[2]
        self.assertFalse(metadata["grounded"])
        self.assertIsNone(metadata["model"])
        self.assertEqual(0, metadata["retrieval_total"])
        self.assertEqual([], metadata["sources"])

    # -- F: contact-only --------------------------------------------------

    def test_contact_only_response_streams_with_contacts_intact_in_metadata(
        self,
    ) -> None:
        """
        A pure contact-intent question ("who do I contact...") resolves
        via resolve_legal_chat_response's early-exit contact branch
        (chat.py ~1661-1720), which never calls legal_answer_generation_fn.
        Proves structured contacts (LegalChatContact, built from a fake
        in-memory ContactState - never real ContactState sidecar files)
        survive intact all the way through NDJSON metadata.
        """

        def fake_contact_search(
            country_codes: list[str], client=None,
        ) -> LegalSearchResponse:
            return LegalSearchResponse(
                query="", total=1, limit=20, offset=0, took_ms=1,
                hits=[_build_contact_hit(country_code="PE", country="Peru")],
            )

        fake_settings = SimpleNamespace(
            rerank_enabled=False,
            rerank_pool_multiplier=1,
            rag_max_context_characters=12000,
            rag_max_source_characters=6000,
            document_source_dir=Path("/fake/source/dir"),
        )
        fake_state = ContactState(
            document_id="document-pe",
            country_code="PE",
            contacts=(
                ContactRecord(
                    contact_id="contact-pe-1",
                    member_firm="Test Firm",
                    contact_person="Jane Doe",
                    email="jane@test-firm.example",
                    phone="+51 111 222 333",
                    address="Lima, Peru",
                    website="https://test-firm.example",
                ),
            ),
        )

        def fake_read_contact_state(source_directory, document_id):
            self.assertEqual("document-pe", document_id)
            return fake_state

        pipeline = _real_orchestration_pipeline(
            catalog_provider=_catalog_provider,
            document_topic_provider=_document_topic_provider,
            search_function=_unexpected_search,
            generation_client=NoCallGenerationClient(),
            understanding_client=_FailingUnderstandingClient(),
        )
        p1, p2, p3 = _patch_chat_stream(pipeline=pipeline)
        with p1, p2, p3, mock.patch(
            "app.routers.chat.search_contact_chunks",
            side_effect=fake_contact_search,
        ), mock.patch(
            "app.routers.chat.get_settings", return_value=fake_settings,
        ), mock.patch(
            "app.services.chat_contact_cards.read_contact_state",
            side_effect=fake_read_contact_state,
        ):
            http_response, records = _run(
                _call_and_consume(
                    legal_chat_stream(
                        request=LegalChatRequest(
                            question=(
                                "Give me the contact details for an "
                                "employment lawyer in Peru."
                            ),
                        ),
                        x_request_id="contact-1",
                    )
                )
            )

        types = [r["type"] for r in records]
        self.assertEqual(["start", "delta", "metadata", "done"], types)
        self.assertIn("Test Firm", records[1]["text"])

        metadata = records[2]
        self.assertTrue(metadata["grounded"])
        self.assertEqual(1, len(metadata["contacts"]))
        contact = metadata["contacts"][0]
        self.assertEqual("contact-pe-1", contact["contact_id"])
        self.assertEqual("PE", contact["country_code"])
        self.assertEqual("Test Firm", contact["member_firm"])
        self.assertEqual("Jane Doe", contact["contact_person"])
        self.assertEqual("jane@test-firm.example", contact["email"])
        self.assertEqual("+51 111 222 333", contact["phone"])

    # -- G: assistant-help / conversation-meta ----------------------------

    def test_assistant_help_response_streams_as_early_finalized(self) -> None:
        """
        Mirrors test_chat.py's AssistantHelpRouteTests.test_capabilities -
        a deterministic (regex-based, non-LLM) meta question about the
        assistant itself. NoCallUnderstandingClient/NoCallGenerationClient/
        _unexpected_search each raise if ever invoked - proving
        RequestUnderstanding, generation, and search are all genuinely
        skipped, not merely unmocked.
        """

        pipeline = _real_orchestration_pipeline(
            catalog_provider=_catalog_provider,
            document_topic_provider=_document_topic_provider,
            search_function=_unexpected_search,
            generation_client=NoCallGenerationClient(),
            understanding_client=NoCallUnderstandingClient(),
        )
        p1, p2, p3 = _patch_chat_stream(pipeline=pipeline)
        with p1, p2, p3:
            http_response, records = _run(
                _call_and_consume(
                    legal_chat_stream(
                        request=LegalChatRequest(question="What can you do?"),
                        x_request_id="help-1",
                    )
                )
            )

        types = [r["type"] for r in records]
        self.assertEqual(["start", "delta", "metadata", "done"], types)
        self.assertTrue(records[1]["text"])

        metadata = records[2]
        self.assertFalse(metadata["grounded"])
        self.assertIsNone(metadata["model"])
        self.assertEqual(0, metadata["retrieval_total"])
        self.assertEqual([], metadata["sources"])

    # -- H: insufficient-evidence / fallback ------------------------------

    def test_unavailable_country_fallback_streams_as_early_finalized(
        self,
    ) -> None:
        """
        Mirrors test_chat.py's ThreeAxisCountryAvailabilityContractTests.
        test_registered_and_allowed_but_not_indexed_is_a_controlled_fallback -
        France is a real, registered, admin-allowed country that the
        fake catalog (_NOT_YET_INDEXED_CODES) deliberately excludes from
        its indexed set, so this hits the hard early "recognized but
        unavailable" return (chat.py ~2797-2853) - legal_answer_
        generation_fn is never referenced, and _unexpected_search proves
        OpenSearch is never touched either.
        """

        understanding_client = FakeUnderstandingClient(
            payload=_understanding_result(
                status="clarification", clarification_reason="missing_country",
            )
        )
        pipeline = _real_orchestration_pipeline(
            catalog_provider=_catalog_provider,
            document_topic_provider=_document_topic_provider,
            search_function=_unexpected_search,
            generation_client=None,
            understanding_client=understanding_client,
        )
        p1, p2, p3 = _patch_chat_stream(pipeline=pipeline)
        with p1, p2, p3:
            http_response, records = _run(
                _call_and_consume(
                    legal_chat_stream(
                        request=LegalChatRequest(
                            question="What are the overtime rules in France?",
                        ),
                        x_request_id="fallback-1",
                    )
                )
            )

        types = [r["type"] for r in records]
        self.assertEqual(["start", "delta", "metadata", "done"], types)
        self.assertIn("France", records[1]["text"])

        metadata = records[2]
        self.assertFalse(metadata["grounded"])
        self.assertIsNone(metadata["model"])
        self.assertEqual(0, metadata["retrieval_total"])
        self.assertEqual([], metadata["sources"])


class ComparisonRouteTests(unittest.TestCase):
    """
    GATE S4B item 5: I/J/K real N-country comparison route tests, plus
    the current stable-code maximum (10, via max_sources's Pydantic
    le=10 ceiling) vs the product-documented default (6) - reported,
    never "fixed", as PRE_EXISTING_PRODUCT_CONSTRAINT_DRIFT.
    """

    def _assert_clean_comparison_stream(
        self, records: list[dict], *, codes: list[str], answer: str,
    ) -> None:
        types = [r["type"] for r in records]
        self.assertEqual("start", types[0])
        self.assertIn("delta", types)
        self.assertEqual("metadata", types[-2])
        self.assertEqual("done", types[-1])
        self.assertNotIn("discard", types)

        deltas = "".join(r["text"] for r, t in zip(records, types) if t == "delta")
        self.assertEqual(answer, deltas)

        metadata = records[-2]
        self.assertTrue(metadata["grounded"])
        self.assertEqual(
            sorted(codes),
            sorted(source["country_code"] for source in metadata["sources"]),
        )

    def test_two_country_comparison(self) -> None:
        codes = ["GB", "ES"]
        names = {"GB": "United Kingdom", "ES": "Spain"}
        request, pipeline, stream_client, answer = _make_comparison_fixture(
            codes, names, max_sources=6,
        )
        p1, p2, p3 = _patch_chat_stream(pipeline=pipeline, stream_client=stream_client)
        with p1, p2, p3:
            http_response, records = _run(
                _call_and_consume(
                    legal_chat_stream(request=request, x_request_id="compare-2"),
                )
            )
        self._assert_clean_comparison_stream(records, codes=codes, answer=answer)

    def test_three_country_comparison(self) -> None:
        codes = ["GB", "ES", "IT"]
        names = {"GB": "United Kingdom", "ES": "Spain", "IT": "Italy"}
        request, pipeline, stream_client, answer = _make_comparison_fixture(
            codes, names, max_sources=6,
        )
        p1, p2, p3 = _patch_chat_stream(pipeline=pipeline, stream_client=stream_client)
        with p1, p2, p3:
            http_response, records = _run(
                _call_and_consume(
                    legal_chat_stream(request=request, x_request_id="compare-3"),
                )
            )
        self._assert_clean_comparison_stream(records, codes=codes, answer=answer)

    def test_six_country_comparison_at_the_default_max_sources_boundary(
        self,
    ) -> None:
        """max_sources defaults to 6 (LegalChatRequest.max_sources,
        Field(default=6, ge=1, le=10)), and the ONLY live country-count
        budget check is `max_sources < len(country_codes)` (strict) -
        so exactly 6 countries is the product-documented, no-override
        boundary."""

        codes = ["GB", "ES", "IT", "CZ", "SE", "CH"]
        names = {
            "GB": "United Kingdom", "ES": "Spain", "IT": "Italy",
            "CZ": "Czech Republic", "SE": "Sweden", "CH": "Switzerland",
        }
        request, pipeline, stream_client, answer = _make_comparison_fixture(
            codes, names, max_sources=6,
        )
        p1, p2, p3 = _patch_chat_stream(pipeline=pipeline, stream_client=stream_client)
        with p1, p2, p3:
            http_response, records = _run(
                _call_and_consume(
                    legal_chat_stream(request=request, x_request_id="compare-6"),
                )
            )
        self._assert_clean_comparison_stream(records, codes=codes, answer=answer)

    def test_current_stable_code_maximum_is_ten_not_six(self) -> None:
        """
        PRE_EXISTING_PRODUCT_CONSTRAINT_DRIFT (reported, not fixed, per
        this mission's standing instruction): the product-documented
        comparison maximum is 6 countries, but the actual stable-code
        ceiling is 10 - LegalChatRequest.max_sources' Pydantic le=10 is
        the ONLY hard limit found anywhere in chat.py/rag_answer.py/
        request_understanding.py (country_codes itself has no
        max_length/model_validator constraint at all), and the
        comparison_source_budget check is `max_sources < country_count`
        (strict), so max_sources=10 with exactly 10 country_codes is
        genuinely reachable through the real dispatch/streaming path -
        proven end to end here, not merely inferred from reading the
        Pydantic Field() definition.
        """

        codes = ["GB", "ES", "IT", "CZ", "SE", "CH", "PL", "PT", "NL", "BE"]
        names = {
            "GB": "United Kingdom", "ES": "Spain", "IT": "Italy",
            "CZ": "Czech Republic", "SE": "Sweden", "CH": "Switzerland",
            "PL": "Poland", "PT": "Portugal", "NL": "Netherlands",
            "BE": "Belgium",
        }
        request, pipeline, stream_client, answer = _make_comparison_fixture(
            codes, names, max_sources=10,
        )
        p1, p2, p3 = _patch_chat_stream(pipeline=pipeline, stream_client=stream_client)
        with p1, p2, p3:
            http_response, records = _run(
                _call_and_consume(
                    legal_chat_stream(request=request, x_request_id="compare-10"),
                )
            )
        self._assert_clean_comparison_stream(records, codes=codes, answer=answer)

    def test_eleven_countries_exceeds_even_the_current_maximum(self) -> None:
        """
        max_sources cannot exceed 10 (Pydantic le=10) - so 11
        countries can never be paired with a large-enough max_sources,
        and hits the real comparison_source_budget check.

        GATE S4B finding: unlike /chat (where legal_chat()'s except
        block wraps resolve_legal_chat_response entirely and can
        return a friendly 200 no matter when this exception fires),
        comparison_source_budget can ONLY be raised from inside
        generation (_retrieve_search_hits, shared by answer_legal_
        question and stream_answer_legal_question) - so for
        /chat/stream it is architecturally always raised AFTER the
        bridge has already signaled "generation starting", i.e. after
        the response is already committed to 200+NDJSON. This module
        special-cases it (see _drain_stream_events) to still stream
        the SAME friendly text /chat would return, as a genuine
        successful completion (replacement -> metadata -> done, no
        prior delta since the failure is detected before any token was
        ever generated) rather than a generic in-band error - the
        closest possible parity with /chat given that constraint.
        """

        codes = [
            "GB", "ES", "IT", "CZ", "SE", "CH",
            "PL", "PT", "NL", "BE", "IE",
        ]
        understanding_client = FakeUnderstandingClient(
            payload=_understanding_result(
                actions=[
                    _understanding_action(
                        "comparison",
                        country_codes=codes,
                        legal_topics=["Working Conditions"],
                    )
                ],
            )
        )
        pipeline = _real_orchestration_pipeline(
            catalog_provider=_catalog_provider,
            document_topic_provider=_document_topic_provider,
            search_function=_unexpected_search,
            generation_client=None,
            understanding_client=understanding_client,
        )
        p1, p2, p3 = _patch_chat_stream(pipeline=pipeline)
        with p1, p2, p3:
            with self.assertLogs(
                "app.routers.chat_stream", level="INFO",
            ) as log_context:
                http_response, records = _run(
                    _call_and_consume(
                        legal_chat_stream(
                            request=LegalChatRequest(
                                question="Compare these countries.",
                                country_codes=codes,
                                max_sources=10,
                            ),
                            x_request_id="compare-11",
                        )
                    )
                )

        types = [r["type"] for r in records]
        self.assertEqual(["start", "replacement", "metadata", "done"], types)
        self.assertIn("11 countries", records[1]["text"])
        self.assertFalse(records[2]["grounded"])

        metric_payloads = [
            json.loads(record.getMessage())
            for record in log_context.records
            if record.getMessage().startswith("{")
        ]
        self.assertEqual(1, len(metric_payloads))
        self.assertEqual("stream_completed", metric_payloads[0]["outcome"])


class StableVsStreamEquivalenceTests(unittest.TestCase):
    """
    GATE S4B item 6: for identical real-dispatch inputs, /api/v1/chat's
    own LegalChatResponse and /api/v1/chat/stream's NDJSON stream
    (reconstructed via _reconstruct_response_from_ndjson, the same
    rules a real browser client must implement) must agree on every
    business-relevant field - not just answer text.

    "evidence-gated legal answer" here is a single-spec request that
    carries evidence_mode explicitly (proving the GATE S3B parameter-
    parity seam - action_specs/subject_text/search_concepts/
    evidence_mode/known_excluded_country_codes - reaches real dispatch
    identically on both routes), rather than a full mixed-insufficient-
    and-direct multi-spec scenario: that deeper equivalence is already
    exhaustively proven at the service level by
    test_stream_answer_legal_question_evidence_gating.py's own
    StrongEquivalenceTests (test_equivalence_evidence_gated_direct_hit/
    test_equivalence_multi_spec/test_equivalence_single_country) - this
    class's job is to prove the SAME equivalence still holds one layer
    up, through real dispatch and the NDJSON wire protocol, not to
    re-prove evidence-gating logic itself.
    """

    def _assert_equivalent(
        self, stable: LegalChatResponse, reconstructed: LegalChatResponse,
    ) -> None:
        self.assertEqual(stable.question, reconstructed.question)
        self.assertEqual(stable.answer, reconstructed.answer)
        self.assertEqual(stable.grounded, reconstructed.grounded)
        self.assertEqual(stable.model, reconstructed.model)
        self.assertEqual(stable.retrieval_total, reconstructed.retrieval_total)
        self.assertEqual(
            [s.model_dump() for s in stable.sources],
            [s.model_dump() for s in reconstructed.sources],
        )
        self.assertEqual(
            [c.model_dump() for c in stable.contacts],
            [c.model_dump() for c in reconstructed.contacts],
        )
        self.assertEqual(
            stable.conversation_state, reconstructed.conversation_state,
        )

    def _stream_reconstructed(
        self,
        request: LegalChatRequest,
        *,
        pipeline,
        stream_client=None,
        x_request_id: str,
    ) -> LegalChatResponse:
        p1, p2, p3 = _patch_chat_stream(pipeline=pipeline, stream_client=stream_client)
        with p1, p2, p3:
            http_response, records = _run(
                _call_and_consume(
                    legal_chat_stream(request=request, x_request_id=x_request_id),
                )
            )
        return _reconstruct_response_from_ndjson(records)

    def test_single_country_legal_answer(self) -> None:
        answer = "Peru\n- Overtime is paid at a premium rate [1]."

        def fake_search(request) -> LegalSearchResponse:
            return LegalSearchResponse(
                query=request.query, total=1, limit=request.limit, offset=0,
                took_ms=1,
                hits=[
                    _build_hit(
                        country_code="PE", country="Peru", chunk_id="chunk-pe-1",
                    )
                ],
            )

        def make_understanding_client():
            return FakeUnderstandingClient(
                payload=_understanding_result(
                    actions=[
                        _understanding_action(
                            "legal_information",
                            country_codes=["PE"], legal_topics=["Working Conditions"],
                        )
                    ],
                )
            )

        request = LegalChatRequest(question="What is the overtime rule in Peru?")

        stable = _run_real_chat_route(
            request,
            catalog_provider=_catalog_provider,
            document_topic_provider=_document_topic_provider,
            search_function=fake_search,
            generation_client=ChatFakeGenerationClient(answer=answer),
            understanding_client=make_understanding_client(),
        )

        stream_generation_client = ChatFakeGenerationClient(answer=answer)
        stream_generation_client.model = stable.model
        pipeline = _real_orchestration_pipeline(
            catalog_provider=_catalog_provider,
            document_topic_provider=_document_topic_provider,
            search_function=fake_search,
            generation_client=stream_generation_client,
            understanding_client=make_understanding_client(),
        )
        stream_client = FakeStreamGenerationClient(_delta_events(answer))
        stream_client.model = stable.model
        reconstructed = self._stream_reconstructed(
            request, pipeline=pipeline, stream_client=stream_client,
            x_request_id="eq-single",
        )

        self._assert_equivalent(stable, reconstructed)

    def test_evidence_gated_legal_answer(self) -> None:
        """
        A single-spec request carrying subject_text/search_concepts/
        evidence_mode explicitly - the exact fixture shape proven at
        the service level by test_stream_answer_legal_question_
        evidence_gating.py's test_equivalence_evidence_gated_direct_hit/
        test_subject_text_direct_hit_proceeds_to_generation (a hit
        whose subsection/content directly addresses the subject, under
        evidence_mode="direct_topic", classifies as "direct" evidence
        and proceeds to a genuinely grounded answer - see class
        docstring for why this single-spec case, not a full mixed-spec
        scenario, is the right scope for THIS route-level matrix).
        """

        answer = (
            "United Kingdom\n- Telework is permitted subject to "
            "agreement. [1]"
        )

        def fake_search(request) -> LegalSearchResponse:
            return LegalSearchResponse(
                query=request.query, total=1, limit=request.limit, offset=0,
                took_ms=1,
                hits=[
                    _build_hit(
                        country_code="GB", country="United Kingdom",
                        chunk_id="chunk-gb-remote-1",
                        legal_topic="Working Conditions",
                        content=(
                            "Employees may telework subject to written "
                            "agreement with their employer."
                        ),
                    )
                ],
            )

        def make_understanding_client():
            return FakeUnderstandingClient(
                payload=_understanding_result(
                    actions=[
                        _understanding_action(
                            "legal_information",
                            country_codes=["GB"], legal_topics=["Working Conditions"],
                            subject_text="remote work",
                            search_concepts=[
                                {"terms": ["remote work", "telework", "teleworking"]}
                            ],
                            evidence_mode="direct_topic",
                        )
                    ],
                )
            )

        request = LegalChatRequest(question="Can employees work remotely?")

        stable = _run_real_chat_route(
            request,
            catalog_provider=_catalog_provider,
            document_topic_provider=_document_topic_provider,
            search_function=fake_search,
            generation_client=ChatFakeGenerationClient(answer=answer),
            understanding_client=make_understanding_client(),
        )
        self.assertTrue(stable.grounded)

        stream_generation_client = ChatFakeGenerationClient(answer=answer)
        stream_generation_client.model = stable.model
        pipeline = _real_orchestration_pipeline(
            catalog_provider=_catalog_provider,
            document_topic_provider=_document_topic_provider,
            search_function=fake_search,
            generation_client=stream_generation_client,
            understanding_client=make_understanding_client(),
        )
        stream_client = FakeStreamGenerationClient(_delta_events(answer))
        stream_client.model = stable.model
        reconstructed = self._stream_reconstructed(
            request, pipeline=pipeline, stream_client=stream_client,
            x_request_id="eq-evidence-gated",
        )

        self._assert_equivalent(stable, reconstructed)

    def test_repair_success_answer(self) -> None:
        initial_answer = (
            "United Kingdom\n- Employees are entitled to unpaid "
            "leave for family reasons [1]."
        )
        repaired_answer = (
            "United Kingdom\n- Employees are entitled to paid "
            "parental leave for four weeks [1]."
        )

        def fake_search(request) -> LegalSearchResponse:
            return LegalSearchResponse(
                query=request.query, total=1, limit=request.limit, offset=0,
                took_ms=1,
                hits=[
                    _build_hit(
                        country_code="GB", country="United Kingdom",
                        chunk_id="chunk-gb-1",
                    )
                ],
            )

        def make_understanding_client():
            return FakeUnderstandingClient(
                payload=_understanding_result(
                    actions=[
                        _understanding_action(
                            "legal_information",
                            country_codes=["GB"], legal_topics=["Working Conditions"],
                        )
                    ],
                )
            )

        request = LegalChatRequest(
            question="What is the paid leave entitlement in the UK?",
        )

        stable = _run_real_chat_route(
            request,
            catalog_provider=_catalog_provider,
            document_topic_provider=_document_topic_provider,
            search_function=fake_search,
            generation_client=FakeGenerationClient(
                answer=initial_answer, repair_answer=repaired_answer,
            ),
            understanding_client=make_understanding_client(),
        )
        self.assertEqual(repaired_answer, stable.answer)

        pipeline = _real_orchestration_pipeline(
            catalog_provider=_catalog_provider,
            document_topic_provider=_document_topic_provider,
            search_function=fake_search,
            generation_client=_RepairOnlyClient(answer=repaired_answer),
            understanding_client=make_understanding_client(),
        )
        stream_client = FakeStreamGenerationClient(_delta_events(initial_answer))
        reconstructed = self._stream_reconstructed(
            request, pipeline=pipeline, stream_client=stream_client,
            x_request_id="eq-repair",
        )

        self.assertEqual(stable.answer, reconstructed.answer)
        self.assertEqual(stable.grounded, reconstructed.grounded)
        self.assertEqual(stable.retrieval_total, reconstructed.retrieval_total)
        self.assertEqual(
            [s.model_dump() for s in stable.sources],
            [s.model_dump() for s in reconstructed.sources],
        )

    def test_two_country_comparison(self) -> None:
        codes = ["GB", "ES"]
        names = {"GB": "United Kingdom", "ES": "Spain"}
        request, pipeline, stream_client, answer = _make_comparison_fixture(
            codes, names, max_sources=6,
        )

        def fake_search(req) -> LegalSearchResponse:
            code = req.country_codes[0]
            return LegalSearchResponse(
                query=req.query, total=1, limit=req.limit, offset=0, took_ms=1,
                hits=[
                    _build_hit(
                        country_code=code, country=names[code],
                        chunk_id=f"chunk-{code.lower()}-1",
                    )
                ],
            )

        stable = _run_real_chat_route(
            request,
            catalog_provider=_catalog_provider,
            document_topic_provider=_document_topic_provider,
            search_function=fake_search,
            generation_client=ChatFakeGenerationClient(answer=answer),
            understanding_client=FakeUnderstandingClient(
                payload=_understanding_result(
                    actions=[
                        _understanding_action(
                            "comparison", country_codes=codes,
                            legal_topics=["Working Conditions"],
                        )
                    ],
                )
            ),
        )

        stream_client.model = stable.model
        reconstructed = self._stream_reconstructed(
            request, pipeline=pipeline, stream_client=stream_client,
            x_request_id="eq-comparison",
        )

        self._assert_equivalent(stable, reconstructed)

    def test_clarification(self) -> None:
        request = LegalChatRequest(
            question="What are the corporate income tax rules in Spain?",
            country_codes=["ES"],
        )

        def make_understanding_client():
            return FakeUnderstandingClient(
                payload=_understanding_result(
                    status="unsupported", clarification_reason="unsupported_request",
                )
            )

        stable = _run_real_chat_route(
            request,
            catalog_provider=_catalog_provider,
            document_topic_provider=_document_topic_provider,
            search_function=_unexpected_search,
            generation_client=NoCallGenerationClient(),
            understanding_client=make_understanding_client(),
        )

        pipeline = _real_orchestration_pipeline(
            catalog_provider=_catalog_provider,
            document_topic_provider=_document_topic_provider,
            search_function=_unexpected_search,
            generation_client=NoCallGenerationClient(),
            understanding_client=make_understanding_client(),
        )
        reconstructed = self._stream_reconstructed(
            request, pipeline=pipeline, x_request_id="eq-clarify",
        )

        self._assert_equivalent(stable, reconstructed)

    def test_contact_response(self) -> None:
        def fake_contact_search(country_codes, client=None) -> LegalSearchResponse:
            return LegalSearchResponse(
                query="", total=1, limit=20, offset=0, took_ms=1,
                hits=[_build_contact_hit(country_code="PE", country="Peru")],
            )

        fake_settings = SimpleNamespace(
            rerank_enabled=False,
            rerank_pool_multiplier=1,
            rag_max_context_characters=12000,
            rag_max_source_characters=6000,
            document_source_dir=Path("/fake/source/dir"),
        )
        fake_state = ContactState(
            document_id="document-pe",
            country_code="PE",
            contacts=(
                ContactRecord(
                    contact_id="contact-pe-1",
                    member_firm="Test Firm",
                    contact_person="Jane Doe",
                    email="jane@test-firm.example",
                    phone="+51 111 222 333",
                    address="Lima, Peru",
                    website="https://test-firm.example",
                ),
            ),
        )

        def fake_read_contact_state(source_directory, document_id):
            return fake_state

        request = LegalChatRequest(
            question="Give me the contact details for an employment lawyer in Peru.",
        )

        with mock.patch(
            "app.routers.chat.search_contact_chunks", side_effect=fake_contact_search,
        ), mock.patch(
            "app.services.chat_contact_cards.read_contact_state",
            side_effect=fake_read_contact_state,
        ):
            stable = _run_real_chat_route(
                request,
                catalog_provider=_catalog_provider,
                document_topic_provider=_document_topic_provider,
                search_function=_unexpected_search,
                generation_client=NoCallGenerationClient(),
                understanding_client=_FailingUnderstandingClient(),
                settings=fake_settings,
            )
            self.assertEqual(1, len(stable.contacts))

            pipeline = _real_orchestration_pipeline(
                catalog_provider=_catalog_provider,
                document_topic_provider=_document_topic_provider,
                search_function=_unexpected_search,
                generation_client=NoCallGenerationClient(),
                understanding_client=_FailingUnderstandingClient(),
            )
            p1, p2, p3 = _patch_chat_stream(pipeline=pipeline)
            with p1, p2, p3, mock.patch(
                "app.routers.chat.get_settings", return_value=fake_settings,
            ):
                http_response, records = _run(
                    _call_and_consume(
                        legal_chat_stream(
                            request=request, x_request_id="eq-contact",
                        )
                    )
                )
            reconstructed = _reconstruct_response_from_ndjson(records)

        self._assert_equivalent(stable, reconstructed)

    def test_insufficient_evidence_fallback(self) -> None:
        request = LegalChatRequest(
            question="What are the overtime rules in France?",
        )

        def make_understanding_client():
            return FakeUnderstandingClient(
                payload=_understanding_result(
                    status="clarification", clarification_reason="missing_country",
                )
            )

        stable = _run_real_chat_route(
            request,
            catalog_provider=_catalog_provider,
            document_topic_provider=_document_topic_provider,
            search_function=_unexpected_search,
            generation_client=None,
            understanding_client=make_understanding_client(),
        )

        pipeline = _real_orchestration_pipeline(
            catalog_provider=_catalog_provider,
            document_topic_provider=_document_topic_provider,
            search_function=_unexpected_search,
            generation_client=None,
            understanding_client=make_understanding_client(),
        )
        reconstructed = self._stream_reconstructed(
            request, pipeline=pipeline, x_request_id="eq-fallback",
        )

        self._assert_equivalent(stable, reconstructed)

    def test_assistant_help_meta(self) -> None:
        request = LegalChatRequest(question="What can you do?")

        stable = _run_real_chat_route(
            request,
            catalog_provider=_catalog_provider,
            document_topic_provider=_document_topic_provider,
            search_function=_unexpected_search,
            generation_client=NoCallGenerationClient(),
            understanding_client=NoCallUnderstandingClient(),
        )

        pipeline = _real_orchestration_pipeline(
            catalog_provider=_catalog_provider,
            document_topic_provider=_document_topic_provider,
            search_function=_unexpected_search,
            generation_client=NoCallGenerationClient(),
            understanding_client=NoCallUnderstandingClient(),
        )
        reconstructed = self._stream_reconstructed(
            request, pipeline=pipeline, x_request_id="eq-help",
        )

        self._assert_equivalent(stable, reconstructed)


class EventLoopNonBlockingTests(unittest.TestCase):
    """Section 23: a slow SYNCHRONOUS preparation stage, run via
    asyncio.to_thread inside legal_chat_stream, must not freeze an
    unrelated async heartbeat running concurrently on the same loop."""

    def test_heartbeat_keeps_ticking_during_blocking_pipeline_work(
        self,
    ) -> None:
        import time

        def slow_blocking_pipeline(request, **kwargs):
            time.sleep(0.3)  # genuinely blocks a real OS thread
            return LegalChatResponse(
                question="hello", answer="done", grounded=False,
                model=None, retrieval_total=0, sources=[],
            )

        async def scenario():
            heartbeat_ticks = []

            async def heartbeat():
                for _ in range(20):
                    heartbeat_ticks.append(asyncio.get_running_loop().time())
                    await asyncio.sleep(0.02)

            with (
                mock.patch(
                    "app.routers.chat_stream.get_settings",
                    return_value=_fake_settings(),
                ),
                mock.patch(
                    "app.routers.chat_stream.resolve_legal_chat_response",
                    side_effect=slow_blocking_pipeline,
                ),
                mock.patch(
                    "app.routers.chat_stream.get_openai_answer_stream_client",
                    return_value=None,
                ),
            ):
                heartbeat_task = asyncio.create_task(heartbeat())
                await legal_chat_stream(
                    request=LegalChatRequest(question="hello"),
                    x_request_id="nonblock-1",
                )
                await heartbeat_task

            return heartbeat_ticks

        ticks = _run(scenario())

        # If the event loop were blocked for the full 0.3s sleep, far
        # fewer than 20 heartbeat ticks (spaced 0.02s apart) could have
        # completed concurrently with it. This proves the synchronous
        # pipeline genuinely ran off the main event loop.
        self.assertEqual(20, len(ticks))
        span = ticks[-1] - ticks[0]
        self.assertGreaterEqual(
            span, 0.15,
            "heartbeat must have kept ticking for a real, elapsed "
            "duration WHILE the blocking pipeline ran concurrently - "
            "too fast suggests the blocking work somehow finished "
            "the loop first rather than truly overlapping",
        )


if __name__ == "__main__":
    unittest.main()
