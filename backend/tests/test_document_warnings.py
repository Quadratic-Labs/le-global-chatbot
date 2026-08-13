"""Mission "ORDER 3", sections 10/11 - topic-coverage warning thresholds."""

from __future__ import annotations

import unittest

from app.core.legal_taxonomy import LEGAL_TOPICS
from app.models.document import DocumentChunk
from app.services.document_warnings import (
    CONTEXT_WARNING_CODE,
    EXPECTED_TOPICS_COUNT,
    STRUCTURE_WARNING_CODE,
    evaluate_topic_coverage,
    recognized_topics_for,
)


def _chunk(
    *,
    legal_topic: str | None,
    document_type: str = "comparator",
) -> DocumentChunk:
    return DocumentChunk(
        document_id="doc_" + "a" * 64,
        chunk_id="chunk_" + "a" * 64,
        country="Testland",
        country_code="ZZ",
        legal_topic=legal_topic,
        document_type=document_type,
        language="en",
        section=legal_topic or "General",
        subsection=None,
        content="Some content.",
        source_filename="test.docx",
        source_format="docx",
        content_hash="hash",
    )


def _chunks_with_topics(count: int) -> list[DocumentChunk]:
    overview = _chunk(legal_topic=None, document_type="overview")

    topic_chunks = [
        _chunk(legal_topic=topic)
        for topic in LEGAL_TOPICS[:count]
    ]

    return [overview, *topic_chunks]


class TopicCoverageThresholdTests(unittest.TestCase):
    def test_expected_topics_count_is_eleven(self) -> None:
        self.assertEqual(EXPECTED_TOPICS_COUNT, 11)
        self.assertEqual(len(LEGAL_TOPICS), 11)

    def test_zero_recognized_topics_is_context_warning(self) -> None:
        chunks = _chunks_with_topics(0)

        warning = evaluate_topic_coverage(chunks)

        self.assertIsNotNone(warning)
        self.assertEqual(warning.code, CONTEXT_WARNING_CODE)
        self.assertEqual(warning.recognized_topics_count, 0)
        self.assertEqual(warning.expected_topics_count, 11)
        self.assertEqual(len(warning.missing_topics), 11)

    def test_one_to_five_recognized_topics_is_structure_warning(
        self,
    ) -> None:
        for count in (1, 2, 3, 4, 5):
            with self.subTest(count=count):
                chunks = _chunks_with_topics(count)

                warning = evaluate_topic_coverage(chunks)

                self.assertIsNotNone(warning)
                self.assertEqual(warning.code, STRUCTURE_WARNING_CODE)
                self.assertEqual(
                    warning.recognized_topics_count, count
                )
                self.assertEqual(
                    len(warning.missing_topics), 11 - count
                )

    def test_six_or_more_recognized_topics_is_no_warning(self) -> None:
        # "6 correspond à la majorité stricte de 11" - the exact
        # boundary, and every value above it, must both be None.
        for count in (6, 7, 8, 9, 10, 11):
            with self.subTest(count=count):
                chunks = _chunks_with_topics(count)

                self.assertIsNone(
                    evaluate_topic_coverage(chunks)
                )

    def test_overview_chunks_never_count_as_recognized_topics(
        self,
    ) -> None:
        # Only comparator chunks with a real legal_topic ever count -
        # an overview-heavy document with zero comparator content
        # must be a CONTEXT_WARNING, not silently exempted.
        chunks = [
            _chunk(legal_topic=None, document_type="overview")
            for _ in range(20)
        ]

        warning = evaluate_topic_coverage(chunks)

        self.assertIsNotNone(warning)
        self.assertEqual(warning.code, CONTEXT_WARNING_CODE)

    def test_duplicate_topic_chunks_count_once(self) -> None:
        # Multiple chunks under the same topic (subsections) must not
        # inflate recognized_topics_count past the true distinct count.
        chunks = [
            _chunk(legal_topic="Hiring Practices")
            for _ in range(5)
        ]

        recognized = recognized_topics_for(chunks)

        self.assertEqual(recognized, ("Hiring Practices",))

        warning = evaluate_topic_coverage(chunks)

        self.assertIsNotNone(warning)
        self.assertEqual(warning.recognized_topics_count, 1)

    def test_missing_topics_are_exactly_the_complement(self) -> None:
        chunks = _chunks_with_topics(3)

        warning = evaluate_topic_coverage(chunks)

        self.assertIsNotNone(warning)
        self.assertEqual(
            set(warning.recognized_topics) | set(warning.missing_topics),
            set(LEGAL_TOPICS),
        )
        self.assertEqual(
            set(warning.recognized_topics)
            & set(warning.missing_topics),
            set(),
        )


if __name__ == "__main__":
    unittest.main()
