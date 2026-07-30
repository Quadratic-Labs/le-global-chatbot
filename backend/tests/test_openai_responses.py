"""Tests for the OpenAI Responses API client."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from app.clients.openai_responses import (
    OpenAIConfigurationError,
    OpenAIResponseError,
    OpenAIResponsesClient,
    get_openai_answer_client,
    get_openai_rerank_client,
)
from app.core.config import Settings


def _build_settings(**overrides: Any) -> Settings:
    """Build a Settings instance for tests, without reading real env vars."""

    defaults: dict[str, Any] = {
        "app_env": "test",
        "opensearch_url": "https://opensearch:9200",
        "opensearch_username": "admin",
        "opensearch_password": "password",
        "opensearch_verify_certs": False,
        "redis_url": "redis://localhost:6379/0",
        "document_source_dir": Path("/tmp/source"),
        "document_processed_dir": Path("/tmp/processed"),
        "document_upload_max_bytes": 1000,
        "openai_api_key": "test-key",
        "openai_model": "test-model",
        "openai_timeout_seconds": 60.0,
        "openai_answer_reasoning_effort": "low",
        "openai_answer_max_output_tokens": 2000,
        "openai_rerank_reasoning_effort": "low",
        "openai_rerank_max_output_tokens": 500,
        "api_access_key": None,
        "admin_api_key": None,
        "cors_allowed_origins": (),
        "rate_limit_requests": 60,
        "rate_limit_window_seconds": 60,
        "rerank_enabled": False,
        "rerank_pool_multiplier": 3,
        "rag_max_context_characters": 16000,
        "rag_max_source_characters": 4000,
    }
    defaults.update(overrides)

    return Settings(**defaults)


class _FakeHTTPResponse:
    """Minimal stand-in for the object returned by urlopen()."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeHTTPResponse":
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None


def _build_client(**kwargs: Any) -> OpenAIResponsesClient:
    return OpenAIResponsesClient(
        api_key="test-key",
        model="test-model",
        **kwargs,
    )


def _generate_and_capture_request(
    client: OpenAIResponsesClient,
    response_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Call generate() against a fake transport and return the sent body."""

    captured: dict[str, Any] = {}

    def fake_urlopen(request: Any, timeout: float) -> _FakeHTTPResponse:
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return _FakeHTTPResponse(
            response_payload
            or {"output_text": "Answer.", "model": "test-model"}
        )

    with patch(
        "app.clients.openai_responses.urlopen",
        side_effect=fake_urlopen,
    ):
        client.generate(instructions="Instructions", input_text="Input")

    return captured["body"]


class OpenAIResponsesClientTests(unittest.TestCase):
    """Tests for OpenAIResponsesClient.generate()."""

    def test_reasoning_effort_is_sent_when_configured(self) -> None:
        client = _build_client(
            reasoning_effort="low",
            max_output_tokens=2000,
        )

        body = _generate_and_capture_request(client)

        self.assertEqual(body["reasoning"], {"effort": "low"})

    def test_max_output_tokens_is_sent_when_configured(self) -> None:
        client = _build_client(
            reasoning_effort="low",
            max_output_tokens=2000,
        )

        body = _generate_and_capture_request(client)

        self.assertEqual(body["max_output_tokens"], 2000)

    def test_reasoning_and_max_output_tokens_omitted_by_default(self) -> None:
        client = _build_client()

        body = _generate_and_capture_request(client)

        self.assertNotIn("reasoning", body)
        self.assertNotIn("max_output_tokens", body)

    def test_rejects_non_positive_max_output_tokens(self) -> None:
        with self.assertRaises(OpenAIConfigurationError):
            _build_client(max_output_tokens=0)

        with self.assertRaises(OpenAIConfigurationError):
            _build_client(max_output_tokens=-10)

    def test_rejects_blank_reasoning_effort(self) -> None:
        with self.assertRaises(OpenAIConfigurationError):
            _build_client(reasoning_effort="   ")

    def test_incomplete_response_raises_error(self) -> None:
        client = _build_client(
            reasoning_effort="low",
            max_output_tokens=2000,
        )

        def fake_urlopen(request: Any, timeout: float) -> _FakeHTTPResponse:
            return _FakeHTTPResponse(
                {
                    "status": "incomplete",
                    "incomplete_details": {
                        "reason": "max_output_tokens",
                    },
                    "model": "test-model",
                }
            )

        with patch(
            "app.clients.openai_responses.urlopen",
            side_effect=fake_urlopen,
        ):
            with self.assertRaises(OpenAIResponseError):
                client.generate(
                    instructions="Instructions",
                    input_text="Input",
                )

    def test_incomplete_response_without_reason_uses_placeholder(
        self,
    ) -> None:
        client = _build_client()

        def fake_urlopen(request: Any, timeout: float) -> _FakeHTTPResponse:
            return _FakeHTTPResponse(
                {
                    "status": "incomplete",
                    "model": "test-model",
                }
            )

        with patch(
            "app.clients.openai_responses.urlopen",
            side_effect=fake_urlopen,
        ):
            with self.assertRaises(OpenAIResponseError) as context:
                client.generate(
                    instructions="Instructions",
                    input_text="Input",
                )

        self.assertIn("unknown reason", str(context.exception))


class OpenAIClientFactoryTests(unittest.TestCase):
    """Tests for the separate answer/rerank client factories."""

    def test_answer_client_uses_answer_budget(self) -> None:
        settings = _build_settings()

        with patch(
            "app.clients.openai_responses.get_settings",
            return_value=settings,
        ):
            client = get_openai_answer_client()

        self.assertEqual(client.reasoning_effort, "low")
        self.assertEqual(client.max_output_tokens, 2000)

    def test_rerank_client_uses_rerank_budget(self) -> None:
        settings = _build_settings()

        with patch(
            "app.clients.openai_responses.get_settings",
            return_value=settings,
        ):
            client = get_openai_rerank_client()

        self.assertEqual(client.reasoning_effort, "low")
        self.assertEqual(client.max_output_tokens, 500)

    def test_answer_and_rerank_clients_are_independent(self) -> None:
        settings = _build_settings(
            openai_answer_max_output_tokens=2500,
            openai_rerank_max_output_tokens=300,
        )

        with patch(
            "app.clients.openai_responses.get_settings",
            return_value=settings,
        ):
            answer_client = get_openai_answer_client()
            rerank_client = get_openai_rerank_client()

        self.assertEqual(answer_client.max_output_tokens, 2500)
        self.assertEqual(rerank_client.max_output_tokens, 300)

    def test_missing_api_key_raises_configuration_error(self) -> None:
        settings = _build_settings(openai_api_key=None)

        with patch(
            "app.clients.openai_responses.get_settings",
            return_value=settings,
        ):
            with self.assertRaises(OpenAIConfigurationError):
                get_openai_answer_client()


if __name__ == "__main__":
    unittest.main()
