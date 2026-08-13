"""
Shared, reusable backend invariants for admin document lifecycle tests.

Mission "ORDER 3", section 7: rather than copying the same checks into
30 different test methods, every test that wants to assert one of
these invariants imports the matching function from here. Each one
maps directly to one of the mission's own lettered invariants (A-F).

Not a retrofit of every pre-existing test (ORDER 1/2's transactional
integrity suites already assert B/C/D/E precisely, in ways specific to
each failure scenario they simulate, and changing already-reviewed,
passing tests to route through a new indirection layer is exactly the
kind of unnecessary churn this mission's own rules caution against).
This module exists for ORDER 3's own new tests and for ORDER 4+ to
build on, without re-deriving these checks from scratch each time.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any


LOCK_DIRECTORY_NAME = ".admin-locks"
SECTION_STATE_DIRECTORY_NAME = ".admin-state"

_NEVER_A_SOURCE_DOCUMENT = frozenset(
    {LOCK_DIRECTORY_NAME, SECTION_STATE_DIRECTORY_NAME}
)


def real_source_entries(
    source_directory: Path,
) -> list[Path]:
    """
    List source_directory's real content, excluding the technical
    subdirectories that live inside it but are never real DOCX
    sources - country_lock.py's `.admin-locks` (mission "ORDER 5")
    and document_section_state.py's `.admin-state` (mission
    "ORDER 5C"). Both depend on source_directory itself always being
    writable, which a sibling location cannot guarantee in production
    (mission "ORDER 5"'s own lesson) - any test asserting exact
    source contents must filter both out, the same way
    document_source_resolver already does implicitly (it resolves
    specific, expected filenames only, never matching either as a
    real filename).
    """

    return sorted(
        path
        for path in source_directory.iterdir()
        if path.name not in _NEVER_A_SOURCE_DOCUMENT
    )


def assert_one_active_document_per_country(
    documents: Sequence[dict[str, Any]],
    country_code: str,
) -> None:
    """
    Invariant B ("clean country"): after a normal, successful
    operation, exactly one active document exists for country_code.
    `documents` is the same list shape list_indexed_documents()
    returns (or an equivalent [{"country_code": ..., ...}, ...]).
    """

    matching = [
        document
        for document in documents
        if document["country_code"] == country_code
    ]

    assert len(matching) == 1, (
        f"Expected exactly 1 active document for {country_code!r}, "
        f"found {len(matching)}: {matching!r}"
    )


def assert_one_active_source(
    documents: Sequence[dict[str, Any]],
    country_code: str,
) -> None:
    """
    Invariant C ("source"): exactly one active, present source file
    backs the country's one active document when the catalog is
    clean - never zero (indexed_source_missing), never a conflict
    (indexed_source_conflict).
    """

    matching = [
        document
        for document in documents
        if document["country_code"] == country_code
    ]

    assert len(matching) == 1, (
        f"Expected exactly 1 document for {country_code!r} before "
        f"checking its source, found {len(matching)}."
    )

    document = matching[0]

    assert document["source_file_present"] is True, (
        f"Expected {country_code!r}'s source to be present, "
        f"status was {document.get('status')!r}."
    )
    assert document["status"] == "indexed", (
        f"Expected {country_code!r}'s status to be 'indexed', "
        f"got {document.get('status')!r}."
    )


def assert_chunk_count_matches(
    catalog_chunk_count: int,
    real_opensearch_count: int,
    *,
    country_code: str | None = None,
) -> None:
    """
    Invariant D ("chunks"): the catalog's own reported chunk_count for
    a document must exactly equal the real count of chunks bearing
    that document_id/country_code in OpenSearch - never an
    approximation, never silently rounded.
    """

    label = (
        f" for {country_code!r}" if country_code else ""
    )

    assert catalog_chunk_count == real_opensearch_count, (
        f"Catalog chunk_count{label} ({catalog_chunk_count}) does not "
        f"match the real OpenSearch count ({real_opensearch_count})."
    )


def assert_zero_mutation(
    documents_before: Sequence[dict[str, Any]],
    documents_after: Sequence[dict[str, Any]],
) -> None:
    """
    Invariant F ("decision responses"): a WARNING or REPLACEMENT
    decision gate that has not been confirmed must leave the catalog
    byte-for-byte identical - not just "the same count", the exact
    same set of (document_id, chunk_count, status) tuples.
    """

    def _fingerprint(
        documents: Sequence[dict[str, Any]],
    ) -> set[tuple[Any, Any, Any]]:
        return {
            (
                document["document_id"],
                document["chunk_count"],
                document["status"],
            )
            for document in documents
        }

    before = _fingerprint(documents_before)
    after = _fingerprint(documents_after)

    assert before == after, (
        "Expected zero mutation, but the catalog changed:\n"
        f"  before: {sorted(before)}\n"
        f"  after:  {sorted(after)}"
    )


def assert_no_orphan_chunks(
    catalog_total_chunks: int,
    real_opensearch_total_count: int,
) -> None:
    """
    Invariant E, catalog-wide form: the sum of every document's own
    chunk_count must equal OpenSearch's real total - any excess is an
    orphan chunk a failed/partial transaction left behind without a
    document to own it.
    """

    assert catalog_total_chunks == real_opensearch_total_count, (
        "Orphan chunks detected: catalog reports "
        f"{catalog_total_chunks} total chunks, OpenSearch actually "
        f"holds {real_opensearch_total_count}."
    )
