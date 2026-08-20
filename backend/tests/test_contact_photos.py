from __future__ import annotations

import unittest
from pathlib import Path

from app.services.contact_photos import (
    ContactPhotoCandidate,
    extract_contact_photo_candidates,
)


SOURCE_ROOT = Path("/data/documents/source")


class RealCorpusContactPhotoTests(unittest.TestCase):

    def _require(self, filename: str) -> Path:
        path = SOURCE_ROOT / filename

        if not path.exists():
            self.skipTest(
                f"Real corpus source unavailable: {path}"
            )

        return path

    def test_belgium_has_exactly_two_contact_photos(self) -> None:
        path = self._require(
            "Labour and Employment Law in Belgium 2026.docx"
        )

        photos = extract_contact_photo_candidates(path)

        self.assertEqual(2, len(photos))

        self.assertEqual(
            ["image2.jpg", "image1.png"],
            [photo.source_filename for photo in photos],
        )

        self.assertEqual(
            ["GEOMETRY", "GEOMETRY"],
            [photo.reason for photo in photos],
        )

        for photo in photos:
            self.assertTrue(photo.data)
            self.assertTrue(photo.sha256)
            self.assertTrue(photo.content_type.startswith("image/"))

    def test_ireland_rejects_the_wide_false_positive(self) -> None:
        path = self._require("IE.docx")

        photos = extract_contact_photo_candidates(path)

        self.assertEqual(1, len(photos))
        self.assertEqual(
            "image2.jpg",
            photos[0].source_filename,
        )

    def test_indonesia_rejects_pagoda_and_logo(self) -> None:
        path = self._require("ID.docx")

        photos = extract_contact_photo_candidates(path)

        self.assertEqual(1, len(photos))
        self.assertEqual(
            "image3.jpeg",
            photos[0].source_filename,
        )
        self.assertEqual(
            "UNIQUE_PORTRAIT",
            photos[0].reason,
        )

    def test_chile_has_no_contact_photo(self) -> None:
        path = self._require("CL.docx")

        self.assertEqual(
            [],
            extract_contact_photo_candidates(path),
        )

    def test_germany_has_no_contact_photo(self) -> None:
        path = self._require("DE.docx")

        self.assertEqual(
            [],
            extract_contact_photo_candidates(path),
        )

    def test_india_has_no_contact_photo(self) -> None:
        path = self._require("IN.docx")

        self.assertEqual(
            [],
            extract_contact_photo_candidates(path),
        )

    def test_france_has_no_contact_photo(self) -> None:
        path = self._require("FR.docx")

        self.assertEqual(
            [],
            extract_contact_photo_candidates(path),
        )

    def test_result_is_deterministic(self) -> None:
        path = self._require(
            "Labour and Employment Law in Belgium 2026.docx"
        )

        first = extract_contact_photo_candidates(path)
        second = extract_contact_photo_candidates(path)

        self.assertEqual(
            [
                (
                    item.source_filename,
                    item.sha256,
                    item.reason,
                )
                for item in first
            ],
            [
                (
                    item.source_filename,
                    item.sha256,
                    item.reason,
                )
                for item in second
            ],
        )

    def test_candidate_is_an_immutable_value_object(self) -> None:
        candidate = ContactPhotoCandidate(
            source_filename="portrait.jpg",
            content_type="image/jpeg",
            data=b"photo",
            sha256="abc",
            reason="GEOMETRY",
        )

        with self.assertRaises(
            (AttributeError, TypeError),
        ):
            candidate.source_filename = "changed.jpg"


if __name__ == "__main__":
    unittest.main()
