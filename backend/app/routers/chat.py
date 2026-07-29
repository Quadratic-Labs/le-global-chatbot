"""HTTP endpoint for grounded legal answers."""

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
    CountryDetectionError,
    prepare_legal_chat_request,
)
from app.services.legal_topic_detection import (
    prepare_legal_chat_topics,
)
from app.services.rag_answer import (
    InvalidLegalChatRequestError,
    RagAnswerError,
    answer_legal_question,
)


router = APIRouter(
    prefix="/api/v1",
    tags=["Legal Chat"],
)


@router.post(
    "/chat",
    response_model=LegalChatResponse,
    response_model_exclude_none=True,
)
def legal_chat(
    request: LegalChatRequest,
) -> LegalChatResponse:
    """Generate an answer grounded in validated documents."""

    try:
        country_prepared_request = (
            prepare_legal_chat_request(
                request
            )
        )

        prepared_request = (
            prepare_legal_chat_topics(
                country_prepared_request
            )
        )

        settings = get_settings()

        return answer_legal_question(
            prepared_request,
            rerank_enabled=settings.rerank_enabled,
            rerank_pool_multiplier=(
                settings.rerank_pool_multiplier
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