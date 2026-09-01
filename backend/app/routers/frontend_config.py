"""HTTP endpoint exposing chatbot frontend configuration."""

from fastapi import (
    APIRouter,
    HTTPException,
    status,
)

from app.models.frontend import (
    FrontendConfigResponse,
)
from app.services.legal_catalog import (
    FrontendConfigError,
    get_frontend_config,
)


router = APIRouter(
    prefix="/api/v1",
    tags=["Frontend Configuration"],
)


@router.get(
    "/frontend-config",
    response_model=FrontendConfigResponse,
)
def frontend_config() -> FrontendConfigResponse:
    """Return the configuration needed by the chatbot UI."""

    try:
        return get_frontend_config()

    except FrontendConfigError as error:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(
                "The frontend configuration service "
                "is temporarily unavailable."
            ),
        ) from error