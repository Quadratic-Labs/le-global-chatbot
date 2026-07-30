"""Tests for application-wide logging configuration."""

from __future__ import annotations

import contextlib
import importlib
import io
import logging
import os
import sys
import unittest

from app.services.chat_metrics import LegalChatMetrics


class LoggingConfigurationTests(unittest.TestCase):
    """Tests for the LOG_LEVEL/stdout-handler startup configuration."""

    def _reload_app_main_with_log_level(
        self,
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

    def test_chat_metrics_logger_accepts_info_when_log_level_info(
        self,
    ) -> None:
        self._reload_app_main_with_log_level(
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
        self._reload_app_main_with_log_level(
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
        self._reload_app_main_with_log_level(
            "INFO"
        )
        self._reload_app_main_with_log_level(
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
            self._reload_app_main_with_log_level(
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


if __name__ == "__main__":
    unittest.main()
