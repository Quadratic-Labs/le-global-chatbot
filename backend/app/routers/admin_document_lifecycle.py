"""HTTP endpoints for document reindexing and deletion."""

from __future__ import annotations

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    status,
)

from app.core.config import get_settings
from app.models.admin_document_lifecycle import (
    AdminDocumentDeleteResponse,
    AdminDocumentReindexResponse,
)
from app.security.admin import (
    require_admin_key,
)
from app.services.admin_document_lifecycle import (
    AdminDocumentLifecycleError,
    AdminDocumentNotFoundError,
    AdminDocumentSourceConflictError,
    AdminDocumentSourceMissingError,
    InvalidAdminDocumentIdError,
    delete_indexed_document,
    reindex_indexed_document,
)
from app.services.document_indexer import (
    DocumentIndexingError,
)


router = APIRouter(
    prefix="/api/v1/admin",
    tags=["Document Administration"],
    dependencies=[
        Depends(
            require_admin_key
        )
    ],
)


@router.post(
    "/documents/{document_id}/reindex",
    response_model=AdminDocumentReindexResponse,
)
def reindex_admin_document(
    document_id: str,
) -> AdminDocumentReindexResponse:
    """Reindex one document from its persisted source DOCX."""

    settings = get_settings()

    try:
        return reindex_indexed_document(
            document_id=document_id,
            source_directory=(
                settings.document_source_dir
            ),
        )

    except InvalidAdminDocumentIdError as error:
        raise HTTPException(
            status_code=(
                status.HTTP_422_UNPROCESSABLE_ENTITY
            ),
            detail=str(
                error
            ),
        ) from error

    except AdminDocumentNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(
                error
            ),
        ) from error

    except AdminDocumentSourceMissingError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(
                error
            ),
        ) from error

    except AdminDocumentSourceConflictError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(
                error
            ),
        ) from error

    except (
        DocumentIndexingError,
        AdminDocumentLifecycleError,
    ) as error:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(
                "The document could not be reindexed."
            ),
        ) from error


@router.delete(
    "/documents/{document_id}",
    response_model=AdminDocumentDeleteResponse,
)
def delete_admin_document(
    document_id: str,
) -> AdminDocumentDeleteResponse:
    """Delete one indexed document and its source DOCX."""

    settings = get_settings()

    try:
        return delete_indexed_document(
            document_id=document_id,
            source_directory=(
                settings.document_source_dir
            ),
            processed_directory=(
                settings.document_processed_dir
            ),
        )

    except InvalidAdminDocumentIdError as error:
        raise HTTPException(
            status_code=(
                status.HTTP_422_UNPROCESSABLE_ENTITY
            ),
            detail=str(
                error
            ),
        ) from error

    except AdminDocumentNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(
                error
            ),
        ) from error

    except AdminDocumentSourceConflictError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(
                error
            ),
        ) from error

    except AdminDocumentLifecycleError as error:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(
                "The document could not be deleted."
            ),
        ) from error