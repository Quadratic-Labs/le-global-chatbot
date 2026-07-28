"""HTTP endpoint exposing the indexed legal catalog."""

from fastapi import (
    APIRouter,
    HTTPException,
    status,
)

from app.models.catalog import (
    LegalCatalogResponse,
)
from app.services.legal_catalog import (
    LegalCatalogError,
    get_legal_catalog,
)


router = APIRouter(
    prefix="/api/v1",
    tags=["Legal Catalog"],
)


@router.get(
    "/legal-catalog",
    response_model=LegalCatalogResponse,
)
def legal_catalog() -> LegalCatalogResponse:
    """Return countries and topics available in the corpus."""

    try:
        return get_legal_catalog()

    except LegalCatalogError as error:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(
                "The legal catalog service "
                "is temporarily unavailable."
            ),
        ) from error