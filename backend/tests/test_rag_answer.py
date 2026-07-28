"""Tests for grounded legal answer generation."""

from __future__ import annotations

import unittest
from typing import Any

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
    InvalidLegalChatRequestError,
    NO_INFORMATION_ANSWER,
    RagAnswerError,
    answer_legal_question,
)


def _build_hit(
    *,
    chunk_id: str = "chunk-1",
    country: str = "United Kingdom",
    country_code: str = "GB",
    content: str = (
        "Employees with between one month and two years "
        "of service are entitled to one week's notice."
    ),
) -> LegalSearchHit:
    """Build one valid legal search hit."""

    return LegalSearchHit(
        score=12.5,
        document_id=(
            f"document-{country_code.lower()}"
        ),
        chunk_id=chunk_id,
        country=country,
        country_code=country_code,
        legal_topic="Employment Contracts",
        document_type="comparator",
        language="en",
        section="02. Employment Contracts",
        subsection="Notice Period",
        content=content,
        source_filename=(
            "Labour and Employment Law in "
            f"{country} 2026.docx"
        ),
        source_format="docx",
        reference_year=2026,
    )


class FakeGenerationClient:
    """Test text-generation client."""

    model = "test-model"

    def __init__(
        self,
        answer: str = (
            "The minimum notice is one week "
            "in the stated circumstances [1]."
        ),
    ) -> None:
        self.answer = answer
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
            text=self.answer,
            model=self.model,
        )


class RagAnswerTests(unittest.TestCase):
    """Tests for retrieval and grounded generation."""

    def test_grounded_answer_uses_retrieved_source(
        self,
    ) -> None:
        client = FakeGenerationClient()

        def fake_search(
            request: Any,
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
            len(
                response.sources
            ),
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
            request: Any,
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
            request: Any,
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

    def test_only_cited_sources_are_returned(
        self,
    ) -> None:
        client = FakeGenerationClient(
            answer=(
                "The answer is supported only "
                "by the second extract [2]."
            )
        )

        def fake_search(
            request: Any,
        ) -> LegalSearchResponse:
            return LegalSearchResponse(
                query=request.query,
                total=2,
                limit=request.limit,
                offset=0,
                took_ms=2,
                hits=[
                    _build_hit(
                        chunk_id="chunk-1",
                    ),
                    _build_hit(
                        chunk_id="chunk-2",
                    ),
                ],
            )

        response = answer_legal_question(
            request=LegalChatRequest(
                question="Notice period",
            ),
            search_function=fake_search,
            generation_client=client,
        )

        self.assertEqual(
            len(
                response.sources
            ),
            1,
        )

        self.assertEqual(
            response.sources[0].citation,
            2,
        )

        self.assertEqual(
            response.sources[0].chunk_id,
            "chunk-2",
        )

    def test_unknown_citation_is_rejected(
        self,
    ) -> None:
        client = FakeGenerationClient(
            answer=(
                "This citation does not exist [2]."
            )
        )

        def fake_search(
            request: Any,
        ) -> LegalSearchResponse:
            return LegalSearchResponse(
                query=request.query,
                total=1,
                limit=request.limit,
                offset=0,
                took_ms=1,
                hits=[
                    _build_hit()
                ],
            )

        with self.assertRaises(
            RagAnswerError
        ):
            answer_legal_question(
                request=LegalChatRequest(
                    question="Notice period",
                ),
                search_function=fake_search,
                generation_client=client,
            )

    def test_multi_country_retrieval_is_balanced(
        self,
    ) -> None:
        captured_requests: list[Any] = []

        client = FakeGenerationClient(
            answer=(
                "The UK position is supported by [1]. "
                "The Spanish position is supported by [2]."
            )
        )

        def fake_search(
            request: Any,
        ) -> LegalSearchResponse:
            captured_requests.append(
                request
            )

            country_code = (
                request.country_codes[0]
            )

            country = (
                "United Kingdom"
                if country_code == "GB"
                else "Spain"
            )

            hits = [
                _build_hit(
                    chunk_id=(
                        f"{country_code}-chunk-1"
                    ),
                    country=country,
                    country_code=country_code,
                ),
                _build_hit(
                    chunk_id=(
                        f"{country_code}-chunk-2"
                    ),
                    country=country,
                    country_code=country_code,
                ),
            ]

            return LegalSearchResponse(
                query=request.query,
                total=2,
                limit=request.limit,
                offset=0,
                took_ms=2,
                hits=hits[
                    :request.limit
                ],
            )

        response = answer_legal_question(
            request=LegalChatRequest(
                question=(
                    "Compare statutory notice periods "
                    "in the UK and Spain."
                ),
                country_codes=[
                    "GB",
                    "ES",
                ],
                max_sources=4,
            ),
            search_function=fake_search,
            generation_client=client,
        )

        self.assertEqual(
            len(
                captured_requests
            ),
            2,
        )

        self.assertEqual(
            captured_requests[0].country_codes,
            ["GB"],
        )

        self.assertEqual(
            captured_requests[1].country_codes,
            ["ES"],
        )

        self.assertEqual(
            captured_requests[0].limit,
            2,
        )

        self.assertEqual(
            captured_requests[1].limit,
            2,
        )

        self.assertEqual(
            [
                source.country_code
                for source in response.sources
            ],
            [
                "GB",
                "ES",
            ],
        )

    def test_source_budget_must_cover_all_countries(
        self,
    ) -> None:
        search_called = False

        def fake_search(
            request: Any,
        ) -> LegalSearchResponse:
            nonlocal search_called
            search_called = True

            return LegalSearchResponse(
                query=request.query,
                total=0,
                limit=request.limit,
                offset=0,
                took_ms=0,
                hits=[],
            )

        with self.assertRaises(
            InvalidLegalChatRequestError
        ):
            answer_legal_question(
                request=LegalChatRequest(
                    question=(
                        "Compare the UK and Spain."
                    ),
                    country_codes=[
                        "GB",
                        "ES",
                    ],
                    max_sources=1,
                ),
                search_function=fake_search,
            )

        self.assertFalse(
            search_called
        )


if __name__ == "__main__":
    unittest.main()