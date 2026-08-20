from __future__ import annotations

from contextlib import nullcontext
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.models.admin_contacts import AdminContactWriteRequest
from app.services import admin_contacts
from app.services.contact_photo_store import (
    write_contact_photo_atomic,
)
from app.services.contact_state import (
    ContactRecord,
    ContactState,
    write_contact_state_atomic,
)


class ContactPhotoCrudPreservationTests(unittest.TestCase):

    def test_business_update_preserves_existing_photo_metadata(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source_directory = Path(temp)

            document_id = "doc_" + ("a" * 64)
            contact_id = "contact-123"

            photo = write_contact_photo_atomic(
                source_directory,
                contact_id,
                data=b"existing-photo",
                content_type="image/jpeg",
            )

            original = ContactRecord(
                contact_id=contact_id,
                member_firm="Firm",
                contact_person="Jane Doe",
                email="jane@example.com",
                phone="+32 OLD",
                address="Old address",
                website="example.com",
                photo_filename=photo.filename,
                photo_content_type=photo.content_type,
                photo_sha256=photo.sha256,
            )

            write_contact_state_atomic(
                source_directory,
                ContactState(
                    document_id=document_id,
                    country_code="BE",
                    contacts=(original,),
                ),
            )

            fields = AdminContactWriteRequest(
                member_firm="Firm",
                contact_person="Jane Doe",
                email="jane@example.com",
                phone="+32 NEW",
                address="New address",
                website="example.com",
            )

            with (
                patch.object(
                    admin_contacts,
                    "_get_document_metadata",
                    return_value={
                        "country_code": "BE",
                    },
                ),
                patch.object(
                    admin_contacts,
                    "_load_country_code_and_metadata",
                    return_value=(
                        "BE",
                        {},
                    ),
                ),
                patch.object(
                    admin_contacts,
                    "country_lock",
                    return_value=nullcontext(),
                ),
                patch.object(
                    admin_contacts,
                    "_apply_contact_state_change",
                ) as apply_mock,
            ):
                admin_contacts.update_contact(
                    document_id=document_id,
                    contact_id=contact_id,
                    fields=fields,
                    source_directory=source_directory,
                    client=object(),
                )

            new_contacts = (
                apply_mock.call_args.kwargs[
                    "new_contacts"
                ]
            )

            self.assertEqual(1, len(new_contacts))

            updated = new_contacts[0]

            # Business values really changed.
            self.assertEqual(
                "+32 NEW",
                updated.phone,
            )
            self.assertEqual(
                "New address",
                updated.address,
            )

            # Stable identity remains unchanged.
            self.assertEqual(
                contact_id,
                updated.contact_id,
            )

            # The business edit must NOT remove the photo.
            self.assertEqual(
                photo.filename,
                updated.photo_filename,
            )
            self.assertEqual(
                photo.content_type,
                updated.photo_content_type,
            )
            self.assertEqual(
                photo.sha256,
                updated.photo_sha256,
            )


if __name__ == "__main__":
    unittest.main()
