"""
Admin CRUD for persisted contact photos.

A contact photo is a real, permanent part of the persisted source DOCX
(mission "COMPLETE CONTACT PHOTO CRUD + DOCX SOURCE SYNCHRONIZATION",
section 6) - unlike Contact business-field text, which is only ever
materialized into an EPHEMERAL copy at download time. Every mutation
here therefore keeps four things in lockstep: the structured
ContactState, the persisted source DOCX (via
app.services.contact_document_photos' deterministic primitives), the
physical photo store, and the admin-modified marker - reusing the SAME
per-country lock and same-filesystem atomic-replace discipline
app.services.admin_document_sections.py already established for every
other DOCX mutation, rather than a parallel, lower-quality mechanism.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import os
from pathlib import Path

from opensearchpy import OpenSearch

from app.clients.opensearch import get_opensearch_client
from app.services.admin_document_lifecycle import (
    AdminDocumentLifecycleError,
    _get_document_metadata,
    _required_string,
)
from app.services.admin_document_sections import (
    _fsync_path,
    _make_temp_docx_path,
)
from app.services.admin_modification_marker import mark_admin_modified
from app.services.contact_document_photos import (
    ContactDocumentPhotoError,
    add_contact_photo_to_document,
    add_new_contact_photo_to_document,
    remove_contact_photo_from_document,
    replace_contact_photo_in_document,
)
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
from app.services.country_lock import (
    DEFAULT_LOCK_TIMEOUT_SECONDS,
    country_lock,
)
from app.services.document_chunk_builder import validate_docx_format
from app.services.document_source_resolver import (
    DocumentSourceConflictError,
    resolve_document_source_path,
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


def _resolve_current_source_path(
    *,
    document_id: str,
    source_directory: Path,
    client: OpenSearch,
) -> Path:
    """The CURRENT persisted source DOCX for one document - a
    read-only OpenSearch metadata lookup, never a reindex (mission
    section 20)."""

    try:
        document_metadata = _get_document_metadata(
            document_id=document_id,
            client=client,
        )
    except AdminDocumentLifecycleError as error:
        raise AdminContactPhotoError(str(error)) from error

    country_code = _required_string(document_metadata, "country_code")
    source_filename = _required_string(
        document_metadata, "source_filename"
    )

    try:
        resolved = resolve_document_source_path(
            source_root=source_directory,
            country_code=country_code,
            source_filename=source_filename,
        )
    except DocumentSourceConflictError as error:
        raise AdminContactPhotoError(
            "The source DOCX could not be unambiguously resolved."
        ) from error

    if resolved.path is None:
        raise AdminContactPhotoError(
            "The source DOCX file is missing."
        )

    return resolved.path


def _stage_and_commit_document_bytes(
    *,
    source_path: Path,
    new_document_bytes: bytes,
) -> None:
    """
    Write, fsync, validate, then atomically replace the persisted
    source DOCX - the SAME staging discipline
    admin_document_sections.py already uses for every other DOCX
    mutation (temp file in the same directory, fsync, validate, then
    os.replace). Guarantees zero mutation to source_path unless this
    returns normally: os.replace is an atomic rename on the same
    filesystem, so it either fully applies or not at all.
    """

    temp_path = _make_temp_docx_path(source_path)

    try:
        temp_path.write_bytes(new_document_bytes)
        _fsync_path(temp_path)
        validate_docx_format(temp_path)
        os.replace(temp_path, source_path)

    except Exception as error:
        temp_path.unlink(missing_ok=True)
        raise AdminContactPhotoError(
            f"The updated source document could not be saved: {error}"
        ) from error

    _fsync_path(source_path.parent)


def _restore_original_document(
    *,
    source_path: Path,
    original_bytes: bytes,
) -> None:
    """Restore the source DOCX to its exact pre-mutation bytes after
    the DOCX commit succeeded but a later step (ContactState) failed -
    an orphaned uncommitted photo is acceptable; a source DOCX left
    out of sync with ContactState is not."""

    try:
        source_path.write_bytes(original_bytes)
        _fsync_path(source_path)
        _fsync_path(source_path.parent)

    except Exception as error:
        raise AdminContactPhotoError(
            "A contact photo change failed and the source document "
            "could not be restored to its original state - manual "
            "recovery is required."
        ) from error


def _delete_photo_best_effort(
    source_directory: Path,
    filename: str,
) -> None:
    try:
        delete_contact_photo(source_directory, filename)
    except Exception:
        # The authoritative state/DOCX already committed successfully
        # (or never referenced this file) - a stale unreferenced photo
        # is safer than turning a successful transaction into a
        # misleading failure.
        pass


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
    client: OpenSearch | None = None,
    lock_timeout_seconds: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
) -> AdminContactPhoto:
    """
    Set a contact's photo - REPLACING its current one, or ADDING one
    where none exists - keeping the persisted source DOCX, the
    physical photo store, and ContactState all in sync. Fails closed
    (raising before anything is written) when the target photo/zone
    cannot be located unambiguously in the source DOCX.
    """

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

    preliminary_state, _, _ = _state_and_contact(
        source_directory, document_id, contact_id
    )
    opensearch_client = client or get_opensearch_client()

    with country_lock(
        source_directory,
        preliminary_state.country_code,
        timeout_seconds=lock_timeout_seconds,
    ):
        state, index, contact = _state_and_contact(
            source_directory, document_id, contact_id
        )

        source_path = _resolve_current_source_path(
            document_id=document_id,
            source_directory=source_directory,
            client=opensearch_client,
        )
        original_bytes = source_path.read_bytes()

        other_contact_persons = tuple(
            other.contact_person
            for other in state.contacts
            if other.contact_id != contact.contact_id
            and other.contact_person
        )

        try:
            if contact.photo_filename is not None:
                new_document_bytes = replace_contact_photo_in_document(
                    source_path,
                    target_sha256=contact.photo_sha256,
                    new_data=data,
                    new_content_type=content_type,
                )
            else:
                try:
                    new_document_bytes = add_contact_photo_to_document(
                        source_path,
                        contact_person=contact.contact_person,
                        new_data=data,
                        new_content_type=content_type,
                        other_contact_persons=other_contact_persons,
                    )
                except ContactDocumentPhotoError:
                    # This contact's own name has no matching CONTACT
                    # PERSON zone anywhere in the document yet - the
                    # ordinary case for a contact just created via Add
                    # Contact (mission "FINAL BLOCKER", section 3: a
                    # brand-new name can never already have a zone).
                    # Anchor to the document's own largest existing
                    # CONTACT PERSON zone instead - never a name-based
                    # search after the fact.
                    new_document_bytes = (
                        add_new_contact_photo_to_document(
                            source_path,
                            new_data=data,
                            new_content_type=content_type,
                        )
                    )
        except ContactDocumentPhotoError as error:
            raise AdminContactPhotoError(str(error)) from error

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

        try:
            _stage_and_commit_document_bytes(
                source_path=source_path,
                new_document_bytes=new_document_bytes,
            )
        except AdminContactPhotoError:
            _delete_photo_best_effort(
                source_directory, stored.filename
            )
            raise

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
        except Exception as error:
            _restore_original_document(
                source_path=source_path,
                original_bytes=original_bytes,
            )
            _delete_photo_best_effort(
                source_directory, stored.filename
            )
            raise AdminContactPhotoError(
                f"The contact photo could not be saved: {error}"
            ) from error

        mark_admin_modified(source_directory, document_id)

    if old_filename and old_filename != stored.filename:
        _delete_photo_best_effort(source_directory, old_filename)

    return AdminContactPhoto(
        data=data,
        content_type=stored.content_type,
        sha256=stored.sha256,
    )


def remove_admin_contact_photo(
    source_directory: Path,
    document_id: str,
    contact_id: str,
    *,
    client: OpenSearch | None = None,
    lock_timeout_seconds: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
) -> bool:
    """
    Remove a contact's photo from the persisted source DOCX, the
    physical photo store, and ContactState together. Fails closed
    (raising before anything is written) when the photo cannot be
    located unambiguously in the source DOCX.
    """

    preliminary_state, _, preliminary_contact = _state_and_contact(
        source_directory, document_id, contact_id
    )

    if preliminary_contact.photo_filename is None:
        return False

    opensearch_client = client or get_opensearch_client()

    with country_lock(
        source_directory,
        preliminary_state.country_code,
        timeout_seconds=lock_timeout_seconds,
    ):
        state, index, contact = _state_and_contact(
            source_directory, document_id, contact_id
        )

        if contact.photo_filename is None:
            return False

        source_path = _resolve_current_source_path(
            document_id=document_id,
            source_directory=source_directory,
            client=opensearch_client,
        )
        original_bytes = source_path.read_bytes()

        try:
            new_document_bytes = remove_contact_photo_from_document(
                source_path,
                target_sha256=contact.photo_sha256,
            )
        except ContactDocumentPhotoError as error:
            raise AdminContactPhotoError(str(error)) from error

        old_filename = contact.photo_filename

        _stage_and_commit_document_bytes(
            source_path=source_path,
            new_document_bytes=new_document_bytes,
        )

        updated = replace(
            contact,
            photo_filename=None,
            photo_content_type=None,
            photo_sha256=None,
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
        except Exception as error:
            _restore_original_document(
                source_path=source_path,
                original_bytes=original_bytes,
            )
            raise AdminContactPhotoError(
                f"The contact photo could not be removed: {error}"
            ) from error

        mark_admin_modified(source_directory, document_id)

    _delete_photo_best_effort(source_directory, old_filename)

    return True
