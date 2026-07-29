"""HTTP endpoints for legal document administration."""

from __future__ import annotations

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    UploadFile,
    status,
)

from app.core.config import get_settings
from app.models.admin_documents import (
    AdminDocumentListResponse,
    AdminDocumentUploadResponse,
)
from app.security.admin import (
    require_admin_key,
)
from app.services.admin_documents import (
    AdminDocumentCatalogError,
    AdminDocumentStorageError,
    InvalidDocumentUploadError,
    list_indexed_documents,
    upload_and_index_document,
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


@router.get(
    "/documents",
    response_model=AdminDocumentListResponse,
)
def get_admin_documents(
) -> AdminDocumentListResponse:
    """List documents currently indexed by OpenSearch."""

    settings = get_settings()

    try:
        return list_indexed_documents(
            source_directory=(
                settings.document_source_dir
            )
        )

    except AdminDocumentCatalogError as error:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(
                "The indexed document catalog "
                "is temporarily unavailable."
            ),
        ) from error


@router.post(
    "/documents",
    response_model=AdminDocumentUploadResponse,
    status_code=status.HTTP_201_CREATED,
)
def upload_admin_document(
    file: UploadFile = File(...),
) -> AdminDocumentUploadResponse:
    """Validate, persist, and index one DOCX document."""

    settings = get_settings()

    try:
        return upload_and_index_document(
            filename=file.filename or "",
            file_stream=file.file,
            source_directory=(
                settings.document_source_dir
            ),
            processed_directory=(
                settings.document_processed_dir
            ),
            maximum_bytes=(
                settings.document_upload_max_bytes
            ),
        )

    except InvalidDocumentUploadError as error:
        raise HTTPException(
            status_code=(
                status.HTTP_422_UNPROCESSABLE_ENTITY
            ),
            detail=str(
                error
            ),
        ) from error

    except DocumentIndexingError as error:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(
                "The document was valid but could "
                "not be indexed."
            ),
        ) from error

    except AdminDocumentStorageError as error:
        raise HTTPException(
            status_code=(
                status.HTTP_500_INTERNAL_SERVER_ERROR
            ),
            detail=(
                "The document could not be "
                "stored safely."
            ),
        ) from error