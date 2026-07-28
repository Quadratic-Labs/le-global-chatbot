import unittest

from app.services.docx_parser import ParsedSection
from app.services.section_splitter import (
    SectionSplitterError,
    split_parsed_sections,
    split_text,
)


class SectionSplitterTests(
    unittest.TestCase
):
    def test_keeps_short_text_unchanged(
        self,
    ) -> None:
        text = "A short legal paragraph."

        self.assertEqual(
            split_text(
                text=text,
                max_chars=100,
            ),
            [
                text
            ],
        )

    def test_prefers_paragraph_boundaries(
        self,
    ) -> None:
        first_paragraph = "A" * 60
        second_paragraph = "B" * 60

        chunks = split_text(
            text=(
                f"{first_paragraph}\n\n"
                f"{second_paragraph}"
            ),
            max_chars=100,
        )

        self.assertEqual(
            chunks,
            [
                first_paragraph,
                second_paragraph,
            ],
        )

    def test_splits_oversized_paragraph_by_sentences(
        self,
    ) -> None:
        text = (
            "First sentence contains legal information. "
            "Second sentence contains additional information. "
            "Third sentence completes the legal explanation."
        )

        chunks = split_text(
            text=text,
            max_chars=100,
        )

        self.assertGreater(
            len(chunks),
            1,
        )

        self.assertTrue(
            all(
                len(chunk) <= 100
                for chunk in chunks
            )
        )

        self.assertIn(
            "First sentence",
            chunks[0],
        )

    def test_uses_whitespace_fallback(
        self,
    ) -> None:
        text = " ".join(
            f"word{index}"
            for index in range(100)
        )

        chunks = split_text(
            text=text,
            max_chars=100,
        )

        self.assertGreater(
            len(chunks),
            1,
        )

        self.assertTrue(
            all(
                len(chunk) <= 100
                for chunk in chunks
            )
        )

    def test_preserves_section_metadata(
        self,
    ) -> None:
        parsed_section = ParsedSection(
            section=(
                "07. Termination of "
                "Employment Contracts"
            ),
            subsection="Grounds for Termination",
            content=" ".join(
                "termination"
                for _ in range(100)
            ),
        )

        split_sections = split_parsed_sections(
            parsed_sections=[
                parsed_section
            ],
            max_chars=100,
        )

        self.assertGreater(
            len(split_sections),
            1,
        )

        for split_section in split_sections:
            self.assertEqual(
                split_section.section,
                parsed_section.section,
            )

            self.assertEqual(
                split_section.subsection,
                parsed_section.subsection,
            )

            self.assertLessEqual(
                len(split_section.content),
                100,
            )

    def test_is_deterministic(
        self,
    ) -> None:
        text = "\n\n".join(
            (
                "A" * 75,
                "B" * 75,
                "C" * 75,
            )
        )

        first_result = split_text(
            text=text,
            max_chars=100,
        )

        second_result = split_text(
            text=text,
            max_chars=100,
        )

        self.assertEqual(
            first_result,
            second_result,
        )

    def test_rejects_invalid_limit(
        self,
    ) -> None:
        with self.assertRaises(
            SectionSplitterError
        ):
            split_text(
                text="Legal content.",
                max_chars=50,
            )


if __name__ == "__main__":
    unittest.main()