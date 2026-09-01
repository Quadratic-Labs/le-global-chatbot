"""Consolidated test module generated during test-suite rationalisation."""

from __future__ import annotations


# ====================================================================
# SOURCE: test_openai_responses.py
# ====================================================================

import io as _responses_io
import json as _responses_json
import unittest as _responses_unittest
import urllib.error as _responses_urllib_error
from pathlib import Path as _responses_Path
from typing import Any as _responses_Any
from unittest.mock import patch as _responses_patch
from app.clients.openai_responses import OpenAIConfigurationError as _responses_OpenAIConfigurationError, OpenAIResponseError as _responses_OpenAIResponseError, OpenAIResponsesClient as _responses_OpenAIResponsesClient, get_openai_answer_client as _responses_get_openai_answer_client, get_openai_rerank_client as _responses_get_openai_rerank_client, get_openai_understanding_client as _responses_get_openai_understanding_client
from app.core.config import Settings as _responses_Settings

def _responses_build_settings(**overrides: _responses_Any) -> _responses_Settings:
    """Build a Settings instance for tests, without reading real env vars."""
    defaults: dict[str, _responses_Any] = {'app_env': 'test', 'opensearch_url': 'https://opensearch:9200', 'opensearch_username': 'admin', 'opensearch_password': 'password', 'opensearch_verify_certs': False, 'redis_url': 'redis://localhost:6379/0', 'document_source_dir': _responses_Path('/tmp/source'), 'document_processed_dir': _responses_Path('/tmp/processed'), 'document_upload_max_bytes': 1000, 'openai_api_key': 'test-key', 'openai_model': 'test-model', 'openai_timeout_seconds': 60.0, 'openai_answer_reasoning_effort': 'low', 'openai_answer_max_output_tokens': 2000, 'openai_rerank_reasoning_effort': 'low', 'openai_rerank_max_output_tokens': 500, 'openai_understanding_reasoning_effort': 'low', 'openai_understanding_max_output_tokens': 1200, 'api_access_key': None, 'admin_api_key': None, 'cors_allowed_origins': (), 'rate_limit_requests': 60, 'rate_limit_window_seconds': 60, 'rerank_enabled': False, 'rerank_pool_multiplier': 3, 'rag_max_context_characters': 16000, 'rag_max_source_characters': 4000}
    defaults.update(overrides)
    return _responses_Settings(**defaults)

class _responses_FakeHTTPResponse:
    """Minimal stand-in for the object returned by urlopen()."""

    def __init__(self, payload: dict[str, _responses_Any]) -> None:
        self._body = _responses_json.dumps(payload).encode('utf-8')

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> '_FakeHTTPResponse':
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

def _responses_build_client(**kwargs: _responses_Any) -> _responses_OpenAIResponsesClient:
    return _responses_OpenAIResponsesClient(api_key='test-key', model='test-model', **kwargs)

def _responses_generate_and_capture_request(client: _responses_OpenAIResponsesClient, response_payload: dict[str, _responses_Any] | None=None) -> dict[str, _responses_Any]:
    """Call generate() against a fake transport and return the sent body."""
    captured: dict[str, _responses_Any] = {}

    def fake_urlopen(request: _responses_Any, timeout: float) -> _responses_FakeHTTPResponse:
        captured['body'] = _responses_json.loads(request.data.decode('utf-8'))
        return _responses_FakeHTTPResponse(response_payload or {'output_text': 'Answer.', 'model': 'test-model'})
    with _responses_patch('app.clients.openai_responses.urlopen', side_effect=fake_urlopen):
        client.generate(instructions='Instructions', input_text='Input')
    return captured['body']

class _responses_OpenAIResponsesClientTests(_responses_unittest.TestCase):
    """Tests for OpenAIResponsesClient.generate()."""

    def test_reasoning_effort_is_sent_when_configured(self) -> None:
        client = _responses_build_client(reasoning_effort='low', max_output_tokens=2000)
        body = _responses_generate_and_capture_request(client)
        self.assertEqual(body['reasoning'], {'effort': 'low'})

    def test_max_output_tokens_is_sent_when_configured(self) -> None:
        client = _responses_build_client(reasoning_effort='low', max_output_tokens=2000)
        body = _responses_generate_and_capture_request(client)
        self.assertEqual(body['max_output_tokens'], 2000)

    def test_reasoning_and_max_output_tokens_omitted_by_default(self) -> None:
        client = _responses_build_client()
        body = _responses_generate_and_capture_request(client)
        self.assertNotIn('reasoning', body)
        self.assertNotIn('max_output_tokens', body)

    def test_rejects_non_positive_max_output_tokens(self) -> None:
        with self.assertRaises(_responses_OpenAIConfigurationError):
            _responses_build_client(max_output_tokens=0)
        with self.assertRaises(_responses_OpenAIConfigurationError):
            _responses_build_client(max_output_tokens=-10)

    def test_rejects_blank_reasoning_effort(self) -> None:
        with self.assertRaises(_responses_OpenAIConfigurationError):
            _responses_build_client(reasoning_effort='   ')

    def test_incomplete_response_raises_error(self) -> None:
        client = _responses_build_client(reasoning_effort='low', max_output_tokens=2000)

        def fake_urlopen(request: _responses_Any, timeout: float) -> _responses_FakeHTTPResponse:
            return _responses_FakeHTTPResponse({'status': 'incomplete', 'incomplete_details': {'reason': 'max_output_tokens'}, 'model': 'test-model'})
        with _responses_patch('app.clients.openai_responses.urlopen', side_effect=fake_urlopen):
            with self.assertRaises(_responses_OpenAIResponseError):
                client.generate(instructions='Instructions', input_text='Input')

    def test_incomplete_response_without_reason_uses_placeholder(self) -> None:
        client = _responses_build_client()

        def fake_urlopen(request: _responses_Any, timeout: float) -> _responses_FakeHTTPResponse:
            return _responses_FakeHTTPResponse({'status': 'incomplete', 'model': 'test-model'})
        with _responses_patch('app.clients.openai_responses.urlopen', side_effect=fake_urlopen):
            with self.assertRaises(_responses_OpenAIResponseError) as context:
                client.generate(instructions='Instructions', input_text='Input')
        self.assertIn('unknown reason', str(context.exception))

class _responses_OpenAIClientFactoryTests(_responses_unittest.TestCase):
    """Tests for the separate answer/rerank client factories."""

    def test_answer_client_uses_answer_budget(self) -> None:
        settings = _responses_build_settings()
        with _responses_patch('app.clients.openai_responses.get_settings', return_value=settings):
            client = _responses_get_openai_answer_client()
        self.assertEqual(client.reasoning_effort, 'low')
        self.assertEqual(client.max_output_tokens, 2000)

    def test_rerank_client_uses_rerank_budget(self) -> None:
        settings = _responses_build_settings()
        with _responses_patch('app.clients.openai_responses.get_settings', return_value=settings):
            client = _responses_get_openai_rerank_client()
        self.assertEqual(client.reasoning_effort, 'low')
        self.assertEqual(client.max_output_tokens, 500)

    def test_answer_and_rerank_clients_are_independent(self) -> None:
        settings = _responses_build_settings(openai_answer_max_output_tokens=2500, openai_rerank_max_output_tokens=300)
        with _responses_patch('app.clients.openai_responses.get_settings', return_value=settings):
            answer_client = _responses_get_openai_answer_client()
            rerank_client = _responses_get_openai_rerank_client()
        self.assertEqual(answer_client.max_output_tokens, 2500)
        self.assertEqual(rerank_client.max_output_tokens, 300)

    def test_understanding_client_uses_understanding_budget(self) -> None:
        settings = _responses_build_settings(openai_understanding_reasoning_effort='minimal', openai_understanding_max_output_tokens=400)
        with _responses_patch('app.clients.openai_responses.get_settings', return_value=settings):
            client = _responses_get_openai_understanding_client()
        self.assertEqual(client.reasoning_effort, 'minimal')
        self.assertEqual(client.max_output_tokens, 400)

    def test_missing_api_key_raises_configuration_error(self) -> None:
        settings = _responses_build_settings(openai_api_key=None)
        with _responses_patch('app.clients.openai_responses.get_settings', return_value=settings):
            with self.assertRaises(_responses_OpenAIConfigurationError):
                _responses_get_openai_answer_client()

def _responses_build_http_error(code: int) -> _responses_urllib_error.HTTPError:
    return _responses_urllib_error.HTTPError(url='https://api.openai.com/v1/responses', code=code, msg='error', hdrs=None, fp=_responses_io.BytesIO(_responses_json.dumps({'error': {'message': 'boom'}}).encode()))

class _responses_OpenAIResponseErrorClassificationTests(_responses_unittest.TestCase):
    """
    Tests that generate() classifies each failure as retryable or not,
    matching the mission's retry-eligibility table: HTTP
    429/500/502/503/504 and any connection-level failure are
    retryable; HTTP 400/401/403 are not.
    """

    def _generate_with_http_error(self, code: int) -> _responses_OpenAIResponseError:
        client = _responses_build_client()

        def fake_urlopen(request: _responses_Any, timeout: float) -> _responses_Any:
            raise _responses_build_http_error(code)
        with _responses_patch('app.clients.openai_responses.urlopen', side_effect=fake_urlopen):
            with self.assertRaises(_responses_OpenAIResponseError) as context:
                client.generate(instructions='Instructions', input_text='Input')
        return context.exception

    def test_http_429_is_retryable(self) -> None:
        error = self._generate_with_http_error(429)
        self.assertTrue(error.retryable)
        self.assertEqual(error.status_code, 429)

    def test_http_500_502_503_504_are_retryable(self) -> None:
        for code in (500, 502, 503, 504):
            with self.subTest(code=code):
                error = self._generate_with_http_error(code)
                self.assertTrue(error.retryable)
                self.assertEqual(error.status_code, code)

    def test_http_400_401_403_are_not_retryable(self) -> None:
        for code in (400, 401, 403):
            with self.subTest(code=code):
                error = self._generate_with_http_error(code)
                self.assertFalse(error.retryable)
                self.assertEqual(error.status_code, code)

    def test_connection_level_failure_is_retryable(self) -> None:
        client = _responses_build_client()

        def fake_urlopen(request: _responses_Any, timeout: float) -> _responses_Any:
            raise _responses_urllib_error.URLError('connection refused')
        with _responses_patch('app.clients.openai_responses.urlopen', side_effect=fake_urlopen):
            with self.assertRaises(_responses_OpenAIResponseError) as context:
                client.generate(instructions='Instructions', input_text='Input')
        self.assertTrue(context.exception.retryable)
        self.assertIsNone(context.exception.status_code)

    def test_invalid_json_after_success_is_not_retryable(self) -> None:
        client = _responses_build_client()

        class _BadResponse:

            def read(self) -> bytes:
                return b'not json'

            def __enter__(self) -> '_BadResponse':
                return self

            def __exit__(self, *exc_info: object) -> None:
                return None

        def fake_urlopen(request: _responses_Any, timeout: float) -> _responses_Any:
            return _BadResponse()
        with _responses_patch('app.clients.openai_responses.urlopen', side_effect=fake_urlopen):
            with self.assertRaises(_responses_OpenAIResponseError) as context:
                client.generate(instructions='Instructions', input_text='Input')
        self.assertFalse(context.exception.retryable)

    def test_default_retryable_is_false(self) -> None:
        error = _responses_OpenAIResponseError('boom')
        self.assertFalse(error.retryable)
        self.assertIsNone(error.status_code)

class _responses_OpenAITextFormatTests(_responses_unittest.TestCase):
    """Tests that an optional text_format is sent through as-is."""

    def test_text_format_is_included_when_provided(self) -> None:
        client = _responses_build_client()
        schema = {'type': 'json_schema', 'name': 'example', 'schema': {'type': 'object'}, 'strict': True}
        captured: dict[str, _responses_Any] = {}

        def fake_urlopen(request: _responses_Any, timeout: float) -> _responses_FakeHTTPResponse:
            captured['body'] = _responses_json.loads(request.data.decode('utf-8'))
            return _responses_FakeHTTPResponse({'output_text': 'Answer.', 'model': 'test-model'})
        with _responses_patch('app.clients.openai_responses.urlopen', side_effect=fake_urlopen):
            client.generate(instructions='Instructions', input_text='Input', text_format=schema)
        self.assertEqual(captured['body']['text'], {'format': schema})

    def test_text_format_omitted_by_default(self) -> None:
        body = _responses_generate_and_capture_request(_responses_build_client())
        self.assertNotIn('text', body)


# ====================================================================
# SOURCE: test_openai_responses_stream.py
# ====================================================================

import asyncio as _stream_asyncio
import json as _stream_json
import unittest as _stream_unittest
import httpx as _stream_httpx
from app.clients.openai_responses import OpenAIConfigurationError as _stream_OpenAIConfigurationError
from app.clients.openai_responses_stream import MalformedProviderEventError as _stream_MalformedProviderEventError, OpenAIResponsesStreamClient as _stream_OpenAIResponsesStreamClient, StreamEvent as _stream_StreamEvent, StreamEventType as _stream_StreamEventType, _IncrementalSSEDecoder as _stream_IncrementalSSEDecoder, _map_provider_event as _stream_map_provider_event

def _stream_sse_frame(event_type: str | None, data: dict) -> bytes:
    lines = []
    if event_type is not None:
        lines.append(f'event: {event_type}')
    lines.append(f'data: {_stream_json.dumps(data)}')
    lines.append('')
    lines.append('')
    return '\n'.join(lines).encode('utf-8')

class _stream_IncrementalSSEDecoderTests(_stream_unittest.TestCase):
    """Pure, offline tests for the SSE frame decoder - no network, no
    httpx, no event loop."""

    def test_single_event_one_chunk(self) -> None:
        decoder = _stream_IncrementalSSEDecoder()
        events = decoder.feed(b'event: response.completed\ndata: {"a": 1}\n\n')
        self.assertEqual([('response.completed', '{"a": 1}')], events)

    def test_event_split_across_many_tiny_chunks(self) -> None:
        decoder = _stream_IncrementalSSEDecoder()
        whole = b'event: response.completed\ndata: {"a": 1}\n\n'
        events: list[tuple[str | None, str]] = []
        for index in range(len(whole)):
            events.extend(decoder.feed(whole[index:index + 1]))
        self.assertEqual([('response.completed', '{"a": 1}')], events)

    def test_multibyte_utf8_character_split_across_chunk_boundary(self) -> None:
        decoder = _stream_IncrementalSSEDecoder()
        payload = _stream_json.dumps({'delta': 'café'}, ensure_ascii=False).encode('utf-8')
        frame = b'event: response.output_text.delta\ndata: ' + payload + b'\n\n'
        split_index = frame.index('é'.encode('utf-8')[:1]) + 1
        events: list[tuple[str | None, str]] = []
        events.extend(decoder.feed(frame[:split_index]))
        events.extend(decoder.feed(frame[split_index:]))
        self.assertEqual(1, len(events))
        event_type, data = events[0]
        self.assertEqual('response.output_text.delta', event_type)
        self.assertEqual({'delta': 'café'}, _stream_json.loads(data))

    def test_multiple_data_lines_joined_with_newline(self) -> None:
        decoder = _stream_IncrementalSSEDecoder()
        events = decoder.feed(b'data: line one\ndata: line two\n\n')
        self.assertEqual([(None, 'line one\nline two')], events)

    def test_comment_lines_are_ignored(self) -> None:
        decoder = _stream_IncrementalSSEDecoder()
        events = decoder.feed(b': this is a comment\ndata: {"a": 1}\n\n')
        self.assertEqual([(None, '{"a": 1}')], events)

    def test_unknown_field_names_are_ignored(self) -> None:
        decoder = _stream_IncrementalSSEDecoder()
        events = decoder.feed(b'id: 42\nretry: 3000\ndata: {"a": 1}\n\n')
        self.assertEqual([(None, '{"a": 1}')], events)

    def test_multiple_events_in_one_chunk(self) -> None:
        decoder = _stream_IncrementalSSEDecoder()
        events = decoder.feed(b'data: {"a": 1}\n\ndata: {"a": 2}\n\n')
        self.assertEqual([(None, '{"a": 1}'), (None, '{"a": 2}')], events)

    def test_event_type_resets_between_events(self) -> None:
        decoder = _stream_IncrementalSSEDecoder()
        events = decoder.feed(b'event: response.completed\ndata: {"a": 1}\n\ndata: {"a": 2}\n\n')
        self.assertEqual([('response.completed', '{"a": 1}'), (None, '{"a": 2}')], events)

    def test_missing_trailing_blank_line_recovered_on_close(self) -> None:
        decoder = _stream_IncrementalSSEDecoder()
        mid_stream_events = decoder.feed(b'event: response.completed\ndata: {"a": 1}')
        self.assertEqual([], mid_stream_events)
        final_events = decoder.close()
        self.assertEqual([('response.completed', '{"a": 1}')], final_events)

    def test_close_with_nothing_pending_yields_nothing(self) -> None:
        decoder = _stream_IncrementalSSEDecoder()
        decoder.feed(b'data: {"a": 1}\n\n')
        self.assertEqual([], decoder.close())

class _stream_MapProviderEventTests(_stream_unittest.TestCase):
    """Pure, offline tests for the strict provider-event allowlist."""

    def test_output_text_delta_maps_to_delta_event(self) -> None:
        event = _stream_map_provider_event('response.output_text.delta', _stream_json.dumps({'delta': 'Hello'}))
        self.assertEqual(_stream_StreamEvent(type=_stream_StreamEventType.DELTA, text='Hello'), event)

    def test_output_text_delta_with_empty_text_is_dropped(self) -> None:
        event = _stream_map_provider_event('response.output_text.delta', _stream_json.dumps({'delta': ''}))
        self.assertIsNone(event)

    def test_completed_maps_to_completed_event(self) -> None:
        event = _stream_map_provider_event('response.completed', _stream_json.dumps({'response': {'status': 'completed'}}))
        self.assertEqual(_stream_StreamEvent(type=_stream_StreamEventType.COMPLETED), event)

    def test_failed_maps_to_error_event_with_sanitized_message(self) -> None:
        event = _stream_map_provider_event('response.failed', _stream_json.dumps({'response': {'error': {'message': 'internal provider detail'}}}))
        self.assertEqual(_stream_StreamEventType.ERROR, event.type)
        self.assertIn('internal provider detail', event.error_message)
        self.assertFalse(event.retryable)

    def test_incomplete_maps_to_error_event(self) -> None:
        event = _stream_map_provider_event('response.incomplete', _stream_json.dumps({'response': {'incomplete_details': {'reason': 'max_tokens'}}}))
        self.assertEqual(_stream_StreamEventType.ERROR, event.type)
        self.assertIn('max_tokens', event.error_message)

    def test_reasoning_event_type_is_silently_dropped(self) -> None:
        """The core safety property: a reasoning/internal event type -
        which this model's reasoning.effort configuration can produce
        - must never become a StreamEvent, not even an error one."""
        event = _stream_map_provider_event('response.reasoning_summary_text.delta', _stream_json.dumps({'delta': 'internal chain-of-thought'}))
        self.assertIsNone(event)

    def test_unrecognized_future_event_type_is_silently_dropped(self) -> None:
        event = _stream_map_provider_event('response.some_future_event_type', _stream_json.dumps({'anything': 'goes here'}))
        self.assertIsNone(event)

    def test_malformed_json_for_allowed_type_raises(self) -> None:
        with self.assertRaises(_stream_MalformedProviderEventError):
            _stream_map_provider_event('response.output_text.delta', 'not valid json{{{')

    def test_non_dict_json_for_allowed_type_raises(self) -> None:
        with self.assertRaises(_stream_MalformedProviderEventError):
            _stream_map_provider_event('response.output_text.delta', _stream_json.dumps([1, 2, 3]))

    def test_malformed_json_for_unrecognized_type_is_still_dropped(self) -> None:
        """A malformed payload only matters for a type we claim to
        understand - garbage data on an already-ignored event type is
        still just ignored, not an error."""
        event = _stream_map_provider_event('response.some_future_event_type', 'not valid json{{{')
        self.assertIsNone(event)

def _stream_run_async(coroutine):
    return _stream_asyncio.run(coroutine)

class _stream_OpenAIResponsesStreamClientTests(_stream_unittest.TestCase):

    def _client(self, **overrides) -> _stream_OpenAIResponsesStreamClient:
        kwargs = dict(api_key='sk-test', model='gpt-5-mini', reasoning_effort='low')
        kwargs.update(overrides)
        return _stream_OpenAIResponsesStreamClient(**kwargs)

    def _make_transport(self, handler) -> _stream_httpx.MockTransport:
        return _stream_httpx.MockTransport(handler)

    def test_happy_path_yields_deltas_then_completed(self) -> None:

        async def handler(request: _stream_httpx.Request) -> _stream_httpx.Response:

            async def body():
                yield _stream_sse_frame('response.output_text.delta', {'delta': 'In '})
                yield _stream_sse_frame('response.output_text.delta', {'delta': 'France, the employer...'})
                yield _stream_sse_frame('response.completed', {'response': {'status': 'completed'}})
            return _stream_httpx.Response(200, content=body())

        async def collect():
            client = self._client()
            transport = self._make_transport(handler)
            events = []
            async for event in client.stream('instructions', 'input', transport=transport):
                events.append(event)
            return events
        events = _stream_run_async(collect())
        self.assertEqual([_stream_StreamEvent(type=_stream_StreamEventType.DELTA, text='In '), _stream_StreamEvent(type=_stream_StreamEventType.DELTA, text='France, the employer...'), _stream_StreamEvent(type=_stream_StreamEventType.COMPLETED)], events)

    def test_concatenated_deltas_reconstruct_expected_text(self) -> None:
        expected = 'In France, the employer must provide notice.'
        words = expected.split(' ')

        async def handler(request: _stream_httpx.Request) -> _stream_httpx.Response:

            async def body():
                for index, word in enumerate(words):
                    text = word if index == 0 else ' ' + word
                    yield _stream_sse_frame('response.output_text.delta', {'delta': text})
                yield _stream_sse_frame('response.completed', {'response': {'status': 'completed'}})
            return _stream_httpx.Response(200, content=body())

        async def collect():
            client = self._client()
            transport = self._make_transport(handler)
            deltas = []
            async for event in client.stream('instructions', 'input', transport=transport):
                if event.type is _stream_StreamEventType.DELTA:
                    deltas.append(event.text)
            return deltas
        deltas = _stream_run_async(collect())
        self.assertEqual(expected, ''.join(deltas))

    def test_reasoning_events_never_surface_as_stream_events(self) -> None:

        async def handler(request: _stream_httpx.Request) -> _stream_httpx.Response:

            async def body():
                yield _stream_sse_frame('response.reasoning_summary_text.delta', {'delta': 'internal chain-of-thought reasoning'})
                yield _stream_sse_frame('response.output_text.delta', {'delta': 'Answer.'})
                yield _stream_sse_frame('response.completed', {'response': {'status': 'completed'}})
            return _stream_httpx.Response(200, content=body())

        async def collect():
            client = self._client()
            transport = self._make_transport(handler)
            events = []
            async for event in client.stream('instructions', 'input', transport=transport):
                events.append(event)
            return events
        events = _stream_run_async(collect())
        for event in events:
            if event.type is _stream_StreamEventType.DELTA:
                self.assertNotIn('reasoning', event.text)
                self.assertNotIn('chain-of-thought', event.text)
        self.assertEqual([_stream_StreamEvent(type=_stream_StreamEventType.DELTA, text='Answer.'), _stream_StreamEvent(type=_stream_StreamEventType.COMPLETED)], events)

    def test_http_error_status_yields_single_error_event(self) -> None:

        async def handler(request: _stream_httpx.Request) -> _stream_httpx.Response:
            return _stream_httpx.Response(429, content=b'{"error": {"message": "rate limited"}}')

        async def collect():
            client = self._client()
            transport = self._make_transport(handler)
            events = []
            async for event in client.stream('instructions', 'input', transport=transport):
                events.append(event)
            return events
        events = _stream_run_async(collect())
        self.assertEqual(1, len(events))
        self.assertEqual(_stream_StreamEventType.ERROR, events[0].type)
        self.assertTrue(events[0].retryable)
        self.assertNotIn('rate limited', events[0].error_message)

    def test_response_failed_event_terminates_stream_with_error(self) -> None:

        async def handler(request: _stream_httpx.Request) -> _stream_httpx.Response:

            async def body():
                yield _stream_sse_frame('response.output_text.delta', {'delta': 'Partial'})
                yield _stream_sse_frame('response.failed', {'response': {'error': {'message': 'content policy'}}})
                yield _stream_sse_frame('response.output_text.delta', {'delta': 'should never appear'})
            return _stream_httpx.Response(200, content=body())

        async def collect():
            client = self._client()
            transport = self._make_transport(handler)
            events = []
            async for event in client.stream('instructions', 'input', transport=transport):
                events.append(event)
            return events
        events = _stream_run_async(collect())
        self.assertEqual([_stream_StreamEvent(type=_stream_StreamEventType.DELTA, text='Partial')], events[:-1])
        self.assertEqual(_stream_StreamEventType.ERROR, events[-1].type)
        self.assertEqual(2, len(events))

    def test_malformed_frame_terminates_stream_with_error_not_exception(self) -> None:

        async def handler(request: _stream_httpx.Request) -> _stream_httpx.Response:

            async def body():
                yield b'event: response.output_text.delta\ndata: not valid json{{{\n\n'
            return _stream_httpx.Response(200, content=body())

        async def collect():
            client = self._client()
            transport = self._make_transport(handler)
            events = []
            async for event in client.stream('instructions', 'input', transport=transport):
                events.append(event)
            return events
        events = _stream_run_async(collect())
        self.assertEqual(1, len(events))
        self.assertEqual(_stream_StreamEventType.ERROR, events[0].type)

    def test_connection_closes_without_completion_is_an_error(self) -> None:

        async def handler(request: _stream_httpx.Request) -> _stream_httpx.Response:

            async def body():
                yield _stream_sse_frame('response.output_text.delta', {'delta': 'Partial'})
            return _stream_httpx.Response(200, content=body())

        async def collect():
            client = self._client()
            transport = self._make_transport(handler)
            events = []
            async for event in client.stream('instructions', 'input', transport=transport):
                events.append(event)
            return events
        events = _stream_run_async(collect())
        self.assertEqual(2, len(events))
        self.assertEqual(_stream_StreamEventType.DELTA, events[0].type)
        self.assertEqual(_stream_StreamEventType.ERROR, events[1].type)
        self.assertTrue(events[1].retryable)

    def test_connect_timeout_yields_retryable_error(self) -> None:

        async def handler(request: _stream_httpx.Request) -> _stream_httpx.Response:
            raise _stream_httpx.ConnectTimeout('connect timed out', request=request)

        async def collect():
            client = self._client()
            transport = self._make_transport(handler)
            events = []
            async for event in client.stream('instructions', 'input', transport=transport):
                events.append(event)
            return events
        events = _stream_run_async(collect())
        self.assertEqual(1, len(events))
        self.assertEqual(_stream_StreamEventType.ERROR, events[0].type)
        self.assertTrue(events[0].retryable)

    def test_read_timeout_mid_stream_yields_retryable_error(self) -> None:

        async def handler(request: _stream_httpx.Request) -> _stream_httpx.Response:

            async def body():
                yield _stream_sse_frame('response.output_text.delta', {'delta': 'Partial'})
                raise _stream_httpx.ReadTimeout('read timed out', request=request)
            return _stream_httpx.Response(200, content=body())

        async def collect():
            client = self._client()
            transport = self._make_transport(handler)
            events = []
            async for event in client.stream('instructions', 'input', transport=transport):
                events.append(event)
            return events
        events = _stream_run_async(collect())
        self.assertEqual(2, len(events))
        self.assertEqual(_stream_StreamEventType.DELTA, events[0].type)
        self.assertEqual(_stream_StreamEventType.ERROR, events[1].type)
        self.assertTrue(events[1].retryable)

    def test_total_stream_timeout_terminates_a_slow_drip(self) -> None:

        async def handler(request: _stream_httpx.Request) -> _stream_httpx.Response:

            async def body():
                for _ in range(50):
                    yield _stream_sse_frame('response.output_text.delta', {'delta': 'x'})
                    await _stream_asyncio.sleep(0.02)
                yield _stream_sse_frame('response.completed', {'response': {'status': 'completed'}})
            return _stream_httpx.Response(200, content=body())

        async def collect():
            client = self._client(total_stream_timeout_seconds=0.05)
            transport = self._make_transport(handler)
            events = []
            async for event in client.stream('instructions', 'input', transport=transport):
                events.append(event)
            return events
        events = _stream_run_async(collect())
        self.assertEqual(_stream_StreamEventType.ERROR, events[-1].type)
        self.assertIn('maximum allowed duration', events[-1].error_message)
        self.assertLess(len(events), 50)

    def test_generator_can_be_closed_early_without_hanging_or_raising(self) -> None:
        """CANCELLATION primitive (GATE S2 scope): our own async
        generator must close promptly and cleanly when the caller
        stops consuming it early. This does NOT prove the underlying
        upstream TCP connection is torn down - see module docstring:
        httpx.MockTransport does not exercise that code path
        realistically. That remains an open proof for a later,
        real-network gate."""

        async def handler(request: _stream_httpx.Request) -> _stream_httpx.Response:

            async def body():
                for _ in range(1000):
                    yield _stream_sse_frame('response.output_text.delta', {'delta': 'x'})
                    await _stream_asyncio.sleep(1.0)
            return _stream_httpx.Response(200, content=body())

        async def scenario():
            client = self._client()
            transport = self._make_transport(handler)
            generator = client.stream('instructions', 'input', transport=transport)
            first_event = await generator.__anext__()
            await _stream_asyncio.wait_for(generator.aclose(), timeout=2.0)
            with self.assertRaises(StopAsyncIteration):
                await generator.__anext__()
            return first_event
        first_event = _stream_run_async(scenario())
        self.assertEqual(_stream_StreamEventType.DELTA, first_event.type)

    def test_empty_api_key_is_rejected_at_construction(self) -> None:
        with self.assertRaises(_stream_OpenAIConfigurationError):
            _stream_OpenAIResponsesStreamClient(api_key='   ', model='gpt-5-mini')

    def test_empty_model_is_rejected_at_construction(self) -> None:
        with self.assertRaises(_stream_OpenAIConfigurationError):
            _stream_OpenAIResponsesStreamClient(api_key='sk-test', model='')

    def test_non_positive_timeout_is_rejected_at_construction(self) -> None:
        with self.assertRaises(_stream_OpenAIConfigurationError):
            _stream_OpenAIResponsesStreamClient(api_key='sk-test', model='gpt-5-mini', read_timeout_seconds=0)
