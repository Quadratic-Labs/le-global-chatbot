"""
Shared structured-error contract and sanitized logging for admin
document lifecycle failures (upload, reindex, delete).

Mission "ORDER 2": a business exception's own str() was reaching
neither the logs nor a useful HTTP response - routers were collapsing
every business exception into one fixed, generic sentence (see
git history of admin_document_lifecycle.py and admin_documents.py),
so operators had no way to tell "the source file is missing" from
"the DOCX could not be parsed" from "rollback also failed" without
reproducing the failure by hand. This module gives every admin
lifecycle router one place to log the real exception (never a raw
document body, never an API/admin key - those never appear in any of
these exception messages to begin with, since they only ever carry
identifiers, counts, and fixed English sentences) and to return the
same structured {"code", "message", "operation", ...} JSON shape the
upload router already used for AdminDocumentReplacementRequiredError/
AdminDocumentAlreadyCurrentError, rather than inventing a second,
inconsistent shape.
"""

from __future__ import annotations

import json
import logging
from typing import Any


logger = logging.getLogger("app.admin")


def log_admin_business_error(
    *,
    operation: str,
    error: Exception,
    document_id: str | None = None,
    country_code: str | None = None,
) -> None:
    """Log one admin lifecycle business exception as one JSON line."""

    logger.error(
        "%s",
        json.dumps(
            {
                "event": "admin_document_lifecycle_error",
                "operation": operation,
                "document_id": document_id,
                "country_code": country_code,
                "error_type": type(error).__name__,
                "message": str(error),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ),
    )


def admin_error_detail(
    *,
    code: str,
    message: str,
    operation: str,
    document_id: str | None = None,
) -> dict[str, Any]:
    """
    Build the structured JSON body returned to WordPress for one admin
    lifecycle failure - document_id is only ever included when known
    (mission wording: "si disponible").
    """

    detail: dict[str, Any] = {
        "code": code,
        "message": message,
        "operation": operation,
    }

    if document_id is not None:
        detail["document_id"] = document_id

    return detail
