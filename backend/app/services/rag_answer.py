"""Generate grounded answers from retrieved legal chunks."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Sequence
from time import perf_counter
from typing import Final, Protocol

from app.clients.openai_responses import (
    GeneratedText,
    OpenAIConfigurationError,
    OpenAIResponseError,
    get_openai_answer_client,
    get_openai_rerank_client,
)
from app.core.country_registry import (
    COUNTRIES,
)
from app.services.chat_metrics import (
    LegalChatMetrics,
)
from app.models.chat import (
    LegalAnswerSource,
    LegalChatRequest,
    LegalChatResponse,
)
from app.models.search import (
    LegalSearchHit,
    LegalSearchRequest,
    LegalSearchResponse,
)
from app.services.legal_search import (
    LegalSearchError,
    search_legal_documents,
)


logger = logging.getLogger(__name__)


DEFAULT_MAX_CONTEXT_CHARACTERS: Final[int] = 16000
DEFAULT_MAX_SOURCE_CHARACTERS: Final[int] = 4000

MAX_RERANK_POOL_SIZE: Final[int] = 20
RERANK_SNIPPET_CHARACTERS: Final[int] = 1500

GENERIC_QUERY_TERMS: Final[frozenset[str]] = frozenset(
    {
        "compare",
        "comparison",
        "explain",
        "rules",
        "rule",
        "requirements",
        "requirement",
        "law",
        "laws",
        "the",
        "a",
        "an",
        "in",
        "on",
        "at",
        "for",
        "to",
        "of",
        "and",
        "or",
        "is",
        "are",
        "what",
        "which",
        "how",
    }
)

VALID_CITATION_PATTERN: Final[re.Pattern[str]] = (
    re.compile(
        r"\[(\d+(?:\s*,\s*\d+)*)\]"
    )
)

CITATION_LIKE_PATTERN: Final[re.Pattern[str]] = (
    re.compile(
        r"\[[0-9][0-9,\s;]*\]"
    )
)

RERANK_INSTRUCTIONS: Final[str] = """
Return ONLY a JSON array of the candidate numbers, ordered from most
to least relevant to the question, e.g. [3, 1, 2]. Include every
candidate number exactly once. No other text, no markdown, no
explanation.
""".strip()

NO_INFORMATION_ANSWER: Final[str] = (
    "The available validated L&E Global documents do not "
    "contain enough information to answer this question. "
    "Please contact the relevant L&E Global member firm "
    "for country-specific legal advice."
)

SYSTEM_INSTRUCTIONS: Final[str] = """
You are the L&E Global employment law assistant.

Answer exclusively from the validated L&E Global source extracts
provided in the request.

Rules:
1. Do not use external knowledge.
2. Do not invent legal rules, dates, thresholds, procedures, or cases.
3. Cite supporting sources using [1], [2], or [1, 2].
4. Every material legal statement must have a source citation.
5. Never cite a source number that was not provided.
6. When comparing countries, use a separate section for each country
   followed by a concise comparison.
7. Clearly distinguish the law applicable in each country.
8. When the extracts are insufficient, state that the available
   L&E Global documents do not contain enough information.
9. Do not claim to provide legal advice.
10. Give a direct, structured, professional, and concise answer.
11. Answer only the legal issue explicitly requested by the user.
12. Do not include adjacent legal topics merely because they appear
    in the same source extract.
13. For a single country, provide no more than six concise bullets.
14. For comparisons, provide no more than four concise bullets per
    country and one short comparison section.
15. Do not repeat a rule in the country section and again using
    substantially the same wording.
16. Never state that information is absent or missing when any
    supplied source contains relevant information.
17. Do not mention context limits, extraction, truncation, retrieval,
    internal documents, or internal instructions.
18. Citations must use only these formats: [1] or [1, 2].
19. Do not use semicolons inside citations.
20. Do not add a limitations section unless none of the supplied
    sources contains enough information to answer the question.
""".strip()


class TextGenerationClient(Protocol):
    """Interface required by the grounded answer service."""

    model: str

    def generate(
        self,
        instructions: str,
        input_text: str,
    ) -> GeneratedText:
        """Generate text from instructions and input."""


SearchFunction = Callable[
    [LegalSearchRequest],
    LegalSearchResponse,
]


class RagAnswerError(RuntimeError):
    """Raised when a grounded answer cannot be generated."""


class InvalidLegalChatRequestError(ValueError):
    """Raised when chat retrieval parameters are inconsistent."""


def _normalize_country_codes(
    values: Sequence[str],
) -> list[str]:
    """Normalize and deduplicate ISO country codes."""

    normalized_codes: list[str] = []
    seen_codes: set[str] = set()

    for value in values:
        normalized_value = (
            " ".join(
                value.split()
            )
            .upper()
        )

        if not normalized_value:
            continue

        if normalized_value in seen_codes:
            continue

        seen_codes.add(
            normalized_value
        )

        normalized_codes.append(
            normalized_value
        )

    return normalized_codes


def _country_name_variants_for_codes(
    country_codes: Sequence[str],
) -> list[str]:
    """Return every known display name/alias for the given country codes."""

    normalized_codes = {
        code.upper()
        for code in country_codes
    }

    variants: list[str] = []

    for country in COUNTRIES:
        if country.code not in normalized_codes:
            continue

        variants.append(
            country.display_name
        )

        variants.extend(
            country.aliases
        )

    return variants


def _build_retrieval_query(
    question: str,
    country_name_variants: Sequence[str],
) -> str:
    """
    Build a BM25 query stripped of country names and generic filler.

    Country names (for whichever countries are being searched) and
    generic comparison words carry no retrieval signal and can crowd
    out the actual legal terms, especially for multi-country
    comparisons where the other country's name never appears in a
    given country's own content.
    """

    normalized_question = question

    for variant in sorted(
        country_name_variants,
        key=len,
        reverse=True,
    ):
        normalized_question = re.sub(
            rf"\b{re.escape(variant)}\b",
            " ",
            normalized_question,
            flags=re.IGNORECASE,
        )

    words = [
        word
        for word in re.findall(
            r"[A-Za-z0-9'-]+",
            normalized_question,
        )
        if word.casefold() not in GENERIC_QUERY_TERMS
    ]

    cleaned_query = " ".join(
        words
    ).strip()

    if len(cleaned_query) < 2:
        return question.strip()

    return cleaned_query


def _build_search_request(
    query: str,
    request: LegalChatRequest,
    country_codes: list[str],
    limit: int,
) -> LegalSearchRequest:
    """Build one OpenSearch request from chat criteria."""

    return LegalSearchRequest(
        query=query,
        country_codes=country_codes,
        legal_topics=request.legal_topics,
        subsections=request.subsections,
        language=request.language,
        reference_year=request.reference_year,
        limit=limit,
        offset=0,
    )


def _interleave_hits(
    hit_groups: Sequence[list[LegalSearchHit]],
    limit: int,
) -> list[LegalSearchHit]:
    """
    Interleave country-specific rankings.

    This prevents every top result from coming from the
    first country in a comparison.
    """

    combined_hits: list[LegalSearchHit] = []
    seen_chunk_ids: set[str] = set()

    maximum_group_size = max(
        (
            len(
                hit_group
            )
            for hit_group in hit_groups
        ),
        default=0,
    )

    for rank in range(
        maximum_group_size
    ):
        for hit_group in hit_groups:
            if rank >= len(
                hit_group
            ):
                continue

            hit = hit_group[
                rank
            ]

            if hit.chunk_id in seen_chunk_ids:
                continue

            seen_chunk_ids.add(
                hit.chunk_id
            )

            combined_hits.append(
                hit
            )

            if len(
                combined_hits
            ) >= limit:
                return combined_hits

    return combined_hits


def _candidate_search_limit(
    fair_share_limit: int,
    rerank_enabled: bool,
    rerank_pool_multiplier: int,
) -> int:
    """Return how many candidates to fetch before reranking."""

    if not rerank_enabled:
        return fair_share_limit

    return min(
        fair_share_limit * max(1, rerank_pool_multiplier),
        MAX_RERANK_POOL_SIZE,
    )


def _build_rerank_input(
    question: str,
    hits: list[LegalSearchHit],
) -> str:
    """Build a compact prompt: question plus truncated candidate snippets."""

    blocks = [
        (
            f"[{position}] Country: {hit.country} | "
            f"Topic: {hit.legal_topic or 'n/a'}\n"
            f"{hit.content[:RERANK_SNIPPET_CHARACTERS]}"
        )
        for position, hit in enumerate(hits, start=1)
    ]

    return (
        "QUESTION:\n"
        + question.strip()
        + "\n\nCANDIDATES:\n"
        + "\n\n".join(blocks)
    )


def _parse_rerank_order(
    text: str,
    candidate_count: int,
) -> list[int] | None:
    """Parse a strict JSON array permutation of 1..candidate_count."""

    cleaned = text.strip().strip("`")

    if cleaned.lower().startswith("json"):
        cleaned = cleaned[4:].strip()

    try:
        parsed = json.loads(cleaned)
    except (json.JSONDecodeError, ValueError):
        return None

    if not isinstance(parsed, list) or not all(
        isinstance(value, int) and not isinstance(value, bool)
        for value in parsed
    ):
        return None

    if (
        len(parsed) != candidate_count
        or set(parsed) != set(range(1, candidate_count + 1))
    ):
        return None

    return parsed


def _rerank_hits(
    question: str,
    hits: list[LegalSearchHit],
    generation_client: TextGenerationClient | None,
) -> list[LegalSearchHit]:
    """
    Reorder hits by LLM-judged relevance.

    Never raises: any failure is logged and falls back to the
    original BM25 order, since reranking is a quality enhancement,
    not a correctness guarantee.
    """

    if len(hits) <= 1:
        return hits

    try:
        client = (
            generation_client
            if generation_client is not None
            else get_openai_rerank_client()
        )

        generated = client.generate(
            instructions=RERANK_INSTRUCTIONS,
            input_text=_build_rerank_input(
                question=question,
                hits=hits,
            ),
        )

    except (OpenAIConfigurationError, OpenAIResponseError) as error:
        logger.warning(
            "Legal search reranking call failed, "
            "falling back to BM25 order: %s",
            error,
        )
        return hits

    order = _parse_rerank_order(
        text=generated.text,
        candidate_count=len(hits),
    )

    if order is None:
        logger.warning(
            "Legal search reranking returned an unparsable "
            "response, falling back to BM25 order."
        )
        return hits

    return [hits[position - 1] for position in order]


def _retrieve_search_hits(
    request: LegalChatRequest,
    search_function: SearchFunction,
    generation_client: TextGenerationClient | None = None,
    rerank_enabled: bool = False,
    rerank_pool_multiplier: int = 1,
    metrics: LegalChatMetrics | None = None,
) -> tuple[int, list[LegalSearchHit]]:
    """
    Retrieve legal chunks.

    Multi-country questions are searched separately per country
    so every requested jurisdiction receives retrieval capacity.
    """

    country_codes = _normalize_country_codes(
        request.country_codes
    )

    retrieval_query = _build_retrieval_query(
        question=request.question,
        country_name_variants=(
            _country_name_variants_for_codes(
                country_codes
            )
        ),
    )

    if len(
        country_codes
    ) <= 1:
        search_started_at = perf_counter()

        response = search_function(
            _build_search_request(
                query=retrieval_query,
                request=request,
                country_codes=country_codes,
                limit=_candidate_search_limit(
                    fair_share_limit=request.max_sources,
                    rerank_enabled=rerank_enabled,
                    rerank_pool_multiplier=rerank_pool_multiplier,
                ),
            )
        )

        if metrics is not None:
            metrics.add_opensearch_seconds(
                perf_counter() - search_started_at
            )

        hits = response.hits

        if rerank_enabled:
            rerank_started_at = perf_counter()

            hits = _rerank_hits(
                question=request.question,
                hits=hits,
                generation_client=generation_client,
            )[: request.max_sources]

            if metrics is not None:
                metrics.add_rerank_seconds(
                    perf_counter() - rerank_started_at
                )

        return (
            response.total,
            hits,
        )

    if request.max_sources < len(
        country_codes
    ):
        raise InvalidLegalChatRequestError(
            "max_sources must be greater than or equal "
            "to the number of requested countries."
        )

    base_limit, remainder = divmod(
        request.max_sources,
        len(
            country_codes
        ),
    )

    retrieval_total = 0
    country_hit_groups: list[
        list[LegalSearchHit]
    ] = []

    for position, country_code in enumerate(
        country_codes
    ):
        country_limit = (
            base_limit
            + (
                1
                if position < remainder
                else 0
            )
        )

        search_started_at = perf_counter()

        response = search_function(
            _build_search_request(
                query=retrieval_query,
                request=request,
                country_codes=[
                    country_code
                ],
                limit=_candidate_search_limit(
                    fair_share_limit=country_limit,
                    rerank_enabled=rerank_enabled,
                    rerank_pool_multiplier=rerank_pool_multiplier,
                ),
            )
        )

        if metrics is not None:
            metrics.add_opensearch_seconds(
                perf_counter() - search_started_at
            )

        retrieval_total += (
            response.total
        )

        country_hits = response.hits

        if rerank_enabled:
            rerank_started_at = perf_counter()

            country_hits = _rerank_hits(
                question=request.question,
                hits=country_hits,
                generation_client=generation_client,
            )

            if metrics is not None:
                metrics.add_rerank_seconds(
                    perf_counter() - rerank_started_at
                )

        country_hit_groups.append(
            country_hits
        )

    return (
        retrieval_total,
        _interleave_hits(
            hit_groups=country_hit_groups,
            limit=request.max_sources,
        ),
    )


def _truncate_context(
    content: str,
    maximum_characters: int,
) -> str:
    """Silently truncate one extract at a paragraph boundary when possible."""

    normalized_content = content.strip()

    if len(normalized_content) <= maximum_characters:
        return normalized_content

    candidate = normalized_content[
        :maximum_characters
    ]

    paragraph_boundary = candidate.rfind(
        "\n\n"
    )

    if paragraph_boundary >= (
        maximum_characters // 2
    ):
        candidate = candidate[
            :paragraph_boundary
        ]

    return candidate.rstrip()


def _allocate_country_context_budgets(
    hits: list[LegalSearchHit],
    maximum_characters: int,
    maximum_source_characters: int,
) -> list[LegalSearchHit]:
    """
    Keep every retrieved hit, budgeted fairly per country, not per source.

    Splitting the total budget evenly across every hit penalizes
    countries with more sources: in a 3-country, 6-source comparison,
    an even per-source split gives each source only a sixth of the
    budget, which can truncate a country's second-ranked extract away
    entirely even though it holds material the model needs. Splitting
    per country instead gives every requested country the same total
    allowance, spent first on its best-ranked hit.
    """

    if not hits:
        return []

    hits_by_country: dict[
        str,
        list[LegalSearchHit],
    ] = {}

    for hit in hits:
        hits_by_country.setdefault(
            hit.country_code,
            [],
        ).append(hit)

    country_budget = max(
        1,
        maximum_characters
        // len(hits_by_country),
    )

    selected_hits: list[LegalSearchHit] = []

    for country_hits in hits_by_country.values():
        remaining_budget = country_budget

        for position, hit in enumerate(country_hits):
            if remaining_budget <= 0:
                break

            remaining_sources = (
                len(country_hits) - position
            )

            if position == 0:
                source_budget = min(
                    maximum_source_characters,
                    remaining_budget,
                )
            else:
                source_budget = min(
                    maximum_source_characters,
                    max(
                        1,
                        remaining_budget
                        // remaining_sources,
                    ),
                )

            selected_hits.append(
                hit.model_copy(
                    update={
                        "content": _truncate_context(
                            content=hit.content,
                            maximum_characters=(
                                source_budget
                            ),
                        )
                    }
                )
            )

            remaining_budget -= source_budget

    return selected_hits


def _build_context(
    hits: list[LegalSearchHit],
) -> str:
    """Build numbered source extracts for the model."""

    source_blocks: list[str] = []

    for citation, hit in enumerate(
        hits,
        start=1,
    ):
        source_blocks.append(
            "\n".join(
                [
                    f"[SOURCE {citation}]",
                    f"Country: {hit.country}",
                    (
                        "Country code: "
                        f"{hit.country_code}"
                    ),
                    (
                        "Legal topic: "
                        f"{hit.legal_topic or 'Not specified'}"
                    ),
                    f"Section: {hit.section}",
                    (
                        "Subsection: "
                        f"{hit.subsection or 'Not specified'}"
                    ),
                    (
                        "Reference year: "
                        f"{hit.reference_year or 'Not specified'}"
                    ),
                    (
                        "Source file: "
                        f"{hit.source_filename}"
                    ),
                    "Extract:",
                    hit.content,
                ]
            )
        )

    return "\n\n".join(
        source_blocks
    )


def _build_model_input(
    request: LegalChatRequest,
    hits: list[LegalSearchHit],
) -> str:
    """Build the complete grounded generation input."""

    return "\n\n".join(
        [
            "USER QUESTION",
            request.question.strip(),
            "VALIDATED L&E GLOBAL SOURCES",
            _build_context(
                hits
            ),
            (
                "Write the answer using only the source "
                "extracts above. Cite every material legal "
                "statement using source numbers such as "
                "[1], [2], or [1, 2]."
            ),
        ]
    )


def _validate_citation_format(
    answer: str,
) -> None:
    """
    Reject citation-like substrings that are not a valid citation.

    Catches malformed citations such as "[1, 3; 2]" or "[4; 1, 3]"
    that a lenient extraction regex would otherwise silently ignore,
    leaving garbled citation syntax visible in the final answer.
    """

    for match in CITATION_LIKE_PATTERN.finditer(
        answer
    ):
        citation_text = match.group(0)

        if not VALID_CITATION_PATTERN.fullmatch(
            citation_text
        ):
            raise RagAnswerError(
                "The generated answer contains "
                "an invalid citation format."
            )


def _extract_citation_numbers(
    answer: str,
    source_count: int,
) -> list[int]:
    """
    Extract and validate source numbers cited by the model.

    Only citations that correspond to supplied sources are accepted.
    """

    citation_numbers: list[int] = []
    seen_citations: set[int] = set()

    for citation_group in VALID_CITATION_PATTERN.findall(
        answer
    ):
        for raw_citation in citation_group.split(
            ","
        ):
            citation = int(
                raw_citation.strip()
            )

            if (
                citation < 1
                or citation > source_count
            ):
                raise RagAnswerError(
                    "The generated answer cited an "
                    "unknown source number."
                )

            if citation in seen_citations:
                continue

            seen_citations.add(
                citation
            )

            citation_numbers.append(
                citation
            )

    if not citation_numbers:
        raise RagAnswerError(
            "The generated answer did not include "
            "a valid source citation."
        )

    return citation_numbers


def _build_cited_sources(
    hits: list[LegalSearchHit],
    citation_numbers: Sequence[int],
) -> list[LegalAnswerSource]:
    """Return only sources cited in the generated answer."""

    sources: list[LegalAnswerSource] = []

    for citation in citation_numbers:
        hit = hits[
            citation - 1
        ]

        sources.append(
            LegalAnswerSource(
                citation=citation,
                document_id=hit.document_id,
                chunk_id=hit.chunk_id,
                country=hit.country,
                country_code=hit.country_code,
                legal_topic=hit.legal_topic,
                section=hit.section,
                subsection=hit.subsection,
                source_filename=(
                    hit.source_filename
                ),
                reference_year=(
                    hit.reference_year
                ),
                score=hit.score,
            )
        )

    return sources


def answer_legal_question(
    request: LegalChatRequest,
    search_function: SearchFunction = (
        search_legal_documents
    ),
    generation_client: (
        TextGenerationClient | None
    ) = None,
    rerank_enabled: bool = False,
    rerank_pool_multiplier: int = 1,
    max_context_characters: int = (
        DEFAULT_MAX_CONTEXT_CHARACTERS
    ),
    max_source_characters: int = (
        DEFAULT_MAX_SOURCE_CHARACTERS
    ),
    metrics: LegalChatMetrics | None = None,
) -> LegalChatResponse:
    """Retrieve legal chunks and generate one grounded answer."""

    try:
        (
            retrieval_total,
            retrieved_hits,
        ) = _retrieve_search_hits(
            request=request,
            search_function=search_function,
            generation_client=generation_client,
            rerank_enabled=rerank_enabled,
            rerank_pool_multiplier=rerank_pool_multiplier,
            metrics=metrics,
        )

    except LegalSearchError as error:
        raise RagAnswerError(
            "Legal document retrieval failed."
        ) from error

    selected_hits = _allocate_country_context_budgets(
        hits=retrieved_hits,
        maximum_characters=max_context_characters,
        maximum_source_characters=max_source_characters,
    )

    if not selected_hits:
        if metrics is not None:
            metrics.outcome = "empty_retrieval"
            metrics.retrieval_total = retrieval_total
            metrics.selected_sources = 0

        return LegalChatResponse(
            question=request.question.strip(),
            answer=NO_INFORMATION_ANSWER,
            grounded=False,
            model=None,
            retrieval_total=retrieval_total,
            sources=[],
        )

    client = (
        generation_client
        if generation_client is not None
        else get_openai_answer_client()
    )

    try:
        openai_started_at = perf_counter()

        generated_text = client.generate(
            instructions=SYSTEM_INSTRUCTIONS,
            input_text=_build_model_input(
                request=request,
                hits=selected_hits,
            ),
        )

        if metrics is not None:
            metrics.openai_ms = (
                perf_counter() - openai_started_at
            ) * 1000

    except OpenAIResponseError as error:
        raise RagAnswerError(
            "Grounded answer generation failed."
        ) from error

    _validate_citation_format(
        answer=generated_text.text
    )

    citation_numbers = (
        _extract_citation_numbers(
            answer=generated_text.text,
            source_count=len(
                selected_hits
            ),
        )
    )

    if metrics is not None:
        metrics.outcome = "generated"
        metrics.retrieval_total = retrieval_total
        metrics.selected_sources = len(
            selected_hits
        )
        metrics.model = generated_text.model

    return LegalChatResponse(
        question=request.question.strip(),
        answer=generated_text.text,
        grounded=True,
        model=generated_text.model,
        retrieval_total=retrieval_total,
        sources=_build_cited_sources(
            hits=selected_hits,
            citation_numbers=citation_numbers,
        ),
    )