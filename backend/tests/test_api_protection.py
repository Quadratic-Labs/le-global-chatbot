"""Tests for API-key protection and Redis rate limiting."""

from __future__ import annotations

import unittest

from app.middleware.api_protection import (
    RateLimitBackendError,
    RateLimitConfigurationError,
    api_key_matches,
    build_rate_limit_identity,
    consume_rate_limit,
)


class FakeRedis:
    """Minimal Redis implementation for rate-limit tests."""

    def __init__(
        self,
        result=None,
        error: Exception | None = None,
    ) -> None:
        self.result = result
        self.error = error
        self.arguments = None

    def eval(
        self,
        script,
        key_count,
        key,
        window_seconds,
    ):
        self.arguments = (
            script,
            key_count,
            key,
            window_seconds,
        )

        if self.error is not None:
            raise self.error

        return self.result


class ApiProtectionTests(unittest.TestCase):
    """Tests for API authentication and rate limiting."""

    def test_matching_api_key_is_accepted(
        self,
    ) -> None:
        self.assertTrue(
            api_key_matches(
                provided_key="secret-value",
                expected_key="secret-value",
            )
        )

    def test_missing_or_invalid_api_key_is_rejected(
        self,
    ) -> None:
        self.assertFalse(
            api_key_matches(
                provided_key=None,
                expected_key="secret-value",
            )
        )

        self.assertFalse(
            api_key_matches(
                provided_key="wrong-value",
                expected_key="secret-value",
            )
        )

    def test_rate_limit_identity_does_not_expose_key(
        self,
    ) -> None:
        identity = build_rate_limit_identity(
            client_ip="192.0.2.10",
            api_key="secret-value",
        )

        self.assertIn(
            "192.0.2.10",
            identity,
        )

        self.assertNotIn(
            "secret-value",
            identity,
        )

    def test_rate_limit_returns_remaining_requests(
        self,
    ) -> None:
        redis_client = FakeRedis(
            result=[
                3,
                42,
            ]
        )

        status = consume_rate_limit(
            identity="consumer-1",
            request_limit=10,
            window_seconds=60,
            client=redis_client,
        )

        self.assertFalse(
            status.exceeded
        )

        self.assertEqual(
            status.current,
            3,
        )

        self.assertEqual(
            status.remaining,
            7,
        )

        self.assertEqual(
            status.retry_after_seconds,
            42,
        )

    def test_rate_limit_detects_exceeded_bucket(
        self,
    ) -> None:
        redis_client = FakeRedis(
            result=[
                11,
                25,
            ]
        )

        status = consume_rate_limit(
            identity="consumer-1",
            request_limit=10,
            window_seconds=60,
            client=redis_client,
        )

        self.assertTrue(
            status.exceeded
        )

        self.assertEqual(
            status.remaining,
            0,
        )

    def test_invalid_rate_limit_configuration_is_rejected(
        self,
    ) -> None:
        with self.assertRaises(
            RateLimitConfigurationError
        ):
            consume_rate_limit(
                identity="consumer-1",
                request_limit=0,
                window_seconds=60,
                client=FakeRedis(
                    result=[1, 60]
                ),
            )

    def test_redis_errors_are_wrapped(
        self,
    ) -> None:
        redis_client = FakeRedis(
            error=RuntimeError(
                "Redis unavailable"
            )
        )

        with self.assertRaises(
            RateLimitBackendError
        ):
            consume_rate_limit(
                identity="consumer-1",
                request_limit=10,
                window_seconds=60,
                client=redis_client,
            )

    def test_invalid_redis_response_is_rejected(
        self,
    ) -> None:
        redis_client = FakeRedis(
            result=[
                1,
            ]
        )

        with self.assertRaises(
            RateLimitBackendError
        ):
            consume_rate_limit(
                identity="consumer-1",
                request_limit=10,
                window_seconds=60,
                client=redis_client,
            )


if __name__ == "__main__":
    unittest.main()