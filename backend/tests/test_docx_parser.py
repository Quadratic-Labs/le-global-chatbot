import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.text.paragraph import Paragraph

from app.core.legal_taxonomy import get_canonical_legal_topic
from app.core.subsection_taxonomy import get_subsection_topic_override
from app.services.docx_parser import (
    build_contact_chunk_content,
    parse_contact_blocks,
    parse_docx_sections,
)


def _mark_as_numbered(
    paragraph: Paragraph,
) -> None:
    """Mark a paragraph as a numbered-list item in Word XML."""

    paragraph_properties = (
        paragraph._p.get_or_add_pPr()
    )

    numbering_properties = OxmlElement(
        "w:numPr"
    )

    indentation_level = OxmlElement(
        "w:ilvl"
    )
    indentation_level.set(
        qn("w:val"),
        "0",
    )

    numbering_id = OxmlElement(
        "w:numId"
    )
    numbering_id.set(
        qn("w:val"),
        "1",
    )

    numbering_properties.append(
        indentation_level
    )
    numbering_properties.append(
        numbering_id
    )

    paragraph_properties.append(
        numbering_properties
    )


def _mark_as_explicitly_unbolded(
    paragraph: Paragraph,
) -> None:
    """Apply the malformed formatting found in some L&E DOCX files."""

    paragraph_properties = (
        paragraph._p.get_or_add_pPr()
    )

    run_properties = paragraph_properties.find(
        qn("w:rPr")
    )

    if run_properties is None:
        run_properties = OxmlElement(
            "w:rPr"
        )
        paragraph_properties.append(
            run_properties
        )

    bold_property = run_properties.find(
        qn("w:b")
    )

    if bold_property is None:
        bold_property = OxmlElement(
            "w:b"
        )
        run_properties.append(
            bold_property
        )

    bold_property.set(
        qn("w:val"),
        "0",
    )


class DocxParserTests(unittest.TestCase):
    def test_groups_content_by_headings(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            file_path = (
                Path(temporary_directory)
                / "sample-legal-document.docx"
            )

            document = Document()

            document.add_paragraph(
                "Introductory legal information."
            )

            document.add_heading(
                "Employment contracts",
                level=1,
            )

            document.add_paragraph(
                "General information about employment contracts."
            )

            document.add_heading(
                "Probationary period",
                level=2,
            )

            document.add_paragraph(
                "The probationary period depends on local law."
            )

            document.save(
                file_path
            )

            sections = parse_docx_sections(
                file_path
            )

            self.assertEqual(
                len(sections),
                3,
            )

            self.assertEqual(
                sections[0].section,
                "General",
            )
            self.assertIsNone(
                sections[0].subsection
            )
            self.assertEqual(
                sections[0].content,
                "Introductory legal information.",
            )

            self.assertEqual(
                sections[1].section,
                "Employment contracts",
            )
            self.assertIsNone(
                sections[1].subsection
            )
            self.assertEqual(
                sections[1].content,
                (
                    "General information about "
                    "employment contracts."
                ),
            )

            self.assertEqual(
                sections[2].section,
                "Employment contracts",
            )
            self.assertEqual(
                sections[2].subsection,
                "Probationary period",
            )
            self.assertEqual(
                sections[2].content,
                (
                    "The probationary period "
                    "depends on local law."
                ),
            )

    def test_preserves_tables_in_document_order(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            file_path = (
                Path(temporary_directory)
                / "legal-table-document.docx"
            )

            document = Document()

            document.add_heading(
                "Termination",
                level=1,
            )

            document.add_paragraph(
                "Rules applicable before the table."
            )

            table = document.add_table(
                rows=3,
                cols=3,
            )

            table.cell(
                0,
                0,
            ).text = "Country"

            table.cell(
                0,
                1,
            ).text = "Notice period"

            table.cell(
                0,
                2,
            ).text = "Written notice"

            table.cell(
                1,
                0,
            ).text = "France"

            table.cell(
                1,
                1,
            ).text = "30 days"

            table.cell(
                1,
                2,
            ).text = "Required"

            table.cell(
                2,
                0,
            ).text = "Belgium"

            table.cell(
                2,
                1,
            ).text = "45 days"

            table.cell(
                2,
                2,
            ).text = "Required"

            document.add_paragraph(
                "Additional rules after the table."
            )

            document.save(
                file_path
            )

            sections = parse_docx_sections(
                file_path
            )

            self.assertEqual(
                len(sections),
                1,
            )

            self.assertEqual(
                sections[0].content,
                (
                    "Rules applicable before the table.\n\n"
                    "Country | Notice period | Written notice\n"
                    "France | 30 days | Required\n"
                    "Belgium | 45 days | Required\n\n"
                    "Additional rules after the table."
                ),
            )

    def test_ignores_decorative_table_content(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            file_path = (
                Path(temporary_directory)
                / "decorative-table-document.docx"
            )

            document = Document()

            document.add_heading(
                "Working Conditions",
                level=1,
            )

            document.add_table(
                rows=1,
                cols=2,
            )

            document.add_heading(
                "Salary",
                level=2,
            )

            document.add_paragraph(
                "Employees must receive their agreed salary."
            )

            document.save(
                file_path
            )

            sections = parse_docx_sections(
                file_path
            )

            self.assertEqual(
                len(sections),
                1,
            )

            self.assertEqual(
                sections[0].subsection,
                "Salary",
            )

            self.assertEqual(
                sections[0].content,
                "Employees must receive their agreed salary.",
            )

    def test_keeps_numbered_heading_as_content(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            file_path = (
                Path(temporary_directory)
                / "numbered-heading-document.docx"
            )

            document = Document()

            document.add_heading(
                "Termination",
                level=1,
            )

            document.add_heading(
                "Remedies",
                level=2,
            )

            document.add_paragraph(
                "The employee may challenge the dismissal."
            )

            numbered_paragraph = document.add_paragraph(
                "Pregnant employees are protected."
            )
            numbered_paragraph.style = "Heading 2"

            _mark_as_numbered(
                numbered_paragraph
            )

            document.add_heading(
                "Whistleblower Laws",
                level=2,
            )

            document.add_paragraph(
                "Whistleblowers receive legal protection."
            )

            document.save(
                file_path
            )

            sections = parse_docx_sections(
                file_path
            )

            self.assertEqual(
                len(sections),
                2,
            )

            self.assertEqual(
                sections[0].subsection,
                "Remedies",
            )

            self.assertIn(
                "Pregnant employees are protected.",
                sections[0].content,
            )

            self.assertEqual(
                sections[1].subsection,
                "Whistleblower Laws",
            )

    def test_keeps_unbolded_headings_as_content(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory:
            file_path = (
                Path(temporary_directory)
                / "unbolded-heading-document.docx"
            )

            document = Document()

            document.add_heading(
                "07. Termination of Employment Contracts",
                level=1,
            )

            document.add_heading(
                "Remedies for Wrongful Termination",
                level=2,
            )

            document.add_paragraph(
                "The employee may challenge the dismissal."
            )

            false_heading = document.add_paragraph(
                (
                    "Objectively null and void termination: "
                    "protected situations include:"
                )
            )
            false_heading.style = "Heading 2"

            _mark_as_explicitly_unbolded(
                false_heading
            )

            numbered_item = document.add_paragraph(
                "Pregnant employees are protected."
            )
            numbered_item.style = "Heading 2"

            _mark_as_numbered(
                numbered_item
            )
            _mark_as_explicitly_unbolded(
                numbered_item
            )

            final_paragraph = document.add_paragraph(
                (
                    "This protection means that the dismissal "
                    "may be declared null and void."
                )
            )
            final_paragraph.style = "Heading 2"

            _mark_as_explicitly_unbolded(
                final_paragraph
            )

            document.add_heading(
                "Whistleblower Laws",
                level=2,
            )

            document.add_paragraph(
                "Whistleblowers receive legal protection."
            )

            document.save(
                file_path
            )

            sections = parse_docx_sections(
                file_path
            )

            self.assertEqual(
                len(sections),
                2,
            )

            self.assertEqual(
                sections[0].subsection,
                "Remedies for Wrongful Termination",
            )

            self.assertIn(
                "Objectively null and void termination:",
                sections[0].content,
            )

            self.assertIn(
                "Pregnant employees are protected.",
                sections[0].content,
            )

            self.assertIn(
                "This protection means that the dismissal",
                sections[0].content,
            )

            self.assertEqual(
                sections[1].subsection,
                "Whistleblower Laws",
            )

    def test_accepts_heading_ending_with_colon(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            file_path = (
                Path(temporary_directory)
                / "heading-with-colon-document.docx"
            )

            document = Document()

            document.add_heading(
                "Employment Contracts",
                level=1,
            )

            document.add_heading(
                "Notice periods:",
                level=2,
            )

            document.add_paragraph(
                "The statutory notice period is fifteen days."
            )

            document.save(
                file_path
            )

            sections = parse_docx_sections(
                file_path
            )

            self.assertEqual(
                len(sections),
                1,
            )

            self.assertEqual(
                sections[0].subsection,
                "Notice periods:",
            )

            self.assertEqual(
                sections[0].content,
                "The statutory notice period is fifteen days.",
            )

    def test_detects_legacy_numbered_bold_topic(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory:
            file_path = (
                Path(temporary_directory)
                / "legacy-document.docx"
            )

            document = Document()

            document.add_paragraph(
                "Introduction content."
            )

            topic = document.add_paragraph(
                "Hiring practices in Greece"
            )
            topic.style = "List Paragraph"
            topic.runs[0].bold = True

            _mark_as_numbered(
                topic
            )

            document.add_paragraph(
                "Foreign employees require permission."
            )

            document.save(
                file_path
            )

            sections = parse_docx_sections(
                file_path=file_path,
                country="Greece",
            )

            self.assertEqual(
                len(sections),
                2,
            )

            self.assertEqual(
                sections[0].section,
                "Employment Law Overview Greece",
            )

            self.assertEqual(
                sections[0].content,
                "Introduction content.",
            )

            self.assertEqual(
                sections[1].section,
                "Hiring practices in Greece",
            )

            self.assertEqual(
                sections[1].content,
                "Foreign employees require permission.",
            )

    def test_detects_hybrid_bold_numbered_topic(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory:
            file_path = (
                Path(temporary_directory)
                / "hybrid-document.docx"
            )

            document = Document()

            topic = document.add_paragraph(
                "02. Employment Contracts"
            )
            topic.runs[0].bold = True

            document.add_paragraph(
                "Employment contracts may be open-ended."
            )

            document.save(
                file_path
            )

            sections = parse_docx_sections(
                file_path=file_path,
                country="Italy",
            )

            self.assertEqual(
                len(sections),
                1,
            )

            self.assertEqual(
                sections[0].section,
                "02. Employment Contracts",
            )

            self.assertEqual(
                sections[0].content,
                "Employment contracts may be open-ended.",
            )

    def test_keeps_unrecognized_heading_one_as_content(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory:
            file_path = (
                Path(temporary_directory)
                / "australia-heading-document.docx"
            )

            document = Document()

            document.add_heading(
                "07. Termination of Employment Contracts",
                level=1,
            )

            document.add_paragraph(
                "Termination content before the malformed heading."
            )

            false_heading = document.add_paragraph(
                "Whistleblowers currently have legal protections."
            )
            false_heading.style = "Heading 1"

            document.add_paragraph(
                "Additional whistleblower content."
            )

            document.save(
                file_path
            )

            sections = parse_docx_sections(
                file_path=file_path,
                country="Australia",
            )

            self.assertEqual(
                len(sections),
                1,
            )

            self.assertEqual(
                sections[0].section,
                "07. Termination of Employment Contracts",
            )

            self.assertIn(
                (
                    "Termination content before "
                    "the malformed heading."
                ),
                sections[0].content,
            )

            self.assertIn(
                "Whistleblowers currently have legal protections.",
                sections[0].content,
            )

            self.assertIn(
                "Additional whistleblower content.",
                sections[0].content,
            )

    def test_keeps_heading_four_as_content(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory:
            file_path = (
                Path(temporary_directory)
                / "heading-four-document.docx"
            )

            document = Document()

            document.add_heading(
                "09. Transfer of Undertakings",
                level=1,
            )

            body_paragraph = document.add_paragraph(
                (
                    "The transfer does not automatically "
                    "terminate employment."
                )
            )
            body_paragraph.style = "Heading 4"

            document.save(
                file_path
            )

            sections = parse_docx_sections(
                file_path=file_path,
                country="Brazil",
            )

            self.assertEqual(
                len(sections),
                1,
            )

            self.assertEqual(
                sections[0].section,
                "09. Transfer of Undertakings",
            )

            self.assertEqual(
                sections[0].content,
                (
                    "The transfer does not automatically "
                    "terminate employment."
                ),
            )

    def test_splits_working_conditions_subsections(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory:
            file_path = (
                Path(temporary_directory)
                / "working-conditions-document.docx"
            )

            document = Document()

            document.add_heading(
                "03. Working Conditions",
                level=1,
            )

            for title, content in (
                (
                    "Overtime",
                    "Overtime legal content.",
                ),
                (
                    "Work Hours Record",
                    "Working time recording content.",
                ),
                (
                    "Paid Leave",
                    "Paid leave legal content.",
                ),
            ):
                paragraph = document.add_paragraph(
                    title
                )
                paragraph.runs[0].bold = True

                document.add_paragraph(
                    content
                )

            document.save(
                file_path
            )

            sections = parse_docx_sections(
                file_path=file_path,
                country="Spain",
            )

            self.assertEqual(
                [
                    section.subsection
                    for section in sections
                ],
                [
                    "Overtime",
                    "Work Hours Record",
                    "Paid Leave",
                ],
            )

            self.assertEqual(
                sections[0].content,
                "Overtime legal content.",
            )

    def test_notice_of_termination_and_redundancy_pay_starts_new_section(
        self,
    ) -> None:
        """
        Regression test for a real L&E document defect.

        In the Australia source document, a bold "Notice of Termination
        and Redundancy Pay" paragraph appears inside the "Working
        Conditions" section (after "Overtime"), using the same plain
        bold-"Normal"-style formatting as ordinary emphasis elsewhere in
        the document. Before the fix, this heading was not recognized
        by any taxonomy rule, so its content silently stayed folded
        into the preceding Overtime subsection under the wrong legal
        topic - making it unreachable for questions about redundancy
        pay, which are filtered by legal topic.
        """

        with TemporaryDirectory() as temporary_directory:
            file_path = (
                Path(temporary_directory)
                / "working-conditions-termination-document.docx"
            )

            document = Document()

            document.add_heading(
                "03. Working Conditions",
                level=1,
            )

            overtime_heading = document.add_paragraph(
                "Overtime"
            )
            overtime_heading.runs[0].bold = True

            document.add_paragraph(
                "Overtime is paid at a premium rate."
            )

            redundancy_heading = document.add_paragraph(
                "Notice of Termination and Redundancy Pay"
            )
            redundancy_heading.runs[0].bold = True

            document.add_paragraph(
                "An employee is entitled to redundancy pay "
                "calculated using the employee's base rate of pay."
            )

            paid_leave_heading = document.add_paragraph(
                "Paid Leave"
            )
            paid_leave_heading.runs[0].bold = True

            document.add_paragraph(
                "Employees accrue paid leave over each year "
                "of service."
            )

            document.save(
                file_path
            )

            sections = parse_docx_sections(
                file_path=file_path,
                country="Australia",
            )

            overtime_section = next(
                section
                for section in sections
                if section.subsection == "Overtime"
            )

            self.assertNotIn(
                "redundancy pay",
                overtime_section.content.casefold(),
            )

            self.assertIn(
                "premium rate",
                overtime_section.content,
            )

            redundancy_section = next(
                (
                    section
                    for section in sections
                    if "redundancy" in section.content.casefold()
                ),
                None,
            )

            self.assertIsNotNone(
                redundancy_section
            )

            self.assertEqual(
                redundancy_section.section,
                "Notice of Termination and Redundancy Pay",
            )

            self.assertIsNone(
                redundancy_section.subsection
            )

            # The section text alone is not a canonical topic heading
            # (structure cannot tell it apart from ordinary bold
            # emphasis) - the override table is what supplies the
            # correct topic, exactly as document_chunk_builder does.
            self.assertIsNone(
                get_canonical_legal_topic(
                    section=redundancy_section.section,
                    country="Australia",
                )
            )

            self.assertEqual(
                get_subsection_topic_override(
                    redundancy_section.section
                ),
                "Termination of Employment Contracts",
            )

            self.assertIn(
                "base rate of pay",
                redundancy_section.content,
            )

            self.assertLess(
                sections.index(
                    overtime_section
                ),
                sections.index(
                    redundancy_section
                ),
            )

            # Regression guard: a legitimate Working Conditions
            # subsection following the override must not be swallowed
            # into the override's topic - the parser must revert to
            # the enclosing section/topic as soon as this next real
            # subsection is recognized.
            paid_leave_section = next(
                section
                for section in sections
                if section.subsection == "Paid Leave"
            )

            self.assertNotIn(
                "redundancy",
                paid_leave_section.content.casefold(),
            )

            self.assertIn(
                "accrue paid leave",
                paid_leave_section.content,
            )

            self.assertEqual(
                get_canonical_legal_topic(
                    section=paid_leave_section.section,
                    country="Australia",
                ),
                "Working Conditions",
            )

            self.assertLess(
                sections.index(
                    redundancy_section
                ),
                sections.index(
                    paid_leave_section
                ),
            )


class CustomTopicRecognitionTests(unittest.TestCase):
    """
    ORDER 8A, section 12 - the parser must recognize a brand-new,
    non-taxonomy top-level legal topic an admin adds, using only the
    document's own real structure, generically (no topic name ever
    hardcoded) - while never promoting front matter, an ordinary
    subsection, or a stray list item that happens to share one weak
    structural signal with the document's own real topics.
    """

    def _build(
        self,
        directory: Path,
        blocks: list[tuple[str, str, int | None]],
    ) -> Path:
        """
        blocks: (text, kind, heading_level) where kind is "heading" or
        "paragraph"; heading_level is the Word heading style level (or
        None for a plain paragraph).
        """

        file_path = directory / "sample.docx"
        document = Document()

        for text, kind, heading_level in blocks:
            if kind == "heading":
                document.add_heading(text, level=heading_level)
            else:
                document.add_paragraph(text)

        document.save(file_path)
        return file_path

    def test_custom_topic_recognized_after_a_confirmed_topic(
        self,
    ) -> None:
        with TemporaryDirectory() as temp_dir:
            file_path = self._build(
                Path(temp_dir),
                [
                    (
                        "Employment Law Overview United Kingdom",
                        "heading",
                        1,
                    ),
                    ("Hiring Practices", "heading", 1),
                    ("Real hiring content.", "paragraph", None),
                    ("Remote Working", "heading", 1),
                    (
                        "Employees may work remotely. MARKER.",
                        "paragraph",
                        None,
                    ),
                ],
            )

            sections = parse_docx_sections(
                file_path,
                country="United Kingdom",
            )

            custom = [
                section
                for section in sections
                if section.is_custom_legal_topic
            ]

            self.assertEqual(len(custom), 1)
            self.assertEqual(custom[0].section, "Remote Working")
            self.assertIn("MARKER", custom[0].content)

    def test_custom_topic_not_recognized_before_any_confirmed_topic(
        self,
    ) -> None:
        # A front-matter/introductory Heading 1 before the document's
        # own overview or first real topic must never become a fake
        # legal topic.
        with TemporaryDirectory() as temp_dir:
            file_path = self._build(
                Path(temp_dir),
                [
                    ("Introduction", "heading", 1),
                    (
                        "Some introductory front matter text.",
                        "paragraph",
                        None,
                    ),
                    (
                        "Employment Law Overview United Kingdom",
                        "heading",
                        1,
                    ),
                    ("Hiring Practices", "heading", 1),
                    ("Real hiring content.", "paragraph", None),
                ],
            )

            sections = parse_docx_sections(
                file_path,
                country="United Kingdom",
            )

            self.assertFalse(
                any(
                    section.is_custom_legal_topic
                    for section in sections
                )
            )
            self.assertNotIn(
                "Introduction",
                [section.section for section in sections],
            )

    def test_custom_topic_unsupported_when_real_topics_use_bold_only(
        self,
    ) -> None:
        # Real topics recognized only via explicit bold (no Heading 1
        # style, no numbering) give no reliable, unambiguous anchor to
        # hold a candidate heading to - ordinary bold subsections are
        # too common for that signal alone to be safe. Custom topics
        # are simply unsupported for this document shape.
        with TemporaryDirectory() as temp_dir:
            file_path = Path(temp_dir) / "sample.docx"
            document = Document()

            document.add_heading(
                "Employment Law Overview United Kingdom", level=1
            )

            bold_heading = document.add_paragraph()
            bold_heading.add_run("Hiring Practices").bold = True
            document.add_paragraph("Real hiring content.")

            bold_custom = document.add_paragraph()
            bold_custom.add_run("Remote Working").bold = True
            document.add_paragraph("Remote work content.")

            document.save(file_path)

            sections = parse_docx_sections(
                file_path,
                country="United Kingdom",
            )

            self.assertFalse(
                any(
                    section.is_custom_legal_topic
                    for section in sections
                )
            )

    def test_custom_topic_requires_the_same_signal_as_confirmed_topics(
        self,
    ) -> None:
        # Real topics here all carry BOTH Heading 1 AND list numbering
        # - a heading_level-1-only candidate (no numbering) must not
        # be promoted, even though it comes after a confirmed topic.
        with TemporaryDirectory() as temp_dir:
            file_path = Path(temp_dir) / "sample.docx"
            document = Document()

            document.add_heading(
                "Employment Law Overview United Kingdom", level=1
            )

            hiring_heading = document.add_heading(
                "Hiring Practices", level=1
            )
            _mark_as_numbered(hiring_heading)
            document.add_paragraph("Real hiring content.")

            document.add_heading(
                "Key Points", level=1
            )
            document.add_paragraph(
                "This is an ordinary sub-heading, not numbered like "
                "the real topics."
            )

            document.save(file_path)

            sections = parse_docx_sections(
                file_path,
                country="United Kingdom",
            )

            self.assertFalse(
                any(
                    section.is_custom_legal_topic
                    for section in sections
                )
            )
            self.assertNotIn(
                "Key Points",
                [section.section for section in sections],
            )

    def test_custom_topic_rejects_sentence_shaped_candidates(
        self,
    ) -> None:
        # A numbered list item that happens to share the document's
        # own (Heading 1 + numbering) signal, but reads like a clause
        # in an enumerated sentence (lowercase start, trailing
        # semicolon) rather than a title, must never be promoted.
        with TemporaryDirectory() as temp_dir:
            file_path = Path(temp_dir) / "sample.docx"
            document = Document()

            document.add_heading(
                "Employment Law Overview United Kingdom", level=1
            )

            hiring_heading = document.add_heading(
                "Hiring Practices", level=1
            )
            _mark_as_numbered(hiring_heading)
            document.add_paragraph("Real hiring content.")

            list_item_heading = document.add_heading(
                "the Corporations Act;", level=1
            )
            _mark_as_numbered(list_item_heading)
            document.add_paragraph("More content.")

            document.save(file_path)

            sections = parse_docx_sections(
                file_path,
                country="United Kingdom",
            )

            self.assertFalse(
                any(
                    section.is_custom_legal_topic
                    for section in sections
                )
            )

    def test_generic_mode_never_recognizes_custom_legal_topics(
        self,
    ) -> None:
        # Custom-topic recognition is a strict L&E (country-supplied)
        # concept only - generic mode's own Heading 1 handling is
        # unrelated and must never mark anything is_custom_legal_topic.
        with TemporaryDirectory() as temp_dir:
            file_path = self._build(
                Path(temp_dir),
                [
                    ("Some Section", "heading", 1),
                    ("Some content.", "paragraph", None),
                    ("Another Section", "heading", 1),
                    ("More content.", "paragraph", None),
                ],
            )

            sections = parse_docx_sections(file_path)

            self.assertFalse(
                any(
                    section.is_custom_legal_topic
                    for section in sections
                )
            )


class AdminSectionMarkerRecognitionTests(unittest.TestCase):
    """
    ORDER 8A-C - the dedicated ADMIN-section marker style
    (ADMIN_SECTION_STYLE_NAME) is the sole, deterministic identity a
    reparse relies on for an admin-added top-level topic: it must work
    regardless of the surrounding document's own native convention
    (Heading 1, bold-only, or anything else), regardless of country,
    and regardless of document position - unlike the older, structural-
    signal-based custom-topic heuristic this supersedes for anything
    the ADMIN Add feature itself creates.
    """

    def _add_marker_heading(
        self,
        document: Document,
        title: str,
    ) -> None:
        from docx.enum.style import WD_STYLE_TYPE

        from app.services.docx_parser import ADMIN_SECTION_STYLE_NAME

        try:
            style = document.styles[ADMIN_SECTION_STYLE_NAME]
        except KeyError:
            style = document.styles.add_style(
                ADMIN_SECTION_STYLE_NAME,
                WD_STYLE_TYPE.PARAGRAPH,
            )
            style.font.bold = True

        document.add_paragraph(title, style=style.name)

    def test_marker_recognized_in_bold_only_document(self) -> None:
        with TemporaryDirectory() as temp_dir:
            file_path = Path(temp_dir) / "sample.docx"
            document = Document()

            document.add_paragraph(
                "Employment Law Overview United Kingdom"
            )

            hiring = document.add_paragraph()
            hiring.add_run("Hiring Practices").bold = True
            document.add_paragraph("Real hiring content.")

            self._add_marker_heading(document, "Remote Working")
            document.add_paragraph("Remote work content. MARKER.")

            document.save(file_path)

            sections = parse_docx_sections(
                file_path, country="United Kingdom"
            )

            custom = [s for s in sections if s.is_custom_legal_topic]
            self.assertEqual(len(custom), 1)
            self.assertEqual(custom[0].section, "Remote Working")
            self.assertIn("MARKER", custom[0].content)

            # native topic untouched
            hiring_sections = [
                s for s in sections if s.section == "Hiring Practices"
            ]
            self.assertEqual(len(hiring_sections), 1)
            self.assertEqual(
                hiring_sections[0].content, "Real hiring content."
            )

    def test_marker_recognized_with_no_native_topics_at_all(
        self,
    ) -> None:
        # The marker never depends on any structural-signal-learning
        # from the surrounding document - it must fire even when there
        # is nothing else to learn a signal from.
        with TemporaryDirectory() as temp_dir:
            file_path = Path(temp_dir) / "sample.docx"
            document = Document()

            document.add_paragraph("Just some plain front matter.")
            self._add_marker_heading(document, "Custom Only Section")
            document.add_paragraph("Custom-only content.")

            document.save(file_path)

            sections = parse_docx_sections(
                file_path, country="United Kingdom"
            )

            custom = [s for s in sections if s.is_custom_legal_topic]
            self.assertEqual(len(custom), 1)
            self.assertEqual(custom[0].section, "Custom Only Section")

    def test_marker_style_alone_never_appears_on_native_content(
        self,
    ) -> None:
        # Sanity check on the corpus-safety assumption the marker
        # design depends on: an ordinary document (no marker style
        # ever applied) never has any paragraph whose is_custom_legal_
        # topic could be confused with the marker mechanism.
        with TemporaryDirectory() as temp_dir:
            file_path = Path(temp_dir) / "sample.docx"
            document = Document()

            document.add_heading(
                "Employment Law Overview United Kingdom", level=1
            )
            document.add_heading("Hiring Practices", level=1)
            document.add_paragraph("Real hiring content.")

            document.save(file_path)

            sections = parse_docx_sections(
                file_path, country="United Kingdom"
            )

            self.assertFalse(
                any(s.is_custom_legal_topic for s in sections)
            )


class ContactBlockParsingTests(unittest.TestCase):
    """
    Tests for parse_contact_blocks() against a synthetic paragraph
    structure - the shape _extract_text_box_blocks() would have
    produced from a real DOCX, without needing one.
    """

    def test_firm_then_contact_person_are_paired(
        self,
    ) -> None:
        blocks = [
            [
                "Example & Partners Advogados",
                "Freedonia",
                "1 Example Street, 6th floor, 00000 Sample City",
                "+00 000 000 00",
                "www.example-partners.test",
            ],
            [
                "CONTACT PERSON",
                "Alex Example",
                "alex@example-partners.test",
            ],
        ]

        contacts = parse_contact_blocks(
            blocks,
            country="Freedonia",
        )

        self.assertEqual(
            len(contacts),
            1,
        )

        contact = contacts[0]

        self.assertEqual(
            contact.member_firm,
            "Example & Partners Advogados",
        )
        self.assertEqual(
            contact.contact_person,
            "Alex Example",
        )
        self.assertEqual(
            contact.email,
            "alex@example-partners.test",
        )
        self.assertEqual(
            contact.phone,
            "+00 000 000 00",
        )
        self.assertEqual(
            contact.website,
            "www.example-partners.test",
        )

        # The country line is excluded from the address since it must
        # come from validated document metadata instead.
        self.assertNotIn(
            "Freedonia",
            contact.address or "",
        )

    def test_contact_person_before_firm_block_are_still_paired(
        self,
    ) -> None:
        blocks = [
            [
                "CONTACT PERSON",
                "Nicolás Grandi",
                "ngrandi@allende.com",
            ],
            [
                "Allende & Brea",
                "Argentina",
                "Torre IRSA, Maipú 1300",
                "+54 114 318 9984",
                "www.allendebrea.com",
            ],
        ]

        contacts = parse_contact_blocks(
            blocks,
            country="Argentina",
        )

        self.assertEqual(
            len(contacts),
            1,
        )

        contact = contacts[0]

        self.assertEqual(
            contact.member_firm,
            "Allende & Brea",
        )
        self.assertEqual(
            contact.contact_person,
            "Nicolás Grandi",
        )
        self.assertEqual(
            contact.email,
            "ngrandi@allende.com",
        )

    def test_bare_website_alone_is_not_a_firm_block(
        self,
    ) -> None:
        blocks = [
            [
                "www.leglobal.law",
            ],
            [
                "Some Firm",
                "Country",
                "123 Main Street",
                "+1 555 000 0000",
                "www.somefirm.example",
            ],
            [
                "CONTACT PERSON",
                "Jane Doe",
                "jane@somefirm.example",
            ],
        ]

        contacts = parse_contact_blocks(
            blocks
        )

        self.assertEqual(
            len(contacts),
            1,
        )

        self.assertEqual(
            contacts[0].member_firm,
            "Some Firm",
        )

    def test_plural_contact_persons_marker_with_multiple_emails(
        self,
    ) -> None:
        blocks = [
            [
                "Van Olmen & Wynant",
                "Belgium",
                "Avenue Louise 221, 1050 Brussels",
                "+32 264 405 11",
                "www.vow.be",
            ],
            [
                "CONTACT PERSONS",
                "Chris van Olmen and Nicolas Simon",
                "chris.van.olmen@vow.be",
                "nicolas.simon@vow.be",
            ],
        ]

        contacts = parse_contact_blocks(
            blocks,
            country="Belgium",
        )

        self.assertEqual(
            len(contacts),
            1,
        )

        contact = contacts[0]

        self.assertEqual(
            contact.contact_person,
            "Chris van Olmen and Nicolas Simon",
        )

        self.assertIn(
            "chris.van.olmen@vow.be",
            contact.email or "",
        )
        self.assertIn(
            "nicolas.simon@vow.be",
            contact.email or "",
        )

    def test_multiple_documents_worth_of_contacts_are_all_kept(
        self,
    ) -> None:
        blocks = [
            [
                "Firm One",
                "123 Street",
                "+1 555 111 1111",
            ],
            [
                "CONTACT PERSON",
                "Person One",
                "one@example.com",
            ],
            [
                "Firm Two",
                "456 Avenue",
                "+1 555 222 2222",
            ],
            [
                "CONTACT PERSON",
                "Person Two",
                "two@example.com",
            ],
        ]

        contacts = parse_contact_blocks(
            blocks
        )

        self.assertEqual(
            len(contacts),
            2,
        )

        self.assertEqual(
            [
                contact.email
                for contact in contacts
            ],
            [
                "one@example.com",
                "two@example.com",
            ],
        )

    def test_no_text_box_blocks_returns_no_contacts(
        self,
    ) -> None:
        self.assertEqual(
            parse_contact_blocks(
                []
            ),
            [],
        )

    def test_unmatched_firm_block_reports_only_its_own_fields(
        self,
    ) -> None:
        blocks = [
            [
                "Only Firm",
                "42 Road",
                "+1 555 333 3333",
            ],
        ]

        contacts = parse_contact_blocks(
            blocks
        )

        self.assertEqual(
            len(contacts),
            1,
        )

        self.assertEqual(
            contacts[0].member_firm,
            "Only Firm",
        )
        self.assertIsNone(
            contacts[0].contact_person
        )
        self.assertIsNone(
            contacts[0].email
        )

    def test_build_contact_chunk_content_omits_missing_fields(
        self,
    ) -> None:
        blocks = [
            [
                "CONTACT PERSON",
                "Jane Doe",
                "jane@example.com",
            ],
        ]

        contacts = parse_contact_blocks(
            blocks
        )

        content = build_contact_chunk_content(
            contacts
        )

        self.assertIn(
            "Contact person: Jane Doe",
            content,
        )
        self.assertIn(
            "Email: jane@example.com",
            content,
        )
        self.assertNotIn(
            "Member firm",
            content,
        )
        self.assertNotIn(
            "Phone",
            content,
        )
        self.assertNotIn(
            "Address",
            content,
        )
        self.assertNotIn(
            "Website",
            content,
        )


if __name__ == "__main__":
    unittest.main()