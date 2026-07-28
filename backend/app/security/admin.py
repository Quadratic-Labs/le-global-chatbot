"""Authentication for document administration routes."""

from __future__ import annotations

from hmac import compare_digest
from typing import Final

from fastapi import (
    Header,
    HTTPException,
    status,
)

from app.core.config import get_settings


ADMIN_KEY_HEADER: Final[str] = "X-Admin-Key"


def admin_key_matches(
    provided_key: str | None,
    expected_key: str | None,
) -> bool:
    """Validate an administration key safely."""

    if not provided_key or not expected_key:
        return False

    return compare_digest(
        provided_key.strip(),
        expected_key.strip(),
    )


def require_admin_key(
    x_admin_key: str | None = Header(
        default=None,
        alias=ADMIN_KEY_HEADER,
    ),
) -> None:
    """Require the configured administration key."""

    settings = get_settings()

    if not settings.admin_api_key:
        raise HTTPException(
            status_code=(
                status.HTTP_503_SERVICE_UNAVAILABLE
            ),
            detail=(
                "Document administration "
                "is not configured."
            ),
        )

    if not admin_key_matches(
        provided_key=x_admin_key,
        expected_key=settings.admin_api_key,
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=(
                "Invalid or missing "
                "administration key."
            ),
            headers={
                "WWW-Authenticate": "AdminApiKey",
            },
        )