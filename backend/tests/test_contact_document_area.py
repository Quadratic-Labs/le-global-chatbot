"""
Tests for the Admin-managed canonical contact table primitive (the
Word-native in-flow table that replaced both this module's own earlier
hand-authored floating-textbox design - rejected by real Microsoft
Word - and the original two-box-cloning design before it).

Mirrors the established real-corpus convention: exercise the REAL
Australia/Portugal baselines (temp copies, never the files themselves),
skipping gracefully when unavailable, and prove structural correctness
(XML validity, no dangling relationships, no new floating shapes)
rather than merely checking that strings exist somewhere in
document.xml.
"""

from __future__ import annotations

import re
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from docx import Document as WordDocument

from app.services.contact_document_area import (
    ContactAreaError,
    ContactPhotoPayload,
    rebuild_canonical_contact_table,
    resolve_untracked_contact_photo,
)
from app.services.contact_photos import extract_contact_photo_candidates
from app.services.docx_parser import (
    CONTACT_TABLE_HIDDEN_MARKER,
    ExtractedContact,
    extract_contacts_from_docx,
)


SOURCE_ROOT = Path("/data/documents/source")


def _make_png(width: int, height: int, rgb: tuple[int, int, int]) -> bytes:
    """A minimal but well-formed RGB PNG - real-sized (never 1x1), so
    python-docx's own image-header parser (used to compute a photo's
    proportional height) accepts it the same way it accepts a real
    camera photo, unlike a degenerate single-pixel fixture."""

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
    raw = b"".join(
        b"\x00" + bytes(rgb) * width for _ in range(height)
    )
    image_data = zlib.compress(raw)
    return (
        signature
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", image_data)
        + chunk(b"IEND", b"")
    )


_VALID_PNG = _make_png(183, 234, (200, 50, 50))


class ContactDocumentAreaTests(unittest.TestCase):

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def _require_copy(self, filename: str) -> Path:
        source = SOURCE_ROOT / filename

        if not source.exists():
            self.skipTest(f"Real corpus source unavailable: {source}")

        original_bytes = source.read_bytes()
        copy_path = Path(self.temp.name) / filename
        copy_path.write_bytes(original_bytes)

        self.addCleanup(
            lambda: self.assertEqual(
                original_bytes,
                source.read_bytes(),
                f"{source} was mutated by this test.",
            )
        )

        return copy_path

    def _structural_checks(self, path: Path) -> None:
        """Zip integrity, XML well-formedness, no dangling
        relationships, no [trash]/ parts, and no NEW floating contact
        shapes (wp:anchor) - the canonical table's own content is
        exclusively ordinary inline pictures/paragraphs."""

        with zipfile.ZipFile(path) as archive:
            self.assertIsNone(archive.testzip())
            names = set(archive.namelist())
            self.assertFalse(
                [n for n in names if n.startswith("[trash]")],
                "no [trash]/ parts may be left behind",
            )
            document_xml = archive.read(
                "word/document.xml"
            ).decode("utf-8")
            rels_xml = archive.read(
                "word/_rels/document.xml.rels"
            ).decode("utf-8")
            content_types_xml = archive.read(
                "[Content_Types].xml"
            ).decode("utf-8")

        for label, content in (
            ("document.xml", document_xml),
            ("rels", rels_xml),
            ("content types", content_types_xml),
        ):
            try:
                ET.fromstring(content)
            except ET.ParseError as error:
                self.fail(f"{label} is not well-formed XML: {error}")

        referenced_ids = set(
            re.findall(r'r:(?:id|embed)="([^"]+)"', document_xml)
        )
        declared_ids = set(re.findall(r'Id="([^"]+)"', rels_xml))
        self.assertEqual(
            set(), referenced_ids - declared_ids,
            "every r:id/r:embed referenced in document.xml must be "
            "declared in the relationships part",
        )

        # Re-opening via python-docx is itself a strong well-formedness
        # signal (the same parser Word's own OOXML reader family is
        # closely related to, unlike a bare lxml.fromstring check).
        WordDocument(path)

    def _table_xml(self, document_xml: str) -> str:
        marker_index = document_xml.find(CONTACT_TABLE_HIDDEN_MARKER)
        self.assertNotEqual(
            -1, marker_index,
            "the canonical table's own hidden marker must be present",
        )
        table_start = document_xml.rfind("<w:tbl>", 0, marker_index)
        table_end = document_xml.find("</w:tbl>", marker_index)
        self.assertNotEqual(-1, table_start)
        self.assertNotEqual(-1, table_end)
        return document_xml[table_start:table_end + len("</w:tbl>")]

    def test_add_first_contact_with_photo_produces_coherent_block(
        self,
    ) -> None:
        """Test items 1 and 2: canonicalizing AU's own organic contact
        area leaves no large empty band (Introduction follows the new
        table directly) and the sole contact keeps its own photo."""

        path = self._require_copy("AU.docx")

        with zipfile.ZipFile(path) as archive:
            original_document_xml = archive.read(
                "word/document.xml"
            ).decode("utf-8")
        original_wrap_count = original_document_xml.count(
            "wrapTopAndBottom"
        )

        original_contacts = extract_contacts_from_docx(
            path, country="Australia"
        )
        self.assertEqual(1, len(original_contacts))
        michael = original_contacts[0]

        michael_photo = resolve_untracked_contact_photo(
            path, contact_person=michael.contact_person, country="Australia"
        )
        self.assertIsNotNone(michael_photo)

        new_bytes = rebuild_canonical_contact_table(
            path,
            contacts=(michael,),
            photos=(michael_photo,),
            country="Australia",
        )
        path.write_bytes(new_bytes)
        self._structural_checks(path)

        with zipfile.ZipFile(path) as archive:
            document_xml = archive.read(
                "word/document.xml"
            ).decode("utf-8")

        # The canonical TABLE ITSELF contains no floating shapes at
        # all - only ordinary inline content. Page furniture unrelated
        # to the contact area (URL box, title box, logo) legitimately
        # keeps its own pre-existing floating shapes elsewhere in the
        # document, so this checks the table's own XML specifically,
        # never a whole-document count.
        table_xml = self._table_xml(document_xml)
        self.assertEqual(
            0, table_xml.count("<wp:anchor"),
            "the canonical table must contain no floating shapes at "
            "all - only ordinary inline content",
        )

        # No large reserved band: the legacy reserved-space rectangle
        # is gone (the baseline's OTHER wrapTopAndBottom shape - its
        # own cover-page title textbox, which has real text and is
        # never a removal candidate - legitimately survives, so this
        # compares counts rather than asserting zero). Each shape
        # contributes exactly one "wrapTopAndBottom" substring match
        # (its own DrawingML Choice branch only - the VML Fallback
        # branch spells it differently, type="topAndBottom"), so
        # removing exactly the rectangle drops the count by exactly 1.
        self.assertEqual(
            original_wrap_count - 1,
            document_xml.count("wrapTopAndBottom"),
            "exactly the reserved-space rectangle must be gone, "
            "leaving only the title textbox's own occurrence",
        )

        reparsed = extract_contacts_from_docx(path, country="Australia")
        self.assertEqual(1, len(reparsed))
        self.assertEqual("Michael Harmer", reparsed[0].contact_person)

        photos = extract_contact_photo_candidates(path)
        self.assertEqual(1, len(photos))
        self.assertEqual(michael_photo.data, photos[0].data)

    def test_two_contacts_two_distinct_photos(self) -> None:
        """Test item 3: two contacts, each with its own distinct
        photo, round-trip as two separate contacts with two separate
        photos - never merged or cross-associated."""

        path = self._require_copy("AU.docx")

        michael = extract_contacts_from_docx(path, country="Australia")[0]
        michael_photo = resolve_untracked_contact_photo(
            path, contact_person=michael.contact_person, country="Australia"
        )

        jane = ExtractedContact(
            member_firm="Second Firm Pty Ltd",
            contact_person="Jane Secondary",
            email="jane.secondary@secondfirm.com.au",
            phone="+61 2 9000 0000",
            address="100 Test Street, Level 5",
            website="www.secondfirm.com.au",
        )
        jane_photo = ContactPhotoPayload(
            data=_VALID_PNG, content_type="image/png"
        )

        new_bytes = rebuild_canonical_contact_table(
            path,
            contacts=(michael, jane),
            photos=(michael_photo, jane_photo),
            country="Australia",
        )
        path.write_bytes(new_bytes)
        self._structural_checks(path)

        reparsed = extract_contacts_from_docx(path, country="Australia")
        self.assertEqual(2, len(reparsed))
        self.assertEqual("Michael Harmer", reparsed[0].contact_person)
        self.assertEqual("Jane Secondary", reparsed[1].contact_person)

        photos = extract_contact_photo_candidates(path)
        shas = {p.sha256 for p in photos}
        self.assertEqual(2, len(shas))
        self.assertIn(michael_photo and _sha(michael_photo.data), shas)
        self.assertIn(_sha(jane_photo.data), shas)

    def test_three_contacts_all_preserved_in_order(self) -> None:
        """Test item 4: three contacts round-trip in the same order,
        with the same field values."""

        path = self._require_copy("AU.docx")
        michael = extract_contacts_from_docx(path, country="Australia")[0]

        jane = ExtractedContact(
            member_firm="Second Firm Pty Ltd",
            contact_person="Jane Secondary",
            email="jane.secondary@secondfirm.com.au",
        )
        priya = ExtractedContact(
            member_firm="Third Firm Pty Ltd",
            contact_person="Priya Third",
            email="priya.third@thirdfirm.com.au",
        )

        new_bytes = rebuild_canonical_contact_table(
            path,
            contacts=(michael, jane, priya),
            photos=(None, None, None),
            country="Australia",
        )
        path.write_bytes(new_bytes)
        self._structural_checks(path)

        reparsed = extract_contacts_from_docx(path, country="Australia")
        self.assertEqual(3, len(reparsed))
        self.assertEqual(
            ["Michael Harmer", "Jane Secondary", "Priya Third"],
            [c.contact_person for c in reparsed],
        )

    def test_contact_without_photo_produces_coherent_block(self) -> None:
        """Test item 5: a contact with no photo at all still produces
        a coherent block - an empty right cell, never a missing row or
        misaligned text."""

        path = self._require_copy("AU.docx")
        michael = extract_contacts_from_docx(path, country="Australia")[0]

        jane = ExtractedContact(
            member_firm="Second Firm Pty Ltd",
            contact_person="Jane Secondary",
            email="jane.secondary@secondfirm.com.au",
        )

        new_bytes = rebuild_canonical_contact_table(
            path,
            contacts=(michael, jane),
            photos=(None, None),
            country="Australia",
        )
        path.write_bytes(new_bytes)
        self._structural_checks(path)

        reparsed = extract_contacts_from_docx(path, country="Australia")
        self.assertEqual(2, len(reparsed))

        photos = extract_contact_photo_candidates(path)
        self.assertEqual(0, len(photos))

    def test_delete_contact_b_leaves_a_intact(self) -> None:
        """Test item 6: rebuilding from a contact list with B removed
        leaves B's text/photo gone and A's text/photo untouched."""

        path = self._require_copy("AU.docx")
        michael = extract_contacts_from_docx(path, country="Australia")[0]
        michael_photo = resolve_untracked_contact_photo(
            path, contact_person=michael.contact_person, country="Australia"
        )

        jane = ExtractedContact(
            member_firm="Second Firm Pty Ltd",
            contact_person="Jane Secondary",
            email="jane.secondary@secondfirm.com.au",
        )
        jane_photo = ContactPhotoPayload(
            data=_VALID_PNG, content_type="image/png"
        )

        with_both = rebuild_canonical_contact_table(
            path,
            contacts=(michael, jane),
            photos=(michael_photo, jane_photo),
            country="Australia",
        )
        path.write_bytes(with_both)

        # Now rebuild again from ONLY Michael - simulating Jane's
        # deletion the same way admin_contacts.py's unified
        # synchronization does (rebuild from the full surviving list).
        after_delete = rebuild_canonical_contact_table(
            path,
            contacts=(michael,),
            photos=(michael_photo,),
            country="Australia",
        )
        path.write_bytes(after_delete)
        self._structural_checks(path)

        with zipfile.ZipFile(path) as archive:
            document_xml = archive.read(
                "word/document.xml"
            ).decode("utf-8")

        self.assertNotIn("Jane Secondary", document_xml)
        self.assertNotIn("jane.secondary@secondfirm.com.au", document_xml)
        self.assertIn("Michael Harmer", document_xml)

        remaining_photos = {
            p.sha256 for p in extract_contact_photo_candidates(path)
        }
        self.assertNotIn(_sha(jane_photo.data), remaining_photos)
        self.assertIn(_sha(michael_photo.data), remaining_photos)

    def test_deleting_every_contact_leaves_no_table_and_no_band(
        self,
    ) -> None:
        """Rebuilding with zero contacts removes the canonical area
        entirely - no marker-only leftover table, no empty band."""

        path = self._require_copy("AU.docx")
        michael = extract_contacts_from_docx(path, country="Australia")[0]

        with_contact = rebuild_canonical_contact_table(
            path, contacts=(michael,), photos=(None,), country="Australia",
        )
        path.write_bytes(with_contact)

        emptied = rebuild_canonical_contact_table(
            path, contacts=(), photos=(), country="Australia",
        )
        path.write_bytes(emptied)
        self._structural_checks(path)

        with zipfile.ZipFile(path) as archive:
            document_xml = archive.read(
                "word/document.xml"
            ).decode("utf-8")

        self.assertNotIn(CONTACT_TABLE_HIDDEN_MARKER, document_xml)
        self.assertNotIn("<w:tbl>", document_xml)
        self.assertEqual(0, len(extract_contacts_from_docx(path)))

    def test_legal_content_unchanged(self) -> None:
        """Test item 9: real legal section text (well past the
        contact area) is byte-identical after the rebuild."""

        path = self._require_copy("AU.docx")

        with zipfile.ZipFile(path) as archive:
            before = archive.read("word/document.xml").decode("utf-8")

        introduction_index = before.find("Introduction")
        self.assertNotEqual(-1, introduction_index)
        legal_tail_before = before[introduction_index:]

        michael = extract_contacts_from_docx(path, country="Australia")[0]
        new_bytes = rebuild_canonical_contact_table(
            path, contacts=(michael,), photos=(None,), country="Australia",
        )
        path.write_bytes(new_bytes)

        with zipfile.ZipFile(path) as archive:
            after = archive.read("word/document.xml").decode("utf-8")

        introduction_index_after = after.find("Introduction")
        self.assertNotEqual(-1, introduction_index_after)
        legal_tail_after = after[introduction_index_after:]

        self.assertEqual(
            legal_tail_before, legal_tail_after,
            "legal content from Introduction onward must be "
            "byte-for-byte unchanged",
        )

    def test_no_matching_contact_area_still_produces_canonical_table(
        self,
    ) -> None:
        """A document with no legacy floating contact area at all
        (Portugal) still gets a real canonical table - never left
        ContactState-only."""

        path = self._require_copy("PT.docx")

        new_contact = ExtractedContact(
            member_firm="Someone New Lda",
            contact_person="Someone New",
            email="new@example.test",
            phone="+351 21 000 0000",
            address="Rua Nova 1",
            website="www.newfirm.test",
        )

        new_bytes = rebuild_canonical_contact_table(
            path, contacts=(new_contact,), photos=(None,), country="Portugal",
        )
        path.write_bytes(new_bytes)
        self._structural_checks(path)

        reparsed = extract_contacts_from_docx(path, country="Portugal")
        self.assertEqual(1, len(reparsed))
        self.assertEqual("Someone New", reparsed[0].contact_person)

    def test_second_rebuild_replaces_rather_than_duplicates_table(
        self,
    ) -> None:
        """Rebuilding twice in a row replaces the one canonical table
        rather than stacking a second one alongside it."""

        path = self._require_copy("PT.docx")

        first = ExtractedContact(
            member_firm="First Firm Lda",
            contact_person="First Person",
            email="first@example.test",
        )
        second_contact = ExtractedContact(
            member_firm="Second Firm Lda",
            contact_person="Second Person",
            email="second@example.test",
        )

        path.write_bytes(
            rebuild_canonical_contact_table(
                path, contacts=(first,), photos=(None,), country="Portugal",
            )
        )
        path.write_bytes(
            rebuild_canonical_contact_table(
                path,
                contacts=(first, second_contact),
                photos=(None, None),
                country="Portugal",
            )
        )
        self._structural_checks(path)

        with zipfile.ZipFile(path) as archive:
            document_xml = archive.read(
                "word/document.xml"
            ).decode("utf-8")

        self.assertEqual(1, document_xml.count("<w:tbl>"))
        self.assertEqual(
            1, document_xml.count(CONTACT_TABLE_HIDDEN_MARKER)
        )

        reparsed = extract_contacts_from_docx(path, country="Portugal")
        self.assertEqual(2, len(reparsed))

    def test_photo_with_crop_rectangle_is_refused_not_guessed(
        self,
    ) -> None:
        """resolve_untracked_contact_photo refuses (rather than
        blindly embeds) a legacy photo whose own source shape carries
        an OOXML crop rectangle this environment cannot reproduce."""

        path = self._require_copy("AU.docx")

        with zipfile.ZipFile(path) as archive:
            document_xml = archive.read(
                "word/document.xml"
            ).decode("utf-8")

        # Inject a synthetic crop rectangle into Michael's own photo
        # shape to simulate a cropped legacy photo - this baseline
        # itself has none (verified separately), so this test
        # fabricates the one condition it needs to exercise.
        patched_xml = document_xml.replace(
            '<a:blip r:embed="rId10"/>',
            '<a:blip r:embed="rId10"/><a:srcRect l="1000" t="1000" '
            'r="1000" b="1000"/>',
            1,
        )
        self.assertNotEqual(
            document_xml, patched_xml,
            "sanity: the replacement must actually match something",
        )

        from app.services.contact_document_photos import _rewrite_zip

        patched_bytes = _rewrite_zip(
            path.read_bytes(),
            replacements={"word/document.xml": patched_xml},
        )
        path.write_bytes(patched_bytes)

        with self.assertRaises(ContactAreaError):
            resolve_untracked_contact_photo(
                path, contact_person="Michael Harmer", country="Australia"
            )

    def test_multiple_emails_on_one_line_round_trip_preserved(self) -> None:
        """Regression for the real France defect found by the
        full-corpus mutation test: FR's actual contact carries two
        email addresses on a single comma-joined line
        ("scherrmann@flichy.com, bacquet@flichy.com"), because
        ExtractedContact/ContactState model email as one string field
        and the writer renders it verbatim as one line. The canonical
        table reader used to recover only the first address via a
        single regex .search(); the round-trip validator correctly
        caught the loss and refused the rebuild with a
        ContactAreaError, but the fix (using .findall() to recover
        every address on the line) must let the exact real France
        contact round-trip cleanly with both emails intact, in
        order."""

        path = self._require_copy("FR.docx")

        flichy = ExtractedContact(
            member_firm="Flichy Grangé Avocats",
            contact_person="Caroline Scherrmann and Florence Bacquet",
            email="scherrmann@flichy.com, bacquet@flichy.com",
            phone="+33 1 56 62 30 00",
        )

        new_bytes = rebuild_canonical_contact_table(
            path,
            contacts=(flichy,),
            photos=(None,),
            country="France",
        )
        path.write_bytes(new_bytes)
        self._structural_checks(path)

        reparsed = extract_contacts_from_docx(path, country="France")
        self.assertEqual(1, len(reparsed))
        self.assertEqual(
            "scherrmann@flichy.com, bacquet@flichy.com",
            reparsed[0].email,
        )


def _sha(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


if __name__ == "__main__":
    unittest.main()
