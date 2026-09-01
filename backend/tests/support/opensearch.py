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
router/ASGI/upload/lifecycle tests, which need a stateful, chunk-granular
in-memory index (add/search/delete_by_query all operating on the same
mutable chunk set) rather than one canned response - see
FakeAdminOpenSearch below for that shape. It coexists here as a
separate class rather than sharing an implementation with this
read-only search fake: the two protect genuinely different production
code (read-only legal-search/catalog queries vs. transactional admin
document mutation), and forcing them to share a base would only make
both harder to read.
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


class FakeAdminOpenSearch:
    """
    Stateful, chunk-granular OpenSearch test double for the admin
    document upload/replace/lifecycle/router/ASGI test suites.

    One dict per real chunk (self.chunks), matching the real index's
    own per-chunk granularity - one document commonly has several
    chunks, each its own hit. Handles every query shape the admin
    code issues:

    - the aggregation-only body (list_indexed_documents /
      get_admin_document_stats): one document_id bucket per group,
      built from the real in-memory chunks, never a second,
      hand-maintained fixture;
    - a plain query.term.document_id or query.term.country_code
      lookup: every matching chunk, never deduplicated by
      document_id - callers that want distinct documents (e.g.
      lookup_existing_country_documents) already dedupe client-side,
      and callers that need the true per-document chunk count (e.g.
      the transactional rollback machinery's own snapshot/expected-
      chunks validation) require every chunk hit;
    - delete_by_query: a flat term.document_id or term.country_code
      deletes every matching chunk; the bool/filter+must_not "keep
      these chunk ids" shape (_delete_country_chunks /
      _delete_chunks_except) deletes every match NOT in that
      keep-list.

    A prior consolidation pass found a real behavioral split between
    this fake's two previous, independently-maintained copies: one
    (router-integration) simulated genuine deletion; the other (ASGI)
    stubbed delete_by_query as a permanent no-op returning
    {"deleted": 0}. Every ASGI-transport test was checked and none of
    them triggers a delete at all, so the real-deletion behavior below
    is safe for both call sites - there is exactly one behavior now,
    not a silent, untested divergence between two copies.
    """

    def __init__(self) -> None:
        self.chunks: dict[str, dict[str, Any]] = {}

    def add(
        self,
        *,
        document_id: str,
        country_code: str,
        source_filename: str,
        chunk_id: str | None = None,
        country: str | None = None,
        language: str = "en",
        document_type: str = "comparator",
        reference_year: int | None = 2026,
    ) -> None:
        resolved_chunk_id = chunk_id or f"{document_id}-chunk-0"
        self.chunks[resolved_chunk_id] = {
            "document_id": document_id,
            "chunk_id": resolved_chunk_id,
            "country_code": country_code,
            "source_filename": source_filename,
            "country": country or country_code,
            "reference_year": reference_year,
            "language": language,
            "document_type": document_type,
        }

    def document_ids_for_country(self, country_code: str) -> set[str]:
        return {
            chunk["document_id"]
            for chunk in self.chunks.values()
            if chunk["country_code"] == country_code
        }

    def search(
        self,
        *,
        index: str,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        del index

        if "query" not in body:
            # build_admin_document_catalog_body()'s aggregation shape
            # (list_indexed_documents/get_admin_document_stats) - one
            # bucket per distinct document_id, built from the real
            # in-memory chunks rather than a second, hardcoded fixture.
            by_document: dict[str, list[dict[str, Any]]] = {}

            for chunk in self.chunks.values():
                by_document.setdefault(
                    chunk["document_id"], []
                ).append(chunk)

            return {
                "aggregations": {
                    "documents": {
                        "buckets": [
                            {
                                "key": document_id,
                                "doc_count": len(chunks),
                                "metadata": {
                                    "hits": {
                                        "hits": [
                                            {"_source": chunks[0]}
                                        ]
                                    }
                                },
                            }
                            for document_id, chunks in sorted(
                                by_document.items()
                            )
                        ]
                    }
                }
            }

        term = body["query"].get("term") or {}

        if "country_code" in term:
            hits = [
                chunk
                for chunk in self.chunks.values()
                if chunk["country_code"] == term["country_code"]
            ]
        elif "document_id" in term:
            hits = [
                chunk
                for chunk in self.chunks.values()
                if chunk["document_id"] == term["document_id"]
            ]
        else:
            hits = list(self.chunks.values())

        return {
            "hits": {
                "total": {"value": len(hits)},
                "hits": [
                    {
                        "_id": chunk["chunk_id"],
                        "_source": chunk,
                        # Real OpenSearch 3.7 includes a "sort" array
                        # per hit whenever the request itself carries
                        # a "sort" clause, as the admin code's own
                        # search_after pagination always does.
                        "sort": [chunk["chunk_id"]],
                    }
                    for chunk in sorted(
                        hits, key=lambda chunk: chunk["chunk_id"]
                    )
                ],
            }
        }

    def delete_by_query(self, **kwargs: Any) -> dict[str, Any]:
        query = kwargs["body"]["query"]
        term = query.get("term")
        deleted = 0

        if term and "document_id" in term:
            target = term["document_id"]

            for chunk_id in [
                chunk_id
                for chunk_id, chunk in self.chunks.items()
                if chunk["document_id"] == target
            ]:
                del self.chunks[chunk_id]
                deleted += 1

        elif term and "country_code" in term:
            country = term["country_code"]

            for chunk_id in [
                chunk_id
                for chunk_id, chunk in self.chunks.items()
                if chunk["country_code"] == country
            ]:
                del self.chunks[chunk_id]
                deleted += 1

        elif "bool" in query:
            country = query["bool"]["filter"][0]["term"]["country_code"]
            keep_ids = set(
                query["bool"]["must_not"][0]["terms"]["chunk_id"]
            )

            for chunk_id in [
                chunk_id
                for chunk_id, chunk in self.chunks.items()
                if chunk["country_code"] == country
                and chunk_id not in keep_ids
            ]:
                del self.chunks[chunk_id]
                deleted += 1

        return {"deleted": deleted}


def bulk_writer_for(fake: FakeAdminOpenSearch):
    """
    A `bulk()` side_effect that writes every action into fake's own
    self.chunks - shared by every admin test module that patches
    app.services.document_indexer.bulk, so replace_*_chunks' real
    effect on the fake's state can be asserted afterward rather than
    merely inferred from a mock's own call arguments.
    """

    def _bulk(client: Any, actions: Any, **kwargs: Any):
        del client, kwargs

        action_list = list(actions)

        for action in action_list:
            doc = action["_source"]
            fake.add(
                document_id=doc["document_id"],
                chunk_id=doc["chunk_id"],
                country_code=doc["country_code"],
                source_filename=doc["source_filename"],
            )

        return (len(action_list), [])

    return _bulk
