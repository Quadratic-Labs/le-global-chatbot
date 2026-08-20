from __future__ import annotations

from types import SimpleNamespace
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services import admin_contacts
from app.services.contact_photo_store import (
    ContactPhotoStorageError,
    write_contact_photo_atomic as real_write_contact_photo_atomic,
)
from app.services.contact_photos import (
    extract_contact_photo_candidates,
)
from app.services.contact_state import (
    read_contact_state,
)
from app.services.docx_parser import (
    extract_contacts_from_docx,
)


SOURCE_ROOT = Path("/data/documents/source")

BELGIUM = "Labour and Employment Law in Belgium 2026.docx"


class ParsedContactPhotoReseedTests(unittest.TestCase):

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.source_directory = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _require(self, filename: str) -> Path:
        path = SOURCE_ROOT / filename

        if not path.exists():
            self.skipTest(f"Corpus file unavailable: {path}")

        return path

    def _photo_files(self) -> list[Path]:
        directory = (
            self.source_directory
            / ".admin-state"
            / "contact-photos"
        )

        if not directory.exists():
            return []

        return sorted(
            path
            for path in directory.iterdir()
            if path.is_file()
        )

    def test_parsed_reseed_belgium_creates_two_contacts_and_two_photos(
        self,
    ) -> None:
        path = self._require(BELGIUM)

        parsed = extract_contacts_from_docx(
            path,
            country="Belgium",
        )

        self.assertEqual(1, len(parsed))

        admin_contacts.reseed_contact_state_from_parsed_contacts(
            document_id="doc-belgium",
            country_code="BE",
            source_directory=self.source_directory,
            contacts=parsed,
            docx_path=path,
        )

        state = read_contact_state(
            self.source_directory,
            "doc-belgium",
        )

        self.assertIsNotNone(state)
        self.assertEqual(2, len(state.contacts))

        first, second = state.contacts

        self.assertEqual(
            "Chris van Olmen",
            first.contact_person,
        )
        self.assertEqual(
            "chris.van.olmen@vow.be",
            first.email,
        )

        self.assertEqual(
            "Nicolas Simon",
            second.contact_person,
        )
        self.assertEqual(
            "nicolas.simon@vow.be",
            second.email,
        )

        self.assertNotEqual(
            first.contact_id,
            second.contact_id,
        )

        for record in (first, second):
            self.assertIsNotNone(record.photo_filename)
            self.assertIsNotNone(record.photo_content_type)
            self.assertIsNotNone(record.photo_sha256)

            self.assertTrue(
                (
                    self.source_directory
                    / ".admin-state"
                    / "contact-photos"
                    / record.photo_filename
                ).is_file()
            )

        expected_photos = extract_contact_photo_candidates(path)

        self.assertEqual(
            expected_photos[0].sha256,
            first.photo_sha256,
        )
        self.assertEqual(
            expected_photos[1].sha256,
            second.photo_sha256,
        )

        self.assertEqual(
            2,
            len(self._photo_files()),
        )

    def test_parsed_reseed_france_remains_one_contact_without_photo(
        self,
    ) -> None:
        path = self._require("FR.docx")

        parsed = extract_contacts_from_docx(
            path,
            country="France",
        )

        admin_contacts.reseed_contact_state_from_parsed_contacts(
            document_id="doc-france",
            country_code="FR",
            source_directory=self.source_directory,
            contacts=parsed,
            docx_path=path,
        )

        state = read_contact_state(
            self.source_directory,
            "doc-france",
        )

        self.assertEqual(1, len(state.contacts))

        contact = state.contacts[0]

        self.assertEqual(
            "Caroline Scherrmann and Florence Bacquet",
            contact.contact_person,
        )
        self.assertIsNone(contact.photo_filename)
        self.assertIsNone(contact.photo_content_type)
        self.assertIsNone(contact.photo_sha256)

        self.assertEqual([], self._photo_files())

    def test_parsed_reseed_indonesia_persists_the_valid_photo(
        self,
    ) -> None:
        path = self._require("ID.docx")

        parsed = extract_contacts_from_docx(
            path,
            country="Indonesia",
        )

        expected = extract_contact_photo_candidates(path)

        self.assertEqual(1, len(expected))
        self.assertEqual(
            "image3.jpeg",
            expected[0].source_filename,
        )

        admin_contacts.reseed_contact_state_from_parsed_contacts(
            document_id="doc-indonesia",
            country_code="ID",
            source_directory=self.source_directory,
            contacts=parsed,
            docx_path=path,
        )

        state = read_contact_state(
            self.source_directory,
            "doc-indonesia",
        )

        self.assertEqual(1, len(state.contacts))

        contact = state.contacts[0]

        self.assertEqual(
            expected[0].sha256,
            contact.photo_sha256,
        )

        self.assertTrue(
            (
                self.source_directory
                / ".admin-state"
                / "contact-photos"
                / contact.photo_filename
            ).is_file()
        )

    def test_photo_extraction_failure_keeps_contacts_without_photo(
        self,
    ) -> None:
        path = self._require("FR.docx")

        parsed = extract_contacts_from_docx(
            path,
            country="France",
        )

        with patch.object(
            admin_contacts,
            "extract_contact_photo_candidates",
            side_effect=admin_contacts.ContactPhotoExtractionError(
                "unsupported synthetic DOCX image package"
            ),
        ):
            admin_contacts.reseed_contact_state_from_parsed_contacts(
                document_id="doc-photo-fallback",
                country_code="FR",
                source_directory=self.source_directory,
                contacts=parsed,
                docx_path=path,
            )

        state = read_contact_state(
            self.source_directory,
            "doc-photo-fallback",
        )

        self.assertIsNotNone(state)
        self.assertEqual(1, len(state.contacts))

        contact = state.contacts[0]

        self.assertEqual(
            "Caroline Scherrmann and Florence Bacquet",
            contact.contact_person,
        )
        self.assertIsNone(contact.photo_filename)
        self.assertIsNone(contact.photo_content_type)
        self.assertIsNone(contact.photo_sha256)

        self.assertEqual([], self._photo_files())


    def test_second_photo_write_failure_leaves_no_partial_seed(
        self,
    ) -> None:
        path = self._require(BELGIUM)

        parsed = extract_contacts_from_docx(
            path,
            country="Belgium",
        )

        calls = 0

        def fail_second(*args, **kwargs):
            nonlocal calls
            calls += 1

            if calls == 2:
                raise ContactPhotoStorageError(
                    "second photo boom"
                )

            return real_write_contact_photo_atomic(
                *args,
                **kwargs,
            )

        with patch.object(
            admin_contacts,
            "write_contact_photo_atomic",
            side_effect=fail_second,
            create=True,
        ):
            with self.assertRaises(ContactPhotoStorageError):
                admin_contacts.reseed_contact_state_from_parsed_contacts(
                    document_id="doc-belgium",
                    country_code="BE",
                    source_directory=self.source_directory,
                    contacts=parsed,
                    docx_path=path,
                )

        self.assertIsNone(
            read_contact_state(
                self.source_directory,
                "doc-belgium",
            )
        )

        self.assertEqual(
            [],
            self._photo_files(),
        )


class CurrentDocxPhotoReseedTests(unittest.TestCase):

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.source_directory = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_current_docx_reseed_belgium_seeds_two_photo_contacts(
        self,
    ) -> None:
        path = SOURCE_ROOT / BELGIUM

        if not path.exists():
            self.skipTest("Belgium corpus DOCX unavailable")

        metadata = {
            "source_filename": BELGIUM,
            "country": "Belgium",
            "country_code": "BE",
        }

        with (
            patch.object(
                admin_contacts,
                "_load_country_code_and_metadata",
                return_value=("BE", metadata),
            ),
            patch.object(
                admin_contacts,
                "resolve_document_source_path",
                return_value=SimpleNamespace(
                    path=path,
                ),
            ),
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
                "replace_document_contact_chunk",
            ),
            patch.object(
                admin_contacts,
                "is_admin_modified_since_upload",
                return_value=False,
            ),
            patch.object(
                admin_contacts,
                "reset_admin_modified",
            ),
        ):
            admin_contacts._reseed_contacts_from_current_docx_locked(
                validated_document_id="doc-current-belgium",
                source_directory=self.source_directory,
                opensearch_client=object(),
            )

        state = read_contact_state(
            self.source_directory,
            "doc-current-belgium",
        )

        self.assertIsNotNone(state)
        self.assertEqual(2, len(state.contacts))

        self.assertEqual(
            [
                "Chris van Olmen",
                "Nicolas Simon",
            ],
            [
                contact.contact_person
                for contact in state.contacts
            ],
        )

        self.assertEqual(
            2,
            sum(
                contact.photo_filename is not None
                for contact in state.contacts
            ),
        )


if __name__ == "__main__":
    unittest.main()
