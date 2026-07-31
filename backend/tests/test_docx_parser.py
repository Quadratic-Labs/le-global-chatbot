import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.text.paragraph import Paragraph

from app.core.legal_taxonomy import get_canonical_legal_topic
from app.core.subsection_taxonomy import get_subsection_topic_override
from app.services.docx_parser import parse_docx_sections


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


if __name__ == "__main__":
    unittest.main()