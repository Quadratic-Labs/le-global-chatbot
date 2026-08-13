"""
Tests for admin effective-section editing (mission "ORDER 5C").

Country -> Section -> current effective content -> Edit -> Save. These
exercise the full CRUD surface (list/get/update) in
app.services.admin_document_sections against a Fake OpenSearch double,
mirroring the FakeOpenSearchClient style already established in
test_admin_document_lifecycle.py/test_admin_documents.py: plain
unittest.TestCase, tempfile.TemporaryDirectory() for source_directory,
explicit dependency injection rather than a mocking framework.

One exception, deliberate and consistent with the rest of this
codebase: replace_document_section_chunks (app/services/
document_indexer.py) calls its own module-level `ensure_legal_
documents_index`/`bulk` helpers directly - admin_document_sections.py
has no `client`-only seam for the indexing step itself (only for
search/delete_by_query), exactly like replace_country_document_chunks
already has none in test_admin_document_replacement.py's own
CountryIndexerTests. unittest.mock.patch is the only way to reach
that internal seam, the same technique already used there - never a
broader mocking framework, and never anything beyond that one narrow
seam.
"""

from __future__ import annotations

import contextlib
import copy
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from docx import Document
from opensearchpy.exceptions import OpenSearchException

from app.services.admin_document_lifecycle import (
    AdminDocumentRollbackError,
)
from app.services.admin_document_sections import (
    AdminDocumentSectionInvalidError,
    AdminDocumentSectionNotFoundError,
    AdminDocumentSectionUpdateFailedError,
    get_effective_section,
    list_effective_sections,
    update_effective_section,
)
from app.services.document_chunk_builder import (
    DocumentMetadata,
    build_document_chunks,
)
from app.services.document_indexer import (
    DocumentIndexingError,
    replace_document_section_chunks,
)
from app.services.document_section_state import (
    SectionEdit,
    SectionEditState,
    read_section_edit_state,
    section_id_for_legal_topic,
    write_section_edit_state_atomic,
)
from app.services.docx_parser import ParsedSection


def _real_document_id_for(
    country_code: str,
    language: str = "en",
) -> str:
    """
    The one real, deterministic document_id build_document_chunks
    would compute for this country_code/language (see this mission's
    own "Determinism facts": document_id is derived solely from
    country_code + DOCUMENT_FAMILY + language, never from anything a
    test happens to pick). update_effective_section rebuilds every
    edited section's chunk through build_document_chunks itself
    (never trusting the document_id it was called with for that), so
    a seed/fixture chunk that does not carry this exact same,
    independently-computed document_id would never actually collide
    or interact with a real edit's own chunk - computed here via one
    real, throwaway call rather than re-deriving/hardcoding the
    sha256 formula in a test.
    """

    probe_chunks = build_document_chunks(
        [
            ParsedSection(
                section="Employment Contracts",
                subsection=None,
                content="probe content",
            ),
        ],
        DocumentMetadata(
            country="United Kingdom",
            country_code=country_code,
            reference_year=None,
            language=language,
            source_filename="probe.docx",
        ),
    )

    return probe_chunks[0].document_id


DOCUMENT_ID = _real_document_id_for("GB")
OTHER_DOCUMENT_ID = "doc_" + "b" * 64

EMPLOYMENT_CONTRACTS_SECTION_ID = section_id_for_legal_topic(
    "Employment Contracts"
)
HIRING_PRACTICES_SECTION_ID = section_id_for_legal_topic(
    "Hiring Practices"
)


def _write_docx(
    path: Path,
    sections: list[tuple[str, str]],
) -> None:
    """
    Build one minimal, real DOCX with a Heading 1 (a genuine L&E
    legal-topic name) followed by one content paragraph, per entry.
    """

    document = Document()

    for heading, content in sections:
        document.add_heading(heading, level=1)
        document.add_paragraph(content)

    document.save(path)


def _seed_chunk(
    *,
    document_id: str,
    legal_topic: str,
    content: str,
    country_code: str = "GB",
) -> dict[str, Any]:
    """One minimal OpenSearch-shaped chunk source dict for the fake."""

    return {
        "document_id": document_id,
        "country_code": country_code,
        "legal_topic": legal_topic,
        "content": content,
    }


class FakeSectionOpenSearchClient:
    """
    OpenSearch test double for admin_document_sections.py.

    Stateful over self.chunks (chunk_id -> source dict), so a real
    edit's effect (a stale chunk removed, a new/overwritten chunk
    present) can be asserted afterwards, not merely inferred from a
    mock's own call arguments - the same idea as
    ReindexTransactionOpenSearchClient in
    test_admin_document_lifecycle.py. Paired at the call site with
    `bulk` patched to write real entries into this same self.chunks
    (see _bulk_writer/_patched_indexer below).
    """

    def __init__(
        self,
        *,
        document_id: str = DOCUMENT_ID,
        country_code: str = "GB",
        country: str = "United Kingdom",
        source_filename: str = "UK 2026.docx",
        reference_year: int | None = 2026,
        chunks: dict[str, dict[str, Any]] | None = None,
        fail_delete_by_query_calls: int = 0,
        delete_by_query_failure: Exception | None = None,
        fail_snapshot_search: bool = False,
        snapshot_search_failure: Exception | None = None,
    ) -> None:
        self.document_id = document_id
        self.country_code = country_code
        self.country = country
        self.source_filename = source_filename
        self.reference_year = reference_year
        self.chunks: dict[str, dict[str, Any]] = dict(chunks or {})
        self.delete_by_query_calls: list[dict[str, Any]] = []

        # Failure-injection knob (mirrors FakeCountryOpenSearch(
        # fail_cleanup=True)/CountryIndexerTests.test_country_indexer_
        # restores_snapshot_on_cleanup_failure in
        # test_admin_document_replacement.py): a simple call counter,
        # not a legal_topic-keyed set, since every rollback test here
        # only ever needs to fail exactly the FIRST delete_by_query
        # call (the original edit attempt's own stale-delete) while
        # leaving every later call (the rollback's own wipe, or a
        # second edit's own stale-delete) genuinely succeeding.
        self.fail_delete_by_query_calls = fail_delete_by_query_calls
        self.delete_by_query_failure = delete_by_query_failure
        self.delete_by_query_call_count = 0

        # Fails the exhaustive-snapshot search itself (mission
        # "ORDER 5C" corrective gate, section 3 follow-up): proves a
        # snapshot-fetch failure BEFORE any mutation is reported
        # through the same structured error contract as every other
        # failure point, never as a raw DocumentIndexingError
        # escaping unwrapped.
        self.fail_snapshot_search = fail_snapshot_search
        self.snapshot_search_failure = snapshot_search_failure

    def search(
        self,
        index: str,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        del index

        term = body.get("query", {}).get("term", {})
        requested_document_id = term.get("document_id")

        if "sort" in body:
            if self.fail_snapshot_search:
                raise (
                    self.snapshot_search_failure
                    if self.snapshot_search_failure is not None
                    else OpenSearchException(
                        "simulated snapshot search failure"
                    )
                )

            # _fetch_all_chunks' own exhaustive-snapshot shape:
            # track_total_hits + sort on chunk_id (search_after
            # pagination) - the mechanism replace_document_chunks/
            # replace_document_section_chunks now use to capture the
            # pre-mutation baseline they roll back to on failure.
            # This fake never has enough chunks to need a second
            # page, so one page always exhausts the result set.
            matching_ids = sorted(
                chunk_id
                for chunk_id, chunk in self.chunks.items()
                if chunk["document_id"] == requested_document_id
            )

            return {
                "hits": {
                    "total": {"value": len(matching_ids)},
                    "hits": [
                        {
                            "_id": chunk_id,
                            "_source": self.chunks[chunk_id],
                            "sort": [chunk_id],
                        }
                        for chunk_id in matching_ids
                    ],
                }
            }

        if "aggs" in body:
            # _real_topics_from_opensearch's own shape: size 0 + a
            # terms aggregation on legal_topic - only topics with a
            # real chunk for this document_id ever appear.
            matching = [
                chunk
                for chunk in self.chunks.values()
                if chunk["document_id"] == requested_document_id
            ]
            topics = sorted(
                {
                    chunk["legal_topic"]
                    for chunk in matching
                    if chunk.get("legal_topic")
                }
            )

            return {
                "aggregations": {
                    "topics": {
                        "buckets": [
                            {"key": topic, "doc_count": 1}
                            for topic in topics
                        ]
                    }
                }
            }

        if requested_document_id is not None:
            # _get_document_metadata's own shape: size 1, term on
            # document_id, no aggregation.
            if requested_document_id != self.document_id:
                return {"hits": {"hits": []}}

            return {
                "hits": {
                    "hits": [
                        {
                            "_source": {
                                "document_id": self.document_id,
                                "source_filename": (
                                    self.source_filename
                                ),
                                "country": self.country,
                                "country_code": self.country_code,
                                "reference_year": self.reference_year,
                            }
                        }
                    ]
                }
            }

        return {"hits": {"hits": []}}

    def delete_by_query(
        self,
        *,
        index: str,
        body: dict[str, Any],
        conflicts: str,
        refresh: bool,
    ) -> dict[str, Any]:
        del index, conflicts, refresh

        self.delete_by_query_call_count += 1

        if self.delete_by_query_call_count <= self.fail_delete_by_query_calls:
            # Fails BEFORE touching self.chunks at all - a genuine
            # delete_by_query failure never partially mutates state in
            # this fake, exactly like _delete_chunks_except's own
            # contract expects (either the deletion is real, or
            # nothing happened).
            raise (
                self.delete_by_query_failure
                if self.delete_by_query_failure is not None
                else RuntimeError(
                    "simulated delete_by_query failure (call #"
                    f"{self.delete_by_query_call_count})."
                )
            )

        # _delete_stale_section_chunks' own shape: bool/filter on
        # document_id AND legal_topic, must_not/terms on chunk_id.
        filters = body["query"]["bool"]["filter"]
        document_id = next(
            clause["term"]["document_id"]
            for clause in filters
            if "document_id" in clause.get("term", {})
        )
        legal_topic = next(
            clause["term"]["legal_topic"]
            for clause in filters
            if "legal_topic" in clause.get("term", {})
        )

        keep_ids: set[str] = set()

        for clause in body["query"]["bool"].get("must_not", []):
            keep_ids.update(
                clause.get("terms", {}).get("chunk_id", [])
            )

        to_delete = [
            chunk_id
            for chunk_id, chunk in self.chunks.items()
            if chunk["document_id"] == document_id
            and chunk.get("legal_topic") == legal_topic
            and chunk_id not in keep_ids
        ]

        for chunk_id in to_delete:
            del self.chunks[chunk_id]

        self.delete_by_query_calls.append(
            {
                "document_id": document_id,
                "legal_topic": legal_topic,
                "deleted": len(to_delete),
            }
        )

        return {"deleted": len(to_delete)}


class MustNotBeCalledClient:
    """
    A client double that fails any test relying on it - proves a
    code path really does short-circuit before ever touching
    OpenSearch (e.g. an invalid section_id, or empty content).
    """

    def search(self, index: str, body: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError(
            "OpenSearch search() must not be called for this path."
        )

    def delete_by_query(self, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError(
            "OpenSearch delete_by_query() must not be called for "
            "this path."
        )


def _bulk_writer(fake_client: FakeSectionOpenSearchClient):
    """
    A `bulk()` side_effect that writes every action into fake_client's
    own self.chunks, so replace_document_section_chunks' effect on the
    fake's state is real, not merely a recorded call - mirrors
    _reindex_bulk_fake in test_admin_document_lifecycle.py.
    """

    def fake_bulk(client, actions, **kwargs):
        del client, kwargs

        action_list = list(actions)

        for action in action_list:
            fake_client.chunks[action["_id"]] = dict(action["_source"])

        return (len(action_list), [])

    return fake_bulk


@contextlib.contextmanager
def _patched_indexer(fake_client: FakeSectionOpenSearchClient):
    """
    Patch the two document_indexer.py internals
    replace_document_section_chunks calls directly - see
    FakeSectionOpenSearchClient's own docstring for why this seam
    (rather than dependency injection) is unavoidable here.
    """

    with patch(
        "app.services.document_indexer.ensure_legal_documents_index"
    ), patch(
        "app.services.document_indexer.bulk",
        side_effect=_bulk_writer(fake_client),
    ):
        yield


class AdminDocumentSectionListTests(unittest.TestCase):
    def test_only_topics_with_real_chunks_are_listed(self) -> None:
        # Mission "ORDER 5C", section 20 - never a fixed list of all
        # 11 taxonomy topics; only ones this document actually has
        # chunks for right now.
        client = FakeSectionOpenSearchClient(
            chunks={
                "chunk-ec-1": _seed_chunk(
                    document_id=DOCUMENT_ID,
                    legal_topic="Employment Contracts",
                    content="irrelevant for listing",
                ),
                "chunk-hp-1": _seed_chunk(
                    document_id=DOCUMENT_ID,
                    legal_topic="Hiring Practices",
                    content="irrelevant for listing",
                ),
                # A chunk belonging to a completely different
                # document must never leak into this list.
                "chunk-other-1": _seed_chunk(
                    document_id=OTHER_DOCUMENT_ID,
                    legal_topic="Pay Equity Laws",
                    content="irrelevant for listing",
                ),
            }
        )

        response = list_effective_sections(
            document_id=DOCUMENT_ID,
            client=client,
        )

        self.assertEqual(response.document_id, DOCUMENT_ID)
        self.assertEqual(
            [section.legal_topic for section in response.sections],
            ["Employment Contracts", "Hiring Practices"],
        )
        self.assertEqual(
            {section.section_id for section in response.sections},
            {
                EMPLOYMENT_CONTRACTS_SECTION_ID,
                HIRING_PRACTICES_SECTION_ID,
            },
        )
        self.assertNotIn(
            "Pay Equity Laws",
            [section.legal_topic for section in response.sections],
        )


class AdminDocumentSectionGetTests(unittest.TestCase):
    def test_never_edited_falls_back_to_docx(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root)

            _write_docx(
                source_directory / "GB.docx",
                [
                    (
                        "Employment Contracts",
                        "DOCX text for employment contracts.",
                    ),
                ],
            )

            client = FakeSectionOpenSearchClient(
                chunks={
                    "chunk-ec-1": _seed_chunk(
                        document_id=DOCUMENT_ID,
                        legal_topic="Employment Contracts",
                        # OpenSearch chunk text is never the source of
                        # truth for a GET fallback - only the real DOCX
                        # is, structurally re-parsed.
                        content="irrelevant chunk text",
                    ),
                }
            )

            response = get_effective_section(
                document_id=DOCUMENT_ID,
                section_id=EMPLOYMENT_CONTRACTS_SECTION_ID,
                source_directory=source_directory,
                client=client,
            )

            self.assertEqual(
                response.content,
                "DOCX text for employment contracts.",
            )
            self.assertEqual(response.country_code, "GB")
            self.assertEqual(response.country_name, "United Kingdom")
            self.assertEqual(
                response.legal_topic, "Employment Contracts"
            )

    def test_previously_edited_returns_persisted_content_not_docx(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root)

            _write_docx(
                source_directory / "GB.docx",
                [
                    (
                        "Employment Contracts",
                        "STALE DOCX text - must never be returned.",
                    ),
                ],
            )

            write_section_edit_state_atomic(
                source_directory,
                SectionEditState(
                    document_id=DOCUMENT_ID,
                    country_code="GB",
                    sections={
                        EMPLOYMENT_CONTRACTS_SECTION_ID: SectionEdit(
                            legal_topic="Employment Contracts",
                            section="Employment Contracts",
                            subsection=None,
                            content="EDITED text - this must win.",
                        ),
                    },
                ),
            )

            client = FakeSectionOpenSearchClient(
                chunks={
                    "chunk-ec-1": _seed_chunk(
                        document_id=DOCUMENT_ID,
                        legal_topic="Employment Contracts",
                        content="irrelevant chunk text",
                    ),
                }
            )

            response = get_effective_section(
                document_id=DOCUMENT_ID,
                section_id=EMPLOYMENT_CONTRACTS_SECTION_ID,
                source_directory=source_directory,
                client=client,
            )

            self.assertEqual(
                response.content,
                "EDITED text - this must win.",
            )

    def test_invalid_section_id_is_not_found_without_touching_opensearch(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(
                AdminDocumentSectionNotFoundError
            ) as context:
                get_effective_section(
                    document_id=DOCUMENT_ID,
                    section_id="not-a-real-topic-slug",
                    source_directory=Path(root),
                    client=MustNotBeCalledClient(),
                )

            self.assertEqual(
                context.exception.to_detail()["code"],
                "document_section_not_found",
            )

    def test_valid_topic_never_indexed_and_never_edited_is_not_found(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root)

            client = FakeSectionOpenSearchClient(chunks={})

            with self.assertRaises(AdminDocumentSectionNotFoundError):
                get_effective_section(
                    document_id=DOCUMENT_ID,
                    section_id=EMPLOYMENT_CONTRACTS_SECTION_ID,
                    source_directory=source_directory,
                    client=client,
                )


class AdminDocumentSectionUpdateTests(unittest.TestCase):
    def test_update_success_indexes_and_persists(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root)

            _write_docx(
                source_directory / "GB.docx",
                [("Employment Contracts", "Original DOCX text.")],
            )

            client = FakeSectionOpenSearchClient(
                chunks={
                    "chunk-seed-ec": _seed_chunk(
                        document_id=DOCUMENT_ID,
                        legal_topic="Employment Contracts",
                        content="placeholder",
                    ),
                }
            )

            with _patched_indexer(client):
                response = update_effective_section(
                    document_id=DOCUMENT_ID,
                    section_id=EMPLOYMENT_CONTRACTS_SECTION_ID,
                    new_content="  New effective content.  ",
                    source_directory=source_directory,
                    client=client,
                )

            self.assertEqual(
                response.legal_topic, "Employment Contracts"
            )
            self.assertEqual(
                response.section_id, EMPLOYMENT_CONTRACTS_SECTION_ID
            )
            self.assertEqual(response.indexed_chunks, 1)

            # The stale seed chunk is gone; exactly one chunk remains
            # for this topic, holding the trimmed new content - the
            # real OpenSearch mutation the fake actually recorded.
            self.assertNotIn("chunk-seed-ec", client.chunks)
            remaining = [
                chunk
                for chunk in client.chunks.values()
                if chunk["legal_topic"] == "Employment Contracts"
            ]
            self.assertEqual(len(remaining), 1)
            self.assertEqual(
                remaining[0]["content"], "New effective content."
            )

            state = read_section_edit_state(source_directory, DOCUMENT_ID)
            self.assertIsNotNone(state)
            edit = state.sections[EMPLOYMENT_CONTRACTS_SECTION_ID]
            self.assertEqual(edit.legal_topic, "Employment Contracts")
            self.assertEqual(edit.section, "Employment Contracts")
            self.assertIsNone(edit.subsection)
            self.assertEqual(edit.content, "New effective content.")

    def test_empty_content_is_invalid_with_zero_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root)

            with self.assertRaises(
                AdminDocumentSectionInvalidError
            ) as context:
                update_effective_section(
                    document_id=DOCUMENT_ID,
                    section_id=EMPLOYMENT_CONTRACTS_SECTION_ID,
                    new_content="   \n\t  ",
                    source_directory=source_directory,
                    client=MustNotBeCalledClient(),
                )

            self.assertEqual(
                context.exception.to_detail()["code"],
                "document_section_invalid",
            )
            self.assertIsNone(
                read_section_edit_state(source_directory, DOCUMENT_ID)
            )

    def test_second_edit_fully_overwrites_first_no_history(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root)

            _write_docx(
                source_directory / "GB.docx",
                [("Employment Contracts", "Original DOCX text.")],
            )

            client = FakeSectionOpenSearchClient(
                chunks={
                    "chunk-seed-ec": _seed_chunk(
                        document_id=DOCUMENT_ID,
                        legal_topic="Employment Contracts",
                        content="placeholder",
                    ),
                }
            )

            with _patched_indexer(client):
                update_effective_section(
                    document_id=DOCUMENT_ID,
                    section_id=EMPLOYMENT_CONTRACTS_SECTION_ID,
                    new_content="First edit content.",
                    source_directory=source_directory,
                    client=client,
                )

                update_effective_section(
                    document_id=DOCUMENT_ID,
                    section_id=EMPLOYMENT_CONTRACTS_SECTION_ID,
                    new_content="Second edit content - final.",
                    source_directory=source_directory,
                    client=client,
                )

            response = get_effective_section(
                document_id=DOCUMENT_ID,
                section_id=EMPLOYMENT_CONTRACTS_SECTION_ID,
                source_directory=source_directory,
                client=client,
            )
            self.assertEqual(
                response.content, "Second edit content - final."
            )

            state = read_section_edit_state(source_directory, DOCUMENT_ID)
            self.assertEqual(len(state.sections), 1)
            self.assertEqual(
                state.sections[EMPLOYMENT_CONTRACTS_SECTION_ID].content,
                "Second edit content - final.",
            )

            # No history kept in OpenSearch either - exactly one chunk
            # for this topic, not one per edit.
            remaining = [
                chunk
                for chunk in client.chunks.values()
                if chunk["legal_topic"] == "Employment Contracts"
            ]
            self.assertEqual(len(remaining), 1)
            self.assertEqual(
                remaining[0]["content"], "Second edit content - final."
            )

    def test_independent_topics_never_affect_each_others_chunks(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root)

            _write_docx(
                source_directory / "GB.docx",
                [
                    ("Employment Contracts", "Original EC text."),
                    ("Hiring Practices", "Original HP text."),
                ],
            )

            client = FakeSectionOpenSearchClient(
                chunks={
                    "chunk-seed-ec": _seed_chunk(
                        document_id=DOCUMENT_ID,
                        legal_topic="Employment Contracts",
                        content="placeholder EC",
                    ),
                    "chunk-seed-hp": _seed_chunk(
                        document_id=DOCUMENT_ID,
                        legal_topic="Hiring Practices",
                        content="placeholder HP",
                    ),
                }
            )

            with _patched_indexer(client):
                update_effective_section(
                    document_id=DOCUMENT_ID,
                    section_id=EMPLOYMENT_CONTRACTS_SECTION_ID,
                    new_content="Edited EC content.",
                    source_directory=source_directory,
                    client=client,
                )

            # Topic B's seed chunk survives completely untouched -
            # _delete_stale_section_chunks' own staleness cleanup is
            # scoped to legal_topic as well as document_id.
            self.assertIn("chunk-seed-hp", client.chunks)
            self.assertEqual(
                client.chunks["chunk-seed-hp"]["content"],
                "placeholder HP",
            )
            self.assertEqual(
                client.delete_by_query_calls[-1]["legal_topic"],
                "Employment Contracts",
            )
            self.assertEqual(
                client.delete_by_query_calls[-1]["deleted"], 1
            )

            with _patched_indexer(client):
                update_effective_section(
                    document_id=DOCUMENT_ID,
                    section_id=HIRING_PRACTICES_SECTION_ID,
                    new_content="Edited HP content.",
                    source_directory=source_directory,
                    client=client,
                )

            # Topic A's own already-edited chunk stays untouched by
            # topic B's own later, independent edit.
            ec_chunks = [
                chunk
                for chunk in client.chunks.values()
                if chunk["legal_topic"] == "Employment Contracts"
            ]
            self.assertEqual(len(ec_chunks), 1)
            self.assertEqual(
                ec_chunks[0]["content"], "Edited EC content."
            )

            hp_chunks = [
                chunk
                for chunk in client.chunks.values()
                if chunk["legal_topic"] == "Hiring Practices"
            ]
            self.assertEqual(len(hp_chunks), 1)
            self.assertEqual(
                hp_chunks[0]["content"], "Edited HP content."
            )

            state = read_section_edit_state(source_directory, DOCUMENT_ID)
            self.assertEqual(
                set(state.sections.keys()),
                {
                    EMPLOYMENT_CONTRACTS_SECTION_ID,
                    HIRING_PRACTICES_SECTION_ID,
                },
            )

    def test_update_on_unknown_section_is_not_found_with_zero_mutation(
        self,
    ) -> None:
        # A section that was never indexed AND never edited cannot be
        # "created" through Edit - only an existing section may be
        # edited.
        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root)

            client = FakeSectionOpenSearchClient(chunks={})

            with self.assertRaises(AdminDocumentSectionNotFoundError):
                update_effective_section(
                    document_id=DOCUMENT_ID,
                    section_id=EMPLOYMENT_CONTRACTS_SECTION_ID,
                    new_content="Some content.",
                    source_directory=source_directory,
                    client=client,
                )

            self.assertEqual(client.chunks, {})
            self.assertIsNone(
                read_section_edit_state(source_directory, DOCUMENT_ID)
            )


class AdminDocumentSectionUpdateRollbackTests(unittest.TestCase):
    """
    Mission "ORDER 5C", section 26 - OpenSearch is mutated first, and
    the durable state file is committed only once that has fully
    succeeded. These tests probe both failure points directly against
    the real update_effective_section code path (never re-derived).
    """

    def test_opensearch_bulk_failure_never_writes_state(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root)

            _write_docx(
                source_directory / "GB.docx",
                [("Employment Contracts", "Original DOCX text.")],
            )

            client = FakeSectionOpenSearchClient(
                chunks={
                    "chunk-seed-ec": _seed_chunk(
                        document_id=DOCUMENT_ID,
                        legal_topic="Employment Contracts",
                        content="placeholder",
                    ),
                }
            )

            bulk_call_count = 0

            def selective_bulk(
                client: Any,
                actions: Any,
                **kwargs: Any,
            ) -> tuple[int, list[dict[str, Any]]]:
                nonlocal bulk_call_count

                del kwargs
                bulk_call_count += 1
                action_list = list(actions)

                if bulk_call_count == 1:
                    # Call #1 - the edit's OWN bulk write for the new
                    # content. Fails WITHOUT ever touching
                    # fake_client.chunks (a bulk item error is a
                    # genuine no-write-happened failure).
                    return (
                        0,
                        [
                            {
                                "index": {
                                    "_id": "whatever-id",
                                    "status": 500,
                                    "error": {
                                        "type": "simulated_error",
                                        "reason": (
                                            "simulated bulk failure"
                                        ),
                                    },
                                }
                            }
                        ],
                    )

                # Call #2+ - the rollback's OWN reindex-snapshot bulk
                # call (replace_document_section_chunks' internal
                # _restore_section_snapshot), which genuinely re-
                # indexes the pre-edit snapshot - behaves exactly like
                # the real _bulk_writer from here on.
                for action in action_list:
                    client.chunks[action["_id"]] = dict(action["_source"])

                return (len(action_list), [])

            with patch(
                "app.services.document_indexer."
                "ensure_legal_documents_index"
            ), patch(
                "app.services.document_indexer.bulk",
                side_effect=selective_bulk,
            ):
                with self.assertRaises(
                    AdminDocumentSectionUpdateFailedError
                ):
                    update_effective_section(
                        document_id=DOCUMENT_ID,
                        section_id=EMPLOYMENT_CONTRACTS_SECTION_ID,
                        new_content="New content that never lands.",
                        source_directory=source_directory,
                        client=client,
                    )

            # The state file is never written...
            self.assertIsNone(
                read_section_edit_state(source_directory, DOCUMENT_ID)
            )

            # ...and the seed chunk is back, RESTORED by the
            # rollback's own reindex call - replace_document_section_
            # chunks' internal rollback wipes whatever the failed
            # attempt left behind (here, nothing new - the bulk write
            # itself never landed) and then re-indexes the pre-edit
            # snapshot verbatim, so the seed chunk's presence proves
            # the rollback actually ran, not merely that OpenSearch
            # was "never touched".
            self.assertEqual(
                client.chunks["chunk-seed-ec"]["content"], "placeholder"
            )
            self.assertEqual(bulk_call_count, 2)

    def test_state_file_write_failure_rolls_opensearch_back_to_pre_edit_state(
        self,
    ) -> None:
        # Mission "ORDER 5C" corrective gate, section 1: an Edit has
        # only two allowed outcomes - fully applied, or exactly as
        # before - never OpenSearch=new with persisted state=old. This
        # replaces a PRIOR test that (incorrectly, per an explicit
        # corrective instruction) documented that asymmetry as
        # accepted; it is now forbidden, and update_effective_section
        # rolls OpenSearch back to the exact pre-edit snapshot when
        # the durable state-file commit fails after OpenSearch already
        # succeeded.
        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root)

            _write_docx(
                source_directory / "GB.docx",
                [("Employment Contracts", "Original DOCX text.")],
            )

            client = FakeSectionOpenSearchClient(
                chunks={
                    "chunk-seed-ec": _seed_chunk(
                        document_id=DOCUMENT_ID,
                        legal_topic="Employment Contracts",
                        content="placeholder",
                    ),
                }
            )

            # This document had no prior edit before this attempt -
            # deliberately, so the "exactly None" assertion below
            # covers the case where no state ever existed at all, not
            # merely "the NEW value is absent".
            before = copy.deepcopy(client.chunks)

            with _patched_indexer(client), patch(
                "app.services.admin_document_sections."
                "write_section_edit_state_atomic",
                side_effect=OSError("simulated disk failure"),
            ):
                with self.assertRaises(
                    AdminDocumentSectionUpdateFailedError
                ):
                    update_effective_section(
                        document_id=DOCUMENT_ID,
                        section_id=EMPLOYMENT_CONTRACTS_SECTION_ID,
                        new_content=(
                            "Indexed, then rolled back because the "
                            "durable state commit failed."
                        ),
                        source_directory=source_directory,
                        client=client,
                    )

            # Exact atomicity, not merely "the new content is absent":
            # every chunk - the new one removed, the old seed chunk
            # restored - matches the pre-call state dict-for-dict.
            self.assertEqual(client.chunks, before)

            # The durable state is exactly None - never partially
            # written, and never left claiming the new content either.
            self.assertIsNone(
                read_section_edit_state(source_directory, DOCUMENT_ID)
            )

            # No stray/temp state file survives on disk either -
            # write_section_edit_state_atomic is fully mocked out here
            # (it never runs its own real mkdir/tempfile/os.replace
            # sequence), so the section-edits directory itself must
            # never have been created at all.
            section_edits_directory = (
                source_directory / ".admin-state" / "section-edits"
            )
            self.assertFalse(section_edits_directory.exists())

    def test_stale_delete_failure_after_successful_bulk_rolls_back(
        self,
    ) -> None:
        # EDIT_STALE_DELETE_ROLLBACK - the bulk write for the NEW
        # content genuinely succeeds, but the stale-chunk cleanup
        # DURING THE ORIGINAL EDIT ATTEMPT (_delete_stale_section_
        # chunks -> _delete_chunks_except) fails. This exercises
        # replace_document_section_chunks' OWN internal rollback
        # (distinct from update_effective_section's outer one, tested
        # above) - the failure-injection knob fails only the FIRST
        # delete_by_query call, leaving the rollback's own subsequent
        # wipe call (inside _restore_section_snapshot) free to succeed
        # genuinely, mirroring FakeCountryOpenSearch(fail_cleanup=True)
        # / CountryIndexerTests.test_country_indexer_restores_
        # snapshot_on_cleanup_failure in test_admin_document_
        # replacement.py.
        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root)

            _write_docx(
                source_directory / "GB.docx",
                [("Employment Contracts", "Original DOCX text.")],
            )

            client = FakeSectionOpenSearchClient(
                chunks={
                    "chunk-seed-ec": _seed_chunk(
                        document_id=DOCUMENT_ID,
                        legal_topic="Employment Contracts",
                        content="placeholder",
                    ),
                },
                fail_delete_by_query_calls=1,
                delete_by_query_failure=RuntimeError(
                    "simulated stale-chunk delete failure"
                ),
            )

            before = copy.deepcopy(client.chunks)

            with _patched_indexer(client):
                with self.assertRaises(
                    AdminDocumentSectionUpdateFailedError
                ):
                    update_effective_section(
                        document_id=DOCUMENT_ID,
                        section_id=EMPLOYMENT_CONTRACTS_SECTION_ID,
                        new_content=(
                            "New content whose own stale-delete step "
                            "fails, after its own bulk write already "
                            "succeeded."
                        ),
                        source_directory=source_directory,
                        client=client,
                    )

            self.assertEqual(client.chunks, before)
            self.assertIsNone(
                read_section_edit_state(source_directory, DOCUMENT_ID)
            )
            # Exactly two delete_by_query calls: the original attempt's
            # own (failing) stale-delete, then the rollback's own
            # (succeeding) wipe inside _restore_section_snapshot.
            self.assertEqual(client.delete_by_query_call_count, 2)

    def test_edit_and_its_own_rollback_both_fail_surfaces_rollback_error(
        self,
    ) -> None:
        # EDIT_ROLLBACK_FAILURE_SURFACED, indexer layer - the primary
        # mutation fails AND replace_document_section_chunks' own
        # internal rollback attempt also fails. Called directly
        # (never through update_effective_section) to keep this test
        # focused on the indexer layer's own contract: a
        # DocumentIndexingError whose message says the rollback itself
        # also failed - never silently downgraded, never masked behind
        # the original error alone.
        client = FakeSectionOpenSearchClient(
            chunks={
                "chunk-seed-ec": _seed_chunk(
                    document_id=DOCUMENT_ID,
                    legal_topic="Employment Contracts",
                    content="placeholder",
                ),
            }
        )

        new_chunks = build_document_chunks(
            [
                ParsedSection(
                    section="Employment Contracts",
                    subsection=None,
                    content=(
                        "New content that never lands, and whose own "
                        "rollback also fails."
                    ),
                ),
            ],
            DocumentMetadata(
                country="United Kingdom",
                country_code="GB",
                reference_year=2026,
                language="en",
                source_filename="UK 2026.docx",
            ),
        )

        with patch(
            "app.services.document_indexer.ensure_legal_documents_index"
        ), patch(
            "app.services.document_indexer.bulk",
            # Every call fails identically - the edit's own bulk
            # write (call #1) AND the rollback's own reindex-snapshot
            # bulk call (call #2, inside _restore_section_snapshot).
            side_effect=RuntimeError(
                "simulated bulk failure - both the edit's own write "
                "and its own rollback's reindex fail identically."
            ),
        ):
            with self.assertRaises(DocumentIndexingError) as context:
                replace_document_section_chunks(
                    new_chunks,
                    "Employment Contracts",
                    client=client,
                )

        self.assertIn(
            "rollback also failed", str(context.exception)
        )

    def test_state_commit_and_its_own_rollback_both_fail_surfaces_rollback_error(
        self,
    ) -> None:
        # EDIT_ROLLBACK_FAILURE_SURFACED, outer layer - the durable
        # state-file commit fails AFTER OpenSearch already succeeded,
        # AND update_effective_section's own outer rollback
        # (_restore_section_snapshot) also fails. This must never be
        # silently downgraded to the plain AdminDocumentSectionUpdate
        # FailedError - it must surface as AdminDocumentRollbackError
        # specifically, exactly like reindex/delete already do for
        # this same "primary operation failed AND its own rollback
        # also failed" situation.
        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root)

            _write_docx(
                source_directory / "GB.docx",
                [("Employment Contracts", "Original DOCX text.")],
            )

            client = FakeSectionOpenSearchClient(
                chunks={
                    "chunk-seed-ec": _seed_chunk(
                        document_id=DOCUMENT_ID,
                        legal_topic="Employment Contracts",
                        content="placeholder",
                    ),
                }
            )

            with _patched_indexer(client), patch(
                "app.services.admin_document_sections."
                "write_section_edit_state_atomic",
                side_effect=OSError("simulated disk failure"),
            ), patch(
                "app.services.admin_document_sections."
                "_restore_section_snapshot",
                side_effect=RuntimeError("boom"),
            ):
                with self.assertRaises(AdminDocumentRollbackError):
                    update_effective_section(
                        document_id=DOCUMENT_ID,
                        section_id=EMPLOYMENT_CONTRACTS_SECTION_ID,
                        new_content=(
                            "Indexed, then the state commit fails, and "
                            "the rollback that would normally fix "
                            "OpenSearch also fails."
                        ),
                        source_directory=source_directory,
                        client=client,
                    )

    def test_no_raw_opensearch_exception_ever_escapes_section_update(
        self,
    ) -> None:
        # EDIT_OPENSEARCH_EXCEPTION_MAPPING - a raw OpenSearchException
        # injected at the delete_by_query layer must never itself be
        # the type update_effective_section raises, nor the type of
        # its immediate __cause__: the documented mapping chain (raw
        # OpenSearch error -> DocumentIndexingError ->
        # AdminDocumentSectionUpdateFailedError) is proven by walking
        # __cause__ explicitly. The raw exception is still reachable
        # one level further down as DocumentIndexingError's OWN
        # __cause__ - that is standard, deliberate `raise ... from
        # error` chaining (document_indexer.py's _delete_chunks_except
        # itself does exactly this), never a boundary leak: it never
        # changes the TYPE any caller must catch (always
        # DocumentIndexingError, never OpenSearchException), it only
        # preserves the original low-level detail for diagnostics.
        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root)

            _write_docx(
                source_directory / "GB.docx",
                [("Employment Contracts", "Original DOCX text.")],
            )

            client = FakeSectionOpenSearchClient(
                chunks={
                    "chunk-seed-ec": _seed_chunk(
                        document_id=DOCUMENT_ID,
                        legal_topic="Employment Contracts",
                        content="placeholder",
                    ),
                },
                fail_delete_by_query_calls=1,
                delete_by_query_failure=OpenSearchException(
                    "simulated low-level OpenSearch driver failure"
                ),
            )

            with _patched_indexer(client):
                with self.assertRaises(
                    AdminDocumentSectionUpdateFailedError
                ) as context:
                    update_effective_section(
                        document_id=DOCUMENT_ID,
                        section_id=EMPLOYMENT_CONTRACTS_SECTION_ID,
                        new_content=(
                            "Content whose stale-delete raises a raw "
                            "OpenSearchException."
                        ),
                        source_directory=source_directory,
                        client=client,
                    )

            outer = context.exception
            self.assertNotIsInstance(outer, OpenSearchException)

            inner = outer.__cause__
            self.assertIsInstance(inner, DocumentIndexingError)
            self.assertNotIsInstance(inner, OpenSearchException)

            self.assertIsInstance(inner.__cause__, OpenSearchException)

    def test_pre_edit_snapshot_fetch_failure_is_reported_structurally(
        self,
    ) -> None:
        # Corrective-gate follow-up: update_effective_section's own
        # pre_edit_snapshot capture (the read that happens BEFORE
        # replace_document_section_chunks is ever called - nothing
        # has mutated yet) must fail through the exact same
        # AdminDocumentSectionUpdateFailedError contract as every
        # later failure point, never as a raw DocumentIndexingError
        # escaping this function (and, downstream, the router's own
        # except clauses) unwrapped. Zero mutation: the state file is
        # never written and OpenSearch was never even asked to index
        # anything (delete_by_query_call_count stays at 0).
        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root)

            _write_docx(
                source_directory / "GB.docx",
                [("Employment Contracts", "Original DOCX text.")],
            )

            client = FakeSectionOpenSearchClient(
                chunks={
                    "chunk-seed-ec": _seed_chunk(
                        document_id=DOCUMENT_ID,
                        legal_topic="Employment Contracts",
                        content="placeholder",
                    ),
                },
                fail_snapshot_search=True,
                snapshot_search_failure=OpenSearchException(
                    "simulated pre-edit snapshot fetch failure"
                ),
            )

            with _patched_indexer(client):
                with self.assertRaises(
                    AdminDocumentSectionUpdateFailedError
                ) as context:
                    update_effective_section(
                        document_id=DOCUMENT_ID,
                        section_id=EMPLOYMENT_CONTRACTS_SECTION_ID,
                        new_content="Never reached.",
                        source_directory=source_directory,
                        client=client,
                    )

            outer = context.exception
            self.assertNotIsInstance(outer, OpenSearchException)

            inner = outer.__cause__
            self.assertIsInstance(inner, DocumentIndexingError)
            self.assertNotIsInstance(inner, OpenSearchException)

            self.assertEqual(client.delete_by_query_call_count, 0)
            self.assertIsNone(
                read_section_edit_state(source_directory, DOCUMENT_ID)
            )
            self.assertEqual(
                client.chunks["chunk-seed-ec"]["content"], "placeholder"
            )


if __name__ == "__main__":
    unittest.main()
