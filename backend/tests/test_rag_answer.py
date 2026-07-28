"""Tests for grounded legal answer generation."""

from __future__ import annotations

import unittest

from app.clients.openai_responses import (
    GeneratedText,
    _extract_output_text,
)
from app.models.chat import (
    LegalChatRequest,
)
from app.models.search import (
    LegalSearchHit,
    LegalSearchResponse,
)
from app.services.rag_answer import (
    NO_INFORMATION_ANSWER,
    answer_legal_question,
)


def _build_hit() -> LegalSearchHit:
    return LegalSearchHit(
        score=12.5,
        document_id="document-1",
        chunk_id="chunk-1",
        country="United Kingdom",
        country_code="GB",
        legal_topic="Employment Contracts",
        document_type="comparator",
        language="en",
        section="02. Employment Contracts",
        subsection="Notice Period",
        content=(
            "Employees with between one month and two years "
            "of service are entitled to one week's notice."
        ),
        source_filename=(
            "Labour and Employment Law in UK 2026.docx"
        ),
        source_format="docx",
        reference_year=2026,
    )


class FakeGenerationClient:
    model = "test-model"

    def __init__(self) -> None:
        self.instructions: str | None = None
        self.input_text: str | None = None
        self.called = False

    def generate(
        self,
        instructions: str,
        input_text: str,
    ) -> GeneratedText:
        self.called = True
        self.instructions = instructions
        self.input_text = input_text

        return GeneratedText(
            text=(
                "The minimum notice is one week "
                "in the stated circumstances [1]."
            ),
            model=self.model,
        )


class RagAnswerTests(unittest.TestCase):
    def test_grounded_answer_uses_retrieved_source(
        self,
    ) -> None:
        client = FakeGenerationClient()

        def fake_search(
            request,
        ) -> LegalSearchResponse:
            return LegalSearchResponse(
                query=request.query,
                total=1,
                limit=request.limit,
                offset=0,
                took_ms=3,
                hits=[
                    _build_hit()
                ],
            )

        response = answer_legal_question(
            request=LegalChatRequest(
                question=(
                    "What is the notice period in the UK?"
                ),
                country_codes=["GB"],
            ),
            search_function=fake_search,
            generation_client=client,
        )

        self.assertTrue(
            response.grounded
        )

        self.assertTrue(
            client.called
        )

        self.assertEqual(
            response.model,
            "test-model",
        )

        self.assertEqual(
            len(response.sources),
            1,
        )

        self.assertEqual(
            response.sources[0].citation,
            1,
        )

        self.assertIn(
            "[SOURCE 1]",
            client.input_text or "",
        )

        self.assertIn(
            "one week's notice",
            client.input_text or "",
        )

    def test_empty_retrieval_returns_fallback(
        self,
    ) -> None:
        client = FakeGenerationClient()

        def fake_search(
            request,
        ) -> LegalSearchResponse:
            return LegalSearchResponse(
                query=request.query,
                total=0,
                limit=request.limit,
                offset=0,
                took_ms=1,
                hits=[],
            )

        response = answer_legal_question(
            request=LegalChatRequest(
                question="Unknown legal rule",
            ),
            search_function=fake_search,
            generation_client=client,
        )

        self.assertFalse(
            response.grounded
        )

        self.assertFalse(
            client.called
        )

        self.assertEqual(
            response.answer,
            NO_INFORMATION_ANSWER,
        )

        self.assertEqual(
            response.sources,
            [],
        )

    def test_extract_output_text_from_response_items(
        self,
    ) -> None:
        payload = {
            "output": [
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "Grounded answer.",
                        }
                    ],
                }
            ]
        }

        self.assertEqual(
            _extract_output_text(
                payload
            ),
            "Grounded answer.",
        )

    def test_question_filters_are_forwarded(
        self,
    ) -> None:
        captured_request = None

        def fake_search(
            request,
        ) -> LegalSearchResponse:
            nonlocal captured_request
            captured_request = request

            return LegalSearchResponse(
                query=request.query,
                total=0,
                limit=request.limit,
                offset=0,
                took_ms=1,
                hits=[],
            )

        answer_legal_question(
            request=LegalChatRequest(
                question="Notice period",
                country_codes=["GB"],
                legal_topics=[
                    "Employment Contracts"
                ],
                subsections=[
                    "Notice Period"
                ],
                reference_year=2026,
                max_sources=4,
            ),
            search_function=fake_search,
        )

        self.assertIsNotNone(
            captured_request
        )

        self.assertEqual(
            captured_request.country_codes,
            ["GB"],
        )

        self.assertEqual(
            captured_request.limit,
            4,
        )

        self.assertEqual(
            captured_request.reference_year,
            2026,
        )


if __name__ == "__main__":
    unittest.main()