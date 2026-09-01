"""
Shared minimal OpenSearch test double for read-only query tests: records
the last index/body it was called with, raises a configured error if one
was given, otherwise returns a configured canned response.

Extracted from test_legal_search.py and test_legal_catalog.py, which each
defined an identical `FakeOpenSearchClient` class. Callers that need a
default response now pass it explicitly (there is no implicit fallback
shape here) - the two call sites that previously relied on an implicit
default only ever needed it for a code path that never inspects it (an
empty-country-codes short-circuit that never calls OpenSearch at all, and
a test that replaces `.search` itself), so no behavior changed.

This is deliberately NOT the right double for the admin document
lifecycle/replacement/upload tests, which need a stateful, chunk-granular
in-memory index (add/search/delete_by_query all operating on the same
mutable chunk set) rather than one canned response - see
test_admin_documents_router_integration.py's own FakeOpenSearch for that
shape, which test_admin_documents_asgi.py's near-identical copy should
eventually be reconciled with directly (deferred - its delete_by_query is
currently a no-op stub while the router-integration version simulates
real deletion, a genuine behavioral difference that needs verifying
call-site by call-site before it's safe to unify).
"""

from __future__ import annotations

from typing import Any


class FakeOpenSearchClient:
    """Minimal single-canned-response OpenSearch client for unit tests."""

    def __init__(
        self,
        response: dict[str, Any] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.response = response
        self.error = error
        self.index: str | None = None
        self.body: dict[str, Any] | None = None

    def search(
        self,
        index: str,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        self.index = index
        self.body = body

        if self.error is not None:
            raise self.error

        return self.response or {}
