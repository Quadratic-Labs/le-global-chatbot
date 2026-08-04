"""Tests for application-wide logging configuration."""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import io
import json
import logging
import os
import sys
import unittest

from fastapi.exceptions import RequestValidationError
from starlette.requests import Request

from app.services.chat_metrics import LegalChatMetrics


def _build_request(path: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": path,
            "headers": [],
            "query_string": b"",
            "server": ("testserver", 80),
            "scheme": "http",
        }
    )


def _reload_app_main_with_log_level(
    log_level: str,
) -> None:
    """Re-run app.main's module-level logging setup with LOG_LEVEL set."""

    os.environ["LOG_LEVEL"] = log_level

    if "app.main" in sys.modules:
        importlib.reload(
            sys.modules["app.main"]
        )
    else:
        import app.main  # noqa: F401


class _ResetAppLoggerStateMixin:
    """
    Snapshots and restores the shared "app" logger's handler state
    around each test.

    Critical for correctness, not just tidiness: configure_application_
    logging() only ever installs a handler when app_logger.handlers is
    empty. Without resetting to empty in setUp, whichever test happens
    to reload app.main first (alphabetical discovery order, not
    definition order) permanently binds the handler's stream to
    whatever sys.stdout was at that moment - e.g. a test's own
    contextlib.redirect_stdout StringIO, which is discarded when that
    test ends, silently breaking every later test's stdout capture for
    the rest of the process.
    """

    def setUp(
        self,
    ) -> None:
        self._previous_log_level = (
            os.environ.get("LOG_LEVEL")
        )

        app_logger = logging.getLogger("app")

        self._previous_handlers = list(
            app_logger.handlers
        )
        self._previous_propagate = (
            app_logger.propagate
        )
        self._previous_level = app_logger.level

        app_logger.handlers = []

    def tearDown(
        self,
    ) -> None:
        if self._previous_log_level is None:
            os.environ.pop(
                "LOG_LEVEL",
                None,
            )
        else:
            os.environ["LOG_LEVEL"] = (
                self._previous_log_level
            )

        app_logger = logging.getLogger("app")
        app_logger.handlers = (
            self._previous_handlers
        )
        app_logger.propagate = (
            self._previous_propagate
        )
        app_logger.setLevel(
            self._previous_level
        )


class LoggingConfigurationTests(
    _ResetAppLoggerStateMixin,
    unittest.TestCase,
):
    """Tests for the LOG_LEVEL/stdout-handler startup configuration."""

    def test_chat_metrics_logger_accepts_info_when_log_level_info(
        self,
    ) -> None:
        _reload_app_main_with_log_level(
            "INFO"
        )

        logger = logging.getLogger(
            "app.services.chat_metrics"
        )

        self.assertTrue(
            logger.isEnabledFor(
                logging.INFO
            )
        )

    def test_app_logger_has_a_stdout_handler(
        self,
    ) -> None:
        _reload_app_main_with_log_level(
            "INFO"
        )

        app_logger = logging.getLogger("app")

        self.assertTrue(
            any(
                isinstance(
                    handler,
                    logging.StreamHandler,
                )
                and handler.stream is sys.stdout
                for handler in app_logger.handlers
            )
        )

    def test_reload_does_not_duplicate_handlers(
        self,
    ) -> None:
        _reload_app_main_with_log_level(
            "INFO"
        )
        _reload_app_main_with_log_level(
            "INFO"
        )

        app_logger = logging.getLogger("app")

        self.assertEqual(
            len(app_logger.handlers),
            1,
        )

    def test_metrics_log_actually_reaches_stdout(
        self,
    ) -> None:
        """
        End-to-end proof that a metrics event is written to stdout.

        This reproduces the reported bug precisely: setLevel() alone
        makes the logger *accept* INFO records, but without a handler
        writing to stdout, a container log collector would still see
        nothing.
        """

        captured_output = io.StringIO()

        with contextlib.redirect_stdout(
            captured_output
        ):
            _reload_app_main_with_log_level(
                "INFO"
            )

            metrics = LegalChatMetrics(
                request_id="request-stdout-test",
                question_characters=10,
                max_sources=6,
                rerank_enabled=False,
            )
            metrics.outcome = "generated"

            metrics.log()

        self.assertIn(
            "legal_chat_performance",
            captured_output.getvalue(),
        )

        self.assertIn(
            "request-stdout-test",
            captured_output.getvalue(),
        )


class ConversationStateRejectionLoggingTests(
    _ResetAppLoggerStateMixin,
    unittest.TestCase,
):
    """
    Tests for main.py's RequestValidationError hook (0.4.2).

    A request whose conversation_state fails Pydantic validation
    never reaches legal_chat()/LegalChatMetrics - FastAPI's own body
    parsing rejects it first. This hook is the only place that
    rejection is observable at all, so it is exercised directly here
    rather than through resolve_legal_chat_response.
    """

    def test_logs_when_conversation_state_is_the_invalid_field(
        self,
    ) -> None:
        captured_output = io.StringIO()

        with contextlib.redirect_stdout(captured_output):
            _reload_app_main_with_log_level("INFO")

            import app.main as app_main

            response = asyncio.run(
                app_main._handle_request_validation_error(
                    _build_request("/api/v1/chat"),
                    RequestValidationError(
                        [
                            {
                                "type": "value_error",
                                "loc": (
                                    "body",
                                    "conversation_state",
                                    "actions",
                                    0,
                                    "type",
                                ),
                                "msg": "Unsupported action type",
                                "input": "bogus",
                            }
                        ]
                    ),
                )
            )

        self.assertEqual(response.status_code, 422)
        self.assertIn(
            "conversation_state_recovery_retry",
            captured_output.getvalue(),
        )

    def test_does_not_log_when_the_invalid_field_is_unrelated(
        self,
    ) -> None:
        captured_output = io.StringIO()

        with contextlib.redirect_stdout(captured_output):
            _reload_app_main_with_log_level("INFO")

            import app.main as app_main

            response = asyncio.run(
                app_main._handle_request_validation_error(
                    _build_request("/api/v1/chat"),
                    RequestValidationError(
                        [
                            {
                                "type": "missing",
                                "loc": ("body", "question"),
                                "msg": "Field required",
                                "input": None,
                            }
                        ]
                    ),
                )
            )

        self.assertEqual(response.status_code, 422)
        self.assertNotIn(
            "conversation_state_recovery_retry",
            captured_output.getvalue(),
        )

    def test_does_not_log_for_a_different_endpoint(
        self,
    ) -> None:
        captured_output = io.StringIO()

        with contextlib.redirect_stdout(captured_output):
            _reload_app_main_with_log_level("INFO")

            import app.main as app_main

            response = asyncio.run(
                app_main._handle_request_validation_error(
                    _build_request("/api/v1/legal-search"),
                    RequestValidationError(
                        [
                            {
                                "type": "value_error",
                                "loc": ("body", "conversation_state"),
                                "msg": "irrelevant here",
                                "input": None,
                            }
                        ]
                    ),
                )
            )

        self.assertEqual(response.status_code, 422)
        self.assertNotIn(
            "conversation_state_recovery_retry",
            captured_output.getvalue(),
        )

    def test_response_body_matches_default_validation_error_shape(
        self,
    ) -> None:
        import app.main as app_main

        response = asyncio.run(
            app_main._handle_request_validation_error(
                _build_request("/api/v1/chat"),
                RequestValidationError(
                    [
                        {
                            "type": "value_error",
                            "loc": ("body", "conversation_state"),
                            "msg": "bad",
                            "input": None,
                        }
                    ]
                ),
            )
        )

        body = json.loads(response.body)

        self.assertEqual(len(body["detail"]), 1)
        self.assertEqual(
            body["detail"][0]["loc"],
            ["body", "conversation_state"],
        )
        self.assertEqual(body["detail"][0]["msg"], "bad")


if __name__ == "__main__":
    unittest.main()
