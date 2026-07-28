"""HTTP endpoint for grounded legal answers."""

from fastapi import (
    APIRouter,
    HTTPException,
    status,
)

from app.clients.openai_responses import (
    OpenAIConfigurationError,
)
from app.models.chat import (
    LegalChatRequest,
    LegalChatResponse,
)
from app.services.country_detection import (
    CountryDetectionError,
    prepare_legal_chat_request,
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
        prepared_request = (
            prepare_legal_chat_request(
                request
            )
        )

        return answer_legal_question(
            prepared_request
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