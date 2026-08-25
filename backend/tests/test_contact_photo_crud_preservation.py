from __future__ import annotations

from contextlib import nullcontext
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from docx import Document

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


def _make_png(width: int, height: int, rgb: tuple[int, int, int]) -> bytes:
    """A minimal but well-formed RGB PNG - real-sized, so python-docx's
    own image-header parser (used when embedding the contact's photo
    into the rebuilt canonical table) accepts it, unlike an arbitrary
    placeholder byte string."""

    import struct
    import zlib

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


def _seed_placeholder_source_docx(
    source_directory: Path, filename: str = "BE.docx"
) -> None:
    """A minimal, valid, structurally-empty DOCX - just enough for
    resolve_document_source_path to find a real file on disk. update_
    contact() now rebuilds the persisted source's canonical contact
    table on every business edit (mirroring add_contact/delete_
    contact), so this test needs a real file to rebuild against, not
    just a ContactState sidecar."""

    document = Document()
    document.add_paragraph("Placeholder document body.")
    document.save(str(source_directory / filename))


class ContactPhotoCrudPreservationTests(unittest.TestCase):

    def test_business_update_preserves_existing_photo_metadata(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source_directory = Path(temp)
            _seed_placeholder_source_docx(source_directory)

            document_id = "doc_" + ("a" * 64)
            contact_id = "contact-123"

            photo = write_contact_photo_atomic(
                source_directory,
                contact_id,
                data=_make_png(64, 64, (10, 20, 30)),
                content_type="image/png",
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
                phone="+32 111 0200",
                address="New address",
                website="www.example.com",
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
                        {
                            "country": "Belgium",
                            "source_filename": "BE.docx",
                        },
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
                "+32 111 0200",
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
