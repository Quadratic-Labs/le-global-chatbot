"""HTTP endpoints for generic country-conflict review and resolution."""

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
from app.core.country_registry import (
    CountryRegistryError,
)
from app.models.admin_documents import (
    AdminCountryConflictResolutionResponse,
    AdminCountryConflictReviewResponse,
    AdminDocumentUploadResponse,
)
from app.security.admin import require_admin_key
from app.services.admin_document_conflict_resolution import (
    CountryConflictNotFoundError,
    CountryConflictResolutionError,
    build_country_conflict_review,
    resolve_country_conflict as resolve_country_conflict_service,
)
from app.services.admin_document_replacement import (
    AdminDocumentAlreadyCurrentError,
    AdminDocumentCountryConfirmationRequiredError,
    AdminDocumentCountryConflictReviewRequiredError,
    AdminDocumentCountryNotAllowedError,
    AdminDocumentCountrySelectionInvalidError,
    AdminDocumentCountrySelectionRequiredError,
    AdminDocumentReplacementRequiredError,
    AdminDocumentUnexpectedCountryError,
    AdminDocumentWarningConfirmationRequiredError,
    safe_upload_and_index_document,
)
from app.services.admin_documents import (
    AdminDocumentStorageError,
    DocumentCorruptError,
    DocumentCountryUndeterminedError,
    DocumentEmptyError,
    DocumentParseFailedError,
    DocumentTooLargeError,
    InvalidDocumentUploadError,
    InvalidExtensionError,
)
from app.services.country_lock import (
    AdminDocumentOperationInProgressError,
)
from app.services.document_indexer import DocumentIndexingError


router = APIRouter(
    prefix="/api/v1/admin",
    tags=["Document Administration"],
    dependencies=[Depends(require_admin_key)],
)


REPLACE_WITH_DOCUMENT = "REPLACE_WITH_DOCUMENT"


@router.get(
    "/documents/countries/{country_code}/conflict-review",
    response_model=AdminCountryConflictReviewResponse,
)
def get_country_conflict_review(
    country_code: str,
) -> AdminCountryConflictReviewResponse:
    """Read-only review of one country's current active conflict."""

    settings = get_settings()

    try:
        review = build_country_conflict_review(
            country_code,
            source_directory=settings.document_source_dir,
        )

    except CountryConflictNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=error.to_detail(),
        ) from error

    except CountryRegistryError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=admin_error_detail(
                code="unknown_country_code",
                message=str(error),
                operation="conflict_review",
            ),
        ) from error

    except AdminDocumentStorageError as error:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=admin_error_detail(
                code="document_storage_failed",
                message=str(error),
                operation="conflict_review",
            ),
        ) from error

    return AdminCountryConflictReviewResponse(
        country=review.country,
        country_code=review.country_code,
        candidates=[
            {
                "document_id": candidate.document_id,
                "source_filename": candidate.source_filename,
                "reference_year": candidate.reference_year,
                "updated_at": candidate.updated_at,
                "source_bytes": candidate.source_bytes,
            }
            for candidate in review.candidates
        ],
        auto_deduplicate_available=review.auto_deduplicate_available,
    )


@router.post(
    "/documents/countries/{country_code}/resolve-conflict",
)
def resolve_country_conflict(
    country_code: str,
    resolution_mode: str = Form(...),
    keep_document_id: str | None = Form(None),
    file: UploadFile | None = File(None),
    confirm_warnings: bool = Form(False),
    country_confirmed: bool = Form(False),
    selected_country_code: str | None = Form(None),
) -> AdminCountryConflictResolutionResponse | AdminDocumentUploadResponse:
    """
    Resolve one country's active conflict via one of three modes.

    AUTO_DEDUPLICATE/CHOOSE_DOCUMENT act directly on the existing
    indexed records; REPLACE_WITH_DOCUMENT requires `file` and reuses
    the exact same upload validation flow as a normal upload (never a
    second, parallel implementation) - see
    safe_upload_and_index_document's resolve_country_conflict flag.
    """

    settings = get_settings()

    if resolution_mode == REPLACE_WITH_DOCUMENT:
        if file is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=admin_error_detail(
                    code="document_required",
                    message=(
                        "REPLACE_WITH_DOCUMENT requires an "
                        "authoritative DOCX file."
                    ),
                    operation="conflict_resolution",
                ),
            )

        try:
            return safe_upload_and_index_document(
                filename=file.filename or "",
                file_stream=file.file,
                source_directory=settings.document_source_dir,
                processed_directory=settings.document_processed_dir,
                maximum_bytes=settings.document_upload_max_bytes,
                confirm_warnings=confirm_warnings,
                country_confirmed=country_confirmed,
                selected_country_code=selected_country_code,
                resolve_country_conflict=True,
                expected_country_code=country_code,
            )

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
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=error.to_detail(),
            ) from error

        except AdminDocumentUnexpectedCountryError as error:
            log_admin_business_error(
                operation="conflict_resolution",
                error=error,
                country_code=error.expected_country_code,
            )

            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=error.to_detail(),
            ) from error

        except AdminDocumentCountryNotAllowedError as error:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=error.to_detail(),
            ) from error

        except AdminDocumentOperationInProgressError as error:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=admin_error_detail(
                    code="document_operation_in_progress",
                    message=str(error),
                    operation="conflict_resolution",
                ),
            ) from error

        except DocumentTooLargeError as error:
            detail = admin_error_detail(
                code="document_too_large",
                message=str(error),
                operation="conflict_resolution",
            )
            detail["max_bytes"] = error.maximum_bytes
            detail["max_mb"] = error.maximum_megabytes

            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=detail,
            ) from error

        except InvalidExtensionError as error:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=admin_error_detail(
                    code="invalid_document_type",
                    message=str(error),
                    operation="conflict_resolution",
                ),
            ) from error

        except DocumentEmptyError as error:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=admin_error_detail(
                    code="document_empty",
                    message=str(error),
                    operation="conflict_resolution",
                ),
            ) from error

        except DocumentCorruptError as error:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=admin_error_detail(
                    code="document_corrupt",
                    message=str(error),
                    operation="conflict_resolution",
                ),
            ) from error

        except DocumentCountryUndeterminedError as error:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=admin_error_detail(
                    code="document_country_undetermined",
                    message=str(error),
                    operation="conflict_resolution",
                ),
            ) from error

        except DocumentParseFailedError as error:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=admin_error_detail(
                    code="document_parse_failed",
                    message=str(error),
                    operation="conflict_resolution",
                ),
            ) from error

        except InvalidDocumentUploadError as error:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=admin_error_detail(
                    code="document_validation_failed",
                    message=str(error),
                    operation="conflict_resolution",
                ),
            ) from error

        except DocumentIndexingError as error:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=admin_error_detail(
                    code="document_indexing_failed",
                    message=str(error),
                    operation="conflict_resolution",
                ),
            ) from error

        except AdminDocumentStorageError as error:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=admin_error_detail(
                    code="document_storage_failed",
                    message=str(error),
                    operation="conflict_resolution",
                ),
            ) from error

    try:
        result = resolve_country_conflict_service(
            country_code,
            resolution_mode,
            source_directory=settings.document_source_dir,
            keep_document_id=keep_document_id,
        )

    except CountryConflictNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=error.to_detail(),
        ) from error

    except CountryConflictResolutionError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=error.to_detail(),
        ) from error

    except CountryRegistryError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=admin_error_detail(
                code="unknown_country_code",
                message=str(error),
                operation="conflict_resolution",
            ),
        ) from error

    except AdminDocumentOperationInProgressError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=admin_error_detail(
                code="document_operation_in_progress",
                message=str(error),
                operation="conflict_resolution",
            ),
        ) from error

    except DocumentIndexingError as error:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=admin_error_detail(
                code="document_indexing_failed",
                message=str(error),
                operation="conflict_resolution",
            ),
        ) from error

    except AdminDocumentStorageError as error:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=admin_error_detail(
                code="document_storage_failed",
                message=str(error),
                operation="conflict_resolution",
            ),
        ) from error

    return AdminCountryConflictResolutionResponse(
        country_code=result.country_code,
        resolution_mode=result.resolution_mode,
        kept_document_id=result.kept_document_id,
        removed_document_ids=list(result.removed_document_ids),
        stale_chunks_deleted=result.stale_chunks_deleted,
    )
