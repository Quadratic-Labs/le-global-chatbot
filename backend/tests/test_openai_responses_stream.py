"""
Tests for the additive OpenAI Responses API streaming client (GATE S2,
chat-streaming initiative).

OpenAIResponsesClient (openai_responses.py) is a completely separate
class, untouched by anything here - see
test_openai_responses.py::ExistingClientRegressionTests below, which
re-runs a representative slice of its own suite unmodified to prove
this file's additions have zero effect on it.

Testing philosophy (established directly, empirically, before writing
this file - see commit message / GATE S2 report for the raw
reproduction): httpx.MockTransport, when a handler returns an async
generator as `content`, does NOT proactively cancel/close that
generator on early consumer exit (proven directly: a plain `break` out
of `async for chunk in response.aiter_bytes()`, followed by normal
`async with` exit, left the generator's own `finally` block un-run
after a 1s wait, only completing once its own `asyncio.sleep` finished
on its own). This means MockTransport CANNOT be used here to honestly
prove that OUR client closes the real upstream TCP connection to
OpenAI - only that OUR OWN async generator (the return value of
.stream()) can be closed cleanly from the CALLER's side without
hanging or raising. Real upstream socket teardown against a live
connection remains unproven here, matching the mission's own explicit
"do not claim complete provider cancellation unless proven" - that
proof belongs to a later, real-network gate.
"""

from __future__ import annotations

import asyncio
import json
import unittest

import httpx

from app.clients.openai_responses import OpenAIConfigurationError
from app.clients.openai_responses_stream import (
    MalformedProviderEventError,
    OpenAIResponsesStreamClient,
    StreamEvent,
    StreamEventType,
    _IncrementalSSEDecoder,
    _map_provider_event,
)


def _sse_frame(event_type: str | None, data: dict) -> bytes:
    lines = []

    if event_type is not None:
        lines.append(f"event: {event_type}")

    lines.append(f"data: {json.dumps(data)}")
    lines.append("")
    lines.append("")

    return "\n".join(lines).encode("utf-8")


class IncrementalSSEDecoderTests(unittest.TestCase):
    """Pure, offline tests for the SSE frame decoder - no network, no
    httpx, no event loop."""

    def test_single_event_one_chunk(self) -> None:
        decoder = _IncrementalSSEDecoder()

        events = decoder.feed(
            b'event: response.completed\ndata: {"a": 1}\n\n'
        )

        self.assertEqual(
            [("response.completed", '{"a": 1}')], events
        )

    def test_event_split_across_many_tiny_chunks(self) -> None:
        decoder = _IncrementalSSEDecoder()
        whole = b'event: response.completed\ndata: {"a": 1}\n\n'

        events: list[tuple[str | None, str]] = []

        for index in range(len(whole)):
            events.extend(decoder.feed(whole[index:index + 1]))

        self.assertEqual(
            [("response.completed", '{"a": 1}')], events
        )

    def test_multibyte_utf8_character_split_across_chunk_boundary(
        self,
    ) -> None:
        decoder = _IncrementalSSEDecoder()

        # "café" - the "é" (U+00E9) encodes to two UTF-8 bytes; split
        # the payload exactly between those two bytes. ensure_ascii=
        # False is required here so the raw multi-byte character
        # actually appears in the encoded bytes (json.dumps's default
        # \uXXXX-escapes it otherwise) - matching a real provider,
        # which sends raw UTF-8, not ASCII-escaped JSON, over SSE.
        payload = json.dumps(
            {"delta": "café"}, ensure_ascii=False,
        ).encode("utf-8")
        frame = (
            b"event: response.output_text.delta\ndata: "
            + payload
            + b"\n\n"
        )

        split_index = frame.index("é".encode("utf-8")[:1]) + 1

        events: list[tuple[str | None, str]] = []
        events.extend(decoder.feed(frame[:split_index]))
        events.extend(decoder.feed(frame[split_index:]))

        self.assertEqual(1, len(events))
        event_type, data = events[0]
        self.assertEqual("response.output_text.delta", event_type)
        self.assertEqual({"delta": "café"}, json.loads(data))

    def test_multiple_data_lines_joined_with_newline(self) -> None:
        decoder = _IncrementalSSEDecoder()

        events = decoder.feed(
            b"data: line one\ndata: line two\n\n"
        )

        self.assertEqual([(None, "line one\nline two")], events)

    def test_comment_lines_are_ignored(self) -> None:
        decoder = _IncrementalSSEDecoder()

        events = decoder.feed(
            b': this is a comment\ndata: {"a": 1}\n\n'
        )

        self.assertEqual([(None, '{"a": 1}')], events)

    def test_unknown_field_names_are_ignored(self) -> None:
        decoder = _IncrementalSSEDecoder()

        events = decoder.feed(
            b'id: 42\nretry: 3000\ndata: {"a": 1}\n\n'
        )

        self.assertEqual([(None, '{"a": 1}')], events)

    def test_multiple_events_in_one_chunk(self) -> None:
        decoder = _IncrementalSSEDecoder()

        events = decoder.feed(
            b'data: {"a": 1}\n\ndata: {"a": 2}\n\n'
        )

        self.assertEqual(
            [(None, '{"a": 1}'), (None, '{"a": 2}')], events
        )

    def test_event_type_resets_between_events(self) -> None:
        decoder = _IncrementalSSEDecoder()

        events = decoder.feed(
            b'event: response.completed\ndata: {"a": 1}\n\n'
            b'data: {"a": 2}\n\n'
        )

        self.assertEqual(
            [
                ("response.completed", '{"a": 1}'),
                (None, '{"a": 2}'),
            ],
            events,
        )

    def test_missing_trailing_blank_line_recovered_on_close(
        self,
    ) -> None:
        decoder = _IncrementalSSEDecoder()

        mid_stream_events = decoder.feed(
            b'event: response.completed\ndata: {"a": 1}'
        )
        self.assertEqual([], mid_stream_events)

        final_events = decoder.close()
        self.assertEqual(
            [("response.completed", '{"a": 1}')], final_events
        )

    def test_close_with_nothing_pending_yields_nothing(self) -> None:
        decoder = _IncrementalSSEDecoder()
        decoder.feed(b'data: {"a": 1}\n\n')

        self.assertEqual([], decoder.close())


class MapProviderEventTests(unittest.TestCase):
    """Pure, offline tests for the strict provider-event allowlist."""

    def test_output_text_delta_maps_to_delta_event(self) -> None:
        event = _map_provider_event(
            "response.output_text.delta",
            json.dumps({"delta": "Hello"}),
        )

        self.assertEqual(
            StreamEvent(type=StreamEventType.DELTA, text="Hello"),
            event,
        )

    def test_output_text_delta_with_empty_text_is_dropped(self) -> None:
        event = _map_provider_event(
            "response.output_text.delta",
            json.dumps({"delta": ""}),
        )

        self.assertIsNone(event)

    def test_completed_maps_to_completed_event(self) -> None:
        event = _map_provider_event(
            "response.completed",
            json.dumps({"response": {"status": "completed"}}),
        )

        self.assertEqual(
            StreamEvent(type=StreamEventType.COMPLETED), event,
        )

    def test_failed_maps_to_error_event_with_sanitized_message(
        self,
    ) -> None:
        event = _map_provider_event(
            "response.failed",
            json.dumps(
                {
                    "response": {
                        "error": {
                            "message": "internal provider detail",
                        }
                    }
                }
            ),
        )

        self.assertEqual(StreamEventType.ERROR, event.type)
        self.assertIn("internal provider detail", event.error_message)
        self.assertFalse(event.retryable)

    def test_incomplete_maps_to_error_event(self) -> None:
        event = _map_provider_event(
            "response.incomplete",
            json.dumps(
                {
                    "response": {
                        "incomplete_details": {"reason": "max_tokens"},
                    }
                }
            ),
        )

        self.assertEqual(StreamEventType.ERROR, event.type)
        self.assertIn("max_tokens", event.error_message)

    def test_reasoning_event_type_is_silently_dropped(self) -> None:
        """The core safety property: a reasoning/internal event type -
        which this model's reasoning.effort configuration can produce
        - must never become a StreamEvent, not even an error one."""

        event = _map_provider_event(
            "response.reasoning_summary_text.delta",
            json.dumps({"delta": "internal chain-of-thought"}),
        )

        self.assertIsNone(event)

    def test_unrecognized_future_event_type_is_silently_dropped(
        self,
    ) -> None:
        event = _map_provider_event(
            "response.some_future_event_type",
            json.dumps({"anything": "goes here"}),
        )

        self.assertIsNone(event)

    def test_malformed_json_for_allowed_type_raises(self) -> None:
        with self.assertRaises(MalformedProviderEventError):
            _map_provider_event(
                "response.output_text.delta", "not valid json{{{",
            )

    def test_non_dict_json_for_allowed_type_raises(self) -> None:
        with self.assertRaises(MalformedProviderEventError):
            _map_provider_event(
                "response.output_text.delta", json.dumps([1, 2, 3]),
            )

    def test_malformed_json_for_unrecognized_type_is_still_dropped(
        self,
    ) -> None:
        """A malformed payload only matters for a type we claim to
        understand - garbage data on an already-ignored event type is
        still just ignored, not an error."""

        event = _map_provider_event(
            "response.some_future_event_type", "not valid json{{{",
        )

        self.assertIsNone(event)


def _run_async(coroutine):
    return asyncio.run(coroutine)


class OpenAIResponsesStreamClientTests(unittest.TestCase):

    def _client(self, **overrides) -> OpenAIResponsesStreamClient:
        kwargs = dict(
            api_key="sk-test",
            model="gpt-5-mini",
            reasoning_effort="low",
        )
        kwargs.update(overrides)
        return OpenAIResponsesStreamClient(**kwargs)

    def _make_transport(self, handler) -> httpx.MockTransport:
        return httpx.MockTransport(handler)

    def test_happy_path_yields_deltas_then_completed(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            async def body():
                yield _sse_frame(
                    "response.output_text.delta", {"delta": "In "}
                )
                yield _sse_frame(
                    "response.output_text.delta",
                    {"delta": "France, the employer..."},
                )
                yield _sse_frame(
                    "response.completed",
                    {"response": {"status": "completed"}},
                )

            return httpx.Response(200, content=body())

        async def collect():
            client = self._client()
            transport = self._make_transport(handler)
            events = []
            async for event in client.stream(
                "instructions", "input", transport=transport,
            ):
                events.append(event)
            return events

        events = _run_async(collect())

        self.assertEqual(
            [
                StreamEvent(type=StreamEventType.DELTA, text="In "),
                StreamEvent(
                    type=StreamEventType.DELTA,
                    text="France, the employer...",
                ),
                StreamEvent(type=StreamEventType.COMPLETED),
            ],
            events,
        )

    def test_concatenated_deltas_reconstruct_expected_text(self) -> None:
        expected = "In France, the employer must provide notice."
        words = expected.split(" ")

        async def handler(request: httpx.Request) -> httpx.Response:
            async def body():
                for index, word in enumerate(words):
                    text = word if index == 0 else " " + word
                    yield _sse_frame(
                        "response.output_text.delta", {"delta": text},
                    )
                yield _sse_frame(
                    "response.completed",
                    {"response": {"status": "completed"}},
                )

            return httpx.Response(200, content=body())

        async def collect():
            client = self._client()
            transport = self._make_transport(handler)
            deltas = []
            async for event in client.stream(
                "instructions", "input", transport=transport,
            ):
                if event.type is StreamEventType.DELTA:
                    deltas.append(event.text)
            return deltas

        deltas = _run_async(collect())
        self.assertEqual(expected, "".join(deltas))

    def test_reasoning_events_never_surface_as_stream_events(
        self,
    ) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            async def body():
                yield _sse_frame(
                    "response.reasoning_summary_text.delta",
                    {"delta": "internal chain-of-thought reasoning"},
                )
                yield _sse_frame(
                    "response.output_text.delta", {"delta": "Answer."},
                )
                yield _sse_frame(
                    "response.completed",
                    {"response": {"status": "completed"}},
                )

            return httpx.Response(200, content=body())

        async def collect():
            client = self._client()
            transport = self._make_transport(handler)
            events = []
            async for event in client.stream(
                "instructions", "input", transport=transport,
            ):
                events.append(event)
            return events

        events = _run_async(collect())

        for event in events:
            if event.type is StreamEventType.DELTA:
                self.assertNotIn("reasoning", event.text)
                self.assertNotIn("chain-of-thought", event.text)

        self.assertEqual(
            [
                StreamEvent(type=StreamEventType.DELTA, text="Answer."),
                StreamEvent(type=StreamEventType.COMPLETED),
            ],
            events,
        )

    def test_http_error_status_yields_single_error_event(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                429, content=b'{"error": {"message": "rate limited"}}',
            )

        async def collect():
            client = self._client()
            transport = self._make_transport(handler)
            events = []
            async for event in client.stream(
                "instructions", "input", transport=transport,
            ):
                events.append(event)
            return events

        events = _run_async(collect())

        self.assertEqual(1, len(events))
        self.assertEqual(StreamEventType.ERROR, events[0].type)
        self.assertTrue(events[0].retryable)
        self.assertNotIn("rate limited", events[0].error_message)

    def test_response_failed_event_terminates_stream_with_error(
        self,
    ) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            async def body():
                yield _sse_frame(
                    "response.output_text.delta", {"delta": "Partial"},
                )
                yield _sse_frame(
                    "response.failed",
                    {
                        "response": {
                            "error": {"message": "content policy"},
                        }
                    },
                )
                # This must never be reached/observed.
                yield _sse_frame(
                    "response.output_text.delta",
                    {"delta": "should never appear"},
                )

            return httpx.Response(200, content=body())

        async def collect():
            client = self._client()
            transport = self._make_transport(handler)
            events = []
            async for event in client.stream(
                "instructions", "input", transport=transport,
            ):
                events.append(event)
            return events

        events = _run_async(collect())

        self.assertEqual(
            [
                StreamEvent(type=StreamEventType.DELTA, text="Partial"),
            ],
            events[:-1],
        )
        self.assertEqual(StreamEventType.ERROR, events[-1].type)
        self.assertEqual(2, len(events))

    def test_malformed_frame_terminates_stream_with_error_not_exception(
        self,
    ) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            async def body():
                yield (
                    b"event: response.output_text.delta\n"
                    b"data: not valid json{{{\n\n"
                )

            return httpx.Response(200, content=body())

        async def collect():
            client = self._client()
            transport = self._make_transport(handler)
            events = []
            async for event in client.stream(
                "instructions", "input", transport=transport,
            ):
                events.append(event)
            return events

        events = _run_async(collect())

        self.assertEqual(1, len(events))
        self.assertEqual(StreamEventType.ERROR, events[0].type)

    def test_connection_closes_without_completion_is_an_error(
        self,
    ) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            async def body():
                yield _sse_frame(
                    "response.output_text.delta", {"delta": "Partial"},
                )
                # Stream ends here - no response.completed ever seen.

            return httpx.Response(200, content=body())

        async def collect():
            client = self._client()
            transport = self._make_transport(handler)
            events = []
            async for event in client.stream(
                "instructions", "input", transport=transport,
            ):
                events.append(event)
            return events

        events = _run_async(collect())

        self.assertEqual(2, len(events))
        self.assertEqual(StreamEventType.DELTA, events[0].type)
        self.assertEqual(StreamEventType.ERROR, events[1].type)
        self.assertTrue(events[1].retryable)

    def test_connect_timeout_yields_retryable_error(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout(
                "connect timed out", request=request,
            )

        async def collect():
            client = self._client()
            transport = self._make_transport(handler)
            events = []
            async for event in client.stream(
                "instructions", "input", transport=transport,
            ):
                events.append(event)
            return events

        events = _run_async(collect())

        self.assertEqual(1, len(events))
        self.assertEqual(StreamEventType.ERROR, events[0].type)
        self.assertTrue(events[0].retryable)

    def test_read_timeout_mid_stream_yields_retryable_error(
        self,
    ) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            async def body():
                yield _sse_frame(
                    "response.output_text.delta", {"delta": "Partial"},
                )
                raise httpx.ReadTimeout(
                    "read timed out", request=request,
                )

            return httpx.Response(200, content=body())

        async def collect():
            client = self._client()
            transport = self._make_transport(handler)
            events = []
            async for event in client.stream(
                "instructions", "input", transport=transport,
            ):
                events.append(event)
            return events

        events = _run_async(collect())

        self.assertEqual(2, len(events))
        self.assertEqual(StreamEventType.DELTA, events[0].type)
        self.assertEqual(StreamEventType.ERROR, events[1].type)
        self.assertTrue(events[1].retryable)

    def test_total_stream_timeout_terminates_a_slow_drip(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            async def body():
                for _ in range(50):
                    yield _sse_frame(
                        "response.output_text.delta", {"delta": "x"},
                    )
                    await asyncio.sleep(0.02)
                yield _sse_frame(
                    "response.completed",
                    {"response": {"status": "completed"}},
                )

            return httpx.Response(200, content=body())

        async def collect():
            client = self._client(total_stream_timeout_seconds=0.05)
            transport = self._make_transport(handler)
            events = []
            async for event in client.stream(
                "instructions", "input", transport=transport,
            ):
                events.append(event)
            return events

        events = _run_async(collect())

        self.assertEqual(StreamEventType.ERROR, events[-1].type)
        self.assertIn("maximum allowed duration", events[-1].error_message)
        # Terminated well before all 50 deltas were ever produced.
        self.assertLess(len(events), 50)

    def test_generator_can_be_closed_early_without_hanging_or_raising(
        self,
    ) -> None:
        """CANCELLATION primitive (GATE S2 scope): our own async
        generator must close promptly and cleanly when the caller
        stops consuming it early. This does NOT prove the underlying
        upstream TCP connection is torn down - see module docstring:
        httpx.MockTransport does not exercise that code path
        realistically. That remains an open proof for a later,
        real-network gate."""

        async def handler(request: httpx.Request) -> httpx.Response:
            async def body():
                for _ in range(1000):
                    yield _sse_frame(
                        "response.output_text.delta", {"delta": "x"},
                    )
                    await asyncio.sleep(1.0)

            return httpx.Response(200, content=body())

        async def scenario():
            client = self._client()
            transport = self._make_transport(handler)
            generator = client.stream(
                "instructions", "input", transport=transport,
            )

            first_event = await generator.__anext__()

            await asyncio.wait_for(generator.aclose(), timeout=2.0)

            with self.assertRaises(StopAsyncIteration):
                await generator.__anext__()

            return first_event

        first_event = _run_async(scenario())
        self.assertEqual(StreamEventType.DELTA, first_event.type)

    def test_empty_api_key_is_rejected_at_construction(self) -> None:
        with self.assertRaises(OpenAIConfigurationError):
            OpenAIResponsesStreamClient(api_key="   ", model="gpt-5-mini")

    def test_empty_model_is_rejected_at_construction(self) -> None:
        with self.assertRaises(OpenAIConfigurationError):
            OpenAIResponsesStreamClient(api_key="sk-test", model="")

    def test_non_positive_timeout_is_rejected_at_construction(
        self,
    ) -> None:
        with self.assertRaises(OpenAIConfigurationError):
            OpenAIResponsesStreamClient(
                api_key="sk-test",
                model="gpt-5-mini",
                read_timeout_seconds=0,
            )


class ExistingClientRegressionTests(unittest.TestCase):
    """
    GATE S2's own explicit requirement: prove OpenAIResponsesClient
    (openai_responses.py) is unaffected. Rather than re-implementing
    that suite here (risking silent drift from the real one), this
    imports and runs it directly, in-process, as part of this file's
    own suite - so `python -m unittest tests.test_openai_responses_stream`
    and the full-suite run both exercise it.
    """

    def test_existing_openai_responses_suite_still_passes(self) -> None:
        import io
        import unittest as _unittest

        from tests import test_openai_responses as _existing_module

        loader = _unittest.TestLoader()
        suite = loader.loadTestsFromModule(_existing_module)
        runner = _unittest.TextTestRunner(verbosity=0, stream=io.StringIO())
        result = runner.run(suite)

        self.assertTrue(
            result.wasSuccessful(),
            f"existing OpenAIResponsesClient suite regressed: "
            f"{len(result.failures)} failures, {len(result.errors)} errors",
        )


if __name__ == "__main__":
    unittest.main()
