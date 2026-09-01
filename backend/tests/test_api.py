"""Consolidated test module generated during test-suite rationalisation."""

from __future__ import annotations


# ====================================================================
# SOURCE: test_api_protection.py
# ====================================================================

import unittest as _protection_unittest
from app.middleware.api_protection import RateLimitBackendError as _protection_RateLimitBackendError, RateLimitConfigurationError as _protection_RateLimitConfigurationError, api_key_matches as _protection_api_key_matches, build_rate_limit_identity as _protection_build_rate_limit_identity, consume_rate_limit as _protection_consume_rate_limit

class _protection_FakeRedis:
    """Minimal Redis implementation for rate-limit tests."""

    def __init__(self, result=None, error: Exception | None=None) -> None:
        self.result = result
        self.error = error
        self.arguments = None

    def eval(self, script, key_count, key, window_seconds):
        self.arguments = (script, key_count, key, window_seconds)
        if self.error is not None:
            raise self.error
        return self.result

class _protection_ApiProtectionTests(_protection_unittest.TestCase):
    """Tests for API authentication and rate limiting."""

    def test_matching_api_key_is_accepted(self) -> None:
        self.assertTrue(_protection_api_key_matches(provided_key='secret-value', expected_key='secret-value'))

    def test_missing_or_invalid_api_key_is_rejected(self) -> None:
        self.assertFalse(_protection_api_key_matches(provided_key=None, expected_key='secret-value'))
        self.assertFalse(_protection_api_key_matches(provided_key='wrong-value', expected_key='secret-value'))

    def test_rate_limit_identity_does_not_expose_key(self) -> None:
        identity = _protection_build_rate_limit_identity(client_ip='192.0.2.10', api_key='secret-value')
        self.assertIn('192.0.2.10', identity)
        self.assertNotIn('secret-value', identity)

    def test_rate_limit_returns_remaining_requests(self) -> None:
        redis_client = _protection_FakeRedis(result=[3, 42])
        status = _protection_consume_rate_limit(identity='consumer-1', request_limit=10, window_seconds=60, client=redis_client)
        self.assertFalse(status.exceeded)
        self.assertEqual(status.current, 3)
        self.assertEqual(status.remaining, 7)
        self.assertEqual(status.retry_after_seconds, 42)

    def test_rate_limit_detects_exceeded_bucket(self) -> None:
        redis_client = _protection_FakeRedis(result=[11, 25])
        status = _protection_consume_rate_limit(identity='consumer-1', request_limit=10, window_seconds=60, client=redis_client)
        self.assertTrue(status.exceeded)
        self.assertEqual(status.remaining, 0)

    def test_invalid_rate_limit_configuration_is_rejected(self) -> None:
        with self.assertRaises(_protection_RateLimitConfigurationError):
            _protection_consume_rate_limit(identity='consumer-1', request_limit=0, window_seconds=60, client=_protection_FakeRedis(result=[1, 60]))

    def test_redis_errors_are_wrapped(self) -> None:
        redis_client = _protection_FakeRedis(error=RuntimeError('Redis unavailable'))
        with self.assertRaises(_protection_RateLimitBackendError):
            _protection_consume_rate_limit(identity='consumer-1', request_limit=10, window_seconds=60, client=redis_client)

    def test_invalid_redis_response_is_rejected(self) -> None:
        redis_client = _protection_FakeRedis(result=[1])
        with self.assertRaises(_protection_RateLimitBackendError):
            _protection_consume_rate_limit(identity='consumer-1', request_limit=10, window_seconds=60, client=redis_client)


# ====================================================================
# SOURCE: test_main.py
# ====================================================================

import asyncio as _main_asyncio
import contextlib as _main_contextlib
import importlib as _main_importlib
import io as _main_io
import json as _main_json
import logging as _main_logging
import os as _main_os
import sys as _main_sys
import unittest as _main_unittest
from fastapi.exceptions import RequestValidationError as _main_RequestValidationError
from starlette.requests import Request as _main_Request
from app.services.chat_metrics import LegalChatMetrics as _main_LegalChatMetrics

def _main_build_request(path: str) -> _main_Request:
    return _main_Request({'type': 'http', 'method': 'POST', 'path': path, 'headers': [], 'query_string': b'', 'server': ('testserver', 80), 'scheme': 'http'})

def _main_reload_app_main_with_log_level(log_level: str) -> None:
    """Re-run app.main's module-level logging setup with LOG_LEVEL set."""
    _main_os.environ['LOG_LEVEL'] = log_level
    if 'app.main' in _main_sys.modules:
        _main_importlib.reload(_main_sys.modules['app.main'])
    else:
        import app.main

class _main_ResetAppLoggerStateMixin:
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

    def setUp(self) -> None:
        self._previous_log_level = _main_os.environ.get('LOG_LEVEL')
        app_logger = _main_logging.getLogger('app')
        self._previous_handlers = list(app_logger.handlers)
        self._previous_propagate = app_logger.propagate
        self._previous_level = app_logger.level
        app_logger.handlers = []

    def tearDown(self) -> None:
        if self._previous_log_level is None:
            _main_os.environ.pop('LOG_LEVEL', None)
        else:
            _main_os.environ['LOG_LEVEL'] = self._previous_log_level
        app_logger = _main_logging.getLogger('app')
        app_logger.handlers = self._previous_handlers
        app_logger.propagate = self._previous_propagate
        app_logger.setLevel(self._previous_level)

class _main_LoggingConfigurationTests(_main_ResetAppLoggerStateMixin, _main_unittest.TestCase):
    """Tests for the LOG_LEVEL/stdout-handler startup configuration."""

    def test_chat_metrics_logger_accepts_info_when_log_level_info(self) -> None:
        _main_reload_app_main_with_log_level('INFO')
        logger = _main_logging.getLogger('app.services.chat_metrics')
        self.assertTrue(logger.isEnabledFor(_main_logging.INFO))

    def test_app_logger_has_a_stdout_handler(self) -> None:
        _main_reload_app_main_with_log_level('INFO')
        app_logger = _main_logging.getLogger('app')
        self.assertTrue(any((isinstance(handler, _main_logging.StreamHandler) and handler.stream is _main_sys.stdout for handler in app_logger.handlers)))

    def test_reload_does_not_duplicate_handlers(self) -> None:
        _main_reload_app_main_with_log_level('INFO')
        _main_reload_app_main_with_log_level('INFO')
        app_logger = _main_logging.getLogger('app')
        self.assertEqual(len(app_logger.handlers), 1)

    def test_metrics_log_actually_reaches_stdout(self) -> None:
        """
        End-to-end proof that a metrics event is written to stdout.

        This reproduces the reported bug precisely: setLevel() alone
        makes the logger *accept* INFO records, but without a handler
        writing to stdout, a container log collector would still see
        nothing.
        """
        captured_output = _main_io.StringIO()
        with _main_contextlib.redirect_stdout(captured_output):
            _main_reload_app_main_with_log_level('INFO')
            metrics = _main_LegalChatMetrics(request_id='request-stdout-test', question_characters=10, max_sources=6, rerank_enabled=False)
            metrics.outcome = 'generated'
            metrics.log()
        self.assertIn('legal_chat_performance', captured_output.getvalue())
        self.assertIn('request-stdout-test', captured_output.getvalue())

class _main_ConversationStateRejectionLoggingTests(_main_ResetAppLoggerStateMixin, _main_unittest.TestCase):
    """
    Tests for main.py's RequestValidationError hook (0.4.2).

    A request whose conversation_state fails Pydantic validation
    never reaches legal_chat()/LegalChatMetrics - FastAPI's own body
    parsing rejects it first. This hook is the only place that
    rejection is observable at all, so it is exercised directly here
    rather than through resolve_legal_chat_response.
    """

    def test_logs_when_conversation_state_is_the_invalid_field(self) -> None:
        captured_output = _main_io.StringIO()
        with _main_contextlib.redirect_stdout(captured_output):
            _main_reload_app_main_with_log_level('INFO')
            import app.main as app_main
            response = _main_asyncio.run(app_main._handle_request_validation_error(_main_build_request('/api/v1/chat'), _main_RequestValidationError([{'type': 'value_error', 'loc': ('body', 'conversation_state', 'actions', 0, 'type'), 'msg': 'Unsupported action type', 'input': 'bogus'}])))
        self.assertEqual(response.status_code, 422)
        self.assertIn('conversation_state_recovery_retry', captured_output.getvalue())

    def test_does_not_log_when_the_invalid_field_is_unrelated(self) -> None:
        captured_output = _main_io.StringIO()
        with _main_contextlib.redirect_stdout(captured_output):
            _main_reload_app_main_with_log_level('INFO')
            import app.main as app_main
            response = _main_asyncio.run(app_main._handle_request_validation_error(_main_build_request('/api/v1/chat'), _main_RequestValidationError([{'type': 'missing', 'loc': ('body', 'question'), 'msg': 'Field required', 'input': None}])))
        self.assertEqual(response.status_code, 422)
        self.assertNotIn('conversation_state_recovery_retry', captured_output.getvalue())

    def test_does_not_log_for_a_different_endpoint(self) -> None:
        captured_output = _main_io.StringIO()
        with _main_contextlib.redirect_stdout(captured_output):
            _main_reload_app_main_with_log_level('INFO')
            import app.main as app_main
            response = _main_asyncio.run(app_main._handle_request_validation_error(_main_build_request('/api/v1/legal-search'), _main_RequestValidationError([{'type': 'value_error', 'loc': ('body', 'conversation_state'), 'msg': 'irrelevant here', 'input': None}])))
        self.assertEqual(response.status_code, 422)
        self.assertNotIn('conversation_state_recovery_retry', captured_output.getvalue())

    def test_response_body_matches_default_validation_error_shape(self) -> None:
        import app.main as app_main
        response = _main_asyncio.run(app_main._handle_request_validation_error(_main_build_request('/api/v1/chat'), _main_RequestValidationError([{'type': 'value_error', 'loc': ('body', 'conversation_state'), 'msg': 'bad', 'input': None}])))
        body = _main_json.loads(response.body)
        self.assertEqual(len(body['detail']), 1)
        self.assertEqual(body['detail'][0]['loc'], ['body', 'conversation_state'])
        self.assertEqual(body['detail'][0]['msg'], 'bad')


# ====================================================================
# SOURCE: test_frontend_config.py
# ====================================================================

import unittest as _frontend_unittest
from app.models.catalog import LegalCatalogCountry as _frontend_LegalCatalogCountry, LegalCatalogResponse as _frontend_LegalCatalogResponse, LegalCatalogValue as _frontend_LegalCatalogValue
from app.models.chat import HISTORY_MAX_MESSAGES as _frontend_HISTORY_MAX_MESSAGES
from app.services.legal_catalog import API_VERSION as _frontend_API_VERSION, FrontendConfigError as _frontend_FrontendConfigError, get_frontend_config as _frontend_get_frontend_config
from app.services.legal_catalog import LegalCatalogError as _frontend_LegalCatalogError

def _frontend_build_catalog() -> _frontend_LegalCatalogResponse:
    """Build one test legal catalog."""
    return _frontend_LegalCatalogResponse(countries=[_frontend_LegalCatalogCountry(country_code='GB', country='United Kingdom', chunk_count=41)], legal_topics=[_frontend_LegalCatalogValue(value='Employment Contracts', chunk_count=20)], subsections=[_frontend_LegalCatalogValue(value='Notice Period', chunk_count=12)])

class _frontend_FrontendConfigTests(_frontend_unittest.TestCase):
    """Tests for frontend bootstrap configuration."""

    def test_frontend_config_contains_catalog(self) -> None:
        response = _frontend_get_frontend_config(catalog_provider=_frontend_build_catalog)
        self.assertEqual(response.api_version, _frontend_API_VERSION)
        self.assertEqual(response.default_language, 'en')
        self.assertEqual(response.supported_languages, ['en'])
        self.assertEqual(len(response.catalog.countries), 1)
        self.assertEqual(response.catalog.countries[0].country_code, 'GB')

    def test_frontend_config_exposes_input_limits(self) -> None:
        response = _frontend_get_frontend_config(catalog_provider=_frontend_build_catalog)
        self.assertEqual(response.limits.question_min_length, 2)
        self.assertEqual(response.limits.question_max_length, 2000)
        self.assertEqual(response.limits.max_sources_default, 6)
        self.assertEqual(response.limits.max_sources_min, 1)
        self.assertEqual(response.limits.max_sources_max, 10)
        self.assertEqual(response.limits.max_history_messages, _frontend_HISTORY_MAX_MESSAGES)

    def test_catalog_errors_are_wrapped(self) -> None:

        def failing_catalog_provider() -> _frontend_LegalCatalogResponse:
            raise _frontend_LegalCatalogError('OpenSearch unavailable')
        with self.assertRaises(_frontend_FrontendConfigError):
            _frontend_get_frontend_config(catalog_provider=failing_catalog_provider)
