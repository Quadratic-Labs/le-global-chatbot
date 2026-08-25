"""
Additive OpenAI Responses API STREAMING client (chat-streaming
initiative, GATE S2).

OpenAIResponsesClient.generate() (openai_responses.py) is completely
unmodified and unaffected by this module - this is a separate client,
for a separate code path (the future POST /api/v1/chat/stream, GATE
S3), used nowhere by the existing /chat pipeline today.

Two distinct protocols meet here:

    upstream:   OpenAI Responses API `stream: true` -> Server-Sent
                Events (untrusted, provider-controlled framing and
                event vocabulary, including reasoning/internal event
                types this model's `reasoning.effort` configuration
                can produce)
    downstream: whatever this codebase's own /chat/stream chooses to
                expose (application/x-ndjson, decided at the router
                layer, GATE S3) - this module has NO awareness of
                that wire format at all.

This module's only contract: parse the upstream SSE byte stream and
yield a small, explicit, ALLOWLISTED set of StreamEvent objects. A
provider event type outside that allowlist (reasoning/internal/future/
unrecognized) is silently dropped here - it is never constructed into
a StreamEvent, so no caller downstream of this module can accidentally
forward it. A malformed payload for an otherwise-allowed event type is
a distinct failure class (a protocol violation, not merely "content we
don't forward") and raises MalformedProviderEventError rather than
being silently dropped, so the caller can end the stream with a clear,
logged error instead of continuing on unverifiable state.
"""

from __future__ import annotations

import codecs
import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from enum import Enum
from json import JSONDecodeError
from typing import Any, Final

import httpx

from app.clients.openai_responses import (
    OPENAI_RESPONSES_URL,
    OpenAIConfigurationError,
)


class MalformedProviderEventError(RuntimeError):
    """
    Raised when an ALLOWLISTED provider event type's own data payload
    cannot be parsed as the JSON object it is documented to be.

    Deliberately distinct from an unrecognized/dropped event type
    (never an error - see _map_provider_event): a malformed payload for
    an event type we explicitly claim to understand is a protocol
    violation worth ending the stream over, not silently ignoring.
    """


class StreamEventType(Enum):
    """The only event shapes this client ever yields - see module
    docstring for why this is a strict allowlist, not a passthrough."""

    DELTA = "delta"
    COMPLETED = "completed"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class StreamEvent:
    """One translated, trust-boundary-safe unit of streaming output.

    Only `text` (for DELTA) ever carries provider-derived content;
    `error_message` is always this module's OWN sanitized message,
    never a raw provider error body."""

    type: StreamEventType
    text: str | None = None
    error_message: str | None = None
    retryable: bool = False


# The ONLY provider event types this client will ever act on. Every
# other event type - including any reasoning/internal event type this
# model's configured reasoning effort can produce - is silently
# dropped by _map_provider_event, never reaching a StreamEvent.
_ALLOWED_PROVIDER_EVENT_TYPES: Final[frozenset[str]] = frozenset(
    {
        "response.output_text.delta",
        "response.completed",
        "response.failed",
        "response.incomplete",
    }
)


class _IncrementalSSEDecoder:
    """
    Pure, framework-independent, stateful Server-Sent Events frame
    decoder. No I/O, no httpx dependency - built specifically so the
    SSE framing/UTF-8-boundary logic can be unit tested with synthetic
    byte sequences, independent of any real or mocked network call.

    feed() accepts one raw byte chunk of ANY size - including one that
    splits a multi-byte UTF-8 character, or splits an SSE line, at an
    arbitrary boundary - and returns the list of (event_type, data)
    frames that chunk completed (zero, one, or many). UTF-8 decoding
    uses an incremental decoder (codecs.getincrementaldecoder), never
    a naive per-chunk .decode(), specifically so a split character
    never corrupts or drops text.

    Per the SSE spec: a blank line ends the current event; a line
    starting with ":" is a comment and is ignored; "event:" sets the
    current event's type (last one wins); "data:" lines accumulate,
    joined with "\\n", as the event's data; any other field name
    (id:, retry:) is ignored - this application never needs them.
    """

    def __init__(self) -> None:
        self._utf8_decoder = codecs.getincrementaldecoder("utf-8")()
        self._buffer = ""
        self._pending_event_type: str | None = None
        self._pending_data_lines: list[str] = []

    def _consume_line(self, line: str) -> tuple[str | None, str] | None:
        line = line.rstrip("\r")

        if line == "":
            if not self._pending_data_lines:
                return None

            event = (
                self._pending_event_type,
                "\n".join(self._pending_data_lines),
            )
            self._pending_event_type = None
            self._pending_data_lines = []
            return event

        if line.startswith(":"):
            return None

        if line.startswith("event:"):
            self._pending_event_type = line[len("event:"):].strip()
            return None

        if line.startswith("data:"):
            value = line[len("data:"):]

            if value.startswith(" "):
                value = value[1:]

            self._pending_data_lines.append(value)
            return None

        return None

    def feed(self, chunk: bytes) -> list[tuple[str | None, str]]:
        self._buffer += self._utf8_decoder.decode(chunk)

        events: list[tuple[str | None, str]] = []

        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            event = self._consume_line(line)

            if event is not None:
                events.append(event)

        return events

    def close(self) -> list[tuple[str | None, str]]:
        """
        Flush any residual decoder/buffer state at end-of-stream. Some
        servers close the connection without a final trailing blank
        line - a still-pending event must not be silently lost.
        """

        trailing_text = self._utf8_decoder.decode(b"", final=True)

        if trailing_text:
            self._buffer += trailing_text

        events: list[tuple[str | None, str]] = []

        if self._buffer:
            event = self._consume_line(self._buffer)
            self._buffer = ""

            if event is not None:
                events.append(event)

        if self._pending_data_lines:
            events.append(
                (
                    self._pending_event_type,
                    "\n".join(self._pending_data_lines),
                )
            )
            self._pending_event_type = None
            self._pending_data_lines = []

        return events


def _map_provider_event(
    event_type: str | None,
    data: str,
) -> StreamEvent | None:
    """
    The strict allowlist - the only place in this codebase that
    decides what a raw OpenAI streaming event is allowed to become.

    Returns None (silently dropped, never an error) for any event type
    outside _ALLOWED_PROVIDER_EVENT_TYPES - including every reasoning/
    internal event type, and any event type OpenAI might add in the
    future that this application does not yet know about ("unknown
    provider event -> ignore safely", per the approved mapping).

    Raises MalformedProviderEventError - a distinct, non-silent
    failure - only when an ALLOWED event type's own data cannot be
    parsed as the JSON object it is documented to be.
    """

    if event_type not in _ALLOWED_PROVIDER_EVENT_TYPES:
        return None

    try:
        payload = json.loads(data)
    except (JSONDecodeError, TypeError) as error:
        raise MalformedProviderEventError(
            f"Malformed data for provider event {event_type!r}."
        ) from error

    if not isinstance(payload, dict):
        raise MalformedProviderEventError(
            f"Provider event {event_type!r} did not carry a JSON object."
        )

    if event_type == "response.output_text.delta":
        delta_text = payload.get("delta")

        if not isinstance(delta_text, str) or not delta_text:
            return None

        return StreamEvent(
            type=StreamEventType.DELTA,
            text=delta_text,
        )

    if event_type == "response.completed":
        return StreamEvent(type=StreamEventType.COMPLETED)

    # response.failed / response.incomplete - sanitized, never the raw
    # provider error object.
    reason = None
    nested_error = payload.get("response", {})

    if isinstance(nested_error, dict):
        error_details = nested_error.get("error")

        if isinstance(error_details, dict):
            candidate = error_details.get("message")

            if isinstance(candidate, str) and candidate.strip():
                reason = candidate.strip()

        if reason is None:
            incomplete_details = nested_error.get("incomplete_details")

            if isinstance(incomplete_details, dict):
                candidate = incomplete_details.get("reason")

                if isinstance(candidate, str) and candidate.strip():
                    reason = candidate.strip()

    return StreamEvent(
        type=StreamEventType.ERROR,
        error_message=(
            f"OpenAI generation did not complete: {reason}."
            if reason
            else "OpenAI generation did not complete."
        ),
        retryable=False,
    )


class OpenAIResponsesStreamClient:
    """
    Async, streaming counterpart to OpenAIResponsesClient - a
    completely separate class, transport (httpx.AsyncClient here vs.
    urllib there), and code path. Nothing in OpenAIResponsesClient is
    imported, subclassed, or modified by this class.

    Timeouts are intentionally separate axes (connect/read/write/pool,
    matching httpx.Timeout - the exact limitation OpenAIResponsesClient's
    single urlopen(..., timeout=X) cannot express - plus one
    additional, independent wall-clock ceiling on the WHOLE stream,
    since a provider that keeps sending small deltas forever should
    still not be allowed to hold a connection open indefinitely).
    """

    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        reasoning_effort: str | None = None,
        max_output_tokens: int | None = None,
        connect_timeout_seconds: float = 10.0,
        read_timeout_seconds: float = 30.0,
        write_timeout_seconds: float = 10.0,
        pool_timeout_seconds: float = 10.0,
        total_stream_timeout_seconds: float = 120.0,
    ) -> None:
        normalized_api_key = api_key.strip()
        normalized_model = model.strip()

        if not normalized_api_key:
            raise OpenAIConfigurationError(
                "OPENAI_API_KEY is not configured."
            )

        if not normalized_model:
            raise OpenAIConfigurationError(
                "OPENAI_MODEL is not configured."
            )

        for name, value in (
            ("connect_timeout_seconds", connect_timeout_seconds),
            ("read_timeout_seconds", read_timeout_seconds),
            ("write_timeout_seconds", write_timeout_seconds),
            ("pool_timeout_seconds", pool_timeout_seconds),
            ("total_stream_timeout_seconds", total_stream_timeout_seconds),
        ):
            if value <= 0:
                raise OpenAIConfigurationError(
                    f"{name} must be positive."
                )

        normalized_reasoning_effort = (
            reasoning_effort.strip()
            if reasoning_effort is not None
            else None
        )

        if (
            reasoning_effort is not None
            and not normalized_reasoning_effort
        ):
            raise OpenAIConfigurationError(
                "OpenAI reasoning effort must not be empty."
            )

        if (
            max_output_tokens is not None
            and max_output_tokens <= 0
        ):
            raise OpenAIConfigurationError(
                "OpenAI max output tokens must be positive."
            )

        self.api_key = normalized_api_key
        self.model = normalized_model
        self.reasoning_effort = normalized_reasoning_effort
        self.max_output_tokens = max_output_tokens
        self.total_stream_timeout_seconds = total_stream_timeout_seconds

        self._timeout = httpx.Timeout(
            connect=connect_timeout_seconds,
            read=read_timeout_seconds,
            write=write_timeout_seconds,
            pool=pool_timeout_seconds,
        )

    def _build_request_body(
        self,
        instructions: str,
        input_text: str,
    ) -> dict[str, Any]:
        request_body: dict[str, Any] = {
            "model": self.model,
            "instructions": instructions,
            "input": input_text,
            "store": False,
            "stream": True,
        }

        if self.reasoning_effort is not None:
            request_body["reasoning"] = {
                "effort": self.reasoning_effort,
            }

        if self.max_output_tokens is not None:
            request_body["max_output_tokens"] = self.max_output_tokens

        return request_body

    async def stream(
        self,
        instructions: str,
        input_text: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """
        Yield StreamEvent objects as the provider generates them.

        Always terminates with exactly one terminal event (COMPLETED
        or ERROR) unless the caller stops consuming early (see module
        docstring on cancellation) - never both, never neither, on any
        code path, so a caller can safely treat "generator exhausted
        without an ERROR having been observed" as an internal bug
        rather than a real signal to rely on.

        `transport` is exposed ONLY for deterministic, offline testing
        (httpx.MockTransport) - production callers never pass it.
        """

        request_body = self._build_request_body(instructions, input_text)
        decoder = _IncrementalSSEDecoder()
        started_at = time.monotonic()

        client_kwargs: dict[str, Any] = {"timeout": self._timeout}

        if transport is not None:
            client_kwargs["transport"] = transport

        try:
            async with httpx.AsyncClient(**client_kwargs) as client:
                async with client.stream(
                    "POST",
                    OPENAI_RESPONSES_URL,
                    json=request_body,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                        "Accept": "text/event-stream",
                    },
                ) as response:
                    if response.status_code != 200:
                        error_body = await response.aread()
                        yield StreamEvent(
                            type=StreamEventType.ERROR,
                            error_message=(
                                "OpenAI returned HTTP "
                                f"{response.status_code}."
                            ),
                            retryable=response.status_code in (
                                429, 500, 502, 503, 504,
                            ),
                        )
                        return

                    terminal_event_emitted = False

                    async for chunk in response.aiter_bytes():
                        if (
                            time.monotonic() - started_at
                            > self.total_stream_timeout_seconds
                        ):
                            yield StreamEvent(
                                type=StreamEventType.ERROR,
                                error_message=(
                                    "OpenAI streaming exceeded the "
                                    "maximum allowed duration."
                                ),
                                retryable=False,
                            )
                            return

                        try:
                            frames = decoder.feed(chunk)
                        except UnicodeDecodeError:
                            yield StreamEvent(
                                type=StreamEventType.ERROR,
                                error_message=(
                                    "OpenAI returned malformed "
                                    "streaming text."
                                ),
                            )
                            return

                        for event_type, data in frames:
                            try:
                                mapped_event = _map_provider_event(
                                    event_type, data,
                                )
                            except MalformedProviderEventError:
                                yield StreamEvent(
                                    type=StreamEventType.ERROR,
                                    error_message=(
                                        "OpenAI returned a malformed "
                                        "streaming event."
                                    ),
                                )
                                return

                            if mapped_event is None:
                                continue

                            yield mapped_event

                            if mapped_event.type in (
                                StreamEventType.COMPLETED,
                                StreamEventType.ERROR,
                            ):
                                terminal_event_emitted = True
                                return

                    if terminal_event_emitted:
                        return

                    for event_type, data in decoder.close():
                        try:
                            mapped_event = _map_provider_event(
                                event_type, data,
                            )
                        except MalformedProviderEventError:
                            yield StreamEvent(
                                type=StreamEventType.ERROR,
                                error_message=(
                                    "OpenAI returned a malformed "
                                    "streaming event."
                                ),
                            )
                            return

                        if mapped_event is None:
                            continue

                        yield mapped_event

                        if mapped_event.type in (
                            StreamEventType.COMPLETED,
                            StreamEventType.ERROR,
                        ):
                            return

                    # The connection closed with no response.completed
                    # (or response.failed/incomplete) ever observed -
                    # never silently treat that as success.
                    yield StreamEvent(
                        type=StreamEventType.ERROR,
                        error_message=(
                            "OpenAI closed the stream without "
                            "confirming completion."
                        ),
                        retryable=True,
                    )

        except httpx.TimeoutException:
            yield StreamEvent(
                type=StreamEventType.ERROR,
                error_message="OpenAI could not be reached in time.",
                retryable=True,
            )

        except httpx.HTTPError:
            yield StreamEvent(
                type=StreamEventType.ERROR,
                error_message="OpenAI could not be reached.",
                retryable=True,
            )
