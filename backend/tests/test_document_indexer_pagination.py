"""
Mission "ORDER 3B" - unit tests for _fetch_all_chunks, the centralized
exhaustive chunk-fetch helper introduced to fix the >10,000-chunk
snapshot bug (a single size=10000 search silently treating
OpenSearch's own "at least 10000" default total-hit-tracking ceiling
as if it were the real, exact count).

PaginatingFakeOpenSearch below is a genuine, multi-page fake - unlike
the simpler single-page fakes used elsewhere in this suite (none of
which needed more than one page before this mission), it actually
splits a dataset across pages and honors search_after, so these tests
exercise the real pagination loop, not just a single bounded response.
"""

from __future__ import annotations

import bisect
import unittest
from typing import Any

from opensearchpy.exceptions import OpenSearchException

from app.services.document_indexer import (
    _EXHAUSTIVE_FETCH_PAGE_SIZE,
    DocumentIndexingError,
    _fetch_all_chunks,
)


class PaginatingFakeOpenSearch:
    """
    Genuinely paginates a fixed set of chunk_ids via search_after, on
    the same "chunk_id asc" sort _fetch_all_chunks itself requests -
    mirrors real OpenSearch 3.7's response shape exactly (hits.total,
    per-hit "sort" arrays), not an imagined one (mission "ORDER 3B",
    section 6).
    """

    def __init__(
        self,
        *,
        chunk_ids: list[str],
        reported_total: int | None = None,
    ) -> None:
        self._all_ids = sorted(chunk_ids)
        self._reported_total = (
            reported_total
            if reported_total is not None
            else len(self._all_ids)
        )
        self.search_calls = 0
        self.raise_on_call: int | None = None
        self.inject_duplicate_on_call: int | None = None
        self.force_empty_intermediate_page_on_call: int | None = None

    def search(
        self,
        *,
        index: str,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        del index

        self.search_calls += 1

        if self.raise_on_call == self.search_calls:
            raise OpenSearchException(
                "simulated exhaustive fetch page failure"
            )

        size = body["size"]
        search_after = body.get("search_after")

        if search_after is None:
            start_index = 0

        else:
            cursor = search_after[0]
            start_index = bisect.bisect_right(
                self._all_ids, cursor
            )

        if (
            self.force_empty_intermediate_page_on_call
            == self.search_calls
        ):
            page_ids: list[str] = []

        else:
            page_ids = self._all_ids[
                start_index:start_index + size
            ]

        hits = [
            {
                "_id": chunk_id,
                "_source": {
                    "chunk_id": chunk_id,
                    "document_id": "doc_" + "a" * 64,
                    "country_code": "ZZ",
                },
                "sort": [chunk_id],
            }
            for chunk_id in page_ids
        ]

        if (
            self.inject_duplicate_on_call == self.search_calls
            and hits
        ):
            hits.append(dict(hits[0]))

        return {
            "hits": {
                "total": {"value": self._reported_total},
                "hits": hits,
            }
        }


def _chunk_ids(count: int) -> list[str]:
    # Zero-padded so lexicographic ("chunk_id asc") order matches
    # numeric order, exactly like real, fixed-width SHA-based chunk
    # ids would sort consistently regardless of value.
    return [f"chunk_{i:06d}" for i in range(count)]


class FetchAllChunksPaginationTests(unittest.TestCase):
    def test_zero_hits(self) -> None:
        client = PaginatingFakeOpenSearch(chunk_ids=[])

        result = _fetch_all_chunks(
            client=client, field="document_id", value="doc_x"
        )

        self.assertEqual(result, [])
        self.assertEqual(client.search_calls, 1)

    def test_one_hit(self) -> None:
        client = PaginatingFakeOpenSearch(
            chunk_ids=_chunk_ids(1)
        )

        result = _fetch_all_chunks(
            client=client, field="document_id", value="doc_x"
        )

        self.assertEqual(len(result), 1)

    def test_page_size_minus_one(self) -> None:
        client = PaginatingFakeOpenSearch(
            chunk_ids=_chunk_ids(_EXHAUSTIVE_FETCH_PAGE_SIZE - 1)
        )

        result = _fetch_all_chunks(
            client=client, field="document_id", value="doc_x"
        )

        self.assertEqual(
            len(result), _EXHAUSTIVE_FETCH_PAGE_SIZE - 1
        )
        self.assertEqual(client.search_calls, 1)

    def test_exactly_page_size(self) -> None:
        client = PaginatingFakeOpenSearch(
            chunk_ids=_chunk_ids(_EXHAUSTIVE_FETCH_PAGE_SIZE)
        )

        result = _fetch_all_chunks(
            client=client, field="document_id", value="doc_x"
        )

        self.assertEqual(
            len(result), _EXHAUSTIVE_FETCH_PAGE_SIZE
        )
        # A full first page cannot be assumed to be the last one - one
        # more round trip confirms exhaustion via an empty page.
        self.assertEqual(client.search_calls, 2)

    def test_page_size_plus_one(self) -> None:
        client = PaginatingFakeOpenSearch(
            chunk_ids=_chunk_ids(_EXHAUSTIVE_FETCH_PAGE_SIZE + 1)
        )

        result = _fetch_all_chunks(
            client=client, field="document_id", value="doc_x"
        )

        self.assertEqual(
            len(result), _EXHAUSTIVE_FETCH_PAGE_SIZE + 1
        )
        self.assertEqual(client.search_calls, 2)

    def test_several_pages(self) -> None:
        count = _EXHAUSTIVE_FETCH_PAGE_SIZE * 3 + 250
        client = PaginatingFakeOpenSearch(
            chunk_ids=_chunk_ids(count)
        )

        result = _fetch_all_chunks(
            client=client, field="document_id", value="doc_x"
        )

        self.assertEqual(len(result), count)
        self.assertEqual(
            {hit["_id"] for hit in result},
            set(_chunk_ids(count)),
        )

    def test_exactly_10000(self) -> None:
        client = PaginatingFakeOpenSearch(
            chunk_ids=_chunk_ids(10000)
        )

        result = _fetch_all_chunks(
            client=client, field="document_id", value="doc_x"
        )

        self.assertEqual(len(result), 10000)

    def test_10001_the_original_boundary(self) -> None:
        # One past OpenSearch's own default total-hit-tracking/
        # max_result_window ceiling - the exact boundary the original
        # bug lived on.
        client = PaginatingFakeOpenSearch(
            chunk_ids=_chunk_ids(10001)
        )

        result = _fetch_all_chunks(
            client=client, field="document_id", value="doc_x"
        )

        self.assertEqual(len(result), 10001)

    def test_14083_the_real_mission_document(self) -> None:
        # The exact chunk count ORDER 3's real ~22.4 MB fixture
        # produced against real OpenSearch 3.7.
        client = PaginatingFakeOpenSearch(
            chunk_ids=_chunk_ids(14083)
        )

        result = _fetch_all_chunks(
            client=client, field="document_id", value="doc_x"
        )

        self.assertEqual(len(result), 14083)
        self.assertEqual(
            {hit["_id"] for hit in result},
            set(_chunk_ids(14083)),
        )

    def test_incoherent_intermediate_empty_page_is_rejected(
        self,
    ) -> None:
        # The reported total promises more chunks exist than an empty
        # intermediate page actually returned - must never be treated
        # as "done", must be reported as an explicit error instead of
        # silently returning a short result.
        count = _EXHAUSTIVE_FETCH_PAGE_SIZE + 500
        client = PaginatingFakeOpenSearch(
            chunk_ids=_chunk_ids(count)
        )
        client.force_empty_intermediate_page_on_call = 2

        with self.assertRaises(DocumentIndexingError) as context:
            _fetch_all_chunks(
                client=client, field="document_id", value="doc_x"
            )

        self.assertIn(
            "did not exhaust", str(context.exception)
        )

    def test_exception_on_page_2_propagates(self) -> None:
        count = _EXHAUSTIVE_FETCH_PAGE_SIZE + 10
        client = PaginatingFakeOpenSearch(
            chunk_ids=_chunk_ids(count)
        )
        client.raise_on_call = 2

        with self.assertRaises(DocumentIndexingError) as context:
            _fetch_all_chunks(
                client=client, field="document_id", value="doc_x"
            )

        self.assertIn(
            "exhaustive chunk fetch failed",
            str(context.exception),
        )

    def test_exception_on_final_page_propagates(self) -> None:
        count = _EXHAUSTIVE_FETCH_PAGE_SIZE * 2
        client = PaginatingFakeOpenSearch(
            chunk_ids=_chunk_ids(count)
        )
        # Page 1 and 2 are full pages (exactly page_size each); page 3
        # is the empty page that would normally confirm exhaustion.
        client.raise_on_call = 3

        with self.assertRaises(DocumentIndexingError):
            _fetch_all_chunks(
                client=client, field="document_id", value="doc_x"
            )

    def test_duplicate_hit_across_pages_is_rejected(self) -> None:
        count = _EXHAUSTIVE_FETCH_PAGE_SIZE + 10
        client = PaginatingFakeOpenSearch(
            chunk_ids=_chunk_ids(count)
        )
        client.inject_duplicate_on_call = 2

        with self.assertRaises(DocumentIndexingError) as context:
            _fetch_all_chunks(
                client=client, field="document_id", value="doc_x"
            )

        self.assertIn(
            "same chunk twice across pages",
            str(context.exception),
        )

    def test_scroll_contexts_not_applicable(self) -> None:
        # Mission "ORDER 3B", section 6: "clear_scroll sur exception
        # si scroll utilisé." This implementation deliberately chose
        # search_after (section 3) specifically because chunk_id is a
        # real, verified-unique, sortable keyword field - so there is
        # no server-side scroll context ever opened, and therefore
        # nothing to leak or clear. This test exists only to make that
        # choice explicit and checkable, not to exercise any code.
        import app.services.document_indexer as document_indexer_module

        source = document_indexer_module.__file__

        with open(source) as f:
            contents = f.read()

        self.assertNotIn("scroll", contents.lower())


if __name__ == "__main__":
    unittest.main()
