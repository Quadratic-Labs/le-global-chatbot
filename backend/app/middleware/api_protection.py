"""Protect public API endpoints with an access key and rate limiting."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from hmac import compare_digest
from typing import Any, Final

from fastapi import Request
from starlette.middleware.base import (
    BaseHTTPMiddleware,
    RequestResponseEndpoint,
)
from starlette.responses import JSONResponse, Response

from app.clients.redis import get_redis_client
from app.core.config import get_settings


API_KEY_HEADER: Final[str] = "X-API-Key"
PROTECTED_API_PREFIX: Final[str] = "/api/v1"

RATE_LIMIT_SCRIPT: Final[str] = """
local current = redis.call("INCR", KEYS[1])

if current == 1 then
    redis.call("EXPIRE", KEYS[1], ARGV[1])
end

local ttl = redis.call("TTL", KEYS[1])

return {current, ttl}
""".strip()


class RateLimitConfigurationError(ValueError):
    """Raised when rate-limit settings are invalid."""


class RateLimitBackendError(RuntimeError):
    """Raised when Redis cannot enforce the rate limit."""


@dataclass(frozen=True, slots=True)
class RateLimitStatus:
    """Result of consuming one request from a rate-limit bucket."""

    limit: int
    current: int
    remaining: int
    retry_after_seconds: int

    @property
    def exceeded(self) -> bool:
        """Return whether the request exceeds the configured limit."""

        return self.current > self.limit


def api_key_matches(
    provided_key: str | None,
    expected_key: str | None,
) -> bool:
    """Validate an API key without exposing timing differences."""

    if not provided_key or not expected_key:
        return False

    return compare_digest(
        provided_key.strip(),
        expected_key.strip(),
    )


def resolve_client_ip(
    request: Request,
) -> str:
    """
    Resolve the client address.

    The backend is bound to localhost in production, so forwarded
    headers can only be supplied through the local reverse proxy.
    """

    forwarded_for = request.headers.get(
        "x-forwarded-for"
    )

    if forwarded_for:
        forwarded_ip = forwarded_for.split(
            ",",
            maxsplit=1,
        )[0].strip()

        if forwarded_ip:
            return forwarded_ip

    real_ip = request.headers.get(
        "x-real-ip"
    )

    if real_ip and real_ip.strip():
        return real_ip.strip()

    if request.client is not None:
        return request.client.host

    return "unknown"


def build_rate_limit_identity(
    client_ip: str,
    api_key: str,
) -> str:
    """Build a non-sensitive identity for one API consumer."""

    api_key_digest = hashlib.sha256(
        api_key.encode("utf-8")
    ).hexdigest()

    return f"{client_ip}:{api_key_digest}"


def _build_rate_limit_key(
    identity: str,
) -> str:
    """Build the Redis key without storing the raw identity."""

    identity_digest = hashlib.sha256(
        identity.encode("utf-8")
    ).hexdigest()

    return (
        "le-global:"
        "api-rate-limit:"
        f"{identity_digest}"
    )


def consume_rate_limit(
    identity: str,
    request_limit: int,
    window_seconds: int,
    client: Any | None = None,
) -> RateLimitStatus:
    """Atomically consume one request from a Redis rate-limit bucket."""

    if request_limit <= 0:
        raise RateLimitConfigurationError(
            "request_limit must be greater than zero."
        )

    if window_seconds <= 0:
        raise RateLimitConfigurationError(
            "window_seconds must be greater than zero."
        )

    redis_client = (
        client
        if client is not None
        else get_redis_client()
    )

    redis_key = _build_rate_limit_key(
        identity
    )

    try:
        result = redis_client.eval(
            RATE_LIMIT_SCRIPT,
            1,
            redis_key,
            window_seconds,
        )

    except Exception as error:
        raise RateLimitBackendError(
            "Redis rate-limit operation failed."
        ) from error

    if (
        not isinstance(result, (list, tuple))
        or len(result) != 2
    ):
        raise RateLimitBackendError(
            "Redis returned an invalid rate-limit response."
        )

    try:
        current = int(result[0])
        ttl = int(result[1])

    except (TypeError, ValueError) as error:
        raise RateLimitBackendError(
            "Redis returned invalid rate-limit values."
        ) from error

    if ttl <= 0:
        ttl = window_seconds

    return RateLimitStatus(
        limit=request_limit,
        current=current,
        remaining=max(
            request_limit - current,
            0,
        ),
        retry_after_seconds=ttl,
    )


def _is_protected_request(
    request: Request,
) -> bool:
    """Return whether the request belongs to the protected API."""

    path = request.url.path

    return (
        path == PROTECTED_API_PREFIX
        or path.startswith(
            f"{PROTECTED_API_PREFIX}/"
        )
    )


def _rate_limit_headers(
    status: RateLimitStatus,
) -> dict[str, str]:
    """Build standard rate-limit response headers."""

    return {
        "X-RateLimit-Limit": str(
            status.limit
        ),
        "X-RateLimit-Remaining": str(
            status.remaining
        ),
        "X-RateLimit-Reset": str(
            status.retry_after_seconds
        ),
    }


class ApiProtectionMiddleware(
    BaseHTTPMiddleware
):
    """Protect `/api/v1` routes with an API key and Redis limit."""

    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        """Validate and rate-limit one HTTP request."""

        if (
            request.method == "OPTIONS"
            or not _is_protected_request(request)
        ):
            return await call_next(
                request
            )

        settings = get_settings()

        if not settings.api_access_key:
            return JSONResponse(
                status_code=503,
                content={
                    "detail": (
                        "API access protection "
                        "is not configured."
                    )
                },
            )

        provided_api_key = request.headers.get(
            API_KEY_HEADER
        )

        if not api_key_matches(
            provided_key=provided_api_key,
            expected_key=settings.api_access_key,
        ):
            return JSONResponse(
                status_code=401,
                content={
                    "detail": (
                        "Invalid or missing "
                        "API access key."
                    )
                },
                headers={
                    "WWW-Authenticate": "ApiKey",
                },
            )

        assert provided_api_key is not None

        identity = build_rate_limit_identity(
            client_ip=resolve_client_ip(
                request
            ),
            api_key=provided_api_key,
        )

        try:
            rate_limit_status = consume_rate_limit(
                identity=identity,
                request_limit=(
                    settings.rate_limit_requests
                ),
                window_seconds=(
                    settings.rate_limit_window_seconds
                ),
            )

        except RateLimitBackendError:
            return JSONResponse(
                status_code=503,
                content={
                    "detail": (
                        "The API rate-limit service "
                        "is temporarily unavailable."
                    )
                },
            )

        headers = _rate_limit_headers(
            rate_limit_status
        )

        if rate_limit_status.exceeded:
            headers["Retry-After"] = str(
                rate_limit_status.retry_after_seconds
            )

            return JSONResponse(
                status_code=429,
                content={
                    "detail": (
                        "API request limit exceeded."
                    )
                },
                headers=headers,
            )

        response = await call_next(
            request
        )

        for header_name, header_value in headers.items():
            response.headers[
                header_name
            ] = header_value

        return response