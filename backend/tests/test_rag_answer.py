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
from app.services.chat_metrics import (
    LegalChatMetrics,
)
from app.services.rag_answer import (
    HARD_QUALITY_ERROR_TYPES,
    NON_REPAIRING_SOFT_ERROR_TYPES,
    REPAIR_TRIGGERING_SOFT_ERROR_TYPES,
    RERANK_INSTRUCTIONS,
    RERANK_SNIPPET_CHARACTERS,
    SOFT_QUALITY_ERROR_TYPES,
    InvalidLegalChatRequestError,
    NO_INFORMATION_ANSWER,
    RagAnswerError,
    _allocate_country_context_budgets,
    _build_retrieval_query,
    _build_rerank_input,
    _country_name_variants_for_codes,
    _parse_rerank_order,
    _truncate_context,
    _validate_answer_structure,
    _validate_citation_format,
    _validate_no_false_absence_claims,
    _validate_no_internal_references,
    _validate_paid_leave_scope,
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


def _build_metrics(
    request_id: str,
) -> LegalChatMetrics:
    """Build one LegalChatMetrics instance with the shared test defaults."""

    return LegalChatMetrics(
        request_id=request_id,
        question_characters=10,
        max_sources=6,
        rerank_enabled=False,
    )


def _make_search_function(
    *,
    hits: list[LegalSearchHit] | None = None,
):
    """Build a fake OpenSearch search function returning fixed hits."""

    resolved_hits = (
        hits
        if hits is not None
        else [_build_hit()]
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
            hits=resolved_hits,
        )

    return fake_search


class FakeGenerationClient:
    """Test text-generation client."""

    model = "test-model"

    def __init__(
        self,
        answer: str = (
            "United Kingdom\n"
            "- The minimum notice is one week "
            "in the stated circumstances [1]."
        ),
        repair_answer: str | None = None,
        rerank_order: str | None = None,
        raise_on_rerank: bool = False,
        raise_on_generate: bool = False,
    ) -> None:
        self.answer = answer
        self.repair_answer = repair_answer
        self.rerank_order = rerank_order
        self.raise_on_rerank = raise_on_rerank
        self.raise_on_generate = raise_on_generate
        self.instructions: str | None = None
        self.input_text: str | None = None
        self.called = False
        self.calls: list[tuple[str, str]] = []
        self._main_call_count = 0

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

        self._main_call_count += 1

        if (
            self._main_call_count >= 2
            and self.repair_answer is not None
        ):
            return GeneratedText(
                text=self.repair_answer,
                model=self.model,
            )

        return GeneratedText(
            text=self.answer,
            model=self.model,
        )


class RagAnswerTests(unittest.TestCase):
    """Tests for retrieval and grounded generation."""

    def _ask(
        self,
        *,
        question: str,
        country_codes: list[str],
        client: FakeGenerationClient,
        metrics: LegalChatMetrics,
        search_function=None,
    ):
        """Run answer_legal_question with the repeated test skeleton."""

        return answer_legal_question(
            request=LegalChatRequest(
                question=question,
                country_codes=country_codes,
            ),
            search_function=(
                search_function
                if search_function is not None
                else _make_search_function()
            ),
            generation_client=client,
            metrics=metrics,
        )

    def _assert_non_repairing_soft_warning(
        self,
        *,
        warning_type: str,
        initial_answer: str,
        question: str = "What notice period applies?",
        country_codes: list[str] | None = None,
        search_function=None,
    ):
        """Assert one soft warning is detected but never triggers a repair."""

        client = FakeGenerationClient(
            answer=initial_answer,
        )

        metrics = _build_metrics(
            f"test-non-repairing-{warning_type}"
        )

        result = self._ask(
            question=question,
            country_codes=country_codes or ["GB"],
            client=client,
            metrics=metrics,
            search_function=search_function,
        )

        main_calls = [
            call
            for call in client.calls
            if call[0] != RERANK_INSTRUCTIONS
        ]

        self.assertEqual(
            len(main_calls),
            1,
        )

        self.assertEqual(
            result.answer,
            initial_answer,
        )

        self.assertTrue(
            result.grounded
        )

        self.assertEqual(
            metrics.generation_attempts,
            1,
        )

        self.assertIs(
            metrics.repair_triggered,
            False,
        )

        self.assertIs(
            metrics.repair_answer_returned,
            False,
        )

        self.assertIs(
            metrics.repair_success,
            False,
        )

        self.assertIn(
            warning_type,
            metrics.initial_soft_error_types,
        )

        self.assertIn(
            warning_type,
            metrics.final_soft_error_types,
        )

        self.assertEqual(
            metrics.initial_hard_error_types,
            [],
        )

        self.assertEqual(
            metrics.final_hard_error_types,
            [],
        )

        return result, metrics, client

    def _assert_repair_triggered(
        self,
        *,
        initial_answer: str,
        repaired_answer: str,
        expected_initial_error_type: str,
        expected_initial_error_category: str,
        expected_repair_success: bool,
        expected_final_soft_error_types: list[str] | None = None,
        expected_final_hard_error_types: list[str] | None = None,
        question: str = "What notice period applies?",
        country_codes: list[str] | None = None,
        search_function=None,
    ):
        """Assert a repair is triggered and check its outcome metrics."""

        client = FakeGenerationClient(
            answer=initial_answer,
            repair_answer=repaired_answer,
        )

        metrics = _build_metrics(
            f"test-repair-{expected_initial_error_type}"
        )

        result = self._ask(
            question=question,
            country_codes=country_codes or ["GB"],
            client=client,
            metrics=metrics,
            search_function=search_function,
        )

        self.assertEqual(
            len(client.calls),
            2,
        )

        self.assertEqual(
            metrics.generation_attempts,
            2,
        )

        self.assertIs(
            metrics.repair_triggered,
            True,
        )

        self.assertIs(
            metrics.repair_answer_returned,
            True,
        )

        self.assertIs(
            metrics.repair_success,
            expected_repair_success,
        )

        self.assertEqual(
            result.answer,
            repaired_answer,
        )

        initial_errors = (
            metrics.initial_hard_error_types
            if expected_initial_error_category == "hard"
            else metrics.initial_soft_error_types
        )

        self.assertIn(
            expected_initial_error_type,
            initial_errors,
        )

        # A hard error must never survive to the returned answer: if it
        # did, answer_legal_question would have raised instead of
        # returning here.
        self.assertEqual(
            metrics.final_hard_error_types,
            (
                expected_final_hard_error_types
                if expected_final_hard_error_types is not None
                else []
            ),
        )

        if expected_final_soft_error_types is not None:
            self.assertEqual(
                metrics.final_soft_error_types,
                expected_final_soft_error_types,
            )

        return result, metrics, client

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

    def test_validate_citation_format_accepts_valid_citations(
        self,
    ) -> None:
        self.assertEqual(
            _validate_citation_format(
                "Supported by [1] and also [1, 2]."
            ),
            [],
        )

    def test_validate_citation_format_rejects_semicolons(
        self,
    ) -> None:
        errors = _validate_citation_format(
            "Supported by [1; 2]."
        )

        self.assertTrue(
            any(
                error.error_type
                == "invalid_citation_format"
                for error in errors
            )
        )

    def test_validate_citation_format_rejects_mixed_separators(
        self,
    ) -> None:
        errors = _validate_citation_format(
            "Supported by [1, 3; 2]."
        )

        self.assertTrue(
            any(
                error.error_type
                == "invalid_citation_format"
                for error in errors
            )
        )

    def test_malformed_citation_rejects_the_whole_answer(
        self,
    ) -> None:
        client = FakeGenerationClient(
            answer=(
                "Supported by the extracts [1, 3; 2]."
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
                "The Spanish position is supported by [3]."
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
            answer="United Kingdom\n- Supported by the top extract [1].",
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
            answer="United Kingdom\n- Supported by the top extract [1].",
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
            answer="United Kingdom\n- Supported by the top extract [1].",
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
            answer="United Kingdom\n- Supported by the top extract [1]."
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
                "United Kingdom\n"
                "- Supported by [1], [2].\n"
                "Spain\n"
                "- Supported by [3], [4]."
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
            maximum_characters=30,
        )

        self.assertEqual(
            truncated,
            first_paragraph,
        )

        self.assertNotIn(
            "B",
            truncated,
        )

    def test_truncate_context_hard_cuts_without_boundary(
        self,
    ) -> None:
        content = "A" * 200

        truncated = _truncate_context(
            content=content,
            maximum_characters=50,
        )

        self.assertEqual(
            truncated,
            "A" * 50,
        )

    def test_truncate_context_never_adds_a_marker(
        self,
    ) -> None:
        truncated = _truncate_context(
            content="A" * 200,
            maximum_characters=50,
        )

        self.assertNotIn(
            "truncated",
            truncated.lower(),
        )

        self.assertNotIn(
            "[",
            truncated,
        )

    def test_allocate_country_context_budgets_keeps_every_hit(
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

        selected = _allocate_country_context_budgets(
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

    def test_allocate_country_context_budgets_respects_per_source_cap(
        self,
    ) -> None:
        hits = [
            _build_hit(
                chunk_id="chunk-1",
                content="A" * 10000,
            ),
        ]

        selected = _allocate_country_context_budgets(
            hits=hits,
            maximum_characters=16000,
            maximum_source_characters=4000,
        )

        self.assertLessEqual(
            len(selected[0].content),
            4000,
        )

    def test_allocate_country_context_budgets_returns_empty_for_no_hits(
        self,
    ) -> None:
        self.assertEqual(
            _allocate_country_context_budgets(
                hits=[],
                maximum_characters=16000,
                maximum_source_characters=4000,
            ),
            [],
        )

    def test_budget_is_split_per_country_not_per_source(
        self,
    ) -> None:
        hits = []

        for country_code in (
            "GB",
            "ES",
            "IT",
        ):
            hits.append(
                _build_hit(
                    chunk_id=f"{country_code}-1",
                    country_code=country_code,
                    content="A" * 10000,
                )
            )

            hits.append(
                _build_hit(
                    chunk_id=f"{country_code}-2",
                    country_code=country_code,
                    content="B" * 10000,
                )
            )

        selected = _allocate_country_context_budgets(
            hits=hits,
            maximum_characters=16000,
            maximum_source_characters=4000,
        )

        lengths_by_chunk_id = {
            hit.chunk_id: len(hit.content)
            for hit in selected
        }

        for country_code in (
            "GB",
            "ES",
            "IT",
        ):
            self.assertEqual(
                lengths_by_chunk_id[
                    f"{country_code}-1"
                ],
                4000,
            )

            self.assertEqual(
                lengths_by_chunk_id[
                    f"{country_code}-2"
                ],
                1333,
            )

    def test_every_country_stays_represented_after_allocation(
        self,
    ) -> None:
        hits = [
            _build_hit(
                chunk_id="GB-1",
                country_code="GB",
                content="A" * 10000,
            ),
            _build_hit(
                chunk_id="ES-1",
                country_code="ES",
                content="B" * 10000,
            ),
            _build_hit(
                chunk_id="IT-1",
                country_code="IT",
                content="C" * 10000,
            ),
        ]

        selected = _allocate_country_context_budgets(
            hits=hits,
            maximum_characters=16000,
            maximum_source_characters=4000,
        )

        self.assertEqual(
            sorted(
                hit.country_code
                for hit in selected
            ),
            [
                "ES",
                "GB",
                "IT",
            ],
        )

    def test_paid_leave_context_preserves_belgium_parental_leave(
        self,
    ) -> None:
        """
        Three countries, two sources per country (6 sources total).

        Under the old per-source split (16000 // 6 = 2666 characters
        each), Belgium's primary source is cut before reaching its
        "Maternity and Paternity Leave" section, which starts past
        character 2666. Under the per-country split, that same source
        gets up to 4000 characters and the section survives in full.
        """

        filler = (
            "General leave provisions apply to all workers. "
            * 60
        )

        belgium_primary_content = (
            filler
            + "Maternity and Paternity Leave: parents are "
            "entitled to fifteen days of paid leave "
            "following the birth of a child."
        )

        hits = [
            _build_hit(
                chunk_id="BE-1",
                country_code="BE",
                content=belgium_primary_content,
            ),
            _build_hit(
                chunk_id="BE-2",
                country_code="BE",
                content="Other Belgian leave content. " * 200,
            ),
            _build_hit(
                chunk_id="GB-1",
                country_code="GB",
                content="UK leave content. " * 200,
            ),
            _build_hit(
                chunk_id="GB-2",
                country_code="GB",
                content="UK secondary leave content. " * 200,
            ),
            _build_hit(
                chunk_id="FR-1",
                country_code="FR",
                content="French leave content. " * 200,
            ),
            _build_hit(
                chunk_id="FR-2",
                country_code="FR",
                content=(
                    "French secondary leave content. " * 200
                ),
            ),
        ]

        selected = _allocate_country_context_budgets(
            hits=hits,
            maximum_characters=16000,
            maximum_source_characters=4000,
        )

        belgium_primary = next(
            hit
            for hit in selected
            if hit.chunk_id == "BE-1"
        )

        self.assertIn(
            "Maternity and Paternity Leave",
            belgium_primary.content,
        )

    def test_no_truncation_marker_leaks_into_context(
        self,
    ) -> None:
        hits = [
            _build_hit(
                chunk_id="GB-1",
                country_code="GB",
                content="A" * 10000,
            ),
            _build_hit(
                chunk_id="ES-1",
                country_code="ES",
                content="B" * 10000,
            ),
        ]

        selected = _allocate_country_context_budgets(
            hits=hits,
            maximum_characters=8000,
            maximum_source_characters=4000,
        )

        for hit in selected:
            self.assertNotIn(
                "Extract truncated",
                hit.content,
            )

            self.assertNotIn(
                "[",
                hit.content,
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

    def test_single_country_rejects_more_than_six_bullets(
        self,
    ) -> None:
        answer = (
            "United Kingdom\n"
            + "\n".join(
                f"- Bullet {position}"
                for position in range(1, 8)
            )
        )

        errors = _validate_answer_structure(
            answer=answer,
            requested_country_codes=[
                "GB",
            ],
        )

        self.assertTrue(
            any(
                "six bullets" in error.message
                for error in errors
            )
        )

    def test_comparison_rejects_more_than_four_bullets_per_country(
        self,
    ) -> None:
        answer = (
            "United Kingdom\n"
            + "\n".join(
                f"- UK point {position}"
                for position in range(1, 6)
            )
            + "\nSpain\n"
            "- ES point 1\n"
            "Comparison\n"
            "- Compare point 1"
        )

        errors = _validate_answer_structure(
            answer=answer,
            requested_country_codes=[
                "GB",
                "ES",
            ],
        )

        self.assertTrue(
            any(
                "more than four bullets" in error.message
                for error in errors
            )
        )

    def test_comparison_rejects_more_than_two_comparison_bullets(
        self,
    ) -> None:
        answer = (
            "United Kingdom\n"
            "- UK point 1\n"
            "Spain\n"
            "- ES point 1\n"
            "Comparison\n"
            "- Compare 1\n"
            "- Compare 2\n"
            "- Compare 3"
        )

        errors = _validate_answer_structure(
            answer=answer,
            requested_country_codes=[
                "GB",
                "ES",
            ],
        )

        self.assertTrue(
            any(
                "no more than two bullets" in error.message
                for error in errors
            )
        )

    def test_rejects_internal_extract_references(
        self,
    ) -> None:
        errors = _validate_no_internal_references(
            "Based on the provided extracts, "
            "the rule is X [1]."
        )

        self.assertTrue(
            any(
                "provided extracts" in error.message
                for error in errors
            )
        )

        self.assertTrue(
            all(
                error.error_type == "internal_reference"
                for error in errors
            )
        )

    def test_generic_in_the_extracts_phrase_is_detected(
        self,
    ) -> None:
        for phrase in (
            "The rule is described in the extracts.",
            "The extract does not specify a duration.",
            "This is confirmed by the sources provided.",
        ):
            errors = _validate_no_internal_references(
                phrase
            )

            self.assertTrue(
                errors,
                msg=f"Expected a match for: {phrase!r}",
            )

    def test_paid_leave_rejects_unpaid_leave(
        self,
    ) -> None:
        errors = _validate_paid_leave_scope(
            question=(
                "What is the paid leave "
                "entitlement in Spain?"
            ),
            answer=(
                "Employees are entitled to unpaid "
                "leave for family reasons [1]."
            ),
        )

        self.assertTrue(
            errors
        )

    def test_quality_failure_triggers_one_repair_generation(
        self,
    ) -> None:
        bad_answer = (
            "United Kingdom\n"
            "- Employees are entitled to unpaid "
            "leave for family reasons [1]."
        )

        good_answer = (
            "United Kingdom\n"
            "- Employees are entitled to paid "
            "parental leave for four weeks [1]."
        )

        client = FakeGenerationClient(
            answer=bad_answer,
            repair_answer=good_answer,
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
                question=(
                    "What is the paid leave "
                    "entitlement in the UK?"
                ),
                country_codes=[
                    "GB",
                ],
            ),
            search_function=fake_search,
            generation_client=client,
        )

        main_calls = [
            call
            for call in client.calls
            if call[0] != RERANK_INSTRUCTIONS
        ]

        self.assertEqual(
            len(main_calls),
            2,
        )

    def test_valid_repair_answer_is_returned(
        self,
    ) -> None:
        bad_answer = (
            "United Kingdom\n"
            "- Employees are entitled to unpaid "
            "leave for family reasons [1]."
        )

        good_answer = (
            "United Kingdom\n"
            "- Employees are entitled to paid "
            "parental leave for four weeks [1]."
        )

        client = FakeGenerationClient(
            answer=bad_answer,
            repair_answer=good_answer,
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

        response = answer_legal_question(
            request=LegalChatRequest(
                question=(
                    "What is the paid leave "
                    "entitlement in the UK?"
                ),
                country_codes=[
                    "GB",
                ],
            ),
            search_function=fake_search,
            generation_client=client,
        )

        self.assertEqual(
            response.answer,
            good_answer,
        )

    def test_second_invalid_answer_raises_controlled_error(
        self,
    ) -> None:
        bad_answer = (
            "United Kingdom\n"
            "- Employees are entitled to unpaid "
            "leave for family reasons [1]."
        )

        client = FakeGenerationClient(
            answer=bad_answer
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
                    question=(
                        "What is the paid leave "
                        "entitlement in the UK?"
                    ),
                    country_codes=[
                        "GB",
                    ],
                ),
                search_function=fake_search,
                generation_client=client,
            )

    def test_false_absence_claim_no_longer_auto_repairs_italian_maternity(
        self,
    ) -> None:
        bad_answer = (
            "The duration of Italian maternity leave "
            "is not specified [1]."
        )

        result, _metrics, _client = self._assert_non_repairing_soft_warning(
            warning_type="false_absence_claim",
            initial_answer=bad_answer,
            question="What is the maternity leave duration in Italy?",
            country_codes=["IT"],
            search_function=_make_search_function(
                hits=[
                    _build_hit(
                        country="Italy",
                        country_code="IT",
                        content=(
                            "Maternity leave is compulsory for two "
                            "months prior to the expected date of "
                            "childbirth and three months after "
                            "childbirth."
                        ),
                    )
                ]
            ),
        )

        # Business regression guard: the false absence claim must
        # survive untouched, not be silently corrected away.
        self.assertIn(
            "is not specified",
            result.answer,
        )

    def test_soft_validation_failure_never_returns_502(
        self,
    ) -> None:
        client = FakeGenerationClient(
            answer=(
                "United Kingdom\n"
                "- Supported by the provided extracts [1]."
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

        response = answer_legal_question(
            request=LegalChatRequest(
                question=(
                    "What is the notice period in the UK?"
                ),
                country_codes=[
                    "GB",
                ],
            ),
            search_function=fake_search,
            generation_client=client,
        )

        self.assertTrue(
            response.grounded
        )

        # internal_reference is a non-repairing soft warning: it is
        # detected but must not trigger a second generation call.
        main_calls = [
            call
            for call in client.calls
            if call[0] != RERANK_INSTRUCTIONS
        ]

        self.assertEqual(
            len(main_calls),
            1,
        )

    def test_repaired_answer_with_only_soft_errors_is_returned(
        self,
    ) -> None:
        bad_answer = (
            "United Kingdom\n"
            "- Employees are entitled to unpaid "
            "leave for family reasons [1]."
        )

        repaired_answer = (
            "United Kingdom\n"
            "- Employees are entitled to paid leave "
            "for four weeks, as described in the "
            "provided extracts [1]."
        )

        client = FakeGenerationClient(
            answer=bad_answer,
            repair_answer=repaired_answer,
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

        response = answer_legal_question(
            request=LegalChatRequest(
                question=(
                    "What is the paid leave "
                    "entitlement in the UK?"
                ),
                country_codes=[
                    "GB",
                ],
            ),
            search_function=fake_search,
            generation_client=client,
        )

        self.assertEqual(
            response.answer,
            repaired_answer,
        )

    def test_first_answer_is_returned_when_repair_introduces_hard_error(
        self,
    ) -> None:
        first_answer = (
            "United Kingdom\n"
            "- Supported by the provided extracts [1]."
        )

        repaired_answer = "Supported by [1]."

        client = FakeGenerationClient(
            answer=first_answer,
            repair_answer=repaired_answer,
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

        response = answer_legal_question(
            request=LegalChatRequest(
                question=(
                    "What is the notice period in the UK?"
                ),
                country_codes=[
                    "GB",
                ],
            ),
            search_function=fake_search,
            generation_client=client,
        )

        self.assertEqual(
            response.answer,
            first_answer,
        )

    def test_two_hard_validation_failures_raise_rag_answer_error(
        self,
    ) -> None:
        client = FakeGenerationClient(
            answer="Supported by [1]."
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
                    question=(
                        "What is the notice period in the UK?"
                    ),
                    country_codes=[
                        "GB",
                    ],
                ),
                search_function=fake_search,
                generation_client=client,
            )

    def test_false_absence_claim_is_soft(
        self,
    ) -> None:
        errors = _validate_no_false_absence_claims(
            context=(
                "Maternity leave is compulsory for "
                "two months prior to childbirth."
            ),
            answer=(
                "The exact duration is not specified "
                "in the sources [1]."
            ),
        )

        self.assertTrue(
            errors
        )

        self.assertTrue(
            all(
                error.error_type == "false_absence_claim"
                for error in errors
            )
        )

        self.assertTrue(
            all(
                error.error_type in SOFT_QUALITY_ERROR_TYPES
                for error in errors
            )
        )

        self.assertTrue(
            all(
                error.error_type not in HARD_QUALITY_ERROR_TYPES
                for error in errors
            )
        )

    def test_multi_country_duration_does_not_create_cross_country_hard_failure(
        self,
    ) -> None:
        def fake_search(
            request: Any,
        ) -> LegalSearchResponse:
            country_code = request.country_codes[0]

            if country_code == "GB":
                hit = _build_hit(
                    country="United Kingdom",
                    country_code="GB",
                    content=(
                        "Employees are entitled to "
                        "one week's notice."
                    ),
                )
            else:
                hit = _build_hit(
                    chunk_id="chunk-2",
                    country="Spain",
                    country_code="ES",
                    content=(
                        "Spain applies its general annual "
                        "leave rules without a fixed figure "
                        "stated in this extract."
                    ),
                )

            return LegalSearchResponse(
                query=request.query,
                total=1,
                limit=request.limit,
                offset=0,
                took_ms=1,
                hits=[
                    hit
                ],
            )

        answer = (
            "United Kingdom\n"
            "- Notice period is one week [1].\n"
            "Spain\n"
            "- The exact annual leave duration is "
            "not specified in the sources [2]."
        )

        client = FakeGenerationClient(
            answer=answer
        )

        response = answer_legal_question(
            request=LegalChatRequest(
                question=(
                    "Compare notice and leave rules "
                    "in the UK and Spain."
                ),
                country_codes=[
                    "GB",
                    "ES",
                ],
            ),
            search_function=fake_search,
            generation_client=client,
        )

        self.assertTrue(
            response.grounded
        )

        self.assertIn(
            "not specified",
            response.answer.casefold(),
        )

    def test_error_metrics_preserve_retrieval_and_selected_source_counts(
        self,
    ) -> None:
        client = FakeGenerationClient(
            answer="Supported by [1]."
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

        metrics = LegalChatMetrics(
            request_id="request-1",
            question_characters=10,
            max_sources=6,
            rerank_enabled=False,
        )

        with self.assertRaises(
            RagAnswerError
        ):
            answer_legal_question(
                request=LegalChatRequest(
                    question=(
                        "What is the notice period in the UK?"
                    ),
                    country_codes=[
                        "GB",
                    ],
                ),
                search_function=fake_search,
                generation_client=client,
                metrics=metrics,
            )

        self.assertEqual(
            metrics.retrieval_total,
            1,
        )

        self.assertEqual(
            metrics.selected_sources,
            1,
        )

        self.assertEqual(
            metrics.model,
            "test-model",
        )

        self.assertEqual(
            metrics.generation_attempts,
            2,
        )

        self.assertTrue(
            metrics.repair_triggered
        )

        self.assertFalse(
            metrics.repair_success
        )

        self.assertIn(
            "missing_requested_country",
            metrics.final_hard_error_types,
        )

    def test_repair_triggering_and_non_repairing_soft_sets_are_disjoint(
        self,
    ) -> None:
        self.assertEqual(
            REPAIR_TRIGGERING_SOFT_ERROR_TYPES,
            frozenset({"structure"}),
        )

        self.assertEqual(
            NON_REPAIRING_SOFT_ERROR_TYPES,
            frozenset(
                {
                    "false_absence_claim",
                    "internal_reference",
                    "repetition",
                }
            ),
        )

        self.assertFalse(
            REPAIR_TRIGGERING_SOFT_ERROR_TYPES
            & NON_REPAIRING_SOFT_ERROR_TYPES
        )

        self.assertEqual(
            REPAIR_TRIGGERING_SOFT_ERROR_TYPES
            | NON_REPAIRING_SOFT_ERROR_TYPES,
            SOFT_QUALITY_ERROR_TYPES,
        )

    def test_false_absence_soft_warning_does_not_trigger_repair(
        self,
    ) -> None:
        self._assert_non_repairing_soft_warning(
            warning_type="false_absence_claim",
            initial_answer=(
                "United Kingdom\n"
                "- The exact entitlement is not "
                "specified [1]."
            ),
            question="What is the parental leave duration in the UK?",
            search_function=_make_search_function(
                hits=[
                    _build_hit(
                        content=(
                            "Employees are entitled to "
                            "four weeks of parental leave."
                        ),
                    )
                ]
            ),
        )

    def test_internal_reference_soft_warning_does_not_trigger_repair(
        self,
    ) -> None:
        self._assert_non_repairing_soft_warning(
            warning_type="internal_reference",
            initial_answer=(
                "United Kingdom\n"
                "- Notice period is one week, as "
                "covered in the provided extracts [1]."
            ),
        )

    def test_repetition_soft_warning_does_not_trigger_repair(
        self,
    ) -> None:
        self._assert_non_repairing_soft_warning(
            warning_type="repetition",
            initial_answer=(
                "United Kingdom\n"
                "- Notice period is one week for "
                "qualifying employees [1].\n"
                "- Notice period is one week for "
                "qualifying employees [1]."
            ),
        )

    def test_clean_direct_answer_has_false_repair_metrics(
        self,
    ) -> None:
        answer = (
            "United Kingdom\n"
            "- Notice period is one week for "
            "qualifying employees [1]."
        )

        client = FakeGenerationClient(
            answer=answer,
        )

        metrics = _build_metrics(
            "request-clean-direct"
        )

        result = self._ask(
            question="What is the notice period in the UK?",
            country_codes=["GB"],
            client=client,
            metrics=metrics,
        )

        self.assertEqual(
            result.answer,
            answer,
        )

        self.assertEqual(
            metrics.generation_attempts,
            1,
        )

        self.assertIs(
            metrics.repair_triggered,
            False,
        )

        self.assertIs(
            metrics.repair_answer_returned,
            False,
        )

        self.assertIs(
            metrics.repair_success,
            False,
        )

        self.assertEqual(
            metrics.initial_hard_error_types,
            [],
        )

        self.assertEqual(
            metrics.initial_soft_error_types,
            [],
        )

        self.assertEqual(
            metrics.final_hard_error_types,
            [],
        )

        self.assertEqual(
            metrics.final_soft_error_types,
            [],
        )

    def test_structure_soft_error_triggers_repair(
        self,
    ) -> None:
        self._assert_repair_triggered(
            initial_answer=(
                "United Kingdom\n"
                "- Bullet one covering notice periods [1].\n"
                "- Bullet two covering notice periods [1].\n"
                "- Bullet three covering notice periods [1].\n"
                "- Bullet four covering notice periods [1].\n"
                "- Bullet five covering notice periods [1].\n"
                "- Bullet six covering notice periods [1].\n"
                "- Bullet seven covering notice periods [1]."
            ),
            repaired_answer=(
                "United Kingdom\n"
                "- Notice period is one week for "
                "qualifying employees [1].\n"
                "- Notice increases with length of "
                "service [1].\n"
                "- Statutory minimums apply regardless "
                "of contract terms [1]."
            ),
            expected_initial_error_type="structure",
            expected_initial_error_category="soft",
            expected_repair_success=True,
            expected_final_soft_error_types=[],
        )

    def test_hard_error_still_triggers_repair(
        self,
    ) -> None:
        self._assert_repair_triggered(
            initial_answer=(
                "United Kingdom\n"
                "- Employees are entitled to unpaid "
                "leave for family reasons [1]."
            ),
            repaired_answer=(
                "United Kingdom\n"
                "- Employees are entitled to paid "
                "parental leave for four weeks [1]."
            ),
            expected_initial_error_type="paid_leave_scope",
            expected_initial_error_category="hard",
            expected_repair_success=True,
            expected_final_soft_error_types=[],
            question="What is the paid leave entitlement in the UK?",
        )

    def test_repair_success_requires_no_final_quality_errors(
        self,
    ) -> None:
        with self.subTest("clean repair"):
            self._assert_repair_triggered(
                initial_answer=(
                    "United Kingdom\n"
                    "- Employees are entitled to unpaid "
                    "leave for family reasons [1]."
                ),
                repaired_answer=(
                    "United Kingdom\n"
                    "- Employees are entitled to paid "
                    "parental leave for four weeks [1]."
                ),
                expected_initial_error_type="paid_leave_scope",
                expected_initial_error_category="hard",
                expected_repair_success=True,
                expected_final_soft_error_types=[],
                question="What is the paid leave entitlement in the UK?",
            )

        with self.subTest("returned repair with residual warning"):
            self._assert_repair_triggered(
                initial_answer="Supported by [1].",
                repaired_answer=(
                    "United Kingdom\n"
                    "- Notice entitlement is one week, as "
                    "set out in the provided extracts [1]."
                ),
                expected_initial_error_type=(
                    "missing_requested_country"
                ),
                expected_initial_error_category="hard",
                expected_repair_success=False,
                expected_final_soft_error_types=[
                    "internal_reference",
                ],
            )

    def test_structure_with_non_repairing_warning_still_triggers_repair(
        self,
    ) -> None:
        bad_answer = (
            "United Kingdom\n"
            "- Bullet one covering notice periods [1].\n"
            "- Bullet two covering notice periods [1].\n"
            "- Bullet three covering notice periods [1].\n"
            "- Bullet four covering notice periods [1].\n"
            "- Bullet five covering notice periods [1].\n"
            "- Bullet six covering notice periods [1].\n"
            "- Bullet seven, as covered in the "
            "provided extracts [1]."
        )

        _result, metrics, _client = self._assert_repair_triggered(
            initial_answer=bad_answer,
            repaired_answer=bad_answer,
            expected_initial_error_type="structure",
            expected_initial_error_category="soft",
            expected_repair_success=False,
            expected_final_soft_error_types=[
                "internal_reference",
                "structure",
            ],
        )

        # The presence of "structure" alone must be enough to trigger a
        # repair, even though "internal_reference" - on its own
        # non-repairing - is also present.
        self.assertIn(
            "internal_reference",
            metrics.initial_soft_error_types,
        )


if __name__ == "__main__":
    unittest.main()