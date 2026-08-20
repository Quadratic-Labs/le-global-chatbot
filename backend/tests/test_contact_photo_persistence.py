from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services.contact_state import (
    ContactRecord,
    ContactStateError,
)
from app.services.contact_photo_store import (
    ContactPhotoStorageError,
    delete_contact_photo,
    read_contact_photo,
    write_contact_photo_atomic,
)


class ContactPhotoMetadataTests(unittest.TestCase):

    def test_legacy_contact_without_photo_fields_is_readable(self) -> None:
        record = ContactRecord.from_json_dict(
            {
                "contact_id": "contact-legacy",
                "member_firm": "Firm",
                "contact_person": "Jane Doe",
                "email": "jane@example.com",
                "phone": "+1",
                "address": "Address",
                "website": "example.com",
            }
        )

        self.assertIsNone(record.photo_filename)
        self.assertIsNone(record.photo_content_type)
        self.assertIsNone(record.photo_sha256)

    def test_photo_metadata_serializes_without_binary_data(self) -> None:
        digest = "a" * 64

        record = ContactRecord(
            contact_id="contact-123",
            member_firm="Firm",
            contact_person="Jane Doe",
            email="jane@example.com",
            photo_filename=f"contact-123--{digest}.jpg",
            photo_content_type="image/jpeg",
            photo_sha256=digest,
        )

        payload = record.to_json_dict()

        self.assertEqual(
            f"contact-123--{digest}.jpg",
            payload["photo_filename"],
        )
        self.assertEqual(
            "image/jpeg",
            payload["photo_content_type"],
        )
        self.assertEqual(
            digest,
            payload["photo_sha256"],
        )

        self.assertFalse(
            any(
                isinstance(value, (bytes, bytearray))
                for value in payload.values()
            )
        )

    def test_partial_photo_metadata_is_rejected(self) -> None:
        with self.assertRaises(ContactStateError):
            ContactRecord.from_json_dict(
                {
                    "contact_id": "contact-123",
                    "photo_filename": "photo.jpg",
                    "photo_content_type": None,
                    "photo_sha256": None,
                }
            )


class ContactPhotoStoreTests(unittest.TestCase):

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.source_directory = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_atomic_write_uses_contact_id_and_sha256(self) -> None:
        data = b"fake-jpeg-content"
        digest = hashlib.sha256(data).hexdigest()

        stored = write_contact_photo_atomic(
            self.source_directory,
            "contact-123",
            data=data,
            content_type="image/jpeg",
        )

        self.assertEqual(
            f"contact-123--{digest}.jpg",
            stored.filename,
        )
        self.assertEqual("image/jpeg", stored.content_type)
        self.assertEqual(digest, stored.sha256)

        self.assertEqual(
            data,
            read_contact_photo(
                self.source_directory,
                stored.filename,
            ),
        )

    def test_photo_is_stored_inside_admin_state(self) -> None:
        stored = write_contact_photo_atomic(
            self.source_directory,
            "contact-123",
            data=b"photo",
            content_type="image/png",
        )

        expected = (
            self.source_directory
            / ".admin-state"
            / "contact-photos"
            / stored.filename
        )

        self.assertTrue(expected.is_file())

    def test_same_photo_write_is_idempotent(self) -> None:
        first = write_contact_photo_atomic(
            self.source_directory,
            "contact-123",
            data=b"same-photo",
            content_type="image/jpeg",
        )

        second = write_contact_photo_atomic(
            self.source_directory,
            "contact-123",
            data=b"same-photo",
            content_type="image/jpeg",
        )

        self.assertEqual(first, second)

        files = list(
            (
                self.source_directory
                / ".admin-state"
                / "contact-photos"
            ).iterdir()
        )

        self.assertEqual(1, len(files))

    def test_failed_new_write_preserves_existing_photo(self) -> None:
        old = write_contact_photo_atomic(
            self.source_directory,
            "contact-123",
            data=b"old-photo",
            content_type="image/jpeg",
        )

        with patch(
            "app.services.contact_photo_store.os.replace",
            side_effect=OSError("boom"),
        ):
            with self.assertRaises(ContactPhotoStorageError):
                write_contact_photo_atomic(
                    self.source_directory,
                    "contact-123",
                    data=b"new-photo",
                    content_type="image/png",
                )

        self.assertEqual(
            b"old-photo",
            read_contact_photo(
                self.source_directory,
                old.filename,
            ),
        )

        store = (
            self.source_directory
            / ".admin-state"
            / "contact-photos"
        )

        self.assertEqual(
            [old.filename],
            sorted(
                p.name
                for p in store.iterdir()
                if p.is_file()
            ),
        )

    def test_unsupported_content_type_is_rejected(self) -> None:
        with self.assertRaises(ContactPhotoStorageError):
            write_contact_photo_atomic(
                self.source_directory,
                "contact-123",
                data=b"photo",
                content_type="image/svg+xml",
            )

    def test_empty_photo_is_rejected(self) -> None:
        with self.assertRaises(ContactPhotoStorageError):
            write_contact_photo_atomic(
                self.source_directory,
                "contact-123",
                data=b"",
                content_type="image/jpeg",
            )

    def test_path_traversal_filename_is_rejected(self) -> None:
        with self.assertRaises(ContactPhotoStorageError):
            read_contact_photo(
                self.source_directory,
                "../secret.jpg",
            )

        with self.assertRaises(ContactPhotoStorageError):
            delete_contact_photo(
                self.source_directory,
                "../secret.jpg",
            )

    def test_delete_is_safe_and_idempotent(self) -> None:
        stored = write_contact_photo_atomic(
            self.source_directory,
            "contact-123",
            data=b"photo",
            content_type="image/webp",
        )

        delete_contact_photo(
            self.source_directory,
            stored.filename,
        )

        delete_contact_photo(
            self.source_directory,
            stored.filename,
        )

        with self.assertRaises(ContactPhotoStorageError):
            read_contact_photo(
                self.source_directory,
                stored.filename,
            )


if __name__ == "__main__":
    unittest.main()
