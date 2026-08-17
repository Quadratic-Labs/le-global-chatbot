"""
Targeted tests for the Contact-Download materializer (mission "ORDER
8G-B2.1"). Does not duplicate ORDER 8G-B1/B2's own CRUD tests - only
the NEW export/materialization behavior added here.
"""

from __future__ import annotations

import hashlib
import io
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from docx import Document

from app.services.docx_parser import (
    DETERMINISTIC_CONTACT_BLOCK_MARKER,
    DETERMINISTIC_NO_CONTACTS_LINE,
    ExtractedContact,
    extract_contacts_from_docx,
)
from app.services.document_contact_materializer import (
    _blank_contact_text_boxes,
    _is_contact_related_block,
    materialize_effective_docx,
)


def _build_real_docx_bytes(paragraphs: list[str]) -> bytes:
    document = Document()
    for text in paragraphs:
        document.add_paragraph(text)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


_LEGAL_PARAGRAPHS = [
    "Labour and Employment Law in Testland 2026",
    "I. GENERAL OVERVIEW",
    "1. Introduction",
    "Overview content for Testland.",
    "II. Hiring Practices",
    "Hiring content for Testland.",
]

_CONTACT_A = ExtractedContact(
    member_firm="ORIGINAL FIRM",
    contact_person="Original Person",
    email="original@example.com",
    phone="+1 555 0001",
    address="1 Original Street",
    website="www.original.example",
)
_CONTACT_B = ExtractedContact(
    member_firm="SECOND FIRM",
    contact_person="Second Person",
    email="second@example.com",
    phone="+1 555 0002",
    address="2 Second Street",
    website="www.second.example",
)
_CONTACT_C = ExtractedContact(
    member_firm="THIRD FIRM",
    contact_person="Third Person",
    email="third@example.com",
    phone="+1 555 0003",
    address="3 Third Street",
    website="www.third.example",
)
_INCOMPLETE_CONTACT = ExtractedContact(
    contact_person="Only Name",
    email="only@example.com",
)


class BlankContactTextBoxesTests(unittest.TestCase):
    """
    Direct string-level tests of the shared block classification and
    blanking logic - the exact same predicate
    extract_contacts_from_docx()'s own parser applies, so a block is
    blanked here if and only if it would have contributed to parsed
    contacts in the first place. Real corpus coverage of this same
    logic against the actual 24-document production corpus (mission
    section 12) was run separately as a one-time pre-release pass, not
    duplicated here as a permanent test.
    """

    def _wrap(self, inner: str) -> str:
        return (
            "<w:body><w:p>" + inner + "</w:p></w:body>"
        )

    def test_contact_person_block_is_classified_as_contact_related(
        self,
    ) -> None:
        self.assertTrue(
            _is_contact_related_block(
                ["CONTACT PERSON", "Jane Doe"]
            )
        )

    def test_firm_block_with_email_is_classified_as_contact_related(
        self,
    ) -> None:
        self.assertTrue(
            _is_contact_related_block(
                ["SOME FIRM", "info@example.com"]
            )
        )

    def test_firm_block_with_phone_is_classified_as_contact_related(
        self,
    ) -> None:
        self.assertTrue(
            _is_contact_related_block(
                ["SOME FIRM", "+1 555 000 1234"]
            )
        )

    def test_plain_branding_block_is_not_contact_related(
        self,
    ) -> None:
        self.assertFalse(
            _is_contact_related_block(
                ["www.leglobal.law"]
            )
        )

    def test_document_title_block_is_not_contact_related(
        self,
    ) -> None:
        self.assertFalse(
            _is_contact_related_block(
                ["Employment Law Overview - Testland"]
            )
        )

    def test_blanks_only_the_contact_related_textbox_span(
        self,
    ) -> None:
        xml = (
            "<w:body>"
            "<w:p><w:r><w:pict><v:shape>"
            "<v:textbox><w:txbxContent>"
            "<w:p><w:r><w:t>www.leglobal.law</w:t></w:r></w:p>"
            "</w:txbxContent></v:textbox>"
            "</v:shape></w:pict></w:r></w:p>"
            "<w:p><w:r><w:pict><v:shape>"
            "<v:textbox><w:txbxContent>"
            "<w:p><w:r><w:t>CONTACT PERSON</w:t></w:r></w:p>"
            "<w:p><w:r><w:t>Old Person</w:t></w:r></w:p>"
            "<w:p><w:r><w:t>old@example.com</w:t></w:r></w:p>"
            "</w:txbxContent></v:textbox>"
            "</v:shape></w:pict></w:r></w:p>"
            "</w:body>"
        )

        blanked = _blank_contact_text_boxes(xml)

        self.assertIn("www.leglobal.law", blanked)
        self.assertNotIn("Old Person", blanked)
        self.assertNotIn("old@example.com", blanked)
        self.assertNotIn("CONTACT PERSON", blanked)
        self.assertEqual(
            blanked.count("<w:txbxContent>"), 2,
            "both text boxes must remain present, only one blanked",
        )


class MaterializeEffectiveDocxTests(unittest.TestCase):
    """CASE A-F from mission "ORDER 8G-B2.1", section 5."""

    def setUp(self) -> None:
        self._tempdir = TemporaryDirectory()
        self.source_path = (
            Path(self._tempdir.name) / "Testland.docx"
        )
        self.source_path.write_bytes(
            _build_real_docx_bytes(_LEGAL_PARAGRAPHS)
        )
        self.original_hash = hashlib.sha256(
            self.source_path.read_bytes()
        ).hexdigest()

    def tearDown(self) -> None:
        self._tempdir.cleanup()

    def _assert_source_unchanged(self) -> None:
        self.assertEqual(
            hashlib.sha256(
                self.source_path.read_bytes()
            ).hexdigest(),
            self.original_hash,
        )

    def _materialize_and_reparse(
        self,
        contacts: list[ExtractedContact],
    ) -> list[ExtractedContact]:
        effective_bytes = materialize_effective_docx(
            source_path=self.source_path,
            contacts=contacts,
        )

        # must open cleanly
        Document(io.BytesIO(effective_bytes))

        return extract_contacts_from_docx(
            io.BytesIO(effective_bytes)
        )

    def test_case_a_edited_contact_appears_and_old_value_is_absent(
        self,
    ) -> None:
        # Materialize once with the "old" value already present as a
        # structured contact, then again with the "new" value - the
        # second Download must show only the new value, matching how
        # Download always reflects current structured state rather
        # than tracking a diff against a prior Download.
        reparsed = self._materialize_and_reparse([_CONTACT_A])

        self.assertEqual(reparsed, [_CONTACT_A])

        edited = replace(
            _CONTACT_A,
            phone="+1 555 9999",
        )
        reparsed_after_edit = self._materialize_and_reparse(
            [edited]
        )

        self.assertEqual(reparsed_after_edit, [edited])
        self.assertNotIn(
            "555 0001",
            reparsed_after_edit[0].phone or "",
        )
        self._assert_source_unchanged()

    def test_case_b_n_contacts_all_present_in_order(
        self,
    ) -> None:
        reparsed = self._materialize_and_reparse(
            [_CONTACT_A, _CONTACT_B, _CONTACT_C]
        )

        self.assertEqual(
            reparsed, [_CONTACT_A, _CONTACT_B, _CONTACT_C]
        )
        self._assert_source_unchanged()

    def test_case_c_intentional_duplicates_both_preserved(
        self,
    ) -> None:
        duplicate = replace(_CONTACT_A)

        reparsed = self._materialize_and_reparse(
            [_CONTACT_A, duplicate]
        )

        self.assertEqual(len(reparsed), 2)
        self.assertEqual(reparsed[0], reparsed[1])
        self._assert_source_unchanged()

    def test_case_d_delete_one_among_n_leaves_the_others_intact(
        self,
    ) -> None:
        # Structured state after deleting the middle contact (B).
        reparsed = self._materialize_and_reparse(
            [_CONTACT_A, _CONTACT_C]
        )

        self.assertEqual(reparsed, [_CONTACT_A, _CONTACT_C])
        self.assertNotIn(
            _CONTACT_B.member_firm,
            [c.member_firm for c in reparsed],
        )
        self._assert_source_unchanged()

    def test_case_e_delete_last_contact_leaves_no_stale_information(
        self,
    ) -> None:
        self._materialize_and_reparse([_CONTACT_A])

        reparsed_after_delete = self._materialize_and_reparse([])

        self.assertEqual(reparsed_after_delete, [])
        self._assert_source_unchanged()

    def test_case_f_originally_zero_contact_document_plus_admin_contact(
        self,
    ) -> None:
        # No prior materialize call at all - this source never had any
        # embedded contact information (an FR/PT-style document).
        reparsed = self._materialize_and_reparse([_CONTACT_A])

        self.assertEqual(reparsed, [_CONTACT_A])
        self._assert_source_unchanged()

    def test_zero_contacts_end_to_end_never_used_when_source_never_had_any(
        self,
    ) -> None:
        reparsed = self._materialize_and_reparse([])

        self.assertEqual(reparsed, [])
        self._assert_source_unchanged()

    def test_incomplete_legacy_contact_round_trips_with_blanks_preserved(
        self,
    ) -> None:
        reparsed = self._materialize_and_reparse(
            [_INCOMPLETE_CONTACT]
        )

        self.assertEqual(reparsed, [_INCOMPLETE_CONTACT])
        self.assertIsNone(reparsed[0].member_firm)
        self.assertIsNone(reparsed[0].phone)
        self._assert_source_unchanged()

    def test_legal_content_unchanged_by_contact_export(
        self,
    ) -> None:
        effective_bytes = materialize_effective_docx(
            source_path=self.source_path,
            contacts=[_CONTACT_A, _CONTACT_B],
        )

        document = Document(io.BytesIO(effective_bytes))
        paragraph_texts = [p.text for p in document.paragraphs]

        for original_paragraph in _LEGAL_PARAGRAPHS:
            self.assertIn(
                original_paragraph, paragraph_texts
            )

        # Original legal paragraphs must appear as an exact,
        # contiguous prefix - the Contact block is appended after
        # them, never interleaved or reordered.
        self.assertEqual(
            paragraph_texts[: len(_LEGAL_PARAGRAPHS)],
            _LEGAL_PARAGRAPHS,
        )

    def test_deterministic_marker_and_zero_contact_sentinel_present(
        self,
    ) -> None:
        effective_bytes = materialize_effective_docx(
            source_path=self.source_path,
            contacts=[],
        )

        document = Document(io.BytesIO(effective_bytes))
        paragraph_texts = [p.text for p in document.paragraphs]

        self.assertIn(
            DETERMINISTIC_CONTACT_BLOCK_MARKER, paragraph_texts
        )
        self.assertIn(
            DETERMINISTIC_NO_CONTACTS_LINE, paragraph_texts
        )

    def test_source_file_never_written_to(self) -> None:
        original_bytes = self.source_path.read_bytes()

        materialize_effective_docx(
            source_path=self.source_path,
            contacts=[_CONTACT_A, _CONTACT_B],
        )

        self.assertEqual(
            self.source_path.read_bytes(), original_bytes
        )


if __name__ == "__main__":
    unittest.main()
