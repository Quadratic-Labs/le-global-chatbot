"""HTTP endpoints for Admin Contact Management (mission "ORDER 8G-B1")."""

from __future__ import annotations

from fastapi import Header, HTTPException, Request, Response, status

from app.core.config import get_settings
from app.services.admin_contact_photos import (
    AdminContactPhotoError,
    AdminContactPhotoNotFoundError,
    read_admin_contact_photo,
    remove_admin_contact_photo,
    replace_admin_contact_photo,
)


from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    status,
)

from app.core.admin_error_reporting import (
    admin_error_detail,
    log_admin_business_error,
)
from app.core.config import get_settings
from app.models.admin_contacts import (
    AdminContactDeleteResponse,
    AdminContactListResponse,
    AdminContactResponse,
    AdminContactWriteRequest,
)
from app.security.admin import (
    require_admin_key,
)
from app.services.admin_contacts import (
    AdminContactMutationFailedError,
    AdminContactNotFoundError,
    add_contact,
    delete_contact,
    list_contacts,
    update_contact,
)
from app.services.admin_document_lifecycle import (
    AdminDocumentLifecycleError,
    AdminDocumentNotFoundError,
    AdminDocumentRollbackError,
    InvalidAdminDocumentIdError,
)
from app.services.country_lock import (
    AdminDocumentOperationInProgressError,
)


router = APIRouter(
    prefix="/api/v1/admin",
    tags=["Contact Administration"],
    dependencies=[
        Depends(
            require_admin_key
        )
    ],
)


def _raise_common_document_errors(
    error: Exception,
    *,
    operation: str,
    document_id: str,
) -> None:
    """Shared mapping for the errors every contact route can raise
    before it ever reaches a contact-specific mutation - the same
    "existing safe Admin error family" every other admin route already
    uses."""

    if isinstance(error, InvalidAdminDocumentIdError):
        log_admin_business_error(
            operation=operation,
            error=error,
            document_id=document_id,
        )

        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=admin_error_detail(
                code="invalid_document_id",
                message=str(error),
                operation=operation,
                document_id=document_id,
            ),
        ) from error

    if isinstance(error, AdminDocumentNotFoundError):
        log_admin_business_error(
            operation=operation,
            error=error,
            document_id=document_id,
        )

        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=admin_error_detail(
                code="document_not_found",
                message=str(error),
                operation=operation,
                document_id=document_id,
            ),
        ) from error

    if isinstance(error, AdminDocumentOperationInProgressError):
        log_admin_business_error(
            operation=operation,
            error=error,
            document_id=document_id,
            country_code=error.country_code,
        )

        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=admin_error_detail(
                code="document_operation_in_progress",
                message=str(error),
                operation=operation,
                document_id=document_id,
            ),
        ) from error


def _raise_contact_mutation_errors(
    error: Exception,
    *,
    operation: str,
    document_id: str,
) -> None:
    """Shared mapping for the errors only Add/Update/Delete can raise."""

    if isinstance(error, AdminContactNotFoundError):
        log_admin_business_error(
            operation=operation,
            error=error,
            document_id=document_id,
        )

        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=error.to_detail(),
        ) from error

    if isinstance(error, AdminDocumentRollbackError):
        log_admin_business_error(
            operation=operation,
            error=error,
            document_id=document_id,
        )

        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=admin_error_detail(
                code="rollback_failed",
                message=str(error),
                operation=operation,
                document_id=document_id,
            ),
        ) from error

    if isinstance(error, AdminContactMutationFailedError):
        log_admin_business_error(
            operation=operation,
            error=error,
            document_id=document_id,
        )

        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=admin_error_detail(
                code="contact_mutation_failed",
                message=str(error),
                operation=operation,
                document_id=document_id,
            ),
        ) from error


@router.get(
    "/documents/{document_id}/contacts",
    response_model=AdminContactListResponse,
)
def list_admin_document_contacts(
    document_id: str,
) -> AdminContactListResponse:
    """Every contact currently configured for one document/country."""

    settings = get_settings()

    try:
        return list_contacts(
            document_id=document_id,
            source_directory=settings.document_source_dir,
        )

    except (
        InvalidAdminDocumentIdError,
        AdminDocumentNotFoundError,
        AdminDocumentOperationInProgressError,
    ) as error:
        _raise_common_document_errors(
            error,
            operation="contact_list",
            document_id=document_id,
        )
        raise

    except AdminDocumentLifecycleError as error:
        log_admin_business_error(
            operation="contact_list",
            error=error,
            document_id=document_id,
        )

        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=admin_error_detail(
                code="contact_list_failed",
                message=str(error),
                operation="contact_list",
                document_id=document_id,
            ),
        ) from error


@router.post(
    "/documents/{document_id}/contacts",
    response_model=AdminContactResponse,
)
def add_admin_document_contact(
    document_id: str,
    request: AdminContactWriteRequest,
) -> AdminContactResponse:
    """Add one new contact - duplicates of an existing contact's exact
    field values are explicitly allowed."""

    settings = get_settings()

    try:
        return add_contact(
            document_id=document_id,
            fields=request,
            source_directory=settings.document_source_dir,
        )

    except (
        InvalidAdminDocumentIdError,
        AdminDocumentNotFoundError,
        AdminDocumentOperationInProgressError,
    ) as error:
        _raise_common_document_errors(
            error,
            operation="contact_add",
            document_id=document_id,
        )
        raise

    except (
        AdminDocumentRollbackError,
        AdminContactMutationFailedError,
    ) as error:
        _raise_contact_mutation_errors(
            error,
            operation="contact_add",
            document_id=document_id,
        )
        raise

    except AdminDocumentLifecycleError as error:
        log_admin_business_error(
            operation="contact_add",
            error=error,
            document_id=document_id,
        )

        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=admin_error_detail(
                code="contact_add_failed",
                message=str(error),
                operation="contact_add",
                document_id=document_id,
            ),
        ) from error


@router.put(
    "/documents/{document_id}/contacts/{contact_id}",
    response_model=AdminContactResponse,
)
def update_admin_document_contact(
    document_id: str,
    contact_id: str,
    request: AdminContactWriteRequest,
) -> AdminContactResponse:
    """Save new field values for one existing contact - its
    contact_id and position are both preserved."""

    settings = get_settings()

    try:
        return update_contact(
            document_id=document_id,
            contact_id=contact_id,
            fields=request,
            source_directory=settings.document_source_dir,
        )

    except (
        InvalidAdminDocumentIdError,
        AdminDocumentNotFoundError,
        AdminDocumentOperationInProgressError,
    ) as error:
        _raise_common_document_errors(
            error,
            operation="contact_update",
            document_id=document_id,
        )
        raise

    except (
        AdminContactNotFoundError,
        AdminDocumentRollbackError,
        AdminContactMutationFailedError,
    ) as error:
        _raise_contact_mutation_errors(
            error,
            operation="contact_update",
            document_id=document_id,
        )
        raise

    except AdminDocumentLifecycleError as error:
        log_admin_business_error(
            operation="contact_update",
            error=error,
            document_id=document_id,
        )

        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=admin_error_detail(
                code="contact_update_failed",
                message=str(error),
                operation="contact_update",
                document_id=document_id,
            ),
        ) from error


@router.delete(
    "/documents/{document_id}/contacts/{contact_id}",
    response_model=AdminContactDeleteResponse,
)
def delete_admin_document_contact(
    document_id: str,
    contact_id: str,
) -> AdminContactDeleteResponse:
    """
    Remove exactly one contact by its contact_id.

    Delete confirmation is a WordPress/B2 concern - this endpoint
    performs the authorized deletion directly, with no confirmation
    step of its own.
    """

    settings = get_settings()

    try:
        return delete_contact(
            document_id=document_id,
            contact_id=contact_id,
            source_directory=settings.document_source_dir,
        )

    except (
        InvalidAdminDocumentIdError,
        AdminDocumentNotFoundError,
        AdminDocumentOperationInProgressError,
    ) as error:
        _raise_common_document_errors(
            error,
            operation="contact_delete",
            document_id=document_id,
        )
        raise

    except (
        AdminContactNotFoundError,
        AdminDocumentRollbackError,
        AdminContactMutationFailedError,
    ) as error:
        _raise_contact_mutation_errors(
            error,
            operation="contact_delete",
            document_id=document_id,
        )
        raise

    except AdminDocumentLifecycleError as error:
        log_admin_business_error(
            operation="contact_delete",
            error=error,
            document_id=document_id,
        )

        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=admin_error_detail(
                code="contact_delete_failed",
                message=str(error),
                operation="contact_delete",
                document_id=document_id,
            ),
        ) from error

@router.get(
    "/{document_id}/contacts/{contact_id}/photo",
    response_class=Response,
)
def get_admin_document_contact_photo(
    document_id: str,
    contact_id: str,
) -> Response:
    try:
        photo = read_admin_contact_photo(
            get_settings().document_source_dir,
            document_id,
            contact_id,
        )
    except AdminContactPhotoNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except AdminContactPhotoError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        ) from exc

    return Response(
        content=photo.data,
        media_type=photo.content_type,
        headers={
            "ETag": f'"{photo.sha256}"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.put(
    "/{document_id}/contacts/{contact_id}/photo",
)
async def replace_admin_document_contact_photo(
    document_id: str,
    contact_id: str,
    request: Request,
    content_type: str | None = Header(
        default=None,
        alias="Content-Type",
    ),
    content_length: int | None = Header(
        default=None,
        alias="Content-Length",
    ),
):
    if (
        content_length is not None
        and content_length > 10 * 1024 * 1024
    ):
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="Photo file exceeds the 10 MiB limit.",
        )

    body = await request.body()

    if not body:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Photo file is empty.",
        )

    try:
        photo = replace_admin_contact_photo(
            get_settings().document_source_dir,
            document_id,
            contact_id,
            data=body,
            content_type=content_type or "",
        )
    except AdminContactPhotoNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except AdminContactPhotoError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    return {
        "document_id": document_id,
        "contact_id": contact_id,
        "photo_sha256": photo.sha256,
    }


@router.delete(
    "/{document_id}/contacts/{contact_id}/photo",
)
def delete_admin_document_contact_photo(
    document_id: str,
    contact_id: str,
):
    try:
        removed = remove_admin_contact_photo(
            get_settings().document_source_dir,
            document_id,
            contact_id,
        )
    except AdminContactPhotoNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except AdminContactPhotoError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        ) from exc

    return {
        "document_id": document_id,
        "contact_id": contact_id,
        "photo_removed": removed,
    }
