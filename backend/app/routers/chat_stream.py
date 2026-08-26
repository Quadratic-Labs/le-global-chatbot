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
import concurrent.futures
import json
import logging
from collections.abc import AsyncIterator
from time import perf_counter
from typing import Any, Final
from uuid import uuid4

from fastapi import Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

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

# GATE S6D: a single whole-request deadline for /chat/stream only -
# /chat is untouched. No individual OpenAI/OpenSearch timeout is
# changed by this; this is purely an OUTER ceiling on the full
# understanding -> retrieval -> rerank -> generation -> validation ->
# repair sequence, none of which previously had one shared bound.
# Matches GATE S6C's own SUPPORTED_CONFIG_MAX derivation (120s
# understanding + 30s retrieval + 30s rerank-if-enabled + 120s stream
# + 60s repair = 360s), kept strictly below the WordPress proxy's own
# cURL total (400s) and PHP execution (420s) ceilings so this backend
# deadline is always what fires first for a genuinely stuck request -
# WordPress's own ceilings remain the outer safety net only for a
# request that somehow ignores this deadline entirely (e.g. a true
# hang inside a call this deadline can't interrupt mid-flight).
STREAM_REQUEST_DEADLINE_SECONDS: Final[float] = 360.0

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

# GATE S9-LITE: how often the pre-stream phase polls for a client
# disconnect while racing against _get_with_deadline. Detection
# latency is bounded by this interval (empirically verified against
# this deployment's pinned fastapi/starlette/uvicorn - see the S9-LITE
# report) - not instantaneous, but prompt and reliable.
_DISCONNECT_POLL_INTERVAL_SECONDS: Final[float] = 0.5


def _cancel_bridge_work(
    pipeline_task: asyncio.Task,
    consume_future_holder: list[concurrent.futures.Future | None],
) -> None:
    """
    GATE S9-LITE: the ONE place that actually stops in-flight /chat/
    stream generation work, replacing every bare `pipeline_task.cancel()`
    call in this module (deadline expiry, and now client disconnect).

    Cancelling pipeline_task ALONE (the pre-existing, only-ever-used
    mechanism) has NO effect once the worker thread has started
    running _run_pipeline - concurrent.futures.Future.cancel() cannot
    interrupt an already-running callable (a fundamental Python
    limitation, not a bug in this code). Once generation has started,
    that worker thread is blocked inside _bridge on
    `consume_future.result()` (a SEPARATE concurrent.futures.Future,
    bridging into _consume()'s own Task on THIS event loop via
    run_coroutine_threadsafe) - cancelling THAT future, verified
    empirically, DOES promptly deliver asyncio.CancelledError into
    _consume() (and whatever it is currently awaiting inside
    stream_answer_legal_question - the httpx SSE read, or, less
    promptly, a synchronous repair call already running on ITS OWN
    thread), letting the worker thread unwind and pipeline_task
    complete almost immediately instead of running to its own
    independent provider-timeout ceiling (up to ~180s) unobserved.

    consume_future_holder[0] is None whenever generation has not
    reached _bridge yet (understanding/retrieval/preparation) - there
    is nothing to cancel there beyond pipeline_task itself, and per
    the same fundamental limitation, that will not interrupt the
    synchronous call currently running either; see the S9-LITE report
    for why "before first delta" cancellation is bounded, not instant.
    """

    consume_future = consume_future_holder[0]

    if consume_future is not None:
        consume_future.cancel()

    pipeline_task.cancel()


# =============================================================================
# GATE S9B: explicit cancellation by request_id - independent of the
# passive browser-disconnect detection GATE S9-LITE found unreliable
# under real Apache/mod_php (small/infrequent NDJSON chunks can sit in
# the OS send buffer without the kernel ever attempting a real write,
# so connection_aborted() may never observe a failure at all). This is
# a plain, process-local, in-memory dict - no Redis/database, no
# prompts/answers ever stored, nothing persisted across a process
# restart. Registered only once real streaming has actually begun (see
# _drain_stream_events) - a pre-stream request has no request_id the
# client could ever have learned yet (the NDJSON `start` record is the
# only place one is ever revealed), so there is nothing meaningful to
# register or cancel before that point.
# =============================================================================

_ACTIVE_STREAMS: dict[
    str, tuple[asyncio.Task, list[concurrent.futures.Future | None]]
] = {}


def _register_active_stream(
    request_id: str,
    pipeline_task: asyncio.Task,
    consume_future_holder: list[concurrent.futures.Future | None],
) -> None:
    _ACTIVE_STREAMS[request_id] = (pipeline_task, consume_future_holder)


def _deregister_active_stream(request_id: str) -> None:
    """Idempotent - safe to call more than once, and safe for a
    request_id that was never registered (e.g. a pre-stream request)."""

    _ACTIVE_STREAMS.pop(request_id, None)


def cancel_active_stream(request_id: str) -> bool:
    """
    The one function POST /chat/stream/cancel calls. Returns True if
    `request_id` named a currently-active stream (cancellation was
    requested - not a guarantee it has finished yet, just that
    _cancel_bridge_work was invoked for it), False for any unknown or
    already-finished request_id - always safe, never raises, and
    cancelling one request_id can never affect any other (each maps to
    its own independent pipeline_task/consume_future_holder pair).
    """

    entry = _ACTIVE_STREAMS.get(request_id)

    if entry is None:
        return False

    pipeline_task, consume_future_holder = entry
    _cancel_bridge_work(pipeline_task, consume_future_holder)
    return True


async def _wait_for_disconnect(
    request: Request | None,
    poll_interval: float = _DISCONNECT_POLL_INTERVAL_SECONDS,
) -> None:
    """
    GATE S9-LITE: the pre-stream counterpart to Starlette's own
    automatic StreamingResponse disconnect-cancellation (which only
    applies once a StreamingResponse has actually been constructed -
    verified empirically against this deployment's pinned versions).
    Before that point (understanding/retrieval/preparation), nothing
    proactively observes the client going away unless something polls
    request.is_disconnected() - this is that poll, raced against
    _get_with_deadline in legal_chat_stream's own eager pre-stream
    wait.

    `request is None` - a direct/unit-test call of legal_chat_stream()
    that bypasses FastAPI's own routing (http_request defaults to
    None precisely so every existing such call site keeps working
    unchanged) - means there is no ASGI Request to poll at all; this
    simply never completes, so the race in legal_chat_stream always
    resolves via _get_with_deadline instead, identical to this
    module's pre-S9-LITE behavior.
    """

    if request is None:
        await asyncio.Event().wait()
        return

    while True:
        if await request.is_disconnected():
            return

        await asyncio.sleep(poll_interval)


async def _get_with_deadline(
    event_queue: asyncio.Queue, deadline_at: float,
):
    """
    GATE S6D: the ONE place this module waits on event_queue with the
    whole-request deadline applied - used both by the eager pre-stream
    race and by _drain_stream_events's own drain loop, so "the
    deadline expired" is detected identically (and cancels the same
    way) regardless of which phase the request was in when it fired.
    Raises asyncio.TimeoutError when the deadline passes with nothing
    new on the queue - callers decide what that means for THEIR phase
    (a pre-stream HTTP error vs. an in-band NDJSON error).
    """

    remaining = max(0.0, deadline_at - perf_counter())
    return await asyncio.wait_for(event_queue.get(), timeout=remaining)


async def _drain_stream_events(
    *,
    event_queue: asyncio.Queue,
    pipeline_task: asyncio.Task[LegalChatResponse],
    consume_future_holder: list[concurrent.futures.Future | None],
    request_id: str,
    request: LegalChatRequest,
    timings: StreamAnswerTimings,
    sent_text_holder: list[str],
    metrics_holder: list[Any],
    t0: float,
    deadline_at: float,
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

    # GATE S9B: only from this point on does the client have any way
    # to have learned request_id (it is revealed exclusively in the
    # start record above) - registering any earlier would let a
    # cancel request name an id nobody could legitimately know yet.
    _register_active_stream(request_id, pipeline_task, consume_future_holder)

    stream_failed = False
    first_fastapi_delta_at: float | None = None

    try:
        while True:
            try:
                item = await _get_with_deadline(event_queue, deadline_at)
            except asyncio.TimeoutError:
                # GATE S6D: the whole-request deadline fired mid-
                # stream. Whatever provisional text the client already
                # has is no longer trustworthy (the request as a whole
                # never finished), so DISCARD it first - same
                # invalidate-before-terminal-error rule as every other
                # failure that follows partial content.
                if sent_text_holder[0]:
                    yield _serialize_ndjson_record(_discard_record())

                yield _serialize_ndjson_record(
                    _error_record(
                        code="stream_request_timeout",
                        message=(
                            "The legal assistant did not finish "
                            "responding in time."
                        ),
                        retryable=True,
                    )
                )

                _cancel_bridge_work(pipeline_task, consume_future_holder)
                try:
                    await pipeline_task
                except (Exception, asyncio.CancelledError):
                    pass

                _log_stream_metric(
                    _stream_metric_payload(
                        request_id=request_id,
                        t0=t0,
                        outcome="stream_error",
                        error_code="stream_request_timeout",
                        timings=timings,
                        metrics=metrics_holder[0],
                        first_fastapi_delta_at=first_fastapi_delta_at,
                    )
                )
                _deregister_active_stream(request_id)
                return

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
    except asyncio.CancelledError:
        # GATE S9-LITE: the client disconnected (Starlette's own
        # StreamingResponse machinery cancels the task iterating this
        # generator as soon as it observes the disconnect - verified
        # empirically against this deployment's pinned versions, see
        # the S9-LITE report). The client is already gone, so there is
        # nothing to yield - just stop the still-running work
        # promptly, log exactly one distinct terminal metric, and
        # re-raise so this generator's own cancellation completes
        # correctly (never silently swallowed).
        _cancel_bridge_work(pipeline_task, consume_future_holder)

        try:
            await pipeline_task
        except (Exception, asyncio.CancelledError):
            pass

        _log_stream_metric(
            _stream_metric_payload(
                request_id=request_id,
                t0=t0,
                outcome="client_disconnected",
                error_code="client_disconnected",
                timings=timings,
                metrics=metrics_holder[0],
                first_fastapi_delta_at=first_fastapi_delta_at,
            )
        )
        _deregister_active_stream(request_id)
        raise
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
        _deregister_active_stream(request_id)
        return

    try:
        response = await pipeline_task
    except asyncio.CancelledError:
        # GATE S9B: explicit cancellation via POST /chat/stream/cancel
        # arrived while this generator was still waiting on
        # event_queue (NOT this generator's own task being cancelled -
        # that is the separate except asyncio.CancelledError above,
        # around the while loop, which only fires on a real client
        # disconnect). _PIPELINE_DONE still arrived normally (_run_
        # pipeline's own finally always emits it, cancelled or not),
        # but pipeline_task itself now carries the CancelledError that
        # cancelling consume_future_holder[0] produced. The client is
        # very likely still connected here (it asked to cancel, it did
        # not vanish) - same invalidate-before-terminal-error rule as
        # every other failure that follows partial content, so it
        # still gets a normal in-band error record, never silence.
        if sent_text_holder[0]:
            yield _serialize_ndjson_record(_discard_record())

        yield _serialize_ndjson_record(
            _error_record(
                code="stream_cancelled",
                message="The request was cancelled.",
                retryable=False,
            )
        )

        _log_stream_metric(
            _stream_metric_payload(
                request_id=request_id,
                t0=t0,
                outcome="stream_cancelled",
                error_code="explicit_cancel",
                timings=timings,
                metrics=metrics_holder[0],
                first_fastapi_delta_at=first_fastapi_delta_at,
            )
        )
        _deregister_active_stream(request_id)
        return
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
            _deregister_active_stream(request_id)
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
        _deregister_active_stream(request_id)
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
    _deregister_active_stream(request_id)


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
    http_request: Request = None,
    x_request_id: str | None = Header(default=None, alias="X-Request-ID"),
) -> StreamingResponse:
    """NDJSON streaming counterpart to POST /api/v1/chat - see module
    docstring for the full architecture.

    `http_request` (GATE S9-LITE) is the raw ASGI Request, needed only
    for `_wait_for_disconnect`'s `is_disconnected()` polling during the
    pre-stream race - `request` (the parsed LegalChatRequest body)
    keeps every one of its existing meanings/references unchanged.
    Annotated as bare `Request` (not `Request | None`) with a plain
    `None` default deliberately - this exact pinned FastAPI version
    rejects `Request | None` as an invalid field type at route-
    registration time (verified empirically), while a bare `Request`
    annotation is still recognized as FastAPI's special inject-the-
    real-Request parameter regardless of its default value, so real
    HTTP traffic always receives the actual Request unchanged. The
    default only ever applies to a direct/unit-test call bypassing
    FastAPI's own routing, so every pre-existing test call site keeps
    working unchanged; see _wait_for_disconnect's own None handling."""

    t0 = perf_counter()
    deadline_at = t0 + STREAM_REQUEST_DEADLINE_SECONDS
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

    # GATE S9-LITE: the concurrent.futures.Future bridging into
    # _consume()'s own Task on this loop - see _cancel_bridge_work's
    # own docstring for why this (not pipeline_task) is the one handle
    # that can actually interrupt in-flight generation/repair work.
    # Set BEFORE _GENERATION_STARTING is ever emitted (see _bridge
    # below), so any code that reacts to that signal can always find
    # it already populated here - never a race.
    consume_future_holder: list[concurrent.futures.Future | None] = [None]

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

        # GATE S9-LITE: _GENERATION_STARTING must still be the FIRST
        # thing scheduled onto event_queue here - loop.call_soon_
        # threadsafe callbacks run in FIFO order, which is the ONLY
        # reason _drain_stream_events is guaranteed to observe this
        # sentinel before any of _consume()'s own real StreamAnswerEvent
        # objects (queued below via a same-loop put_nowait once
        # _consume() actually starts running). An earlier version of
        # this gate's fix moved this emit to AFTER creating
        # consume_future/populating consume_future_holder, intending to
        # close a different, narrower race (see below) - that
        # reordering broke this ordering guarantee instead, and was
        # caught by this module's own DisconnectCancellationTests
        # (a real StreamAnswerEvent reaching legal_chat_stream's
        # pre-stream race in place of the sentinel, then _drain_stream_
        # events later receiving the sentinel itself as if it were a
        # StreamAnswerEvent - an assertion failure). Reverted.
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

        # GATE S9-LITE: the concurrent.futures.Future bridging into
        # _consume()'s own Task is published to consume_future_holder
        # immediately once created - this is the one change from the
        # future's original discard-immediately shape, and is what
        # lets _cancel_bridge_work actually reach and interrupt real
        # generation work (see that function's own docstring). There
        # is a narrow, disclosed residual race: consume_future_holder[0]
        # is still None for the brief window between _GENERATION_
        # STARTING being observed on the main loop and this line
        # actually running on the worker thread - a disconnect/deadline
        # landing in exactly that window cancels only pipeline_task
        # (no effect, per the same docstring) and is not retried, so
        # cancellation in that one narrow case is missed rather than
        # merely delayed. Accepted as the smaller, disclosed risk
        # against the alternative (see the comment above this
        # function) of reordering this ahead of _GENERATION_STARTING,
        # which is a real, reproduced correctness bug, not just a race.
        consume_future = asyncio.run_coroutine_threadsafe(
            _consume(), loop,
        )
        consume_future_holder[0] = consume_future

        return consume_future.result()

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

    # Eagerly wait for the FIRST signal - "generation is starting",
    # "the whole pipeline is already done", the whole-request deadline
    # (GATE S6D), or (GATE S9-LITE) the client disconnecting before
    # any of those - BEFORE constructing StreamingResponse, so a pre-
    # generation failure preserves today's exact HTTP status (mission
    # section 7/8). Once a StreamingResponse exists, Starlette's own
    # machinery detects a disconnect automatically (verified
    # empirically - see _drain_stream_events's own
    # except asyncio.CancelledError and the S9-LITE report); before
    # that point nothing does unless this race explicitly includes it,
    # since understanding/retrieval/preparation runs as a plain
    # (non-streaming) coroutine await, not a StreamingResponse body.
    get_signal_task = asyncio.ensure_future(
        _get_with_deadline(event_queue, deadline_at)
    )
    disconnect_task = asyncio.ensure_future(
        _wait_for_disconnect(http_request)
    )

    done, pending = await asyncio.wait(
        {get_signal_task, disconnect_task},
        return_when=asyncio.FIRST_COMPLETED,
    )

    for pending_task in pending:
        pending_task.cancel()
        try:
            await pending_task
        except (Exception, asyncio.CancelledError):
            pass

    if disconnect_task in done:
        # The client is already gone - cancelling pipeline_task alone
        # cannot interrupt work already running synchronously on the
        # worker thread (a fundamental Python limitation - see
        # _cancel_bridge_work's own docstring), so this bounds rather
        # than instantly stops that phase; it DOES stop generation
        # promptly if the disconnect happened to land after _bridge
        # had already started (consume_future_holder set).
        _cancel_bridge_work(pipeline_task, consume_future_holder)
        try:
            await pipeline_task
        except (Exception, asyncio.CancelledError):
            pass

        _log_stream_metric(
            _stream_metric_payload(
                request_id=request_id,
                t0=t0,
                outcome="client_disconnected",
                error_code="client_disconnected",
            )
        )
        raise HTTPException(
            status_code=499,
            detail="The client disconnected before the response was ready.",
            headers={"X-Request-ID": request_id},
        )

    try:
        first_signal = get_signal_task.result()
    except asyncio.TimeoutError:
        _cancel_bridge_work(pipeline_task, consume_future_holder)
        try:
            await pipeline_task
        except (Exception, asyncio.CancelledError):
            pass

        _log_stream_metric(
            _stream_metric_payload(
                request_id=request_id,
                t0=t0,
                outcome="pre_stream_failure",
                error_code="stream_request_timeout",
            )
        )
        raise HTTPException(
            status_code=504,
            detail="The legal assistant did not respond in time.",
            headers={"X-Request-ID": request_id},
        ) from None

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
            consume_future_holder=consume_future_holder,
            request_id=request_id,
            request=request,
            timings=timings,
            sent_text_holder=sent_text_holder,
            metrics_holder=metrics_holder,
            t0=t0,
            deadline_at=deadline_at,
        ),
        media_type=NDJSON_MEDIA_TYPE,
        headers=_streaming_response_headers(request_id),
    )


class CancelChatStreamRequest(BaseModel):
    """
    GATE S9B: the ONLY field this endpoint accepts - the request_id a
    client learned from a /chat/stream response's own `start` record.
    No prompts/answers/other request content is ever accepted or
    stored here.
    """

    request_id: str = Field(min_length=1, max_length=200)

    class Config:
        extra = "forbid"


@router.post("/chat/stream/cancel")
async def cancel_chat_stream(
    payload: CancelChatStreamRequest,
) -> dict[str, bool]:
    """
    GATE S9B: explicit cancellation, independent of the passive
    browser-disconnect detection GATE S9-LITE found unreliable under
    real Apache/mod_php. Protected by the same ApiProtectionMiddleware
    as every other /api/v1 route (path-prefix based - no route-
    specific change needed there). Always returns 200 with
    {"cancelled": bool} - an unknown/already-finished request_id is a
    normal, safe outcome (cancelled: false), never an error status;
    there is nothing here for a client to distinguish "already done"
    from "never existed" for, and no reason to let it.
    """

    return {"cancelled": cancel_active_stream(payload.request_id)}
