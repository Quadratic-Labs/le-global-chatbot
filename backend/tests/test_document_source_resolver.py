"""
Tests for the centralized document source path resolver.

Mission "HOTFIX 0.4.4" - Mission 1/2: restore compatibility with the
17 legacy DOCX files stored under their own historical filenames,
before country-keyed storage existed, without ever renaming them or
scanning the source directory.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from app.services.document_source_resolver import (
    DocumentSourceConflictError,
    resolve_document_source_path,
)


# The 17 real production filenames (mission section 7.G) - proves the
# resolver handles every historical naming convention actually used in
# production, not just a synthetic example.
REAL_PRODUCTION_FILENAMES = (
    "Employment Law Overview Australia.docx",
    "Employment Law Overview Peru 2026.docx",
    "Employment Law Overview Singapore 2026.docx",
    "Labour and Employment Law in Argentina 2026.docx",
    "Labour and Employment Law in Belgium 2026.docx",
    "Labour and Employment Law in Brazil 2026.docx",
    "Labour and Employment Law in Italy 2026.docx",
    "Labour and Employment Law in Japan 2026.docx",
    "Labour and Employment Law in Poland 2026.docx",
    "Labour and Employment Law in Romania 2026.docx",
    "Labour and Employment Law in Spain 2026.docx",
    "Labour and Employment Law in Sweden 2026.docx",
    "Labour and Employment Law in Switzerland 2026.docx",
    "Labour and Employment Law in UK 2026.docx",
    "Labour and employment law in Czech Republic 2026.docx",
    "Labour and employment law in Greece.docx",
    "Labour and employment law in Mexico 2026.docx",
)


class ResolveViaHistoricalFilenameTests(unittest.TestCase):
    def test_resolves_via_source_filename_when_canonical_absent(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as root:
            source_root = Path(root)
            legacy_path = (
                source_root / "Labour and Employment Law in Spain 2026.docx"
            )
            legacy_path.write_bytes(b"legacy-docx-bytes")

            resolved = resolve_document_source_path(
                source_root=source_root,
                country_code="ES",
                source_filename=legacy_path.name,
            )

        self.assertEqual(resolved.path, legacy_path)
        self.assertEqual(resolved.origin, "source_filename")

    def test_resolves_via_canonical_when_source_filename_absent(
        self,
    ) -> None:
        # The current (post 0.4.3) pipeline's own on-disk scheme -
        # never broken by this fix.
        with tempfile.TemporaryDirectory() as root:
            source_root = Path(root)
            canonical_path = source_root / "CA.docx"
            canonical_path.write_bytes(b"new-docx-bytes")

            resolved = resolve_document_source_path(
                source_root=source_root,
                country_code="CA",
                source_filename="Canada_2026-04-15-EDITED.docx",
            )

        self.assertEqual(resolved.path, canonical_path)
        self.assertEqual(resolved.origin, "canonical")

    def test_missing_when_neither_exists(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            source_root = Path(root)

            resolved = resolve_document_source_path(
                source_root=source_root,
                country_code="ES",
                source_filename=(
                    "Labour and Employment Law in Spain 2026.docx"
                ),
            )

        self.assertIsNone(resolved.path)
        self.assertEqual(resolved.origin, "missing")

    def test_storage_filename_takes_priority_when_only_one_exists(
        self,
    ) -> None:
        # storage_filename is checked first, but only a real,
        # existing file counts as a match - a nonexistent
        # storage_filename must never block falling through to the
        # next candidate, and must never itself be treated as a
        # conflict with a genuinely resolved source_filename.
        with tempfile.TemporaryDirectory() as root:
            source_root = Path(root)
            legacy_path = (
                source_root / "Labour and Employment Law in Spain 2026.docx"
            )
            legacy_path.write_bytes(b"legacy")

            resolved = resolve_document_source_path(
                source_root=source_root,
                country_code="ES",
                storage_filename="does-not-exist-on-disk.docx",
                source_filename=legacy_path.name,
            )

        self.assertEqual(resolved.path, legacy_path)
        self.assertEqual(resolved.origin, "source_filename")

    def test_the_17_real_production_filenames_all_resolve(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            source_root = Path(root)

            for filename in REAL_PRODUCTION_FILENAMES:
                with self.subTest(filename=filename):
                    file_path = source_root / filename
                    file_path.write_bytes(b"legacy-docx-bytes")

                    resolved = resolve_document_source_path(
                        source_root=source_root,
                        country_code="XX",
                        source_filename=filename,
                    )

                    self.assertEqual(resolved.path, file_path)
                    self.assertEqual(
                        resolved.origin, "source_filename"
                    )

                    file_path.unlink()


class ConflictDetectionTests(unittest.TestCase):
    def test_conflict_when_two_distinct_files_exist(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            source_root = Path(root)
            legacy_path = (
                source_root / "Labour and Employment Law in Spain 2026.docx"
            )
            legacy_path.write_bytes(b"legacy")
            canonical_path = source_root / "ES.docx"
            canonical_path.write_bytes(b"canonical")

            with self.assertRaises(
                DocumentSourceConflictError
            ) as context:
                resolve_document_source_path(
                    source_root=source_root,
                    country_code="ES",
                    source_filename=legacy_path.name,
                )

        self.assertEqual(
            set(context.exception.conflicting_paths),
            {legacy_path, canonical_path},
        )

    def test_no_conflict_when_two_fields_name_the_same_file(
        self,
    ) -> None:
        # source_filename and the canonical name happen to be
        # identical - one real file, not a conflict.
        with tempfile.TemporaryDirectory() as root:
            source_root = Path(root)
            path = source_root / "ES.docx"
            path.write_bytes(b"content")

            resolved = resolve_document_source_path(
                source_root=source_root,
                country_code="ES",
                source_filename="ES.docx",
            )

        self.assertEqual(resolved.path, path)


class PathSecurityTests(unittest.TestCase):
    def test_rejects_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            source_root = Path(root) / "source"
            source_root.mkdir()
            outside_path = Path(root) / "secret.docx"
            outside_path.write_bytes(b"secret")

            resolved = resolve_document_source_path(
                source_root=source_root,
                country_code="ES",
                source_filename="../secret.docx",
            )

        self.assertIsNone(resolved.path)
        self.assertEqual(resolved.origin, "missing")

    def test_rejects_forward_slash(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            source_root = Path(root)

            resolved = resolve_document_source_path(
                source_root=source_root,
                country_code="ES",
                source_filename="folder/document.docx",
            )

        self.assertIsNone(resolved.path)

    def test_rejects_backslash(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            source_root = Path(root)

            resolved = resolve_document_source_path(
                source_root=source_root,
                country_code="ES",
                source_filename="folder\\document.docx",
            )

        self.assertIsNone(resolved.path)

    def test_rejects_null_byte(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            source_root = Path(root)

            resolved = resolve_document_source_path(
                source_root=source_root,
                country_code="ES",
                source_filename="document\x00.docx",
            )

        self.assertIsNone(resolved.path)

    def test_rejects_non_docx_extension(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            source_root = Path(root)
            path = source_root / "document.pdf"
            path.write_bytes(b"content")

            resolved = resolve_document_source_path(
                source_root=source_root,
                country_code="ES",
                source_filename="document.pdf",
            )

        self.assertIsNone(resolved.path)

    def test_rejects_symlink_escaping_source_root(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            source_root = Path(root) / "source"
            source_root.mkdir()
            outside_target = Path(root) / "outside.docx"
            outside_target.write_bytes(b"outside-content")

            symlink_path = source_root / "escape.docx"

            try:
                os.symlink(outside_target, symlink_path)
            except OSError:
                self.skipTest(
                    "Symlinks are not supported in this environment."
                )

            resolved = resolve_document_source_path(
                source_root=source_root,
                country_code="ES",
                source_filename="escape.docx",
            )

        self.assertIsNone(resolved.path)

    def test_accepts_internal_symlink(self) -> None:
        # A symlink whose real target still resolves under source_root
        # is not an escape - it is not rejected.
        with tempfile.TemporaryDirectory() as root:
            source_root = Path(root)
            real_target = source_root / "real.docx"
            real_target.write_bytes(b"content")
            symlink_path = source_root / "alias.docx"

            try:
                os.symlink(real_target, symlink_path)
            except OSError:
                self.skipTest(
                    "Symlinks are not supported in this environment."
                )

            resolved = resolve_document_source_path(
                source_root=source_root,
                country_code="ES",
                source_filename="alias.docx",
            )

        self.assertEqual(resolved.path, real_target)


if __name__ == "__main__":
    unittest.main()
