"""HTTP endpoints for legal document administration."""

from __future__ import annotations

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    UploadFile,
    status,
)

from app.core.admin_error_reporting import (
    admin_error_detail,
    log_admin_business_error,
)
from app.core.config import get_settings
from app.models.admin_documents import (
    AdminDocumentListResponse,
    AdminDocumentStatsResponse,
    AdminDocumentUploadResponse,
)
from app.security.admin import (
    require_admin_key,
)
from app.services.admin_documents import (
    AdminDocumentCatalogError,
    AdminDocumentStorageError,
    DocumentCorruptError,
    DocumentCountryUndeterminedError,
    DocumentEmptyError,
    DocumentParseFailedError,
    DocumentTooLargeError,
    InvalidDocumentUploadError,
    InvalidExtensionError,
    get_admin_document_stats,
    list_indexed_documents,
)
from app.services.admin_document_replacement import (
    AdminDocumentAlreadyCurrentError,
    AdminDocumentCountryConfirmationRequiredError,
    AdminDocumentCountryConflictReviewRequiredError,
    AdminDocumentCountryNotAllowedError,
    AdminDocumentCountrySelectionInvalidError,
    AdminDocumentCountrySelectionRequiredError,
    AdminDocumentIdenticalButAdminModifiedError,
    AdminDocumentReplacementRequiredError,
    AdminDocumentWarningConfirmationRequiredError,
    safe_upload_and_index_document,
)
from app.services.country_lock import (
    AdminDocumentOperationInProgressError,
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
        log_admin_business_error(
            operation="list",
            error=error,
        )

        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=admin_error_detail(
                code="document_catalog_unavailable",
                message=str(error),
                operation="list",
            ),
        ) from error


@router.get(
    "/documents/stats",
    response_model=AdminDocumentStatsResponse,
)
def get_admin_document_stats_route(
) -> AdminDocumentStatsResponse:
    """Aggregate counts over the indexed document catalog."""

    settings = get_settings()

    try:
        return get_admin_document_stats(
            source_directory=(
                settings.document_source_dir
            )
        )

    except AdminDocumentCatalogError as error:
        log_admin_business_error(
            operation="stats",
            error=error,
        )

        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=admin_error_detail(
                code="document_catalog_unavailable",
                message=str(error),
                operation="stats",
            ),
        ) from error


@router.post(
    "/documents",
    response_model=AdminDocumentUploadResponse,
    status_code=status.HTTP_201_CREATED,
)
def upload_admin_document(
    file: UploadFile = File(...),
    replace_existing: bool = Form(False),
    confirm_warnings: bool = Form(False),
    country_confirmed: bool = Form(False),
    selected_country_code: str | None = Form(None),
    confirm_contact_reseed: bool = Form(False),
) -> AdminDocumentUploadResponse:
    """Validate, persist, and index one DOCX document."""

    settings = get_settings()

    try:
        return safe_upload_and_index_document(
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
            replace_existing=replace_existing,
            confirm_warnings=confirm_warnings,
            country_confirmed=country_confirmed,
            selected_country_code=selected_country_code,
            confirm_contact_reseed=confirm_contact_reseed,
        )

    except AdminDocumentIdenticalButAdminModifiedError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=error.to_detail(),
        ) from error

    except AdminDocumentCountryConflictReviewRequiredError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=error.to_detail(),
        ) from error

    except AdminDocumentWarningConfirmationRequiredError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=error.to_detail(),
        ) from error

    except AdminDocumentReplacementRequiredError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=error.to_detail(),
        ) from error

    except AdminDocumentAlreadyCurrentError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=error.to_detail(),
        ) from error

    except AdminDocumentCountryConfirmationRequiredError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=error.to_detail(),
        ) from error

    except AdminDocumentCountrySelectionRequiredError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=error.to_detail(),
        ) from error

    except AdminDocumentCountrySelectionInvalidError as error:
        log_admin_business_error(
            operation="upload",
            error=error,
            country_code=error.country_code,
        )

        raise HTTPException(
            status_code=(
                status.HTTP_422_UNPROCESSABLE_ENTITY
            ),
            detail=error.to_detail(),
        ) from error

    except AdminDocumentCountryNotAllowedError as error:
        log_admin_business_error(
            operation="upload",
            error=error,
            country_code=error.country_code,
        )

        raise HTTPException(
            status_code=(
                status.HTTP_422_UNPROCESSABLE_ENTITY
            ),
            detail=error.to_detail(),
        ) from error

    except AdminDocumentOperationInProgressError as error:
        log_admin_business_error(
            operation="upload",
            error=error,
            country_code=error.country_code,
        )

        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=admin_error_detail(
                code="document_operation_in_progress",
                message=str(error),
                operation="upload",
            ),
        ) from error

    except DocumentTooLargeError as error:
        log_admin_business_error(
            operation="upload",
            error=error,
        )

        detail = admin_error_detail(
            code="document_too_large",
            message=str(error),
            operation="upload",
        )
        detail["max_bytes"] = error.maximum_bytes
        detail["max_mb"] = error.maximum_megabytes

        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=detail,
        ) from error

    except InvalidExtensionError as error:
        log_admin_business_error(
            operation="upload",
            error=error,
        )

        raise HTTPException(
            status_code=(
                status.HTTP_422_UNPROCESSABLE_ENTITY
            ),
            detail=admin_error_detail(
                code="invalid_document_type",
                message=str(error),
                operation="upload",
            ),
        ) from error

    except DocumentEmptyError as error:
        log_admin_business_error(
            operation="upload",
            error=error,
        )

        raise HTTPException(
            status_code=(
                status.HTTP_422_UNPROCESSABLE_ENTITY
            ),
            detail=admin_error_detail(
                code="document_empty",
                message=str(error),
                operation="upload",
            ),
        ) from error

    except DocumentCorruptError as error:
        log_admin_business_error(
            operation="upload",
            error=error,
        )

        raise HTTPException(
            status_code=(
                status.HTTP_422_UNPROCESSABLE_ENTITY
            ),
            detail=admin_error_detail(
                code="document_corrupt",
                message=str(error),
                operation="upload",
            ),
        ) from error

    except DocumentCountryUndeterminedError as error:
        log_admin_business_error(
            operation="upload",
            error=error,
        )

        raise HTTPException(
            status_code=(
                status.HTTP_422_UNPROCESSABLE_ENTITY
            ),
            detail=admin_error_detail(
                code="document_country_undetermined",
                message=str(error),
                operation="upload",
            ),
        ) from error

    except DocumentParseFailedError as error:
        log_admin_business_error(
            operation="upload",
            error=error,
        )

        raise HTTPException(
            status_code=(
                status.HTTP_422_UNPROCESSABLE_ENTITY
            ),
            detail=admin_error_detail(
                code="document_parse_failed",
                message=str(error),
                operation="upload",
            ),
        ) from error

    except InvalidDocumentUploadError as error:
        log_admin_business_error(
            operation="upload",
            error=error,
        )

        raise HTTPException(
            status_code=(
                status.HTTP_422_UNPROCESSABLE_ENTITY
            ),
            detail=admin_error_detail(
                code="document_validation_failed",
                message=str(error),
                operation="upload",
            ),
        ) from error

    except DocumentIndexingError as error:
        log_admin_business_error(
            operation="upload",
            error=error,
        )

        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=admin_error_detail(
                code="document_indexing_failed",
                message=str(error),
                operation="upload",
            ),
        ) from error

    except AdminDocumentStorageError as error:
        log_admin_business_error(
            operation="upload",
            error=error,
        )

        raise HTTPException(
            status_code=(
                status.HTTP_500_INTERNAL_SERVER_ERROR
            ),
            detail=admin_error_detail(
                code="document_storage_failed",
                message=str(error),
                operation="upload",
            ),
        ) from error
