from __future__ import annotations

import unittest
from pathlib import Path

from app.services.contact_people import (
    ContactWithPhoto,
    associate_contact_photos,
)
from app.services.contact_photos import (
    ContactPhotoCandidate,
    extract_contact_photo_candidates,
)
from app.services.docx_parser import (
    ExtractedContact,
    extract_contacts_from_docx,
)


SOURCE_ROOT = Path("/data/documents/source")


def _photo(name: str) -> ContactPhotoCandidate:
    return ContactPhotoCandidate(
        source_filename=name,
        content_type="image/jpeg",
        data=name.encode(),
        sha256="sha-" + name,
        reason="GEOMETRY",
    )


class ContactPhotoAssociationUnitTests(unittest.TestCase):

    def test_single_contact_single_photo_is_associated(self) -> None:
        contact = ExtractedContact(
            member_firm="Firm",
            contact_person="Jane Doe",
            email="jane@example.com",
            phone="+1 111",
            address="Address",
            website="example.com",
        )

        result = associate_contact_photos(
            [contact],
            [_photo("jane.jpg")],
        )

        self.assertEqual(1, len(result))
        self.assertEqual("Jane Doe", result[0].contact_person)
        self.assertEqual("jane@example.com", result[0].email)
        self.assertEqual(
            "jane.jpg",
            result[0].photo.source_filename,
        )

    def test_single_contact_without_photo_is_preserved(self) -> None:
        contact = ExtractedContact(
            member_firm="Firm",
            contact_person="Jane Doe",
            email="jane@example.com",
            phone="+1 111",
            address="Address",
            website="example.com",
        )

        result = associate_contact_photos(
            [contact],
            [],
        )

        self.assertEqual(1, len(result))
        self.assertEqual("Jane Doe", result[0].contact_person)
        self.assertIsNone(result[0].photo)

    def test_two_existing_contacts_two_photos_are_mapped_in_order(
        self,
    ) -> None:
        contacts = [
            ExtractedContact(
                member_firm="Firm",
                contact_person="Person One",
                email="one@example.com",
            ),
            ExtractedContact(
                member_firm="Firm",
                contact_person="Person Two",
                email="two@example.com",
            ),
        ]

        result = associate_contact_photos(
            contacts,
            [
                _photo("one.jpg"),
                _photo("two.jpg"),
            ],
        )

        self.assertEqual(2, len(result))

        self.assertEqual(
            ("Person One", "one@example.com", "one.jpg"),
            (
                result[0].contact_person,
                result[0].email,
                result[0].photo.source_filename,
            ),
        )

        self.assertEqual(
            ("Person Two", "two@example.com", "two.jpg"),
            (
                result[1].contact_person,
                result[1].email,
                result[1].photo.source_filename,
            ),
        )

    def test_combined_contact_is_split_only_when_photo_count_matches(
        self,
    ) -> None:
        contact = ExtractedContact(
            member_firm="Shared Firm",
            contact_person="Person One and Person Two",
            email="one@example.com, two@example.com",
            phone="+32 123",
            address="Shared address",
            website="firm.example",
        )

        result = associate_contact_photos(
            [contact],
            [
                _photo("one.jpg"),
                _photo("two.jpg"),
            ],
        )

        self.assertEqual(2, len(result))

        self.assertEqual("Person One", result[0].contact_person)
        self.assertEqual("one@example.com", result[0].email)
        self.assertEqual("one.jpg", result[0].photo.source_filename)

        self.assertEqual("Person Two", result[1].contact_person)
        self.assertEqual("two@example.com", result[1].email)
        self.assertEqual("two.jpg", result[1].photo.source_filename)

        # Shared firm-level fields are copied to each autonomous card.
        for item in result:
            self.assertEqual("Shared Firm", item.member_firm)
            self.assertEqual("+32 123", item.phone)
            self.assertEqual("Shared address", item.address)
            self.assertEqual("firm.example", item.website)

    def test_combined_contact_without_photos_is_not_split(self) -> None:
        contact = ExtractedContact(
            member_firm="Firm",
            contact_person="Person One and Person Two",
            email="one@example.com, two@example.com",
        )

        result = associate_contact_photos(
            [contact],
            [],
        )

        self.assertEqual(1, len(result))
        self.assertEqual(
            "Person One and Person Two",
            result[0].contact_person,
        )
        self.assertEqual(
            "one@example.com, two@example.com",
            result[0].email,
        )
        self.assertIsNone(result[0].photo)

    def test_mismatched_person_email_photo_counts_never_guess(self) -> None:
        contact = ExtractedContact(
            member_firm="Firm",
            contact_person="Person One and Person Two",
            email="one@example.com",
        )

        result = associate_contact_photos(
            [contact],
            [
                _photo("one.jpg"),
                _photo("two.jpg"),
            ],
        )

        self.assertEqual(1, len(result))
        self.assertEqual(
            "Person One and Person Two",
            result[0].contact_person,
        )
        self.assertEqual(
            "one@example.com",
            result[0].email,
        )
        self.assertIsNone(result[0].photo)

    def test_extra_photos_never_get_arbitrarily_assigned(self) -> None:
        contact = ExtractedContact(
            member_firm="Firm",
            contact_person="Jane Doe",
            email="jane@example.com",
        )

        result = associate_contact_photos(
            [contact],
            [
                _photo("a.jpg"),
                _photo("b.jpg"),
            ],
        )

        self.assertEqual(1, len(result))
        self.assertIsNone(result[0].photo)


class RealCorpusContactPersonPhotoTests(unittest.TestCase):

    def _require(self, filename: str) -> Path:
        path = SOURCE_ROOT / filename

        if not path.exists():
            self.skipTest(
                f"Real corpus source unavailable: {path}"
            )

        return path

    def test_belgium_becomes_two_individual_contacts(self) -> None:
        path = self._require(
            "Labour and Employment Law in Belgium 2026.docx"
        )

        parsed = extract_contacts_from_docx(path)
        photos = extract_contact_photo_candidates(path)

        self.assertEqual(1, len(parsed))
        self.assertEqual(2, len(photos))

        result = associate_contact_photos(
            parsed,
            photos,
        )

        self.assertEqual(2, len(result))

        self.assertEqual(
            "Chris van Olmen",
            result[0].contact_person,
        )
        self.assertEqual(
            "chris.van.olmen@vow.be",
            result[0].email,
        )
        self.assertEqual(
            "image2.jpg",
            result[0].photo.source_filename,
        )

        self.assertEqual(
            "Nicolas Simon",
            result[1].contact_person,
        )
        self.assertEqual(
            "nicolas.simon@vow.be",
            result[1].email,
        )
        self.assertEqual(
            "image1.png",
            result[1].photo.source_filename,
        )

        for contact in result:
            self.assertEqual(
                "Van Olmen & Wynant",
                contact.member_firm,
            )
            self.assertEqual(
                "+32 264 405 11",
                contact.phone,
            )
            self.assertEqual(
                "www.vow.be",
                contact.website,
            )

    def test_france_remains_backward_compatible_without_photos(
        self,
    ) -> None:
        path = self._require("FR.docx")

        parsed = extract_contacts_from_docx(path)
        photos = extract_contact_photo_candidates(path)

        self.assertEqual([], photos)

        result = associate_contact_photos(
            parsed,
            photos,
        )

        self.assertEqual(1, len(result))
        self.assertEqual(
            "Caroline Scherrmann and Florence Bacquet",
            result[0].contact_person,
        )
        self.assertEqual(
            "scherrmann@flichy.com, bacquet@flichy.com",
            result[0].email,
        )
        self.assertIsNone(result[0].photo)


if __name__ == "__main__":
    unittest.main()
