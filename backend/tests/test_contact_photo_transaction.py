from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services import admin_contacts
from app.services.contact_photo_store import (
    write_contact_photo_atomic,
)
from app.services.contact_state import (
    ContactRecord,
    ContactState,
    read_contact_state,
    write_contact_state_atomic,
)


class ContactPhotoTransactionTests(unittest.TestCase):

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.source_directory = Path(self.temp.name)

        self.document_id = "doc-1"
        self.country_code = "BE"

        self.old_photo = write_contact_photo_atomic(
            self.source_directory,
            "contact-old",
            data=b"old-photo",
            content_type="image/jpeg",
        )

        self.old_record = ContactRecord(
            contact_id="contact-old",
            member_firm="Old Firm",
            contact_person="Old Person",
            email="old@example.com",
            photo_filename=self.old_photo.filename,
            photo_content_type=self.old_photo.content_type,
            photo_sha256=self.old_photo.sha256,
        )

        write_contact_state_atomic(
            self.source_directory,
            ContactState(
                document_id=self.document_id,
                country_code=self.country_code,
                contacts=(self.old_record,),
            ),
        )

        self.new_photo = write_contact_photo_atomic(
            self.source_directory,
            "contact-new",
            data=b"new-photo",
            content_type="image/png",
        )

        self.new_record = ContactRecord(
            contact_id="contact-new",
            member_firm="New Firm",
            contact_person="New Person",
            email="new@example.com",
            photo_filename=self.new_photo.filename,
            photo_content_type=self.new_photo.content_type,
            photo_sha256=self.new_photo.sha256,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _photo_path(self, filename: str) -> Path:
        return (
            self.source_directory
            / ".admin-state"
            / "contact-photos"
            / filename
        )

    def _common_patches(self):
        return (
            patch.object(
                admin_contacts,
                "_document_metadata_for_chunks",
                return_value={},
            ),
            patch.object(
                admin_contacts,
                "build_contact_chunk_for_contacts",
                return_value=None,
            ),
            patch.object(
                admin_contacts,
                "is_admin_modified_since_upload",
                return_value=True,
            ),
            patch.object(
                admin_contacts,
                "write_admin_modified_marker",
            ),
        )

    def _apply(self) -> None:
        admin_contacts._apply_contact_state_change(
            document_id=self.document_id,
            country_code=self.country_code,
            source_directory=self.source_directory,
            new_contacts=(self.new_record,),
            document_metadata={},
            client=object(),
            reset_marker=True,
        )

    def _assert_rolled_back(self) -> None:
        state = read_contact_state(
            self.source_directory,
            self.document_id,
        )

        self.assertIsNotNone(state)
        self.assertEqual(
            ["contact-old"],
            [item.contact_id for item in state.contacts],
        )

        self.assertTrue(
            self._photo_path(
                self.old_photo.filename
            ).is_file()
        )

        self.assertFalse(
            self._photo_path(
                self.new_photo.filename
            ).exists()
        )

    def test_success_keeps_new_photo_and_removes_superseded_photo(
        self,
    ) -> None:
        (
            metadata_patch,
            chunk_patch,
            marker_read_patch,
            marker_write_patch,
        ) = self._common_patches()

        with (
            metadata_patch,
            chunk_patch,
            marker_read_patch,
            marker_write_patch,
            patch.object(
                admin_contacts,
                "replace_document_contact_chunk",
            ),
            patch.object(
                admin_contacts,
                "reset_admin_modified",
            ),
        ):
            self._apply()

        state = read_contact_state(
            self.source_directory,
            self.document_id,
        )

        self.assertEqual(
            ["contact-new"],
            [item.contact_id for item in state.contacts],
        )

        self.assertTrue(
            self._photo_path(
                self.new_photo.filename
            ).is_file()
        )

        self.assertFalse(
            self._photo_path(
                self.old_photo.filename
            ).exists()
        )

    def test_opensearch_failure_rolls_back_new_photo(self) -> None:
        (
            metadata_patch,
            chunk_patch,
            marker_read_patch,
            marker_write_patch,
        ) = self._common_patches()

        calls = 0

        def replace_chunk(**kwargs):
            nonlocal calls
            calls += 1

            if calls == 1:
                raise RuntimeError("opensearch boom")

        with (
            metadata_patch,
            chunk_patch,
            marker_read_patch,
            marker_write_patch,
            patch.object(
                admin_contacts,
                "replace_document_contact_chunk",
                side_effect=replace_chunk,
            ),
        ):
            with self.assertRaises(
                admin_contacts.AdminContactMutationFailedError
            ):
                self._apply()

        self._assert_rolled_back()

    def test_marker_failure_rolls_back_new_photo(self) -> None:
        (
            metadata_patch,
            chunk_patch,
            marker_read_patch,
            marker_write_patch,
        ) = self._common_patches()

        with (
            metadata_patch,
            chunk_patch,
            marker_read_patch,
            marker_write_patch,
            patch.object(
                admin_contacts,
                "replace_document_contact_chunk",
            ),
            patch.object(
                admin_contacts,
                "reset_admin_modified",
                side_effect=OSError("marker boom"),
            ),
        ):
            with self.assertRaises(
                admin_contacts.AdminContactMutationFailedError
            ):
                self._apply()

        self._assert_rolled_back()

    def test_sidecar_failure_rolls_back_new_photo(self) -> None:
        (
            metadata_patch,
            chunk_patch,
            marker_read_patch,
            marker_write_patch,
        ) = self._common_patches()

        real_write = admin_contacts.write_contact_state_atomic
        calls = 0

        def fail_first_write(source_directory, state):
            nonlocal calls
            calls += 1

            if calls == 1:
                raise OSError("sidecar boom")

            return real_write(
                source_directory,
                state,
            )

        with (
            metadata_patch,
            chunk_patch,
            marker_read_patch,
            marker_write_patch,
            patch.object(
                admin_contacts,
                "write_contact_state_atomic",
                side_effect=fail_first_write,
            ),
            patch.object(
                admin_contacts,
                "replace_document_contact_chunk",
            ),
        ):
            with self.assertRaises(
                admin_contacts.AdminContactMutationFailedError
            ):
                self._apply()

        self._assert_rolled_back()


if __name__ == "__main__":
    unittest.main()
