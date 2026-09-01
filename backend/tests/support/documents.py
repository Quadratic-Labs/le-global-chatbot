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
