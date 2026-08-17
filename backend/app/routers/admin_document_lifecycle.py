"""HTTP endpoints for document reindexing and deletion."""

from __future__ import annotations

import os

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    status,
)
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

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
    AdminDocumentSectionAddRequest,
    AdminDocumentSectionAddResponse,
    AdminDocumentSectionDeleteResponse,
    AdminDocumentSectionListResponse,
    AdminDocumentSectionResponse,
    AdminDocumentSectionUpdateRequest,
    AdminDocumentSectionUpdateResponse,
)
from app.security.admin import (
    require_admin_key,
)
from app.services.admin_document_lifecycle import (
    AdminDocumentCountryConflictError,
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
    AdminDocumentSectionAlreadyExistsError,
    AdminDocumentSectionInvalidError,
    AdminDocumentSectionLastRemainingError,
    AdminDocumentSectionNotFoundError,
    AdminDocumentSectionPositionError,
    AdminDocumentSectionUpdateFailedError,
    add_new_section,
    delete_section,
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

    except AdminDocumentCountryConflictError as error:
        log_admin_business_error(
            operation="reindex",
            error=error,
            document_id=document_id,
            country_code=error.country_code,
        )

        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=error.to_detail(),
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
    Stream the effective (current) DOCX backing one document_id.

    The client supplies only document_id - never a path - and the
    exact same source resolver reindex/delete already trust decides
    which real file that is (mission "ORDER 3", section 25): no
    client-controlled path ever reaches the filesystem, so path
    traversal is structurally not possible here.

    When the document has structured Contact state, the streamed file
    is a temporary "effective" copy materializing that CURRENT state
    into the document's existing legal content (mission "ORDER
    8G-B2.1") - the persisted source itself is never modified, and the
    temporary file is deleted once the response has been fully sent.
    """

    settings = get_settings()

    try:
        download = get_document_download(
            document_id=document_id,
            source_directory=(
                settings.document_source_dir
            ),
        )

        cleanup_path = download.cleanup_path

        return FileResponse(
            path=download.path,
            media_type=DOCX_MEDIA_TYPE,
            filename=download.download_filename,
            background=(
                BackgroundTask(
                    os.unlink,
                    cleanup_path,
                )
                if cleanup_path is not None
                else None
            ),
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
    """List every top-level legal topic that really exists in the
    document's CURRENT DOCX right now (ORDER 8A, section 6)."""

    settings = get_settings()

    try:
        return list_effective_sections(
            document_id=document_id,
            source_directory=settings.document_source_dir,
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
    """Save a new effective content for one existing section (mission
    "ORDER 5C"), optionally renaming it in the same save (mission
    "ORDER 8G-A") - an omitted or effectively-unchanged title is a
    normal content-only edit, never a rename."""

    settings = get_settings()

    try:
        return update_effective_section(
            document_id=document_id,
            section_id=section_id,
            new_content=payload.content,
            new_title=payload.title,
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

    except AdminDocumentSectionAlreadyExistsError as error:
        log_admin_business_error(
            operation="section_update",
            error=error,
            document_id=document_id,
        )

        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
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

    except AdminDocumentCountryConflictError as error:
        log_admin_business_error(
            operation="section_update",
            error=error,
            document_id=document_id,
            country_code=error.country_code,
        )

        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=error.to_detail(),
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


@router.post(
    "/documents/{document_id}/sections",
    response_model=AdminDocumentSectionAddResponse,
)
def add_admin_document_section(
    document_id: str,
    payload: AdminDocumentSectionAddRequest,
) -> AdminDocumentSectionAddResponse:
    """Add a brand-new top-level legal topic to the current DOCX
    (ORDER 8A, sections 9-11). position must be exactly one of
    "beginning", "end", or "after:<section_id>"."""

    settings = get_settings()

    try:
        return add_new_section(
            document_id=document_id,
            title=payload.title,
            content=payload.content,
            position=payload.position,
            source_directory=settings.document_source_dir,
        )

    except InvalidAdminDocumentIdError as error:
        log_admin_business_error(
            operation="section_add",
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
                operation="section_add",
                document_id=document_id,
            ),
        ) from error

    except AdminDocumentNotFoundError as error:
        log_admin_business_error(
            operation="section_add",
            error=error,
            document_id=document_id,
        )

        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=admin_error_detail(
                code="document_not_found",
                message=str(error),
                operation="section_add",
                document_id=document_id,
            ),
        ) from error

    except AdminDocumentSectionNotFoundError as error:
        log_admin_business_error(
            operation="section_add",
            error=error,
            document_id=document_id,
        )

        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=error.to_detail(),
        ) from error

    except AdminDocumentSectionAlreadyExistsError as error:
        log_admin_business_error(
            operation="section_add",
            error=error,
            document_id=document_id,
        )

        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=error.to_detail(),
        ) from error

    except (
        AdminDocumentSectionInvalidError,
        AdminDocumentSectionPositionError,
    ) as error:
        log_admin_business_error(
            operation="section_add",
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
            operation="section_add",
            error=error,
            document_id=document_id,
            country_code=error.country_code,
        )

        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=admin_error_detail(
                code="document_operation_in_progress",
                message=str(error),
                operation="section_add",
                document_id=document_id,
            ),
        ) from error

    except AdminDocumentCountryConflictError as error:
        log_admin_business_error(
            operation="section_add",
            error=error,
            document_id=document_id,
            country_code=error.country_code,
        )

        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=error.to_detail(),
        ) from error

    except AdminDocumentSectionUpdateFailedError as error:
        log_admin_business_error(
            operation="section_add",
            error=error,
            document_id=document_id,
        )

        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=error.to_detail(),
        ) from error

    except AdminDocumentRollbackError as error:
        log_admin_business_error(
            operation="section_add",
            error=error,
            document_id=document_id,
        )

        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=admin_error_detail(
                code="rollback_failed",
                message=str(error),
                operation="section_add",
                document_id=document_id,
            ),
        ) from error

    except AdminDocumentLifecycleError as error:
        log_admin_business_error(
            operation="section_add",
            error=error,
            document_id=document_id,
        )

        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=admin_error_detail(
                code="document_catalog_unavailable",
                message=str(error),
                operation="section_add",
                document_id=document_id,
            ),
        ) from error


@router.delete(
    "/documents/{document_id}/sections/{section_id}",
    response_model=AdminDocumentSectionDeleteResponse,
)
def delete_admin_document_section(
    document_id: str,
    section_id: str,
) -> AdminDocumentSectionDeleteResponse:
    """Permanently remove one top-level legal section from the current
    DOCX (mission "ORDER 8G-A"). Blocks deleting the document's last
    remaining usable section."""

    settings = get_settings()

    try:
        return delete_section(
            document_id=document_id,
            section_id=section_id,
            source_directory=settings.document_source_dir,
        )

    except InvalidAdminDocumentIdError as error:
        log_admin_business_error(
            operation="section_delete",
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
                operation="section_delete",
                document_id=document_id,
            ),
        ) from error

    except AdminDocumentNotFoundError as error:
        log_admin_business_error(
            operation="section_delete",
            error=error,
            document_id=document_id,
        )

        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=admin_error_detail(
                code="document_not_found",
                message=str(error),
                operation="section_delete",
                document_id=document_id,
            ),
        ) from error

    except AdminDocumentSectionNotFoundError as error:
        log_admin_business_error(
            operation="section_delete",
            error=error,
            document_id=document_id,
        )

        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=error.to_detail(),
        ) from error

    except AdminDocumentSectionLastRemainingError as error:
        log_admin_business_error(
            operation="section_delete",
            error=error,
            document_id=document_id,
        )

        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=error.to_detail(),
        ) from error

    except AdminDocumentOperationInProgressError as error:
        log_admin_business_error(
            operation="section_delete",
            error=error,
            document_id=document_id,
            country_code=error.country_code,
        )

        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=admin_error_detail(
                code="document_operation_in_progress",
                message=str(error),
                operation="section_delete",
                document_id=document_id,
            ),
        ) from error

    except AdminDocumentCountryConflictError as error:
        log_admin_business_error(
            operation="section_delete",
            error=error,
            document_id=document_id,
            country_code=error.country_code,
        )

        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=error.to_detail(),
        ) from error

    except AdminDocumentSectionUpdateFailedError as error:
        log_admin_business_error(
            operation="section_delete",
            error=error,
            document_id=document_id,
        )

        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=error.to_detail(),
        ) from error

    except AdminDocumentRollbackError as error:
        log_admin_business_error(
            operation="section_delete",
            error=error,
            document_id=document_id,
        )

        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=admin_error_detail(
                code="rollback_failed",
                message=str(error),
                operation="section_delete",
                document_id=document_id,
            ),
        ) from error

    except AdminDocumentLifecycleError as error:
        log_admin_business_error(
            operation="section_delete",
            error=error,
            document_id=document_id,
        )

        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=admin_error_detail(
                code="document_catalog_unavailable",
                message=str(error),
                operation="section_delete",
                document_id=document_id,
            ),
        ) from error
