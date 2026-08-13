"""HTTP endpoints for document reindexing and deletion."""

from __future__ import annotations

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    status,
)
from fastapi.responses import FileResponse

from app.core.admin_error_reporting import (
    admin_error_detail,
    log_admin_business_error,
)
from app.core.config import get_settings
from app.models.admin_document_lifecycle import (
    AdminDocumentDeleteResponse,
    AdminDocumentReindexResponse,
)
from app.models.admin_document_sections import (
    AdminDocumentSectionListResponse,
    AdminDocumentSectionResponse,
    AdminDocumentSectionUpdateRequest,
    AdminDocumentSectionUpdateResponse,
)
from app.security.admin import (
    require_admin_key,
)
from app.services.admin_document_lifecycle import (
    AdminDocumentLifecycleError,
    AdminDocumentNotFoundError,
    AdminDocumentRollbackError,
    AdminDocumentSourceConflictError,
    AdminDocumentSourceMissingError,
    InvalidAdminDocumentIdError,
    delete_indexed_document,
    get_document_download,
    reindex_indexed_document,
)
from app.services.admin_document_sections import (
    AdminDocumentSectionInvalidError,
    AdminDocumentSectionNotFoundError,
    AdminDocumentSectionUpdateFailedError,
    get_effective_section,
    list_effective_sections,
    update_effective_section,
)
from app.services.country_lock import (
    AdminDocumentOperationInProgressError,
)
from app.services.document_indexer import (
    DocumentIndexingError,
)


DOCX_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument"
    ".wordprocessingml.document"
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
        log_admin_business_error(
            operation="reindex",
            error=error,
            document_id=document_id,
        )

        raise HTTPException(
            status_code=(
                status.HTTP_422_UNPROCESSABLE_ENTITY
            ),
            detail=admin_error_detail(
                code="invalid_document_id",
                message=str(error),
                operation="reindex",
                document_id=document_id,
            ),
        ) from error

    except AdminDocumentNotFoundError as error:
        log_admin_business_error(
            operation="reindex",
            error=error,
            document_id=document_id,
        )

        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=admin_error_detail(
                code="document_not_found",
                message=str(error),
                operation="reindex",
                document_id=document_id,
            ),
        ) from error

    except AdminDocumentOperationInProgressError as error:
        log_admin_business_error(
            operation="reindex",
            error=error,
            document_id=document_id,
            country_code=error.country_code,
        )

        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=admin_error_detail(
                code="document_operation_in_progress",
                message=str(error),
                operation="reindex",
                document_id=document_id,
            ),
        ) from error

    except AdminDocumentSourceMissingError as error:
        log_admin_business_error(
            operation="reindex",
            error=error,
            document_id=document_id,
            country_code=getattr(error, "country_code", None),
        )

        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=admin_error_detail(
                code="source_missing",
                message=str(error),
                operation="reindex",
                document_id=document_id,
            ),
        ) from error

    except AdminDocumentSourceConflictError as error:
        log_admin_business_error(
            operation="reindex",
            error=error,
            document_id=document_id,
            country_code=getattr(error, "country_code", None),
        )

        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=admin_error_detail(
                code="source_conflict",
                message=str(error),
                operation="reindex",
                document_id=document_id,
            ),
        ) from error

    except AdminDocumentRollbackError as error:
        log_admin_business_error(
            operation="reindex",
            error=error,
            document_id=document_id,
            country_code=getattr(error, "country_code", None),
        )

        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=admin_error_detail(
                code="rollback_failed",
                message=str(error),
                operation="reindex",
                document_id=document_id,
            ),
        ) from error

    except (
        DocumentIndexingError,
        AdminDocumentLifecycleError,
    ) as error:
        log_admin_business_error(
            operation="reindex",
            error=error,
            document_id=document_id,
            country_code=getattr(error, "country_code", None),
        )

        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=admin_error_detail(
                code="document_reindex_failed",
                message=str(error),
                operation="reindex",
                document_id=document_id,
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
        log_admin_business_error(
            operation="delete",
            error=error,
            document_id=document_id,
        )

        raise HTTPException(
            status_code=(
                status.HTTP_422_UNPROCESSABLE_ENTITY
            ),
            detail=admin_error_detail(
                code="invalid_document_id",
                message=str(error),
                operation="delete",
                document_id=document_id,
            ),
        ) from error

    except AdminDocumentNotFoundError as error:
        log_admin_business_error(
            operation="delete",
            error=error,
            document_id=document_id,
        )

        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=admin_error_detail(
                code="document_not_found",
                message=str(error),
                operation="delete",
                document_id=document_id,
            ),
        ) from error

    except AdminDocumentOperationInProgressError as error:
        log_admin_business_error(
            operation="delete",
            error=error,
            document_id=document_id,
            country_code=error.country_code,
        )

        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=admin_error_detail(
                code="document_operation_in_progress",
                message=str(error),
                operation="delete",
                document_id=document_id,
            ),
        ) from error

    except AdminDocumentSourceConflictError as error:
        log_admin_business_error(
            operation="delete",
            error=error,
            document_id=document_id,
            country_code=getattr(error, "country_code", None),
        )

        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=admin_error_detail(
                code="source_conflict",
                message=str(error),
                operation="delete",
                document_id=document_id,
            ),
        ) from error

    except AdminDocumentRollbackError as error:
        log_admin_business_error(
            operation="delete",
            error=error,
            document_id=document_id,
            country_code=getattr(error, "country_code", None),
        )

        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=admin_error_detail(
                code="rollback_failed",
                message=str(error),
                operation="delete",
                document_id=document_id,
            ),
        ) from error

    except AdminDocumentLifecycleError as error:
        log_admin_business_error(
            operation="delete",
            error=error,
            document_id=document_id,
            country_code=getattr(error, "country_code", None),
        )

        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=admin_error_detail(
                code="document_delete_failed",
                message=str(error),
                operation="delete",
                document_id=document_id,
            ),
        ) from error


@router.get(
    "/documents/{document_id}/download",
)
def download_admin_document(
    document_id: str,
) -> FileResponse:
    """
    Stream the real source DOCX backing one document_id.

    The client supplies only document_id - never a path - and the
    exact same source resolver reindex/delete already trust decides
    which real file that is (mission "ORDER 3", section 25): no
    client-controlled path ever reaches the filesystem, so path
    traversal is structurally not possible here.
    """

    settings = get_settings()

    try:
        download = get_document_download(
            document_id=document_id,
            source_directory=(
                settings.document_source_dir
            ),
        )

        return FileResponse(
            path=download.path,
            media_type=DOCX_MEDIA_TYPE,
            filename=download.download_filename,
        )

    except InvalidAdminDocumentIdError as error:
        log_admin_business_error(
            operation="download",
            error=error,
            document_id=document_id,
        )

        raise HTTPException(
            status_code=(
                status.HTTP_422_UNPROCESSABLE_ENTITY
            ),
            detail=admin_error_detail(
                code="invalid_document_id",
                message=str(error),
                operation="download",
                document_id=document_id,
            ),
        ) from error

    except AdminDocumentNotFoundError as error:
        log_admin_business_error(
            operation="download",
            error=error,
            document_id=document_id,
        )

        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=admin_error_detail(
                code="document_not_found",
                message=str(error),
                operation="download",
                document_id=document_id,
            ),
        ) from error

    except AdminDocumentSourceMissingError as error:
        log_admin_business_error(
            operation="download",
            error=error,
            document_id=document_id,
            country_code=getattr(error, "country_code", None),
        )

        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=admin_error_detail(
                code="source_missing",
                message=str(error),
                operation="download",
                document_id=document_id,
            ),
        ) from error

    except AdminDocumentSourceConflictError as error:
        log_admin_business_error(
            operation="download",
            error=error,
            document_id=document_id,
            country_code=getattr(error, "country_code", None),
        )

        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=admin_error_detail(
                code="source_conflict",
                message=str(error),
                operation="download",
                document_id=document_id,
            ),
        ) from error

@router.get(
    "/documents/{document_id}/sections",
    response_model=AdminDocumentSectionListResponse,
)
def list_admin_document_sections(
    document_id: str,
) -> AdminDocumentSectionListResponse:
    """List every section that really exists in one document's
    current effective state (mission "ORDER 5C")."""

    try:
        return list_effective_sections(
            document_id=document_id,
        )

    except InvalidAdminDocumentIdError as error:
        log_admin_business_error(
            operation="section_list",
            error=error,
            document_id=document_id,
        )

        raise HTTPException(
            status_code=(
                status.HTTP_422_UNPROCESSABLE_ENTITY
            ),
            detail=admin_error_detail(
                code="invalid_document_id",
                message=str(error),
                operation="section_list",
                document_id=document_id,
            ),
        ) from error

    except AdminDocumentNotFoundError as error:
        log_admin_business_error(
            operation="section_list",
            error=error,
            document_id=document_id,
        )

        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=admin_error_detail(
                code="document_not_found",
                message=str(error),
                operation="section_list",
                document_id=document_id,
            ),
        ) from error

    except AdminDocumentLifecycleError as error:
        log_admin_business_error(
            operation="section_list",
            error=error,
            document_id=document_id,
        )

        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=admin_error_detail(
                code="document_catalog_unavailable",
                message=str(error),
                operation="section_list",
                document_id=document_id,
            ),
        ) from error


@router.get(
    "/documents/{document_id}/sections/{section_id}",
    response_model=AdminDocumentSectionResponse,
)
def get_admin_document_section(
    document_id: str,
    section_id: str,
) -> AdminDocumentSectionResponse:
    """The current effective content of one document section
    (mission "ORDER 5C")."""

    settings = get_settings()

    try:
        return get_effective_section(
            document_id=document_id,
            section_id=section_id,
            source_directory=settings.document_source_dir,
        )

    except InvalidAdminDocumentIdError as error:
        log_admin_business_error(
            operation="section_get",
            error=error,
            document_id=document_id,
        )

        raise HTTPException(
            status_code=(
                status.HTTP_422_UNPROCESSABLE_ENTITY
            ),
            detail=admin_error_detail(
                code="invalid_document_id",
                message=str(error),
                operation="section_get",
                document_id=document_id,
            ),
        ) from error

    except AdminDocumentNotFoundError as error:
        log_admin_business_error(
            operation="section_get",
            error=error,
            document_id=document_id,
        )

        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=admin_error_detail(
                code="document_not_found",
                message=str(error),
                operation="section_get",
                document_id=document_id,
            ),
        ) from error

    except AdminDocumentSectionNotFoundError as error:
        log_admin_business_error(
            operation="section_get",
            error=error,
            document_id=document_id,
        )

        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=error.to_detail(),
        ) from error

    except AdminDocumentLifecycleError as error:
        log_admin_business_error(
            operation="section_get",
            error=error,
            document_id=document_id,
        )

        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=admin_error_detail(
                code="document_catalog_unavailable",
                message=str(error),
                operation="section_get",
                document_id=document_id,
            ),
        ) from error


@router.put(
    "/documents/{document_id}/sections/{section_id}",
    response_model=AdminDocumentSectionUpdateResponse,
)
def update_admin_document_section(
    document_id: str,
    section_id: str,
    payload: AdminDocumentSectionUpdateRequest,
) -> AdminDocumentSectionUpdateResponse:
    """Save a new effective content for one existing section
    (mission "ORDER 5C"). Never creates a new legal_topic - only an
    already-existing section may be edited."""

    settings = get_settings()

    try:
        return update_effective_section(
            document_id=document_id,
            section_id=section_id,
            new_content=payload.content,
            source_directory=settings.document_source_dir,
        )

    except InvalidAdminDocumentIdError as error:
        log_admin_business_error(
            operation="section_update",
            error=error,
            document_id=document_id,
        )

        raise HTTPException(
            status_code=(
                status.HTTP_422_UNPROCESSABLE_ENTITY
            ),
            detail=admin_error_detail(
                code="invalid_document_id",
                message=str(error),
                operation="section_update",
                document_id=document_id,
            ),
        ) from error

    except AdminDocumentNotFoundError as error:
        log_admin_business_error(
            operation="section_update",
            error=error,
            document_id=document_id,
        )

        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=admin_error_detail(
                code="document_not_found",
                message=str(error),
                operation="section_update",
                document_id=document_id,
            ),
        ) from error

    except AdminDocumentSectionNotFoundError as error:
        log_admin_business_error(
            operation="section_update",
            error=error,
            document_id=document_id,
        )

        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=error.to_detail(),
        ) from error

    except AdminDocumentSectionInvalidError as error:
        log_admin_business_error(
            operation="section_update",
            error=error,
            document_id=document_id,
        )

        raise HTTPException(
            status_code=(
                status.HTTP_422_UNPROCESSABLE_ENTITY
            ),
            detail=error.to_detail(),
        ) from error

    except AdminDocumentOperationInProgressError as error:
        log_admin_business_error(
            operation="section_update",
            error=error,
            document_id=document_id,
            country_code=error.country_code,
        )

        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=admin_error_detail(
                code="document_operation_in_progress",
                message=str(error),
                operation="section_update",
                document_id=document_id,
            ),
        ) from error

    except AdminDocumentSectionUpdateFailedError as error:
        log_admin_business_error(
            operation="section_update",
            error=error,
            document_id=document_id,
        )

        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=error.to_detail(),
        ) from error

    except AdminDocumentRollbackError as error:
        log_admin_business_error(
            operation="section_update",
            error=error,
            document_id=document_id,
        )

        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=admin_error_detail(
                code="rollback_failed",
                message=str(error),
                operation="section_update",
                document_id=document_id,
            ),
        ) from error

    except AdminDocumentLifecycleError as error:
        log_admin_business_error(
            operation="section_update",
            error=error,
            document_id=document_id,
        )

        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=admin_error_detail(
                code="document_catalog_unavailable",
                message=str(error),
                operation="section_update",
                document_id=document_id,
            ),
        ) from error
