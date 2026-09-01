"""
Shared fixtures for the Contacts domain test suite (test_admin_contacts.py,
test_contact_documents.py): real-corpus copy helpers, minimal-but-valid
image builders, and the "has this real DOCX already been canonicalized by
live Admin usage" skip check that many real-corpus tests need before
asserting against a document's ORIGINAL, pre-canonicalization shape.

Extracted from near-identical private copies that used to live in
test_admin_contacts.py, test_admin_contact_photos.py,
test_contact_document_area.py, test_contact_document_photos.py,
test_contact_photos.py, test_contact_people.py, and
test_contact_photo_reseed.py.
"""

from __future__ import annotations

import struct
import unittest
import zlib
from pathlib import Path


def make_png(width: int, height: int, rgb: tuple[int, int, int]) -> bytes:
    """A minimal but well-formed, real-sized RGB PNG - real dimensions
    (never a degenerate single-pixel stub), since python-docx's own
    image-header parser (used to compute a photo's proportional height in
    the canonical contact table) needs real width/height to work with."""

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data))
        )

    signature = b"\x89PNG\r\n\x1a\n"
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))
    image_data = zlib.compress(raw)
    return (
        signature
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", image_data)
        + chunk(b"IEND", b"")
    )


def require_corpus_copy(
    test_case: unittest.TestCase,
    source_root: Path,
    filename: str,
    dest_dir: Path,
) -> Path:
    """
    Copy filename from the real sanitized corpus (source_root) into
    dest_dir, skipping the test gracefully when the corpus is unavailable,
    and registering a cleanup that fails the test if the REAL file was
    ever mutated - every test that copies a real DOCX to mutate a scratch
    copy must never touch the original.
    """

    source = source_root / filename

    if not source.exists():
        test_case.skipTest(f"Real corpus source unavailable: {source}")

    original_bytes = source.read_bytes()
    copy_path = dest_dir / filename
    copy_path.write_bytes(original_bytes)

    test_case.addCleanup(
        lambda: test_case.assertEqual(
            original_bytes,
            source.read_bytes(),
            f"{source} was mutated by this test.",
        )
    )

    return copy_path


def skip_if_already_canonicalized(
    test_case: unittest.TestCase, path: Path
) -> None:
    """
    Skip when a real Admin has since used the live Contact CRUD feature
    against this document: it now has a canonical contact table (real-
    world content drift, unrelated to whatever test is about to assert
    against the document's ORIGINAL, never-yet-canonicalized organic
    shape).
    """

    from docx import Document as WordDocument

    from app.services.docx_parser import CONTACT_TABLE_HIDDEN_MARKER

    document = WordDocument(str(path))
    already_canonicalized = any(
        table.rows
        and CONTACT_TABLE_HIDDEN_MARKER in table.rows[0].cells[0].text
        for table in document.tables
    )

    if already_canonicalized:
        test_case.skipTest(
            f"{path.name} has since been canonicalized by real Admin "
            "usage (real corpus content has drifted since this test "
            "was written) - its original organic content no longer "
            "exists to assert against"
        )


# FOLDED_FROM: backend/tests/support/admin_invariants.py
from collections.abc import Sequence
from pathlib import Path
from typing import Any
LOCK_DIRECTORY_NAME = '.admin-locks'
SECTION_STATE_DIRECTORY_NAME = '.admin-state'
_NEVER_A_SOURCE_DOCUMENT = frozenset({LOCK_DIRECTORY_NAME, SECTION_STATE_DIRECTORY_NAME})

def real_source_entries(source_directory: Path) -> list[Path]:
    """
    List source_directory's real content, excluding the technical
    subdirectories that live inside it but are never real DOCX
    sources - country_lock.py's `.admin-locks` and
    document_section_state.py's `.admin-state`. Both depend on
    source_directory itself always being writable, which a sibling
    location cannot guarantee in production - any test asserting
    exact source contents must filter both out, the same way
    document_source_resolver already does implicitly (it resolves
    specific, expected filenames only, never matching either as a
    real filename).
    """
    return sorted((path for path in source_directory.iterdir() if path.name not in _NEVER_A_SOURCE_DOCUMENT))

def assert_one_active_document_per_country(documents: Sequence[dict[str, Any]], country_code: str) -> None:
    """
    Invariant B ("clean country"): after a normal, successful
    operation, exactly one active document exists for country_code.
    `documents` is the same list shape list_indexed_documents()
    returns (or an equivalent [{"country_code": ..., ...}, ...]).
    """
    matching = [document for document in documents if document['country_code'] == country_code]
    assert len(matching) == 1, f'Expected exactly 1 active document for {country_code!r}, found {len(matching)}: {matching!r}'

def assert_one_active_source(documents: Sequence[dict[str, Any]], country_code: str) -> None:
    """
    Invariant C ("source"): exactly one active, present source file
    backs the country's one active document when the catalog is
    clean - never zero (indexed_source_missing), never a conflict
    (indexed_source_conflict).
    """
    matching = [document for document in documents if document['country_code'] == country_code]
    assert len(matching) == 1, f'Expected exactly 1 document for {country_code!r} before checking its source, found {len(matching)}.'
    document = matching[0]
    assert document['source_file_present'] is True, f"Expected {country_code!r}'s source to be present, status was {document.get('status')!r}."
    assert document['status'] == 'indexed', f"Expected {country_code!r}'s status to be 'indexed', got {document.get('status')!r}."

def assert_chunk_count_matches(catalog_chunk_count: int, real_opensearch_count: int, *, country_code: str | None=None) -> None:
    """
    Invariant D ("chunks"): the catalog's own reported chunk_count for
    a document must exactly equal the real count of chunks bearing
    that document_id/country_code in OpenSearch - never an
    approximation, never silently rounded.
    """
    label = f' for {country_code!r}' if country_code else ''
    assert catalog_chunk_count == real_opensearch_count, f'Catalog chunk_count{label} ({catalog_chunk_count}) does not match the real OpenSearch count ({real_opensearch_count}).'

def assert_zero_mutation(documents_before: Sequence[dict[str, Any]], documents_after: Sequence[dict[str, Any]]) -> None:
    """
    Invariant F ("decision responses"): a WARNING or REPLACEMENT
    decision gate that has not been confirmed must leave the catalog
    byte-for-byte identical - not just "the same count", the exact
    same set of (document_id, chunk_count, status) tuples.
    """

    def _fingerprint(documents: Sequence[dict[str, Any]]) -> set[tuple[Any, Any, Any]]:
        return {(document['document_id'], document['chunk_count'], document['status']) for document in documents}
    before = _fingerprint(documents_before)
    after = _fingerprint(documents_after)
    assert before == after, f'Expected zero mutation, but the catalog changed:\n  before: {sorted(before)}\n  after:  {sorted(after)}'

def assert_no_orphan_chunks(catalog_total_chunks: int, real_opensearch_total_count: int) -> None:
    """
    Invariant E, catalog-wide form: the sum of every document's own
    chunk_count must equal OpenSearch's real total - any excess is an
    orphan chunk a failed/partial transaction left behind without a
    document to own it.
    """
    assert catalog_total_chunks == real_opensearch_total_count, f'Orphan chunks detected: catalog reports {catalog_total_chunks} total chunks, OpenSearch actually holds {real_opensearch_total_count}.'


# FOLDED_FROM: backend/tests/support/corpus_paths.py
import os
from pathlib import Path
DEFAULT_SOURCE_ROOT = Path('/data/documents/source')

def resolve_source_root() -> Path:
    """Return the DOCX source root tests should read from."""
    override = os.environ.get('TEST_DOCUMENT_SOURCE_ROOT')
    return Path(override) if override else DEFAULT_SOURCE_ROOT
