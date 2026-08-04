import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory

from docx import Document

from app.core.country_registry import (
    CountryMetadataMismatchError,
)
from app.core.legal_taxonomy import (
    get_canonical_legal_topic,
    normalize_topic,
)
from app.services.document_chunk_builder import (
    AmbiguousDocumentCountryError,
    DocumentMetadata,
    InvalidDocxFormatError,
    UndeterminableDocumentCountryError,
    UnknownLegalTopicError,
    build_document_chunks,
    metadata_from_content,
    validate_docx_format,
)
from app.services.docx_parser import ParsedSection


def _build_docx(
    directory: Path,
    title_lines: list[str],
    body_paragraphs: list[str] | None = None,
    filename: str = "document.docx",
) -> Path:
    """
    A minimal real DOCX whose leading paragraphs are exactly
    `title_lines` - the title/cover area metadata_from_content scans
    - followed by any extra body content, saved under an arbitrary
    filename (never itself a source of metadata).
    """

    document = Document()

    for line in title_lines:
        document.add_paragraph(line)

    for paragraph in body_paragraphs or []:
        document.add_paragraph(paragraph)

    file_path = directory / filename
    document.save(file_path)

    return file_path


class LegalTaxonomyTests(unittest.TestCase):
    def test_normalizes_numbered_topic(self) -> None:
        self.assertEqual(
            normalize_topic(
                section="01. Hiring Practices",
                country="Spain",
            ),
            "Hiring Practices",
        )

    def test_removes_country_suffix(self) -> None:
        self.assertEqual(
            normalize_topic(
                section=(
                    "06. Social Media "
                    "and Data Privacy in Spain"
                ),
                country="Spain",
            ),
            "Social Media and Data Privacy",
        )

    def test_returns_canonical_topic(self) -> None:
        self.assertEqual(
            get_canonical_legal_topic(
                section=(
                    "07. Termination "
                    "of Employment Contracts"
                ),
                country="Spain",
            ),
            "Termination of Employment Contracts",
        )


class DocumentChunkBuilderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.metadata = DocumentMetadata(
            country="Spain",
            country_code="ES",
            reference_year=2026,
            language="en",
            source_filename=(
                "Labour and Employment Law "
                "in Spain 2026.docx"
            ),
        )

    def test_builds_overview_and_comparator_chunks(
        self,
    ) -> None:
        parsed_sections = [
            ParsedSection(
                section="Employment Law Overview Spain",
                subsection="Introduction",
                content="Overview content.",
            ),
            ParsedSection(
                section="01. Hiring Practices",
                subsection=(
                    "Requirement for Foreign "
                    "Employees to Work"
                ),
                content="Hiring content.",
            ),
        ]

        chunks = build_document_chunks(
            parsed_sections=parsed_sections,
            metadata=self.metadata,
        )

        self.assertEqual(
            len(chunks),
            2,
        )

        overview_chunk = chunks[0]

        self.assertEqual(
            overview_chunk.document_type,
            "overview",
        )

        self.assertIsNone(
            overview_chunk.legal_topic
        )

        comparator_chunk = chunks[1]

        self.assertEqual(
            comparator_chunk.document_type,
            "comparator",
        )

        self.assertEqual(
            comparator_chunk.legal_topic,
            "Hiring Practices",
        )

        self.assertEqual(
            comparator_chunk.country,
            "Spain",
        )

        self.assertEqual(
            comparator_chunk.country_code,
            "ES",
        )

        self.assertEqual(
            comparator_chunk.reference_year,
            2026,
        )

    def test_ids_are_deterministic(self) -> None:
        original_sections = [
            ParsedSection(
                section="02. Employment Contracts",
                subsection="Notice Period",
                content="Original legal content.",
            )
        ]

        updated_sections = [
            ParsedSection(
                section="02. Employment Contracts",
                subsection="Notice Period",
                content="Updated legal content.",
            )
        ]

        original_chunks = build_document_chunks(
            parsed_sections=original_sections,
            metadata=self.metadata,
        )

        repeated_chunks = build_document_chunks(
            parsed_sections=original_sections,
            metadata=self.metadata,
        )

        updated_chunks = build_document_chunks(
            parsed_sections=updated_sections,
            metadata=self.metadata,
        )

        self.assertEqual(
            original_chunks[0].document_id,
            repeated_chunks[0].document_id,
        )

        self.assertEqual(
            original_chunks[0].chunk_id,
            repeated_chunks[0].chunk_id,
        )

        self.assertEqual(
            original_chunks[0].chunk_id,
            updated_chunks[0].chunk_id,
        )

        self.assertNotEqual(
            original_chunks[0].content_hash,
            updated_chunks[0].content_hash,
        )

    def test_rejects_unknown_legal_topic(self) -> None:
        parsed_sections = [
            ParsedSection(
                section="12. Imaginary Legal Topic",
                subsection="Unknown subsection",
                content="Content that must not be indexed.",
            )
        ]

        with self.assertRaises(
            UnknownLegalTopicError
        ):
            build_document_chunks(
                parsed_sections=parsed_sections,
                metadata=self.metadata,
            )

    def test_extracts_metadata_from_content(
        self,
    ) -> None:
        # Mission "CONTINUATION PATCH 0.4.3": metadata now comes from
        # the document's own title/cover content, never its filename
        # - the arbitrary filename here proves that directly.
        with TemporaryDirectory() as directory:
            file_path = _build_docx(
                Path(directory),
                ["Labour and Employment Law in Spain 2026"],
                filename="final.docx",
            )

            metadata = metadata_from_content(
                file_path=file_path,
                country_code="es",
            )

        self.assertEqual(metadata.country, "Spain")
        self.assertEqual(metadata.country_code, "ES")
        self.assertEqual(metadata.reference_year, 2026)
        self.assertEqual(metadata.language, "en")
        self.assertEqual(metadata.source_filename, "final.docx")

    def test_rejects_content_with_no_identifiable_country(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            file_path = _build_docx(
                Path(directory),
                ["Some random legal memo with no title structure."],
            )

            with self.assertRaises(
                UndeterminableDocumentCountryError
            ):
                metadata_from_content(file_path=file_path)

    def test_uk_content_uses_canonical_country_name(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            file_path = _build_docx(
                Path(directory),
                ["Labour and Employment Law in UK 2026"],
            )

            metadata = metadata_from_content(
                file_path=file_path,
                country_code="GB",
            )

        self.assertEqual(metadata.country, "United Kingdom")
        self.assertEqual(metadata.country_code, "GB")
        self.assertEqual(metadata.reference_year, 2026)

    def test_infers_country_code_from_content(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            file_path = _build_docx(
                Path(directory),
                ["Labour and Employment Law in Sweden 2026"],
            )

            metadata = metadata_from_content(file_path=file_path)

        self.assertEqual(metadata.country, "Sweden")
        self.assertEqual(metadata.country_code, "SE")
        self.assertEqual(metadata.reference_year, 2026)

    def test_accepts_overview_title_without_year(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            file_path = _build_docx(
                Path(directory),
                ["Employment Law Overview Australia"],
            )

            metadata = metadata_from_content(file_path=file_path)

        self.assertEqual(metadata.country, "Australia")
        self.assertEqual(metadata.country_code, "AU")
        self.assertIsNone(metadata.reference_year)

    def test_an_arbitrary_filename_never_affects_detection(
        self,
    ) -> None:
        # The filename names a different country than the content -
        # the content must always win, and the filename is preserved
        # only as display metadata (mission section 5).
        with TemporaryDirectory() as directory:
            file_path = _build_docx(
                Path(directory),
                ["Employment Law Overview Canada 2026"],
                filename="Spain-template-used-for-Canada.docx",
            )

            metadata = metadata_from_content(file_path=file_path)

        self.assertEqual(metadata.country, "Canada")
        self.assertEqual(metadata.country_code, "CA")
        self.assertEqual(
            metadata.source_filename,
            "Spain-template-used-for-Canada.docx",
        )

    def test_rejects_content_country_code_mismatch(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            file_path = _build_docx(
                Path(directory),
                ["Labour and Employment Law in Spain 2026"],
            )

            with self.assertRaises(
                CountryMetadataMismatchError
            ):
                metadata_from_content(
                    file_path=file_path,
                    country_code="GB",
                )


class ContentMetadataFixtureTests(unittest.TestCase):
    """
    Mission "CONTINUATION PATCH 0.4.3", section 15 - the 8 mandatory
    filename/content/expected-outcome fixture cases A-H.
    """

    def test_case_a_plain_filename_with_title_country_and_year(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            file_path = _build_docx(
                Path(directory),
                ["Employment Law Overview Canada 2026"],
                filename="final.docx",
            )

            metadata = metadata_from_content(file_path=file_path)

        self.assertEqual(metadata.country, "Canada")
        self.assertEqual(metadata.country_code, "CA")
        self.assertEqual(metadata.reference_year, 2026)

    def test_case_b_filename_names_a_different_country_than_content(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            file_path = _build_docx(
                Path(directory),
                ["Labour and Employment Law in Canada 2025"],
                filename="Spain-final.docx",
            )

            metadata = metadata_from_content(file_path=file_path)

        self.assertEqual(metadata.country, "Canada")
        self.assertEqual(metadata.country_code, "CA")
        self.assertEqual(metadata.reference_year, 2025)
        # No conflict is ever raised - the filename is never even
        # consulted, so "Spain" in it has no bearing on the result.
        self.assertEqual(metadata.source_filename, "Spain-final.docx")

    def test_case_c_edited_replacement_filename_with_matching_content(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            file_path = _build_docx(
                Path(directory),
                ["Employment Law Overview Canada 2026"],
                filename=(
                    "Canada_2026-04-15-Employment-Law-"
                    "Overview-EDITED.docx"
                ),
            )

            metadata = metadata_from_content(file_path=file_path)

        self.assertEqual(metadata.country, "Canada")
        self.assertEqual(metadata.country_code, "CA")
        self.assertEqual(metadata.reference_year, 2026)

    def test_case_d_generic_filename_with_no_year_in_content(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            file_path = _build_docx(
                Path(directory),
                ["Employment Law Overview Spain"],
                filename="document_received_from_client.docx",
            )

            metadata = metadata_from_content(file_path=file_path)

        self.assertEqual(metadata.country, "Spain")
        self.assertEqual(metadata.country_code, "ES")
        self.assertIsNone(metadata.reference_year)

    def test_case_e_filename_names_a_country_but_content_names_none(
        self,
    ) -> None:
        # The filename "Peru-2026.docx" must never be used as a
        # fallback - content with no identifiable country is refused
        # outright, never silently resolved to "Peru".
        with TemporaryDirectory() as directory:
            file_path = _build_docx(
                Path(directory),
                ["Some random legal memo with no title structure."],
                filename="Peru-2026.docx",
            )

            with self.assertRaises(
                UndeterminableDocumentCountryError
            ):
                metadata_from_content(file_path=file_path)

    def test_case_f_ambiguous_cover_naming_two_countries_is_refused(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            file_path = _build_docx(
                Path(directory),
                [
                    "Employment Law Overview Spain 2026",
                    "Employment Law Overview Canada 2026",
                ],
            )

            with self.assertRaises(
                AmbiguousDocumentCountryError
            ):
                metadata_from_content(file_path=file_path)

    def test_case_g_body_mentions_of_other_countries_are_ignored(
        self,
    ) -> None:
        # Countries named only in ordinary body prose (never shaped
        # as a title line) must never be treated as candidates - only
        # the one clear cover country is selected.
        with TemporaryDirectory() as directory:
            file_path = _build_docx(
                Path(directory),
                ["Employment Law Overview Canada 2026"],
                body_paragraphs=[
                    "This overview also references comparable rules "
                    "in France, Germany, and Peru for context.",
                    "See also the equivalent Spain and Australia "
                    "frameworks discussed elsewhere.",
                ],
            )

            metadata = metadata_from_content(file_path=file_path)

        self.assertEqual(metadata.country, "Canada")
        self.assertEqual(metadata.country_code, "CA")

    def test_case_h_content_year_wins_over_filename_year(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            file_path = _build_docx(
                Path(directory),
                ["Employment Law Overview Canada 2025"],
                filename="Canada-2026-report.docx",
            )

            metadata = metadata_from_content(file_path=file_path)

        self.assertEqual(metadata.country_code, "CA")
        self.assertEqual(metadata.reference_year, 2025)


def _valid_docx_entries(directory: Path) -> dict[str, bytes]:
    """The raw ZIP entries of one real, minimal, valid DOCX."""

    valid_path = directory / "valid-reference.docx"
    document = Document()
    document.add_paragraph("Employment Law Overview Canada 2026")
    document.save(valid_path)

    with zipfile.ZipFile(valid_path) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


class InvalidDocxFormatTests(unittest.TestCase):
    """
    Mission "CONTINUATION PATCH 0.4.3", section 16 - the .docx
    extension alone proves nothing: each of these must be refused by
    validate_docx_format before any content parsing is attempted.
    """

    def test_renamed_text_file_is_rejected(self) -> None:
        with TemporaryDirectory() as directory:
            file_path = Path(directory) / "renamed.docx"
            file_path.write_bytes(
                b"This is just plain text, not a docx at all."
            )

            with self.assertRaises(InvalidDocxFormatError):
                validate_docx_format(file_path)

    def test_invalid_zip_is_rejected(self) -> None:
        with TemporaryDirectory() as directory:
            file_path = Path(directory) / "invalid-zip.docx"
            # Starts with the ZIP local-file-header magic bytes but has
            # no valid central directory - not a real archive.
            file_path.write_bytes(b"PK\x03\x04" + b"\x00" * 50)

            with self.assertRaises(InvalidDocxFormatError):
                validate_docx_format(file_path)

    def test_empty_file_is_rejected(self) -> None:
        with TemporaryDirectory() as directory:
            file_path = Path(directory) / "empty.docx"
            file_path.write_bytes(b"")

            with self.assertRaises(InvalidDocxFormatError):
                validate_docx_format(file_path)

    def test_zip_missing_content_types_is_rejected(self) -> None:
        with TemporaryDirectory() as directory:
            entries = _valid_docx_entries(Path(directory))
            file_path = Path(directory) / "missing-content-types.docx"

            with zipfile.ZipFile(file_path, "w") as archive:
                for name, data in entries.items():
                    if name == "[Content_Types].xml":
                        continue
                    archive.writestr(name, data)

            with self.assertRaises(InvalidDocxFormatError):
                validate_docx_format(file_path)

    def test_zip_missing_document_xml_is_rejected(self) -> None:
        with TemporaryDirectory() as directory:
            entries = _valid_docx_entries(Path(directory))
            file_path = Path(directory) / "missing-document-xml.docx"

            with zipfile.ZipFile(file_path, "w") as archive:
                for name, data in entries.items():
                    if name == "word/document.xml":
                        continue
                    archive.writestr(name, data)

            with self.assertRaises(InvalidDocxFormatError):
                validate_docx_format(file_path)

    def test_corrupted_document_xml_is_rejected(self) -> None:
        # Both required entries are present by name - only their
        # content is broken, so this exercises python-docx's own
        # Document() openability check specifically.
        with TemporaryDirectory() as directory:
            entries = dict(_valid_docx_entries(Path(directory)))
            entries["word/document.xml"] = b"<this is not valid xml"
            file_path = Path(directory) / "corrupted.docx"

            with zipfile.ZipFile(file_path, "w") as archive:
                for name, data in entries.items():
                    archive.writestr(name, data)

            with self.assertRaises(InvalidDocxFormatError):
                validate_docx_format(file_path)


if __name__ == "__main__":
    unittest.main()