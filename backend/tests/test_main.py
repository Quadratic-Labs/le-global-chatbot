"""Tests for application-wide logging configuration."""

from __future__ import annotations

import importlib
import logging
import os
import sys
import unittest


class LoggingConfigurationTests(unittest.TestCase):
    """Tests for the LOG_LEVEL startup configuration in app.main."""

    def test_chat_metrics_logger_accepts_info_when_log_level_info(
        self,
    ) -> None:
        previous_log_level = os.environ.get(
            "LOG_LEVEL"
        )

        os.environ["LOG_LEVEL"] = "INFO"

        try:
            if "app.main" in sys.modules:
                importlib.reload(
                    sys.modules["app.main"]
                )
            else:
                import app.main  # noqa: F401

        finally:
            if previous_log_level is None:
                os.environ.pop(
                    "LOG_LEVEL",
                    None,
                )
            else:
                os.environ["LOG_LEVEL"] = (
                    previous_log_level
                )

        self.assertTrue(
            logging.getLogger(
                "app.services.chat_metrics"
            ).isEnabledFor(
                logging.INFO
            )
        )


if __name__ == "__main__":
    unittest.main()
