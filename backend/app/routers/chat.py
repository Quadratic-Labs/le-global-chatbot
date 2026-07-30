"""HTTP endpoint for grounded legal answers."""

from __future__ import annotations

from typing import Final

from fastapi import (
    APIRouter,
    HTTPException,
    status,
)

from app.clients.openai_responses import (
    OpenAIConfigurationError,
)
from app.core.config import get_settings
from app.models.chat import (
    LegalChatRequest,
    LegalChatResponse,
)
from app.services.country_detection import (
    CountryCatalogProvider,
    CountryDetectionError,
    resolve_country_availability,
    resolve_country_display_name,
)
from app.services.legal_catalog import (
    get_legal_catalog,
)
from app.services.legal_topic_detection import (
    resolve_legal_scope,
)
from app.services.rag_answer import (
    DEFAULT_MAX_CONTEXT_CHARACTERS,
    DEFAULT_MAX_SOURCE_CHARACTERS,
    NO_INFORMATION_ANSWER,
    InvalidLegalChatRequestError,
    RagAnswerError,
    SearchFunction,
    TextGenerationClient,
    answer_legal_question,
    search_legal_documents,
)


router = APIRouter(
    prefix="/api/v1",
    tags=["Legal Chat"],
)


UNAVAILABLE_COUNTRIES_ANSWER_TEMPLATE: Final[str] = (
    "The validated L&E Global corpus does not currently "
    "contain documents for {countries}. Please contact "
    "the relevant L&E Global member firm for "
    "country-specific legal advice."
)


def _format_country_list(
    display_names: list[str],
) -> str:
    """Join country display names into a readable list."""

    if len(display_names) == 1:
        return display_names[0]

    return (
        ", ".join(
            display_names[:-1]
        )
        + " and "
        + display_names[-1]
    )


def _unavailable_countries_answer(
    unavailable_codes: list[str],
) -> str:
    """Build the fallback answer naming the unavailable countries."""

    display_names = [
        resolve_country_display_name(
            country_code
        )
        for country_code in unavailable_codes
    ]

    return UNAVAILABLE_COUNTRIES_ANSWER_TEMPLATE.format(
        countries=_format_country_list(
            display_names
        )
    )


def resolve_legal_chat_response(
    request: LegalChatRequest,
    catalog_provider: CountryCatalogProvider = (
        get_legal_catalog
    ),
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
) -> LegalChatResponse:
    """
    Resolve one legal-chat request, applying scope checks first.

    Retrieval and generation are skipped entirely when every
    mentioned country is outside the corpus, or when the question
    carries no recognized legal topic and is not a general overview
    request. This avoids searching without a meaningful filter and
    citing unrelated passages.
    """

    country_scope = resolve_country_availability(
        request=request,
        catalog_provider=catalog_provider,
    )

    if (
        country_scope.unavailable_codes
        and not country_scope.available_codes
    ):
        return LegalChatResponse(
            question=request.question.strip(),
            answer=_unavailable_countries_answer(
                country_scope.unavailable_codes
            ),
            grounded=False,
            model=None,
            retrieval_total=0,
            sources=[],
        )

    legal_scope = resolve_legal_scope(
        request
    )

    if not legal_scope.is_supported:
        return LegalChatResponse(
            question=request.question.strip(),
            answer=NO_INFORMATION_ANSWER,
            grounded=False,
            model=None,
            retrieval_total=0,
            sources=[],
        )

    prepared_request = request.model_copy(
        update={
            "country_codes": (
                country_scope.available_codes
            ),
            "legal_topics": (
                legal_scope.legal_topics
            ),
        }
    )

    response = answer_legal_question(
        prepared_request,
        search_function=search_function,
        generation_client=generation_client,
        rerank_enabled=rerank_enabled,
        rerank_pool_multiplier=rerank_pool_multiplier,
        max_context_characters=max_context_characters,
        max_source_characters=max_source_characters,
    )

    if country_scope.unavailable_codes:
        note = (
            "\n\nNote: "
            + _unavailable_countries_answer(
                country_scope.unavailable_codes
            )
        )

        response = response.model_copy(
            update={
                "answer": response.answer + note,
            }
        )

    return response


@router.post(
    "/chat",
    response_model=LegalChatResponse,
    response_model_exclude_none=True,
)
def legal_chat(
    request: LegalChatRequest,
) -> LegalChatResponse:
    """Generate an answer grounded in validated documents."""

    settings = get_settings()

    try:
        return resolve_legal_chat_response(
            request,
            rerank_enabled=settings.rerank_enabled,
            rerank_pool_multiplier=(
                settings.rerank_pool_multiplier
            ),
            max_context_characters=(
                settings.rag_max_context_characters
            ),
            max_source_characters=(
                settings.rag_max_source_characters
            ),
        )

    except InvalidLegalChatRequestError as error:
        raise HTTPException(
            status_code=(
                status.HTTP_422_UNPROCESSABLE_ENTITY
            ),
            detail=str(
                error
            ),
        ) from error

    except OpenAIConfigurationError as error:
        raise HTTPException(
            status_code=(
                status.HTTP_503_SERVICE_UNAVAILABLE
            ),
            detail=(
                "The answer generation service "
                "is not configured."
            ),
        ) from error

    except CountryDetectionError as error:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(
                "The country detection service "
                "is temporarily unavailable."
            ),
        ) from error

    except RagAnswerError as error:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(
                "The grounded legal answer service "
                "is temporarily unavailable."
            ),
        ) from error
