import unittest
from pathlib import Path

from app.core.country_registry import (
    CountryMetadataMismatchError,
)
from app.core.legal_taxonomy import (
    get_canonical_legal_topic,
    normalize_topic,
)
from app.services.document_chunk_builder import (
    DocumentMetadata,
    InvalidSourceFilenameError,
    UnknownLegalTopicError,
    build_document_chunks,
    metadata_from_filename,
)
from app.services.docx_parser import ParsedSection


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

    def test_extracts_metadata_from_filename(
        self,
    ) -> None:
        metadata = metadata_from_filename(
            file_path=Path(
                "Labour and Employment Law "
                "in Spain 2026.docx"
            ),
            country_code="es",
        )

        self.assertEqual(
            metadata.country,
            "Spain",
        )

        self.assertEqual(
            metadata.country_code,
            "ES",
        )

        self.assertEqual(
            metadata.reference_year,
            2026,
        )

        self.assertEqual(
            metadata.language,
            "en",
        )

    def test_rejects_invalid_filename(self) -> None:
        with self.assertRaises(
            InvalidSourceFilenameError
        ):
            metadata_from_filename(
                file_path=Path(
                    "Spain employment law.docx"
                ),
                country_code="ES",
            )

    def test_uk_filename_uses_canonical_country_name(
        self,
    ) -> None:
        metadata = metadata_from_filename(
            file_path=Path(
                "Labour and Employment Law "
                "in UK 2026.docx"
            ),
            country_code="GB",
        )

        self.assertEqual(
            metadata.country,
            "United Kingdom",
        )

        self.assertEqual(
            metadata.country_code,
            "GB",
        )

        self.assertEqual(
            metadata.reference_year,
            2026,
        )

    def test_infers_country_code_from_filename(
        self,
    ) -> None:
        metadata = metadata_from_filename(
            file_path=Path(
                "Labour_and_Employment_Law_"
                "in_Sweden_2026.docx"
            )
        )

        self.assertEqual(
            metadata.country,
            "Sweden",
        )

        self.assertEqual(
            metadata.country_code,
            "SE",
        )

        self.assertEqual(
            metadata.reference_year,
            2026,
        )

    def test_accepts_overview_filename_without_year(
        self,
    ) -> None:
        metadata = metadata_from_filename(
            file_path=Path(
                "Employment Law Overview Australia.docx"
            )
        )

        self.assertEqual(
            metadata.country,
            "Australia",
        )

        self.assertEqual(
            metadata.country_code,
            "AU",
        )

        self.assertIsNone(
            metadata.reference_year
        )

    def test_accepts_copy_suffix(self) -> None:
        metadata = metadata_from_filename(
            file_path=Path(
                "Labour and Employment Law "
                "in Spain 2026(1).docx"
            )
        )

        self.assertEqual(
            metadata.country,
            "Spain",
        )

        self.assertEqual(
            metadata.country_code,
            "ES",
        )

        self.assertEqual(
            metadata.reference_year,
            2026,
        )

    def test_rejects_country_code_mismatch(
        self,
    ) -> None:
        with self.assertRaises(
            CountryMetadataMismatchError
        ):
            metadata_from_filename(
                file_path=Path(
                    "Labour and Employment Law "
                    "in Spain 2026.docx"
                ),
                country_code="GB",
            )


if __name__ == "__main__":
    unittest.main()