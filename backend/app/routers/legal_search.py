"""HTTP endpoints for legal document search."""

from fastapi import (
    APIRouter,
    HTTPException,
    status,
)

from app.models.search import (
    LegalSearchRequest,
    LegalSearchResponse,
)
from app.services.legal_search import (
    InvalidLegalSearchRequestError,
    LegalSearchError,
    search_legal_documents,
)


router = APIRouter(
    prefix="/api/v1",
    tags=["Legal Search"],
)


@router.post(
    "/legal-search",
    response_model=LegalSearchResponse,
    response_model_exclude_none=True,
)
def legal_search(
    request: LegalSearchRequest,
) -> LegalSearchResponse:
    """Search validated L&E Global legal content."""

    try:
        return search_legal_documents(
            request
        )

    except InvalidLegalSearchRequestError as error:
        raise HTTPException(
            status_code=(
                status.HTTP_422_UNPROCESSABLE_ENTITY
            ),
            detail=str(
                error
            ),
        ) from error

    except LegalSearchError as error:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(
                "The legal search service "
                "is temporarily unavailable."
            ),
        ) from error