"""Admin CRUD for persisted contact photos."""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
from pathlib import Path

from app.services.contact_photo_store import (
    ContactPhotoStorageError,
    delete_contact_photo,
    read_contact_photo,
    write_contact_photo_atomic,
)
from app.services.contact_state import (
    ContactState,
    ContactStateError,
    read_contact_state,
    write_contact_state_atomic,
)


class AdminContactPhotoError(RuntimeError):
    pass


class AdminContactPhotoNotFoundError(AdminContactPhotoError):
    pass



MAX_ADMIN_CONTACT_PHOTO_BYTES = 10 * 1024 * 1024


def _detect_image_content_type(data: bytes) -> str | None:
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"

    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"

    if (
        len(data) >= 12
        and data[:4] == b"RIFF"
        and data[8:12] == b"WEBP"
    ):
        return "image/webp"

    return None


@dataclass(frozen=True)
class AdminContactPhoto:
    data: bytes
    content_type: str
    sha256: str


def _state_and_contact(source_directory, document_id, contact_id):
    try:
        state = read_contact_state(source_directory, document_id)
    except (OSError, ContactStateError) as exc:
        raise AdminContactPhotoError(str(exc)) from exc

    if state is None:
        raise AdminContactPhotoNotFoundError("Contact not found.")

    for index, contact in enumerate(state.contacts):
        if contact.contact_id == contact_id:
            return state, index, contact

    raise AdminContactPhotoNotFoundError("Contact not found.")


def read_admin_contact_photo(
    source_directory: Path,
    document_id: str,
    contact_id: str,
) -> AdminContactPhoto:
    _, _, contact = _state_and_contact(
        source_directory, document_id, contact_id
    )

    if (
        not contact.photo_filename
        or not contact.photo_content_type
        or not contact.photo_sha256
    ):
        raise AdminContactPhotoNotFoundError(
            "Contact photo not found."
        )

    try:
        data = read_contact_photo(
            source_directory,
            contact.photo_filename,
        )
    except ContactPhotoStorageError as exc:
        raise AdminContactPhotoNotFoundError(
            "Contact photo not found."
        ) from exc

    actual = hashlib.sha256(data).hexdigest()

    if actual != contact.photo_sha256:
        raise AdminContactPhotoError(
            "Stored contact photo failed integrity validation."
        )

    return AdminContactPhoto(
        data=data,
        content_type=contact.photo_content_type,
        sha256=actual,
    )


def replace_admin_contact_photo(
    source_directory: Path,
    document_id: str,
    contact_id: str,
    *,
    data: bytes,
    content_type: str,
) -> AdminContactPhoto:
    if not data:
        raise AdminContactPhotoError("Photo file is empty.")

    if len(data) > MAX_ADMIN_CONTACT_PHOTO_BYTES:
        raise AdminContactPhotoError(
            "Photo file exceeds the 10 MiB limit."
        )

    detected_content_type = _detect_image_content_type(data)

    if detected_content_type is None:
        raise AdminContactPhotoError(
            "Only JPEG, PNG and WebP images are accepted."
        )

    requested_content_type = (
        content_type.split(";", 1)[0].strip().lower()
    )

    if requested_content_type != detected_content_type:
        raise AdminContactPhotoError(
            "Photo content does not match its declared image type."
        )

    content_type = detected_content_type

    state, index, contact = _state_and_contact(
        source_directory, document_id, contact_id
    )

    old_filename = contact.photo_filename

    try:
        stored = write_contact_photo_atomic(
            source_directory,
            contact_id,
            data=data,
            content_type=content_type,
        )
    except ContactPhotoStorageError as exc:
        raise AdminContactPhotoError(str(exc)) from exc

    updated = replace(
        contact,
        photo_filename=stored.filename,
        photo_content_type=stored.content_type,
        photo_sha256=stored.sha256,
    )

    contacts = list(state.contacts)
    contacts[index] = updated

    try:
        write_contact_state_atomic(
            source_directory,
            ContactState(
                document_id=state.document_id,
                country_code=state.country_code,
                contacts=tuple(contacts),
            ),
        )
    except Exception:
        if stored.filename != old_filename:
            try:
                delete_contact_photo(
                    source_directory,
                    stored.filename,
                )
            except Exception:
                pass
        raise

    if old_filename and old_filename != stored.filename:
        try:
            delete_contact_photo(
                source_directory,
                old_filename,
            )
        except Exception:
            pass

    return AdminContactPhoto(
        data=data,
        content_type=stored.content_type,
        sha256=stored.sha256,
    )


def remove_admin_contact_photo(
    source_directory: Path,
    document_id: str,
    contact_id: str,
) -> bool:
    state, index, contact = _state_and_contact(
        source_directory, document_id, contact_id
    )

    if not contact.photo_filename:
        return False

    old_filename = contact.photo_filename

    updated = replace(
        contact,
        photo_filename=None,
        photo_content_type=None,
        photo_sha256=None,
    )

    contacts = list(state.contacts)
    contacts[index] = updated

    write_contact_state_atomic(
        source_directory,
        ContactState(
            document_id=state.document_id,
            country_code=state.country_code,
            contacts=tuple(contacts),
        ),
    )

    try:
        delete_contact_photo(
            source_directory,
            old_filename,
        )
    except Exception:
        pass

    return True
