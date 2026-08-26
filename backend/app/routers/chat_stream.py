"""
POST /api/v1/chat/stream - NDJSON streaming counterpart to POST
/api/v1/chat (chat-streaming initiative, GATE S4).

Architecture: resolve_legal_chat_response() (chat.py) - the entire
existing, stable, synchronous orchestrator (conversation-meta,
assistant-help, clarification, contact, conservative-fallback,
legal_information/comparison dispatch) - runs COMPLETELY UNCHANGED,
in a worker thread (asyncio.to_thread), for every request. The ONE
swappable seam it exposes (_execute_resolved_plan's own
`legal_answer_generation_fn` parameter, GATE S4's only change to
chat.py itself) lets this module redirect the ONE call site that
would otherwise call answer_legal_question() synchronously into a
bridge that instead drives the already-tested async generator
stream_answer_legal_question() (rag_answer.py, GATE S3/S3B) - and
relays its ANSWER_DELTA/VALIDATING/DISCARD/REPLACEMENT events back to
this module's own async generator via an asyncio.Queue, using
loop.call_soon_threadsafe() to cross from the worker thread back onto
the main event loop safely.

This module owns 100% of the NDJSON wire format. rag_answer.py and
chat.py know nothing about it - StreamAnswerEvent (rag_answer.py) is
transport-neutral, and chat.py's only awareness of streaming is the
one swappable parameter.

PRE_STREAM_ERROR vs IN_STREAM_ERROR (mission section 7/8): this
module races two signals - "generation is about to start" (sent by
the bridge itself, at the very first line of its call, before any
network activity) against "the whole pipeline already finished
(successfully or with an exception) without ever needing to stream".
Whichever arrives first is awaited BEFORE any StreamingResponse is
constructed, so every exception that /chat maps to a specific HTTP
status (InvalidLegalChatRequestError/OpenAIConfigurationError/
CountryDetectionError/RagAnswerError/ConversationTransitionError) is
still mapped to that EXACT status here too, whenever it happens before
generation was ever going to start - never silently downgraded to an
HTTP 200 + in-band error merely because generator execution is lazy.
Only once "generation starting" has been observed does this module
commit to StreamingResponse; any failure after that point becomes an
in-band `error` NDJSON event instead, since the HTTP status can no
longer reliably change at that point.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from time import perf_counter
from typing import Any, Final
from uuid import uuid4

from fastapi import Header, HTTPException
from fastapi.responses import StreamingResponse

from app.clients.openai_responses import OpenAIConfigurationError
from app.clients.openai_responses_stream import get_openai_answer_stream_client
from app.core.config import get_settings
from app.models.chat import LegalChatRequest, LegalChatResponse
from app.routers.chat import (
    _build_comparison_source_budget_response,
    router,
)
from app.services.conversation_transition import ConversationTransitionError
from app.services.country_detection import CountryDetectionError
from app.services.rag_answer import (
    InvalidLegalChatRequestError,
    RagAnswerError,
    StreamAnswerEvent,
    StreamAnswerEventType,
    StreamAnswerTimings,
    stream_answer_legal_question,
)

from app.routers.chat import resolve_legal_chat_response


NDJSON_PROTOCOL_VERSION: Final[int] = 1
NDJSON_MEDIA_TYPE: Final[str] = "application/x-ndjson; charset=utf-8"

logger = logging.getLogger("app.routers.chat_stream")


# =============================================================================
# NDJSON serialization - the ONE place this module turns a Python dict
# into a wire record. Never build a JSON string by concatenation
# anywhere else in this file.
# =============================================================================


def _serialize_ndjson_record(payload: dict[str, Any]) -> bytes:
    """
    Exactly one JSON object, UTF-8 encoded, terminated by exactly one
    newline. allow_nan=False makes a stray NaN/Infinity a hard error
    here rather than silently emitting non-standard JSON;
    ensure_ascii=False preserves real Unicode text (matching this
    codebase's own chat_metrics.py logging convention) rather than
    \\uXXXX-escaping it - either is valid JSON, but raw UTF-8 is more
    compact and directly matches what a browser's TextDecoder expects
    to re-assemble across chunk boundaries.
    """

    line = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )

    return (line + "\n").encode("utf-8")


def _start_record(request_id: str) -> dict[str, Any]:
    return {
        "type": "start",
        "protocol_version": NDJSON_PROTOCOL_VERSION,
        "request_id": request_id,
    }


def _delta_record(text: str) -> dict[str, Any]:
    return {"type": "delta", "text": text}


def _validating_record() -> dict[str, Any]:
    return {"type": "validating"}


def _discard_record() -> dict[str, Any]:
    return {"type": "discard"}


def _replacement_record(text: str) -> dict[str, Any]:
    return {"type": "replacement", "text": text}


def _metadata_record(response: LegalChatResponse) -> dict[str, Any]:
    """
    Every LegalChatResponse field EXCEPT the user-visible `answer`
    text (already delivered via delta/replacement records) - via the
    model's own Pydantic serialization, never a hand-built second
    schema. Preserves sources/contacts/grounded/model/retrieval_total/
    conversation_state/question exactly as /chat itself would return
    them.
    """

    payload = response.model_dump(mode="json", exclude={"answer"})
    return {"type": "metadata", **payload}


def _done_record(request_id: str) -> dict[str, Any]:
    return {"type": "done", "request_id": request_id}


def _error_record(
    *, code: str, message: str, retryable: bool = False,
) -> dict[str, Any]:
    return {
        "type": "error",
        "code": code,
        "message": message,
        "retryable": retryable,
    }


# =============================================================================
# Structured stream metrics (GATE S4B) - exactly one terminal
# "chat_stream_performance" log line per /chat/stream request,
# ADDITIVE to (never replacing) the existing "legal_chat_performance"
# line resolve_legal_chat_response() already emits internally on every
# path, streaming or not. Never logs the question, the answer,
# sources, or any credential - only counts/timings/classification
# labels, matching chat_metrics.py's own existing privacy convention.
# =============================================================================


def _elapsed_ms(started_at: float, event_at: float | None) -> float | None:
    if event_at is None:
        return None

    return round((event_at - started_at) * 1000, 2)


def _stream_metric_payload(
    *,
    request_id: str,
    t0: float,
    outcome: str,
    error_code: str | None = None,
    timings: StreamAnswerTimings | None = None,
    metrics: Any = None,
    first_fastapi_delta_at: float | None = None,
    done_at: float | None = None,
) -> dict[str, Any]:
    """
    Builds the one "chat_stream_performance" log payload for a single
    /chat/stream request.

    `timings` is the StreamAnswerTimings instance threaded into
    stream_answer_legal_question() (rag_answer.py, S3) - populated
    only once real generation actually ran. `metrics` is the SAME
    LegalChatMetrics instance _execute_resolved_plan hands to
    legal_answer_generation_fn (captured by this module's own bridge
    below), reused here ONLY for its already-existing
    repair_triggered/repair_success fields, so this event's repair
    classification is defined identically to /chat's own
    "legal_chat_performance" line - never a second, divergent
    definition of "repair succeeded".

    t1_understanding_complete and t3_rerank_complete are always null:
    neither is observable from this module without adding a new
    timing hook inside resolve_legal_chat_response's own internal
    dispatch (t1), or splitting _retrieve_search_hits's combined
    retrieval+rerank timestamp (t3) - both would touch the stable,
    unmodified /chat pipeline this gate is explicitly forbidden from
    refactoring. Never fabricated; reported as null instead. T7 is
    browser-only and is never computed here.
    """

    repair_triggered = bool(getattr(metrics, "repair_triggered", False))

    return {
        "event": "chat_stream_performance",
        "request_id": request_id,
        "outcome": outcome,
        "error_code": error_code,
        "repair_triggered": repair_triggered,
        "repair_success": (
            bool(getattr(metrics, "repair_success", False))
            if repair_triggered
            else None
        ),
        "t0_request_received": 0.0,
        "t1_understanding_complete": None,
        "t2_retrieval_complete": _elapsed_ms(
            t0,
            timings.retrieval_and_rerank_complete if timings else None,
        ),
        "t3_rerank_complete": None,
        "t4_generation_start": _elapsed_ms(
            t0, timings.generation_start if timings else None,
        ),
        "t5_first_provider_delta": _elapsed_ms(
            t0, timings.first_provider_delta if timings else None,
        ),
        "t6_first_fastapi_delta": _elapsed_ms(t0, first_fastapi_delta_at),
        "t8_done": _elapsed_ms(t0, done_at),
        "validation_start": _elapsed_ms(
            t0, timings.validation_start if timings else None,
        ),
        "validation_end": _elapsed_ms(
            t0, timings.validation_end if timings else None,
        ),
        "repair_start": _elapsed_ms(
            t0, timings.repair_start if timings else None,
        ),
        "repair_end": _elapsed_ms(
            t0, timings.repair_end if timings else None,
        ),
        "total_ms": _elapsed_ms(t0, perf_counter()),
    }


def _log_stream_metric(payload: dict[str, Any]) -> None:
    logger.info(
        "%s",
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
    )


# =============================================================================
# Internal StreamAnswerEvent -> NDJSON mapping (mission section 11).
# The RAG service (rag_answer.py) never sees any of this.
# =============================================================================


def _map_stream_answer_event(event: StreamAnswerEvent) -> dict[str, Any] | None:
    """
    Maps ANSWER_DELTA/VALIDATING/DISCARD/REPLACEMENT directly.
    FINALIZED/ERROR are handled by the caller instead (they need
    context - the final assembled LegalChatResponse, or stream
    termination - that a pure per-event mapping can't provide) and
    intentionally return None here so a caller that forgets to
    special-case them fails loudly instead of emitting a wrong record.
    """

    if event.type is StreamAnswerEventType.ANSWER_DELTA:
        return _delta_record(event.delta_text or "")

    if event.type is StreamAnswerEventType.VALIDATING:
        return _validating_record()

    if event.type is StreamAnswerEventType.DISCARD:
        return _discard_record()

    if event.type is StreamAnswerEventType.REPLACEMENT:
        return _replacement_record(event.replacement_text or "")

    return None


# =============================================================================
# HTTP error boundary - the EXACT same exception -> status mapping
# legal_chat() (chat.py) uses, reused here so a pre-stream failure
# behaves identically whichever endpoint hit it.
# =============================================================================


def _map_pre_stream_exception_to_http(
    error: Exception, *, request: LegalChatRequest, request_id: str,
) -> HTTPException | LegalChatResponse:
    """
    Returns an HTTPException to raise, OR (for the one friendly-200
    case) a LegalChatResponse to serve as an early-finalized stream
    instead of an HTTP error - never a third, divergent classification.
    """

    if isinstance(error, InvalidLegalChatRequestError):
        if error.code == "comparison_source_budget":
            return _build_comparison_source_budget_response(
                request=request, error=error,
            )

        return HTTPException(
            status_code=422,
            detail=str(error),
            headers={"X-Request-ID": request_id},
        )

    if isinstance(error, OpenAIConfigurationError):
        return HTTPException(
            status_code=503,
            detail="The answer generation service is not configured.",
            headers={"X-Request-ID": request_id},
        )

    if isinstance(error, CountryDetectionError):
        return HTTPException(
            status_code=502,
            detail="The country detection service is temporarily unavailable.",
            headers={"X-Request-ID": request_id},
        )

    if isinstance(error, RagAnswerError):
        return HTTPException(
            status_code=502,
            detail=(
                "The grounded legal answer service is temporarily "
                "unavailable."
            ),
            headers={"X-Request-ID": request_id},
        )

    if isinstance(error, ConversationTransitionError):
        return HTTPException(
            status_code=502,
            detail=(
                "The conversation context could not be processed for "
                "this request."
            ),
            headers={"X-Request-ID": request_id},
        )

    raise error


# =============================================================================
# The cross-thread bridge and event-draining generator.
# =============================================================================

# Sentinels placed on the asyncio.Queue - never confused with a real
# StreamAnswerEvent, which is always an instance of that dataclass.
_GENERATION_STARTING: Final[object] = object()
_PIPELINE_DONE: Final[object] = object()


async def _drain_stream_events(
    *,
    event_queue: asyncio.Queue,
    pipeline_task: asyncio.Task[LegalChatResponse],
    request_id: str,
    request: LegalChatRequest,
    timings: StreamAnswerTimings,
    sent_text_holder: list[str],
    metrics_holder: list[Any],
    t0: float,
) -> AsyncIterator[bytes]:
    """
    Runs ONLY once "generation starting" has already been observed -
    HTTP 200 + StreamingResponse is already committed by the time this
    generator's body executes. Any failure from here on becomes an
    in-band `error` record, never a changed HTTP status - EXCEPT
    InvalidLegalChatRequestError(code="comparison_source_budget")
    (GATE S4B item 5/6 finding), which gets special-cased below: see
    that branch's own comment for why.

    `metrics_holder` and `t0` (GATE S4B) exist solely to build the one
    terminal "chat_stream_performance" log line - see
    _stream_metric_payload. metrics_holder[0] is set by the bridge
    (_bridge, below) to the SAME LegalChatMetrics instance
    _execute_resolved_plan already passes it, before this generator
    ever sees its first event.
    """

    yield _serialize_ndjson_record(_start_record(request_id))

    stream_failed = False
    first_fastapi_delta_at: float | None = None

    try:
        while True:
            item = await event_queue.get()

            if item is _PIPELINE_DONE:
                break

            assert isinstance(item, StreamAnswerEvent)

            if item.type is StreamAnswerEventType.ANSWER_DELTA:
                sent_text_holder[0] += item.delta_text or ""
                if first_fastapi_delta_at is None:
                    first_fastapi_delta_at = perf_counter()
                yield _serialize_ndjson_record(_map_stream_answer_event(item))
                continue

            if item.type is StreamAnswerEventType.REPLACEMENT:
                sent_text_holder[0] = item.replacement_text or ""
                if first_fastapi_delta_at is None:
                    first_fastapi_delta_at = perf_counter()
                yield _serialize_ndjson_record(_map_stream_answer_event(item))
                continue

            if item.type in (
                StreamAnswerEventType.VALIDATING,
                StreamAnswerEventType.DISCARD,
            ):
                yield _serialize_ndjson_record(_map_stream_answer_event(item))
                continue

            if item.type is StreamAnswerEventType.ERROR:
                stream_failed = True
                yield _serialize_ndjson_record(
                    _error_record(
                        code="stream_generation_failed",
                        message=(
                            item.error_message
                            or "Answer generation failed."
                        ),
                        retryable=item.retryable,
                    )
                )
                continue

            # FINALIZED carries no direct NDJSON record of its own -
            # the true final answer/metadata come from the pipeline's
            # OWN return value (which may still append an unavailable-
            # countries note or a contact fallback after this point),
            # awaited below once _PIPELINE_DONE arrives.
    finally:
        pass

    if stream_failed:
        # The bridge's own RagAnswerError already unwound the worker
        # thread's pipeline; consume it here so it is never logged as
        # an unretrieved task exception, but the client has already
        # received the in-band error above - nothing more to send.
        try:
            await pipeline_task
        except Exception:
            pass

        _log_stream_metric(
            _stream_metric_payload(
                request_id=request_id,
                t0=t0,
                outcome="stream_error",
                error_code="stream_generation_failed",
                timings=timings,
                metrics=metrics_holder[0],
                first_fastapi_delta_at=first_fastapi_delta_at,
            )
        )
        return

    try:
        response = await pipeline_task
    except Exception as error:
        # GATE S4B finding: comparison_source_budget
        # (InvalidLegalChatRequestError) can ONLY ever be raised from
        # _retrieve_search_hits (rag_answer.py), reached exclusively
        # through legal_answer_generation_fn - i.e. always from INSIDE
        # generation, for both answer_legal_question and
        # stream_answer_legal_question alike. For /chat/stream that
        # means it is raised strictly AFTER the bridge has already
        # signaled "generation starting", so it is architecturally
        # impossible for this exception to ever surface as a
        # PRE_STREAM_ERROR here (unlike /chat, where legal_chat()'s
        # except block wraps the entire call and can still return a
        # friendly 200 no matter when inside the pipeline it fires).
        # Special-case it to stream the SAME friendly response /chat
        # itself would return, as a genuine successful completion -
        # the closest possible parity with /chat given the HTTP status
        # already committed to 200 the instant generation started.
        # Checked by isinstance+code (never a bare re-raise here) so a
        # class of exception this module doesn't fully recognize can
        # never escape this already-active generator uncaught - it
        # falls through to the generic, always-safe handling below
        # instead, same as ever other unexpected exception.
        if (
            isinstance(error, InvalidLegalChatRequestError)
            and error.code == "comparison_source_budget"
        ):
            response = _build_comparison_source_budget_response(
                request=request, error=error,
            )

            if response.answer != sent_text_holder[0]:
                yield _serialize_ndjson_record(
                    _replacement_record(response.answer)
                )

            if timings.finalization is None:
                timings.finalization = perf_counter()

            yield _serialize_ndjson_record(_metadata_record(response))
            done_at = perf_counter()
            yield _serialize_ndjson_record(_done_record(request_id))

            _log_stream_metric(
                _stream_metric_payload(
                    request_id=request_id,
                    t0=t0,
                    outcome="stream_completed",
                    timings=timings,
                    metrics=metrics_holder[0],
                    first_fastapi_delta_at=first_fastapi_delta_at,
                    done_at=done_at,
                )
            )
            return

        logger.exception(
            "chat_stream pipeline failed after generation had "
            "already started streaming (request_id=%s)",
            request_id,
        )

        # Generation itself succeeded and validated cleanly (FINALIZED
        # already fired, or this branch would never be reached), but
        # the surrounding pipeline then failed before ever returning a
        # LegalChatResponse - so nothing streamed so far was ever
        # actually finalized. Whatever provisional/final text the
        # client already has is therefore no longer trustworthy;
        # DISCARD it before the terminal error, same as any other
        # failure that follows partial content (mission section 11).
        if sent_text_holder[0]:
            yield _serialize_ndjson_record(_discard_record())

        yield _serialize_ndjson_record(
            _error_record(
                code="post_generation_failure",
                message="The response could not be finalized.",
                retryable=False,
            )
        )

        _log_stream_metric(
            _stream_metric_payload(
                request_id=request_id,
                t0=t0,
                outcome="post_generation_failure",
                error_code=type(error).__name__,
                timings=timings,
                metrics=metrics_holder[0],
                first_fastapi_delta_at=first_fastapi_delta_at,
            )
        )
        return

    # _execute_resolved_plan can append text AFTER the streamed RAG
    # answer (an unavailable-countries note, or an ungrounded-answer
    # contact fallback) - if what the client already has doesn't match
    # the true final answer, reconcile with one more REPLACEMENT before
    # settling, rather than silently omitting that content.
    if response.answer != sent_text_holder[0]:
        yield _serialize_ndjson_record(_replacement_record(response.answer))

    if timings.finalization is None:
        timings.finalization = perf_counter()

    yield _serialize_ndjson_record(_metadata_record(response))
    done_at = perf_counter()
    yield _serialize_ndjson_record(_done_record(request_id))

    _log_stream_metric(
        _stream_metric_payload(
            request_id=request_id,
            t0=t0,
            outcome="stream_completed",
            timings=timings,
            metrics=metrics_holder[0],
            first_fastapi_delta_at=first_fastapi_delta_at,
            done_at=done_at,
        )
    )


async def _early_finalized_stream(
    response: LegalChatResponse, request_id: str, t0: float,
) -> AsyncIterator[bytes]:
    """
    Mission section 12/4: a request that resolves WITHOUT ever needing
    real RAG generation (conversation-meta, assistant-help,
    clarification, contact, conservative fallback, insufficient-
    evidence, or the friendly comparison_source_budget response) still
    reaches the browser as one complete, immediately-visible answer -
    never silently reduced to metadata-only. No fabricated token
    delay.

    `t0` (GATE S4B) is this request's start time, used only to build
    the terminal "chat_stream_performance" log line - no generation
    ever ran on this path, so `timings`/`metrics` stay unset (repair
    never applies here, and t2/t4/t5/validation/repair are correctly
    null rather than fabricated).
    """

    yield _serialize_ndjson_record(_start_record(request_id))

    first_fastapi_delta_at: float | None = None
    if response.answer:
        first_fastapi_delta_at = perf_counter()
        yield _serialize_ndjson_record(_delta_record(response.answer))

    yield _serialize_ndjson_record(_metadata_record(response))
    done_at = perf_counter()
    yield _serialize_ndjson_record(_done_record(request_id))

    _log_stream_metric(
        _stream_metric_payload(
            request_id=request_id,
            t0=t0,
            outcome="stream_completed",
            first_fastapi_delta_at=first_fastapi_delta_at,
            done_at=done_at,
        )
    )


def _streaming_response_headers(request_id: str) -> dict[str, str]:
    return {
        "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no",
        "X-Request-ID": request_id,
    }


@router.post("/chat/stream")
async def legal_chat_stream(
    request: LegalChatRequest,
    x_request_id: str | None = Header(default=None, alias="X-Request-ID"),
) -> StreamingResponse:
    """NDJSON streaming counterpart to POST /api/v1/chat - see module
    docstring for the full architecture."""

    t0 = perf_counter()
    settings = get_settings()
    request_id = x_request_id.strip() if x_request_id else str(uuid4())

    # PRE-STREAM: a missing/invalid OpenAI configuration is detectable
    # immediately, with zero orchestration work - fail with the exact
    # same HTTP status /chat would (via OpenAIConfigurationError
    # inside resolve_legal_chat_response's own generation path) rather
    # than only discovering this once a request happens to need
    # generation.
    try:
        stream_client = get_openai_answer_stream_client()
    except OpenAIConfigurationError as error:
        _log_stream_metric(
            _stream_metric_payload(
                request_id=request_id,
                t0=t0,
                outcome="pre_stream_failure",
                error_code=type(error).__name__,
            )
        )
        raise HTTPException(
            status_code=503,
            detail="The answer generation service is not configured.",
            headers={"X-Request-ID": request_id},
        ) from error

    timings = StreamAnswerTimings()
    loop = asyncio.get_running_loop()
    event_queue: asyncio.Queue = asyncio.Queue()
    metrics_holder: list[Any] = [None]

    def _emit_threadsafe(item: object) -> None:
        loop.call_soon_threadsafe(event_queue.put_nowait, item)

    def _bridge(
        prepared_request: LegalChatRequest,
        *,
        search_function,
        generation_client,
        rerank_enabled: bool,
        rerank_pool_multiplier: int,
        max_context_characters: int,
        max_source_characters: int,
        metrics,
        current_user_question: str | None = None,
        action_specs=None,
        known_excluded_country_codes=None,
        subject_text: str | None = None,
        search_concepts=None,
        evidence_mode: str | None = None,
    ) -> LegalChatResponse:
        """
        Drop-in replacement for answer_legal_question at
        _execute_resolved_plan's ONE call site (chat.py) - same
        blocking call contract (runs on the worker thread this whole
        pipeline is already executing in), but internally drives
        stream_answer_legal_question() on the MAIN event loop via
        run_coroutine_threadsafe, relaying each event back through
        event_queue for _drain_stream_events to consume.
        """

        # GATE S4B: capture the SAME LegalChatMetrics instance
        # _execute_resolved_plan passes every legal_answer_generation_fn
        # (streaming or not), so the terminal stream metric's
        # repair_triggered/repair_success reuse /chat's own existing
        # definition of "repair succeeded" rather than a second one.
        metrics_holder[0] = metrics
        _emit_threadsafe(_GENERATION_STARTING)

        async def _consume() -> LegalChatResponse:
            result: LegalChatResponse | None = None

            async for event in stream_answer_legal_question(
                request=prepared_request,
                search_function=search_function,
                generation_client=generation_client,
                stream_generation_client=stream_client,
                rerank_enabled=rerank_enabled,
                rerank_pool_multiplier=rerank_pool_multiplier,
                max_context_characters=max_context_characters,
                max_source_characters=max_source_characters,
                metrics=metrics,
                subject_text=subject_text,
                search_concepts=search_concepts,
                evidence_mode=evidence_mode,
                action_specs=action_specs,
                known_excluded_country_codes=known_excluded_country_codes,
                current_user_question=current_user_question,
                timings=timings,
            ):
                # Already running on the main loop (via
                # run_coroutine_threadsafe below) - a direct put_nowait
                # is safe here, unlike _emit_threadsafe's cross-thread
                # case above.
                event_queue.put_nowait(event)

                if event.type is StreamAnswerEventType.FINALIZED:
                    result = event.result
                elif event.type is StreamAnswerEventType.ERROR:
                    raise RagAnswerError(
                        event.error_message
                        or "Streaming generation failed."
                    )

            if result is None:
                raise RagAnswerError(
                    "Streaming generation ended without a result."
                )

            return result

        return asyncio.run_coroutine_threadsafe(
            _consume(), loop,
        ).result()

    def _run_pipeline() -> LegalChatResponse:
        try:
            return resolve_legal_chat_response(
                request,
                request_id=request_id,
                rerank_enabled=settings.rerank_enabled,
                rerank_pool_multiplier=settings.rerank_pool_multiplier,
                max_context_characters=settings.rag_max_context_characters,
                max_source_characters=settings.rag_max_source_characters,
                legal_answer_generation_fn=_bridge,
            )
        finally:
            _emit_threadsafe(_PIPELINE_DONE)

    pipeline_task: asyncio.Task[LegalChatResponse] = asyncio.create_task(
        asyncio.to_thread(_run_pipeline)
    )

    # Eagerly wait for the FIRST signal - "generation is starting" or
    # "the whole pipeline is already done" - BEFORE constructing
    # StreamingResponse, so a pre-generation failure preserves today's
    # exact HTTP status (mission section 7/8).
    first_signal = await event_queue.get()

    if first_signal is _PIPELINE_DONE:
        try:
            response = await pipeline_task
        except Exception as error:
            mapped = _map_pre_stream_exception_to_http(
                error, request=request, request_id=request_id,
            )

            if isinstance(mapped, HTTPException):
                _log_stream_metric(
                    _stream_metric_payload(
                        request_id=request_id,
                        t0=t0,
                        outcome="pre_stream_failure",
                        error_code=type(error).__name__,
                    )
                )
                raise mapped from error

            return StreamingResponse(
                _early_finalized_stream(mapped, request_id, t0),
                media_type=NDJSON_MEDIA_TYPE,
                headers=_streaming_response_headers(request_id),
            )

        return StreamingResponse(
            _early_finalized_stream(response, request_id, t0),
            media_type=NDJSON_MEDIA_TYPE,
            headers=_streaming_response_headers(request_id),
        )

    # first_signal is _GENERATION_STARTING: real token streaming.
    sent_text_holder = [""]

    return StreamingResponse(
        _drain_stream_events(
            event_queue=event_queue,
            pipeline_task=pipeline_task,
            request_id=request_id,
            request=request,
            timings=timings,
            sent_text_holder=sent_text_holder,
            metrics_holder=metrics_holder,
            t0=t0,
        ),
        media_type=NDJSON_MEDIA_TYPE,
        headers=_streaming_response_headers(request_id),
    )
