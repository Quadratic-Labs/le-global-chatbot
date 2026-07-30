"""Tests for the legal-chat router scope checks and orchestration."""

from __future__ import annotations

import unittest
from typing import Any

from app.clients.openai_responses import GeneratedText
from app.core.country_registry import COUNTRIES
from app.models.catalog import (
    LegalCatalogCountry,
    LegalCatalogResponse,
)
from app.models.chat import LegalChatRequest
from app.models.search import (
    LegalSearchHit,
    LegalSearchResponse,
)
from app.routers.chat import resolve_legal_chat_response
from app.services.rag_answer import InvalidLegalChatRequestError


def _build_catalog() -> LegalCatalogResponse:
    """Build a catalog covering every country in the real corpus."""

    return LegalCatalogResponse(
        countries=[
            LegalCatalogCountry(
                country_code=country.code,
                country=country.display_name,
                chunk_count=42,
            )
            for country in COUNTRIES
        ],
        legal_topics=[],
        subsections=[],
    )


def _catalog_provider() -> LegalCatalogResponse:
    """Return the test catalog."""

    return _build_catalog()


def _build_hit(
    *,
    country_code: str,
    country: str,
    content: str = "Overtime legal content.",
) -> LegalSearchHit:
    """Build one valid legal search hit."""

    return LegalSearchHit(
        score=10.0,
        document_id=f"document-{country_code.lower()}",
        chunk_id=f"chunk-{country_code.lower()}",
        country=country,
        country_code=country_code,
        legal_topic="Working Conditions",
        document_type="comparator",
        language="en",
        section="03. Working Conditions",
        subsection="Overtime",
        content=content,
        source_filename=(
            f"Labour and Employment Law in {country} 2026.docx"
        ),
        source_format="docx",
        reference_year=2026,
    )


class FakeGenerationClient:
    """Test text-generation client."""

    model = "test-model"

    def __init__(
        self,
        answer: str,
    ) -> None:
        self.answer = answer

    def generate(
        self,
        instructions: str,
        input_text: str,
    ) -> GeneratedText:
        return GeneratedText(
            text=self.answer,
            model=self.model,
        )


def _unexpected_search(
    request: Any,
) -> LegalSearchResponse:
    """Fail the test if OpenSearch is called for an unsupported request."""

    raise AssertionError(
        "OpenSearch must not be called "
        "for an unsupported request."
    )


class ChatScopeTests(unittest.TestCase):
    """Tests for country-availability and legal-scope short-circuits."""

    def test_country_outside_corpus_returns_fallback_without_search(
        self,
    ) -> None:
        response = resolve_legal_chat_response(
            request=LegalChatRequest(
                question=(
                    "What are the overtime rules in Canada?"
                )
            ),
            catalog_provider=_catalog_provider,
            search_function=_unexpected_search,
        )

        self.assertFalse(
            response.grounded
        )

        self.assertEqual(
            response.retrieval_total,
            0,
        )

        self.assertEqual(
            response.sources,
            [],
        )

        self.assertIn(
            "Canada",
            response.answer,
        )

    def test_second_unavailable_country_returns_fallback(
        self,
    ) -> None:
        response = resolve_legal_chat_response(
            request=LegalChatRequest(
                question=(
                    "What are the tax rules in Germany?"
                )
            ),
            catalog_provider=_catalog_provider,
            search_function=_unexpected_search,
        )

        self.assertFalse(
            response.grounded
        )

        self.assertEqual(
            response.sources,
            [],
        )

        self.assertIn(
            "Germany",
            response.answer,
        )

    def test_mixed_available_and_unavailable_country(
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
                total=1,
                limit=request.limit,
                offset=0,
                took_ms=1,
                hits=[
                    _build_hit(
                        country_code="ES",
                        country="Spain",
                    )
                ],
            )

        client = FakeGenerationClient(
            answer="Supported by the extract [1]."
        )

        response = resolve_legal_chat_response(
            request=LegalChatRequest(
                question=(
                    "Compare overtime rules "
                    "in Spain and Canada."
                )
            ),
            catalog_provider=_catalog_provider,
            search_function=fake_search,
            generation_client=client,
        )

        self.assertEqual(
            len(captured_requests),
            1,
        )

        self.assertEqual(
            captured_requests[0].country_codes,
            [
                "ES",
            ],
        )

        self.assertEqual(
            [
                source.country_code
                for source in response.sources
            ],
            [
                "ES",
            ],
        )

        self.assertIn(
            "Canada",
            response.answer,
        )

    def test_tax_question_returns_fallback_without_search(
        self,
    ) -> None:
        response = resolve_legal_chat_response(
            request=LegalChatRequest(
                question=(
                    "What are the corporate income "
                    "tax rules in Spain?"
                ),
                country_codes=[
                    "ES",
                ],
            ),
            catalog_provider=_catalog_provider,
            search_function=_unexpected_search,
        )

        self.assertFalse(
            response.grounded
        )

        self.assertEqual(
            response.sources,
            [],
        )

    def test_vat_question_returns_fallback_without_search(
        self,
    ) -> None:
        response = resolve_legal_chat_response(
            request=LegalChatRequest(
                question="What is the VAT rate in Italy?",
                country_codes=[
                    "IT",
                ],
            ),
            catalog_provider=_catalog_provider,
            search_function=_unexpected_search,
        )

        self.assertFalse(
            response.grounded
        )

        self.assertEqual(
            response.sources,
            [],
        )

    def test_patents_question_returns_fallback_without_search(
        self,
    ) -> None:
        response = resolve_legal_chat_response(
            request=LegalChatRequest(
                question=(
                    "What about patents and inventions "
                    "for employees in Spain?"
                ),
                country_codes=[
                    "ES",
                ],
            ),
            catalog_provider=_catalog_provider,
            search_function=_unexpected_search,
        )

        self.assertFalse(
            response.grounded
        )

        self.assertEqual(
            response.sources,
            [],
        )

    def test_overview_question_is_allowed_through(
        self,
    ) -> None:
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
                    _build_hit(
                        country_code="ES",
                        country="Spain",
                    )
                ],
            )

        client = FakeGenerationClient(
            answer="Supported by the extract [1]."
        )

        response = resolve_legal_chat_response(
            request=LegalChatRequest(
                question="Employment law overview Spain",
                country_codes=[
                    "ES",
                ],
            ),
            catalog_provider=_catalog_provider,
            search_function=fake_search,
            generation_client=client,
        )

        self.assertTrue(
            response.grounded
        )

    def test_employee_monitoring_is_detected_and_allowed(
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
                total=1,
                limit=request.limit,
                offset=0,
                took_ms=1,
                hits=[
                    _build_hit(
                        country_code="ES",
                        country="Spain",
                    )
                ],
            )

        client = FakeGenerationClient(
            answer="Supported by the extract [1]."
        )

        resolve_legal_chat_response(
            request=LegalChatRequest(
                question=(
                    "Can an employer monitor "
                    "employee emails in Spain?"
                ),
                country_codes=[
                    "ES",
                ],
            ),
            catalog_provider=_catalog_provider,
            search_function=fake_search,
            generation_client=client,
        )

        self.assertEqual(
            captured_requests[0].legal_topics,
            [
                "Social Media and Data Privacy",
            ],
        )

    def test_six_country_comparison_still_covers_all_countries(
        self,
    ) -> None:
        codes = [
            "GB",
            "ES",
            "IT",
            "CZ",
            "SE",
            "CH",
        ]

        captured_requests: list[Any] = []

        def fake_search(
            request: Any,
        ) -> LegalSearchResponse:
            captured_requests.append(
                request
            )

            code = request.country_codes[0]

            return LegalSearchResponse(
                query=request.query,
                total=1,
                limit=request.limit,
                offset=0,
                took_ms=1,
                hits=[
                    _build_hit(
                        country_code=code,
                        country=code,
                    )
                ],
            )

        citations = " ".join(
            f"[{position}]"
            for position in range(1, 7)
        )

        client = FakeGenerationClient(
            answer=f"Supported by {citations}."
        )

        response = resolve_legal_chat_response(
            request=LegalChatRequest(
                question=(
                    "Compare notice periods "
                    "across these countries."
                ),
                country_codes=codes,
                max_sources=6,
            ),
            catalog_provider=_catalog_provider,
            search_function=fake_search,
            generation_client=client,
        )

        self.assertEqual(
            len(captured_requests),
            6,
        )

        self.assertEqual(
            sorted(
                source.country_code
                for source in response.sources
            ),
            sorted(
                codes
            ),
        )

    def test_max_sources_below_country_count_still_raises(
        self,
    ) -> None:
        with self.assertRaises(
            InvalidLegalChatRequestError
        ):
            resolve_legal_chat_response(
                request=LegalChatRequest(
                    question=(
                        "Compare notice periods "
                        "in the UK and Spain."
                    ),
                    country_codes=[
                        "GB",
                        "ES",
                    ],
                    max_sources=1,
                ),
                catalog_provider=_catalog_provider,
                search_function=_unexpected_search,
            )


if __name__ == "__main__":
    unittest.main()
