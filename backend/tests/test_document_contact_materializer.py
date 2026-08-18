"""
Targeted tests for the Contact-Download materializer (mission "ORDER
8G-B2.1" family). Does not duplicate ORDER 8G-B1/B2's own CRUD tests -
only the export/materialization behavior added here.

Covers the box-reuse-in-place strategy (a single contact, resized into
the document's own existing Contact text box) and the in-flow-paragraph
strategy (zero contacts, two or more contacts, or no pre-existing box
at all) introduced by mission "ORDER 8G-B2.1R2" after real human Word
tests found two earlier strategies visually wrong: v0.8.1 appended
plain paragraphs at the document's end, and a resized-floating-box
attempt was found (via direct LibreOffice rendering against the real
corpus) to overlap the document's own title for as few as 3 contacts.
"""

from __future__ import annotations

import hashlib
import io
import unittest
import zipfile
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from lxml import etree

from docx import Document

from app.services.docx_parser import (
    CONTACT_BOX_HIDDEN_END_MARKER,
    CONTACT_BOX_HIDDEN_MARKER,
    DETERMINISTIC_NO_CONTACTS_LINE,
    ExtractedContact,
    extract_contacts_from_docx,
)
from app.services.document_contact_materializer import (
    _find_all_contact_runs,
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


def _textbox_run_xml(
    lines: list[str],
    width_emu: int = 1000000,
    height_emu: int = 500000,
) -> str:
    inner_paragraphs = "".join(
        f"<w:p><w:r><w:t>{line}</w:t></w:r></w:p>" for line in lines
    )
    return (
        '<w:r xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
        'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
        'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
        'xmlns:wps="http://schemas.microsoft.com/office/word/2010/wordprocessingShape">'
        "<w:rPr><w:noProof/></w:rPr>"
        "<w:drawing><wp:anchor>"
        f'<wp:extent cx="{width_emu}" cy="{height_emu}"/>'
        '<wp:docPr id="1" name="Box"/>'
        "<a:graphic>"
        '<a:graphicData uri="http://schemas.microsoft.com/office/word/2010/wordprocessingShape">'
        "<wps:wsp>"
        f"<wps:txbx><w:txbxContent>{inner_paragraphs}</w:txbxContent></wps:txbx>"
        "</wps:wsp></a:graphicData></a:graphic>"
        "</wp:anchor></w:drawing></w:r>"
    )


def _build_docx_with_contact_textbox_at(
    paragraphs: list[str],
    textbox_paragraph_index: int,
    lines: list[str] | None = None,
    width_emu: int = 1000000,
    height_emu: int = 500000,
) -> bytes:
    """
    A real, openable DOCX with a minimal but structurally faithful
    floating Contact text box (CONTACT PERSON marker + name + email,
    matching the real corpus's own <w:txbxContent> shape, wrapped in a
    DrawingML <w:drawing><wp:anchor> run so _extract_extent and the
    <w:r>/<w:txbxContent> nesting _find_run_span must see through both
    apply) injected into one specific paragraph.
    """

    document = Document()
    for text in paragraphs:
        document.add_paragraph(text)

    target_paragraph = document.paragraphs[textbox_paragraph_index]

    run_element = etree.fromstring(
        _textbox_run_xml(
            lines
            or ["CONTACT PERSON", "Test Person", "test.person@example.com"],
            width_emu=width_emu,
            height_emu=height_emu,
        ).encode("utf-8")
    )
    target_paragraph._p.append(run_element)

    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _document_xml_of(docx_bytes: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(docx_bytes)) as archive:
        return archive.read("word/document.xml").decode("utf-8")


def _visible_text(docx_bytes: bytes) -> str:
    """An approximation of what a human actually sees in Word: the raw
    document.xml with every <w:vanish/>-marked run stripped first (both
    a floating box's own paragraphs and plain in-flow body paragraphs
    can carry hidden marker runs), then every remaining tag collapsed
    to a space. python-docx's own paragraph API is not used here since
    it never walks into a floating shape's own content at all, and its
    plain .text property does not honor w:vanish."""

    import re
    from html import unescape

    document_xml = _document_xml_of(docx_bytes)
    visible_xml = re.sub(
        r"<w:r>(?:(?!</w:r>).)*?<w:vanish/>(?:(?!</w:r>).)*?</w:r>",
        "",
        document_xml,
        flags=re.DOTALL,
    )
    return unescape(re.sub(r"<[^>]+>", " ", visible_xml))


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


class IsContactRelatedBlockTests(unittest.TestCase):
    def test_contact_person_block_is_classified_as_contact_related(
        self,
    ) -> None:
        self.assertTrue(
            _is_contact_related_block(["CONTACT PERSON", "Jane Doe"])
        )

    def test_firm_block_with_email_is_classified_as_contact_related(
        self,
    ) -> None:
        self.assertTrue(
            _is_contact_related_block(["SOME FIRM", "info@example.com"])
        )

    def test_firm_block_with_phone_is_classified_as_contact_related(
        self,
    ) -> None:
        self.assertTrue(
            _is_contact_related_block(["SOME FIRM", "+1 555 000 1234"])
        )

    def test_plain_branding_block_is_not_contact_related(self) -> None:
        self.assertFalse(_is_contact_related_block(["www.leglobal.law"]))

    def test_document_title_block_is_not_contact_related(self) -> None:
        self.assertFalse(
            _is_contact_related_block(
                ["Employment Law Overview - Testland"]
            )
        )


class FindAllContactRunsTests(unittest.TestCase):
    """
    Regression coverage for two bugs found and fixed this mission: (1)
    _find_run_span originally took the first "</w:r>" after a
    txbxContent's start, which is actually an INNER run nested inside
    the box's own paragraphs, not the enclosing floating-shape run: it
    must instead balance nested <w:r>/</w:r> tags. (2) a floating
    shape's Choice and Fallback branches each produce their own
    <w:txbxContent> regex match for the SAME enclosing run; the second
    occurrence must be recognized as already-covered by the first
    match's span, not re-resolved (which - before the fix - picked one
    of the first branch's own inner runs as a bogus second "run").
    """

    def test_finds_single_contact_run_with_its_extent(self) -> None:
        docx_bytes = _build_docx_with_contact_textbox_at(
            ["Title", "Body"],
            textbox_paragraph_index=0,
            width_emu=1234000,
            height_emu=567000,
        )

        runs = _find_all_contact_runs(_document_xml_of(docx_bytes))

        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0].width_emu, 1234000)
        self.assertEqual(runs[0].height_emu, 567000)

    def test_ignores_non_contact_textboxes(self) -> None:
        document = Document()
        document.add_paragraph("Title")
        document.add_paragraph("Body")

        branding_run = etree.fromstring(
            _textbox_run_xml(["www.leglobal.law"]).encode("utf-8")
        )
        document.paragraphs[0]._p.append(branding_run)

        buffer = io.BytesIO()
        document.save(buffer)

        runs = _find_all_contact_runs(_document_xml_of(buffer.getvalue()))
        self.assertEqual(runs, [])

    def test_two_separate_contact_runs_both_found_and_largest_is_selectable(
        self,
    ) -> None:
        document = Document()
        document.add_paragraph("Title")

        small_run = etree.fromstring(
            _textbox_run_xml(
                ["CONTACT PERSON", "Jane Doe", "jane@example.com"],
                width_emu=500000,
                height_emu=300000,
            ).encode("utf-8")
        )
        large_run = etree.fromstring(
            _textbox_run_xml(
                ["SOME FIRM", "firm@example.com"],
                width_emu=2000000,
                height_emu=1000000,
            ).encode("utf-8")
        )
        document.paragraphs[0]._p.append(small_run)
        document.paragraphs[0]._p.append(large_run)

        buffer = io.BytesIO()
        document.save(buffer)

        runs = _find_all_contact_runs(_document_xml_of(buffer.getvalue()))

        self.assertEqual(len(runs), 2)
        primary = max(runs, key=lambda run: run.width_emu * run.height_emu)
        self.assertEqual(primary.width_emu, 2000000)

    def test_choice_and_fallback_style_duplicate_txbxcontent_is_one_run(
        self,
    ) -> None:
        # Mirrors a real floating shape's mc:Choice/mc:Fallback pair:
        # the SAME visible box content appears twice inside one <w:r>,
        # each occurrence separately matched by the txbxContent regex.
        # Must resolve to exactly one run, not two.
        document = Document()
        document.add_paragraph("Title")

        inner = (
            "<w:p><w:r><w:t>CONTACT PERSON</w:t></w:r></w:p>"
            "<w:p><w:r><w:t>Jane Doe</w:t></w:r></w:p>"
            "<w:p><w:r><w:t>jane@example.com</w:t></w:r></w:p>"
        )
        duplicated_run_xml = (
            '<w:r xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            "<mc:AlternateContent "
            'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006">'
            '<mc:Choice Requires="wps">'
            f"<w:txbxContent>{inner}</w:txbxContent>"
            "</mc:Choice>"
            "<mc:Fallback>"
            f"<w:txbxContent>{inner}</w:txbxContent>"
            "</mc:Fallback>"
            "</mc:AlternateContent>"
            "</w:r>"
        )
        run_element = etree.fromstring(duplicated_run_xml.encode("utf-8"))
        document.paragraphs[0]._p.append(run_element)

        buffer = io.BytesIO()
        document.save(buffer)

        runs = _find_all_contact_runs(_document_xml_of(buffer.getvalue()))
        self.assertEqual(
            len(runs), 1,
            "Choice+Fallback duplicate content must resolve to one run, "
            "not one real run plus a bogus nested-run duplicate",
        )


class MaterializeEffectiveDocxTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tempdir = TemporaryDirectory()
        self.source_path = Path(self._tempdir.name) / "Testland.docx"
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
            hashlib.sha256(self.source_path.read_bytes()).hexdigest(),
            self.original_hash,
        )

    def _materialize_and_reparse(
        self,
        contacts: list[ExtractedContact],
        source_path: Path | None = None,
    ) -> list[ExtractedContact]:
        effective_bytes = materialize_effective_docx(
            source_path=source_path or self.source_path,
            contacts=contacts,
        )

        Document(io.BytesIO(effective_bytes))  # must open cleanly

        return extract_contacts_from_docx(io.BytesIO(effective_bytes))

    # --- no pre-existing box (FR/PT-style): always in-flow ---------

    def test_case_a_edited_contact_appears_and_old_value_is_absent(
        self,
    ) -> None:
        reparsed = self._materialize_and_reparse([_CONTACT_A])
        self.assertEqual(reparsed, [_CONTACT_A])

        edited = replace(_CONTACT_A, phone="+1 555 9999")
        reparsed_after_edit = self._materialize_and_reparse([edited])

        self.assertEqual(reparsed_after_edit, [edited])
        self.assertNotIn("555 0001", reparsed_after_edit[0].phone or "")
        self._assert_source_unchanged()

    def test_case_b_n_contacts_all_present_in_order(self) -> None:
        reparsed = self._materialize_and_reparse(
            [_CONTACT_A, _CONTACT_B, _CONTACT_C]
        )
        self.assertEqual(reparsed, [_CONTACT_A, _CONTACT_B, _CONTACT_C])
        self._assert_source_unchanged()

    def test_case_c_intentional_duplicates_both_preserved(self) -> None:
        duplicate = replace(_CONTACT_A)
        reparsed = self._materialize_and_reparse([_CONTACT_A, duplicate])

        self.assertEqual(len(reparsed), 2)
        self.assertEqual(reparsed[0], reparsed[1])
        self._assert_source_unchanged()

    def test_case_d_delete_one_among_n_leaves_the_others_intact(
        self,
    ) -> None:
        reparsed = self._materialize_and_reparse([_CONTACT_A, _CONTACT_C])

        self.assertEqual(reparsed, [_CONTACT_A, _CONTACT_C])
        self.assertNotIn(
            _CONTACT_B.member_firm, [c.member_firm for c in reparsed]
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
        reparsed = self._materialize_and_reparse([_CONTACT_A])
        self.assertEqual(reparsed, [_CONTACT_A])
        self._assert_source_unchanged()

    def test_incomplete_legacy_contact_round_trips_with_blanks_preserved(
        self,
    ) -> None:
        reparsed = self._materialize_and_reparse([_INCOMPLETE_CONTACT])

        self.assertEqual(reparsed, [_INCOMPLETE_CONTACT])
        self.assertIsNone(reparsed[0].member_firm)
        self.assertIsNone(reparsed[0].phone)
        self._assert_source_unchanged()

    def test_legal_content_unchanged_by_contact_export(self) -> None:
        effective_bytes = materialize_effective_docx(
            source_path=self.source_path,
            contacts=[_CONTACT_A, _CONTACT_B],
        )

        document = Document(io.BytesIO(effective_bytes))
        paragraph_texts = [p.text for p in document.paragraphs]

        legal_paragraphs_set = set(_LEGAL_PARAGRAPHS)
        surviving_legal_paragraphs = [
            text for text in paragraph_texts if text in legal_paragraphs_set
        ]
        self.assertEqual(surviving_legal_paragraphs, _LEGAL_PARAGRAPHS)

    def test_contact_block_inserted_at_anchor_not_document_end(
        self,
    ) -> None:
        # Reproduces the exact positional bug a real human Word test
        # found in v0.8.1 (content landing ~99% through the document,
        # after all legal content) - the block must sit immediately
        # after the anchor (title) paragraph, with real legal content
        # still following it.
        effective_bytes = materialize_effective_docx(
            source_path=self.source_path, contacts=[_CONTACT_A]
        )

        document = Document(io.BytesIO(effective_bytes))
        paragraph_texts = [p.text for p in document.paragraphs]

        title_index = paragraph_texts.index(_LEGAL_PARAGRAPHS[0])
        member_firm_index = next(
            i
            for i, text in enumerate(paragraph_texts)
            if text.startswith("Member firm:")
        )

        # The hidden marker is its own paragraph immediately after the
        # title (python-docx's plain .text property does not honor
        # w:vanish, so the paragraph is present in this list even
        # though Word never renders it) - "Member firm:" is the next
        # paragraph after that.
        self.assertEqual(member_firm_index, title_index + 2)
        self.assertIn(_LEGAL_PARAGRAPHS[1], paragraph_texts[member_firm_index:])

    def test_source_file_never_written_to(self) -> None:
        original_bytes = self.source_path.read_bytes()

        materialize_effective_docx(
            source_path=self.source_path,
            contacts=[_CONTACT_A, _CONTACT_B],
        )

        self.assertEqual(self.source_path.read_bytes(), original_bytes)

    def test_no_visible_technical_markers_ever(self) -> None:
        for contacts in ([], [_CONTACT_A], [_CONTACT_A, _CONTACT_B]):
            effective_bytes = materialize_effective_docx(
                source_path=self.source_path, contacts=contacts
            )
            visible = _visible_text(effective_bytes)

            self.assertNotIn(CONTACT_BOX_HIDDEN_MARKER, visible)
            self.assertNotIn(CONTACT_BOX_HIDDEN_END_MARKER, visible)
            self.assertNotIn("ADMIN-MANAGED", visible)
            self.assertNotIn("END OF CONTACT", visible)

    # --- pre-existing box, single contact: box-reuse-in-place -------

    def test_single_contact_reuses_existing_box_in_place(self) -> None:
        with TemporaryDirectory() as tempdir:
            source_path = Path(tempdir) / "Testland.docx"
            source_path.write_bytes(
                _build_docx_with_contact_textbox_at(
                    _LEGAL_PARAGRAPHS, textbox_paragraph_index=0
                )
            )

            reparsed = self._materialize_and_reparse(
                [_CONTACT_A], source_path=source_path
            )
            effective_bytes = materialize_effective_docx(
                source_path=source_path, contacts=[_CONTACT_A]
            )

        self.assertEqual(reparsed, [_CONTACT_A])

        # The old box's own stale content must be gone, and no separate
        # in-flow block should have been added alongside it - a single
        # contact must land inside the SAME run the original box used.
        visible = _visible_text(effective_bytes)
        self.assertNotIn("Test Person", visible)
        self.assertNotIn("test.person@example.com", visible)

        document_xml = _document_xml_of(effective_bytes)
        # Exactly one contact-related run remains (the reused box) -
        # no additional in-flow paragraphs were inserted for N=1.
        self.assertEqual(
            document_xml.count("Member firm: ORIGINAL FIRM"), 1
        )

    def test_zero_contacts_with_existing_box_shows_neutral_message_in_box(
        self,
    ) -> None:
        with TemporaryDirectory() as tempdir:
            source_path = Path(tempdir) / "Testland.docx"
            source_path.write_bytes(
                _build_docx_with_contact_textbox_at(
                    _LEGAL_PARAGRAPHS, textbox_paragraph_index=0
                )
            )

            reparsed = self._materialize_and_reparse(
                [], source_path=source_path
            )
            effective_bytes = materialize_effective_docx(
                source_path=source_path, contacts=[]
            )

        self.assertEqual(reparsed, [])
        visible = _visible_text(effective_bytes)
        self.assertNotIn("Test Person", visible)
        self.assertIn(DETERMINISTIC_NO_CONTACTS_LINE, visible)

    # --- pre-existing box, 2+ contacts: safe in-flow fallback -------

    def test_two_contacts_with_existing_box_blanks_box_and_uses_in_flow(
        self,
    ) -> None:
        # The scenario a real human Word test found broken in v0.8.2's
        # box-resize attempt (garbled overlap with the document's own
        # title for 3+ contacts on a real corpus file) - 2+ contacts
        # must never stretch the original box; the box is blanked and
        # the current contacts appear as a safe in-flow block instead.
        with TemporaryDirectory() as tempdir:
            source_path = Path(tempdir) / "Testland.docx"
            source_path.write_bytes(
                _build_docx_with_contact_textbox_at(
                    _LEGAL_PARAGRAPHS, textbox_paragraph_index=0
                )
            )

            reparsed = self._materialize_and_reparse(
                [_CONTACT_A, _CONTACT_B], source_path=source_path
            )
            effective_bytes = materialize_effective_docx(
                source_path=source_path,
                contacts=[_CONTACT_A, _CONTACT_B],
            )

        self.assertEqual(reparsed, [_CONTACT_A, _CONTACT_B])

        visible = _visible_text(effective_bytes)
        self.assertNotIn("Test Person", visible)
        self.assertNotIn("test.person@example.com", visible)

    def test_three_contacts_all_present_with_existing_box(self) -> None:
        with TemporaryDirectory() as tempdir:
            source_path = Path(tempdir) / "Testland.docx"
            source_path.write_bytes(
                _build_docx_with_contact_textbox_at(
                    _LEGAL_PARAGRAPHS, textbox_paragraph_index=0
                )
            )

            reparsed = self._materialize_and_reparse(
                [_CONTACT_A, _CONTACT_B, _CONTACT_C],
                source_path=source_path,
            )

        self.assertEqual(reparsed, [_CONTACT_A, _CONTACT_B, _CONTACT_C])

    def test_blank_line_delimiter_survives_lxml_reserialization(
        self,
    ) -> None:
        # Regression test for a bug where a self-closing <w:p/> blank-
        # line delimiter between contacts was silently collapsed by
        # lxml's serializer during document.save(), merging both
        # contacts' fields into one entry (the second contact's values
        # overwrote the first's for every shared field label). Uses the
        # in-flow (document.save()-backed) path with no pre-existing
        # box, where this bug was originally found.
        reparsed = self._materialize_and_reparse([_CONTACT_A, _CONTACT_B])

        self.assertEqual(len(reparsed), 2)
        self.assertEqual(reparsed[0], _CONTACT_A)
        self.assertEqual(reparsed[1], _CONTACT_B)


if __name__ == "__main__":
    unittest.main()
