"""
Shared RAG test fixtures: a valid search hit builder, a fake OpenSearch
search function, a metrics builder, and a full-featured fake generation
client (supports repair, rerank, and failure injection).

Extracted from test_rag_answer.py, which previously defined these while
test_chat_stream.py and test_stream_answer_legal_question.py imported them
from it directly.

test_rag_answer_evidence_gating.py defines its own, deliberately simpler
variants of _build_hit/_make_search_function/FakeGenerationClient (a
narrower fake sufficient for its own evidence-gating scenarios, with a
different call signature) - left local to that file rather than merged
here, since the two are not behaviorally interchangeable.
"""

from __future__ import annotations

from typing import Any

from app.clients.openai_responses import GeneratedText, OpenAIResponseError
from app.models.search import LegalSearchHit, LegalSearchResponse
from app.services.chat_metrics import LegalChatMetrics
from app.services.rag_answer import RERANK_INSTRUCTIONS


def _build_hit(
    *,
    chunk_id: str = "chunk-1",
    country: str = "United Kingdom",
    country_code: str = "GB",
    content: str = (
        "Employees with between one month and two years "
        "of service are entitled to one week's notice."
    ),
    legal_topic: str = "Employment Contracts",
    score: float = 12.5,
) -> LegalSearchHit:
    """Build one valid legal search hit."""

    return LegalSearchHit(
        score=score,
        document_id=(
            f"document-{country_code.lower()}"
        ),
        chunk_id=chunk_id,
        country=country,
        country_code=country_code,
        legal_topic=legal_topic,
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
