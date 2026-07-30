"""Tests for grounded legal answer generation."""

from __future__ import annotations

import unittest
from typing import Any

from app.clients.openai_responses import (
    GeneratedText,
    OpenAIResponseError,
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
    RERANK_INSTRUCTIONS,
    RERANK_SNIPPET_CHARACTERS,
    TRUNCATION_SUFFIX,
    InvalidLegalChatRequestError,
    NO_INFORMATION_ANSWER,
    RagAnswerError,
    _build_retrieval_query,
    _build_rerank_input,
    _country_name_variants_for_codes,
    _parse_rerank_order,
    _select_context_hits,
    _truncate_context,
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
        rerank_order: str | None = None,
        raise_on_rerank: bool = False,
        raise_on_generate: bool = False,
    ) -> None:
        self.answer = answer
        self.rerank_order = rerank_order
        self.raise_on_rerank = raise_on_rerank
        self.raise_on_generate = raise_on_generate
        self.instructions: str | None = None
        self.input_text: str | None = None
        self.called = False
        self.calls: list[tuple[str, str]] = []

    def generate(
        self,
        instructions: str,
        input_text: str,
    ) -> GeneratedText:
        self.called = True
        self.instructions = instructions
        self.input_text = input_text
        self.calls.append((instructions, input_text))

        if instructions == RERANK_INSTRUCTIONS:
            if self.raise_on_rerank:
                raise OpenAIResponseError("boom")

            return GeneratedText(
                text=self.rerank_order or "[]",
                model=self.model,
            )

        if self.raise_on_generate:
            raise OpenAIResponseError("boom")

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

    def test_rerank_disabled_keeps_existing_behavior(
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
                took_ms=1,
                hits=[
                    _build_hit()
                ],
            )

        answer_legal_question(
            request=LegalChatRequest(
                question="What is the notice period in the UK?",
                country_codes=["GB"],
            ),
            search_function=fake_search,
            generation_client=client,
        )

        self.assertEqual(
            len(client.calls),
            1,
        )

    def test_rerank_reorders_candidates_before_generation(
        self,
    ) -> None:
        client = FakeGenerationClient(
            answer="Supported by the top extract [1].",
            rerank_order="[3, 1, 2]",
        )

        def fake_search(
            request: Any,
        ) -> LegalSearchResponse:
            hits = [
                _build_hit(
                    chunk_id="chunk-1",
                    content="Content A.",
                ),
                _build_hit(
                    chunk_id="chunk-2",
                    content="Content B.",
                ),
                _build_hit(
                    chunk_id="chunk-3",
                    content="Content C.",
                ),
            ]

            return LegalSearchResponse(
                query=request.query,
                total=3,
                limit=request.limit,
                offset=0,
                took_ms=2,
                hits=hits,
            )

        response = answer_legal_question(
            request=LegalChatRequest(
                question="Notice period",
                country_codes=["GB"],
            ),
            search_function=fake_search,
            generation_client=client,
            rerank_enabled=True,
        )

        self.assertEqual(
            len(client.calls),
            2,
        )

        self.assertEqual(
            response.sources[0].chunk_id,
            "chunk-3",
        )

    def test_rerank_falls_back_on_invalid_response(
        self,
    ) -> None:
        client = FakeGenerationClient(
            answer="Supported by the top extract [1].",
            rerank_order="not a valid ranking",
        )

        def fake_search(
            request: Any,
        ) -> LegalSearchResponse:
            hits = [
                _build_hit(chunk_id="chunk-1"),
                _build_hit(chunk_id="chunk-2"),
                _build_hit(chunk_id="chunk-3"),
            ]

            return LegalSearchResponse(
                query=request.query,
                total=3,
                limit=request.limit,
                offset=0,
                took_ms=2,
                hits=hits,
            )

        with self.assertLogs(
            "app.services.rag_answer",
            level="WARNING",
        ):
            response = answer_legal_question(
                request=LegalChatRequest(
                    question="Notice period",
                    country_codes=["GB"],
                ),
                search_function=fake_search,
                generation_client=client,
                rerank_enabled=True,
            )

        self.assertEqual(
            response.sources[0].chunk_id,
            "chunk-1",
        )

    def test_rerank_falls_back_when_call_fails(
        self,
    ) -> None:
        client = FakeGenerationClient(
            answer="Supported by the top extract [1].",
            raise_on_rerank=True,
        )

        def fake_search(
            request: Any,
        ) -> LegalSearchResponse:
            hits = [
                _build_hit(chunk_id="chunk-1"),
                _build_hit(chunk_id="chunk-2"),
            ]

            return LegalSearchResponse(
                query=request.query,
                total=2,
                limit=request.limit,
                offset=0,
                took_ms=2,
                hits=hits,
            )

        with self.assertLogs(
            "app.services.rag_answer",
            level="WARNING",
        ):
            response = answer_legal_question(
                request=LegalChatRequest(
                    question="Notice period",
                    country_codes=["GB"],
                ),
                search_function=fake_search,
                generation_client=client,
                rerank_enabled=True,
            )

        self.assertEqual(
            response.sources[0].chunk_id,
            "chunk-1",
        )

    def test_rerank_skips_call_for_single_candidate(
        self,
    ) -> None:
        client = FakeGenerationClient(
            answer="Supported by the top extract [1]."
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

        answer_legal_question(
            request=LegalChatRequest(
                question="Notice period",
                country_codes=["GB"],
            ),
            search_function=fake_search,
            generation_client=client,
            rerank_enabled=True,
        )

        self.assertEqual(
            len(client.calls),
            1,
        )

    def test_rerank_preserves_country_balance(
        self,
    ) -> None:
        captured_requests: list[Any] = []

        client = FakeGenerationClient(
            answer=(
                "Supported by [1], [2], [3], [4]."
            ),
            rerank_order="[1, 2, 3, 4, 5, 6]",
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
                    chunk_id=f"{country_code}-chunk-{index}",
                    country=country,
                    country_code=country_code,
                )
                for index in range(1, 7)
            ]

            return LegalSearchResponse(
                query=request.query,
                total=6,
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
            rerank_enabled=True,
            rerank_pool_multiplier=3,
        )

        self.assertEqual(
            captured_requests[0].limit,
            6,
        )

        self.assertEqual(
            captured_requests[1].limit,
            6,
        )

        country_codes = [
            source.country_code
            for source in response.sources
        ]

        self.assertEqual(
            country_codes.count("GB"),
            2,
        )

        self.assertEqual(
            country_codes.count("ES"),
            2,
        )

    def test_parse_rerank_order_validates_permutation(
        self,
    ) -> None:
        self.assertEqual(
            _parse_rerank_order("[3, 1, 2]", 3),
            [3, 1, 2],
        )

        self.assertEqual(
            _parse_rerank_order(
                "```json\n[2, 1]\n```",
                2,
            ),
            [2, 1],
        )

        self.assertIsNone(
            _parse_rerank_order("not json", 3)
        )

        self.assertIsNone(
            _parse_rerank_order("[1, 1, 2]", 3)
        )

        self.assertIsNone(
            _parse_rerank_order("[1, 2]", 3)
        )

        self.assertIsNone(
            _parse_rerank_order("[1, 2, 3, 4]", 3)
        )

        self.assertIsNone(
            _parse_rerank_order("", 1)
        )

    def test_truncate_context_keeps_short_content_unchanged(
        self,
    ) -> None:
        content = "Short extract."

        self.assertEqual(
            _truncate_context(
                content=content,
                maximum_characters=100,
            ),
            content,
        )

    def test_truncate_context_truncates_at_paragraph_boundary(
        self,
    ) -> None:
        first_paragraph = "A" * 20
        second_paragraph = "B" * 50
        content = (
            first_paragraph
            + "\n\n"
            + second_paragraph
        )

        truncated = _truncate_context(
            content=content,
            maximum_characters=46,
        )

        self.assertTrue(
            truncated.startswith(first_paragraph)
        )

        self.assertNotIn(
            second_paragraph,
            truncated,
        )

        self.assertTrue(
            truncated.endswith(TRUNCATION_SUFFIX)
        )

    def test_truncate_context_hard_cuts_without_boundary(
        self,
    ) -> None:
        content = "A" * 200

        truncated = _truncate_context(
            content=content,
            maximum_characters=50,
        )

        self.assertTrue(
            truncated.endswith(TRUNCATION_SUFFIX)
        )

        self.assertLessEqual(
            len(truncated),
            50,
        )

    def test_select_context_hits_keeps_every_hit(
        self,
    ) -> None:
        hits = [
            _build_hit(
                chunk_id="chunk-1",
                content="A" * 10000,
            ),
            _build_hit(
                chunk_id="chunk-2",
                content="B" * 10000,
            ),
            _build_hit(
                chunk_id="chunk-3",
                content="C" * 10000,
            ),
        ]

        selected = _select_context_hits(
            hits=hits,
            maximum_characters=6000,
            maximum_source_characters=4000,
        )

        self.assertEqual(
            [hit.chunk_id for hit in selected],
            [
                "chunk-1",
                "chunk-2",
                "chunk-3",
            ],
        )

    def test_select_context_hits_respects_per_source_cap(
        self,
    ) -> None:
        hits = [
            _build_hit(
                chunk_id="chunk-1",
                content="A" * 10000,
            ),
        ]

        selected = _select_context_hits(
            hits=hits,
            maximum_characters=16000,
            maximum_source_characters=4000,
        )

        self.assertLessEqual(
            len(selected[0].content),
            4000,
        )

    def test_select_context_hits_returns_empty_for_no_hits(
        self,
    ) -> None:
        self.assertEqual(
            _select_context_hits(
                hits=[],
                maximum_characters=16000,
                maximum_source_characters=4000,
            ),
            [],
        )

    def test_answer_preserves_all_countries_with_small_context_budget(
        self,
    ) -> None:
        client = FakeGenerationClient(
            answer=(
                "The UK position is supported by [1]. "
                "The Spanish position is supported by [2]."
            )
        )

        def fake_search(
            request: Any,
        ) -> LegalSearchResponse:
            country_code = (
                request.country_codes[0]
            )

            country = (
                "United Kingdom"
                if country_code == "GB"
                else "Spain"
            )

            return LegalSearchResponse(
                query=request.query,
                total=1,
                limit=request.limit,
                offset=0,
                took_ms=2,
                hits=[
                    _build_hit(
                        chunk_id=f"{country_code}-chunk-1",
                        country=country,
                        country_code=country_code,
                        content=(
                            f"{country_code} " * 5000
                        ),
                    ),
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
                max_sources=2,
            ),
            search_function=fake_search,
            generation_client=client,
            max_context_characters=8000,
            max_source_characters=4000,
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

    def test_country_name_variants_for_codes(
        self,
    ) -> None:
        variants = _country_name_variants_for_codes(
            [
                "GB",
            ]
        )

        self.assertIn(
            "United Kingdom",
            variants,
        )

        self.assertIn(
            "UK",
            variants,
        )

    def test_build_retrieval_query_strips_country_names(
        self,
    ) -> None:
        cleaned = _build_retrieval_query(
            question=(
                "Compare overtime rules in the "
                "United Kingdom and Spain."
            ),
            country_name_variants=(
                _country_name_variants_for_codes(
                    [
                        "GB",
                        "ES",
                    ]
                )
            ),
        )

        self.assertEqual(
            cleaned,
            "overtime",
        )

    def test_build_retrieval_query_falls_back_when_empty(
        self,
    ) -> None:
        question = "Compare the UK and Spain."

        cleaned = _build_retrieval_query(
            question=question,
            country_name_variants=(
                _country_name_variants_for_codes(
                    [
                        "GB",
                        "ES",
                    ]
                )
            ),
        )

        self.assertEqual(
            cleaned,
            question,
        )

    def test_search_query_is_cleaned_before_retrieval(
        self,
    ) -> None:
        captured_requests: list[Any] = []

        def fake_search(
            request: Any,
        ) -> LegalSearchResponse:
            captured_requests.append(
                request
            )

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
                question=(
                    "Compare overtime rules in the "
                    "United Kingdom and Spain."
                ),
                country_codes=[
                    "GB",
                    "ES",
                ],
                max_sources=4,
            ),
            search_function=fake_search,
        )

        self.assertEqual(
            captured_requests[0].query,
            "overtime",
        )

        self.assertEqual(
            captured_requests[1].query,
            "overtime",
        )

    def test_build_rerank_input_truncates_content(
        self,
    ) -> None:
        long_content = "A" * (
            RERANK_SNIPPET_CHARACTERS + 500
        )

        hit = _build_hit(
            content=long_content
        )

        prompt = _build_rerank_input(
            question="Notice period?",
            hits=[hit],
        )

        self.assertIn(
            "A" * RERANK_SNIPPET_CHARACTERS,
            prompt,
        )

        self.assertNotIn(
            "A" * (RERANK_SNIPPET_CHARACTERS + 1),
            prompt,
        )


if __name__ == "__main__":
    unittest.main()