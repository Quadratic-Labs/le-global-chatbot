"""
Admin Contact Management backend foundation (mission "ORDER 8G-B1").

Architecture (see also the "ORDER 8G-B0" audit this mission executes):

    NEW DOCX ACCEPTED
            |
    extract_contacts_from_docx()
            |
    structured contact state (this module + contact_state.py)
            |
    OpenSearch Contact chunk = DERIVED representation

Between accepted DOCX uploads, the structured contact state is
authoritative: Admin Contact CRUD (list/add/update/delete, below)
reads and writes it directly, and the OpenSearch Contact chunk is
synchronized from it immediately, via the ONE shared formatter/builder
in document_chunk_builder.py (build_contact_chunk_for_contacts) - never
a second, separately maintained formatting implementation.

Contact edits are never written back into the DOCX's own anchored Word
text boxes (the "ORDER 8G-B0" audit found no safe existing textbox
mutation primitive) - a later CONFIRMED new DOCX for the same country
simply discards this structured state and recreates it fresh from that
new DOCX (see reseed_contact_state_from_docx and
reseed_contacts_from_current_docx below); no merge ever happens.
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from opensearchpy import OpenSearch

from app.clients.opensearch import get_opensearch_client
from app.models.admin_contacts import (
    AdminContactDeleteResponse,
    AdminContactListResponse,
    AdminContactResponse,
    AdminContactSummary,
    AdminContactWriteRequest,
)
from app.services.admin_document_lifecycle import (
    AdminDocumentLifecycleError,
    AdminDocumentRollbackError,
    InvalidAdminDocumentIdError,
    _ensure_no_country_conflict,
    _get_document_metadata,
    _required_string,
)
from app.services.admin_document_sections import (
    _fsync_path,
    _make_temp_docx_path,
)
from app.services.admin_modification_marker import (
    is_admin_modified_since_upload,
    mark_admin_modified,
    reset_admin_modified,
    write_admin_modified_marker,
)
from app.services.contact_document_area import (
    ContactAreaError,
    ContactPhotoPayload,
    rebuild_canonical_contact_table,
    resolve_untracked_contact_photo,
)
from app.services.contact_people import (
    associate_contact_photos,
)
from app.services.contact_photos import (
    ContactPhotoExtractionError,
    extract_contact_photo_candidates,
)
from app.services.contact_photo_store import (
    ContactPhotoStorageError,
    delete_contact_photo,
    read_contact_photo,
    write_contact_photo_atomic,
)
from app.services.contact_state import (
    ContactRecord,
    ContactState,
    delete_contact_state,
    new_contact_id,
    read_contact_state,
    write_contact_state_atomic,
)
from app.services.country_lock import (
    DEFAULT_LOCK_TIMEOUT_SECONDS,
    country_lock,
)
from app.services.document_chunk_builder import (
    DocumentMetadata,
    build_contact_chunk_for_contacts,
    validate_docx_format,
)
from app.services.document_indexer import (
    replace_document_contact_chunk,
)
from app.services.document_source_resolver import (
    DocumentSourceConflictError,
    resolve_document_source_path,
)
from app.services.docx_parser import (
    ExtractedContact,
    extract_contacts_from_docx,
)


DOCUMENT_ID_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^doc_[0-9a-f]{64}$"
)


class AdminContactNotFoundError(LookupError):
    """Raised when a specific contact_id is not found in a document's
    current structured contact state."""

    def __init__(self, *, document_id: str, contact_id: str) -> None:
        self.document_id = document_id
        self.contact_id = contact_id

        super().__init__(
            f"Contact {contact_id!r} was not found for document "
            f"{document_id!r}."
        )

    def to_detail(self) -> dict[str, object]:
        """Return a structured HTTP 404 payload."""

        return {
            "code": "contact_not_found",
            "message": str(self),
            "operation": "contact_mutation",
            "document_id": self.document_id,
            "contact_id": self.contact_id,
        }


class AdminContactMutationFailedError(RuntimeError):
    """
    Raised when a contact Add/Edit/Delete/reseed failed, but its own
    rollback fully succeeded - the previous state, index, and marker
    are all intact; nothing was left half-applied.
    """


def _get_client(client: OpenSearch | None) -> OpenSearch:
    """Return the supplied or configured OpenSearch client - the same
    inline convention every other admin service module in this
    codebase already uses."""

    return (
        client
        if client is not None
        else get_opensearch_client()
    )


def _validate_document_id(document_id: str) -> str:
    """Validate one deterministic document identifier - reuses the
    exact pattern admin_document_lifecycle.py already validates
    against, never a second, independently maintained regex."""

    normalized_document_id = document_id.strip()

    if not DOCUMENT_ID_PATTERN.fullmatch(normalized_document_id):
        raise InvalidAdminDocumentIdError(
            "The document identifier is invalid."
        )

    return normalized_document_id


def _record_to_extracted_contact(
    record: ContactRecord,
) -> ExtractedContact:
    """
    Adapt one persisted ContactRecord to the shape
    build_contact_chunk_content already knows how to render - the
    contact_id itself carries no meaning for the rendered chunk text,
    so it is intentionally dropped here.
    """

    return ExtractedContact(
        member_firm=record.member_firm,
        contact_person=record.contact_person,
        email=record.email,
        phone=record.phone,
        address=record.address,
        website=record.website,
    )


def _extracted_contact_to_record(
    contact: ExtractedContact,
) -> ContactRecord:
    """Adapt one freshly DOCX-parsed ExtractedContact into a persisted
    ContactRecord, assigning it a fresh, stable identifier."""

    return ContactRecord(
        contact_id=new_contact_id(),
        member_firm=contact.member_firm,
        contact_person=contact.contact_person,
        email=contact.email,
        phone=contact.phone,
        address=contact.address,
        website=contact.website,
    )


def _document_metadata_for_chunks(
    document_metadata: dict[str, Any],
) -> DocumentMetadata:
    """
    Build the DocumentMetadata needed to render a Contact chunk from
    one document's own indexed metadata - language/source_format are
    not part of DOCUMENT_METADATA_FIELDS (the whole pipeline supports
    exactly one language and one source format today, so
    admin_document_sections.py already hardcodes them the same way;
    this mirrors that established convention rather than inventing a
    second metadata-fetch path).
    """

    reference_year = document_metadata.get("reference_year")

    return DocumentMetadata(
        country=_required_string(document_metadata, "country"),
        country_code=_required_string(
            document_metadata,
            "country_code",
        ),
        reference_year=(
            int(reference_year)
            if isinstance(reference_year, int)
            else None
        ),
        language="en",
        source_filename=_required_string(
            document_metadata,
            "source_filename",
        ),
        source_format="docx",
    )



def _contact_photo_filenames(
    contacts: tuple[ContactRecord, ...],
) -> set[str]:
    return {
        record.photo_filename
        for record in contacts
        if record.photo_filename is not None
    }


def _build_photo_aware_contact_records(
    *,
    source_directory: Path,
    docx_path: Path,
    contacts: Sequence[ExtractedContact],
) -> tuple[ContactRecord, ...]:
    """
    Build freshly-seeded ContactRecords from one accepted DOCX.

    Photo extraction/person association is deterministic. Contact IDs
    are allocated before storing each person's image, so the durable
    filename belongs to that stable contact identity.

    If any image write fails, every image already created by THIS seed
    attempt is removed before the original error is propagated.
    """

    try:
        photos = extract_contact_photo_candidates(
            docx_path,
        )
    except ContactPhotoExtractionError:
        # A contact photo is optional. A DOCX already accepted by the
        # document/contact pipeline must not become unusable solely
        # because its image package cannot be inspected reliably.
        # Fail closed to zero photo associations; never guess.
        photos = []

    associated = associate_contact_photos(
        contacts,
        photos,
    )

    records: list[ContactRecord] = []
    created_photo_filenames: list[str] = []

    try:
        for contact in associated:
            contact_id = new_contact_id()

            photo_filename = None
            photo_content_type = None
            photo_sha256 = None

            if contact.photo is not None:
                stored = write_contact_photo_atomic(
                    source_directory,
                    contact_id,
                    data=contact.photo.data,
                    content_type=contact.photo.content_type,
                )

                created_photo_filenames.append(
                    stored.filename
                )

                photo_filename = stored.filename
                photo_content_type = stored.content_type
                photo_sha256 = stored.sha256

            records.append(
                ContactRecord(
                    contact_id=contact_id,
                    member_firm=contact.member_firm,
                    contact_person=contact.contact_person,
                    email=contact.email,
                    phone=contact.phone,
                    address=contact.address,
                    website=contact.website,
                    photo_filename=photo_filename,
                    photo_content_type=photo_content_type,
                    photo_sha256=photo_sha256,
                )
            )

        return tuple(records)

    except Exception as original_error:
        cleanup_error: Exception | None = None

        for filename in reversed(
            created_photo_filenames
        ):
            try:
                delete_contact_photo(
                    source_directory,
                    filename,
                )
            except Exception as error:
                if cleanup_error is None:
                    cleanup_error = error

        if cleanup_error is not None:
            raise ContactPhotoStorageError(
                "Contact photo seeding failed and its photo cleanup "
                "was itself incomplete."
            ) from cleanup_error

        raise original_error



def _contact_summary(record: ContactRecord) -> AdminContactSummary:
    return AdminContactSummary(
        contact_id=record.contact_id,
        member_firm=record.member_firm,
        contact_person=record.contact_person,
        email=record.email,
        phone=record.phone,
        address=record.address,
        website=record.website,
        has_photo=record.photo_filename is not None,
    )


def _apply_contact_state_change(
    *,
    document_id: str,
    country_code: str,
    source_directory: Path,
    new_contacts: tuple[ContactRecord, ...],
    document_metadata: dict[str, Any],
    client: OpenSearch,
    reset_marker: bool,
) -> None:
    """
    The one shared transactional core behind every contact-state
    mutation - Add, Update, Delete, and a DOCX-driven reseed (mission
    "ORDER 8G-B1", sections 9/11/12/14).

    Snapshots the current sidecar, the current OpenSearch Contact
    chunk (via replace_document_contact_chunk's own internal snapshot),
    and the current admin-modified marker; writes/syncs the new state;
    and rolls all three back together on ANY failure - never a mixed
    "new state, old index" or "new index, old marker" outcome.

    reset_marker=True clears modified_since_upload to False (a genuine
    DOCX reseed: the new state now exactly matches what was just
    accepted); False marks it True (an ordinary Admin CRUD mutation
    against a document whose accepted DOCX has not changed).
    """

    previous_state = read_contact_state(
        source_directory,
        document_id,
    )
    previous_marker = is_admin_modified_since_upload(
        source_directory,
        document_id,
    )

    previous_photo_filenames = {
        record.photo_filename
        for record in (
            previous_state.contacts
            if previous_state is not None
            else ()
        )
        if record.photo_filename is not None
    }

    new_photo_filenames = {
        record.photo_filename
        for record in new_contacts
        if record.photo_filename is not None
    }

    # Files referenced only by the candidate state were already
    # materialized before this transactional state/index commit.
    # They must disappear again if the transaction rolls back.
    rollback_photo_filenames = (
        new_photo_filenames
        - previous_photo_filenames
    )

    # Files referenced only by the previous committed state may be
    # removed, but strictly AFTER state + OpenSearch + marker commit.
    superseded_photo_filenames = (
        previous_photo_filenames
        - new_photo_filenames
    )

    metadata = _document_metadata_for_chunks(document_metadata)

    new_state = ContactState(
        document_id=document_id,
        country_code=country_code,
        contacts=new_contacts,
    )

    new_contact_chunk = build_contact_chunk_for_contacts(
        [
            _record_to_extracted_contact(record)
            for record in new_contacts
        ],
        metadata,
    )

    try:
        write_contact_state_atomic(
            source_directory,
            new_state,
        )

        replace_document_contact_chunk(
            document_id=document_id,
            chunk=new_contact_chunk,
            client=client,
        )

        if reset_marker:
            reset_admin_modified(source_directory, document_id)
        else:
            mark_admin_modified(source_directory, document_id)

    except Exception as original_error:
        try:
            if previous_state is not None:
                write_contact_state_atomic(
                    source_directory,
                    previous_state,
                )
            else:
                delete_contact_state(source_directory, document_id)

            previous_contact_chunk = build_contact_chunk_for_contacts(
                [
                    _record_to_extracted_contact(record)
                    for record in (
                        previous_state.contacts
                        if previous_state is not None
                        else ()
                    )
                ],
                metadata,
            )

            replace_document_contact_chunk(
                document_id=document_id,
                chunk=previous_contact_chunk,
                client=client,
            )

            write_admin_modified_marker(
                source_directory,
                document_id,
                previous_marker,
            )

            for filename in sorted(
                rollback_photo_filenames
            ):
                delete_contact_photo(
                    source_directory,
                    filename,
                )

        except Exception as rollback_error:
            raise AdminDocumentRollbackError(
                "A contact mutation failed for document "
                f"{document_id!r}, and its rollback afterwards was "
                "itself incomplete - manual recovery is required."
            ) from rollback_error

        raise AdminContactMutationFailedError(
            "The contact change could not be completed: "
            f"{original_error}"
        ) from original_error

    for filename in sorted(
        superseded_photo_filenames
    ):
        try:
            delete_contact_photo(
                source_directory,
                filename,
            )
        except ContactPhotoStorageError:
            # The authoritative state already committed successfully.
            # A stale unreferenced photo is safer than turning that
            # successful transaction into a misleading failure.
            pass


def _load_country_code_and_metadata(
    *,
    document_id: str,
    client: OpenSearch,
    operation: str,
) -> tuple[str, dict[str, Any]]:
    """Validate document_id exists and has no unresolved country
    conflict, returning (country_code, full document metadata)."""

    document_metadata = _get_document_metadata(
        document_id=document_id,
        client=client,
    )

    country_code = _required_string(
        document_metadata,
        "country_code",
    )

    _ensure_no_country_conflict(
        country_code=country_code,
        client=client,
        operation=operation,
    )

    return country_code, document_metadata


def list_contacts(
    *,
    document_id: str,
    source_directory: Path,
    client: OpenSearch | None = None,
) -> AdminContactListResponse:
    """
    Every contact currently configured for one document, in stable
    order.

    Read-only - never acquires the per-country lock (mirrors
    get_document_download's own reasoning: a read must never be
    serialized behind writes). A document with no structured contact
    state yet (never bootstrapped, never mutated) is reported as
    contacts=[] here - the same observable result as an explicitly
    empty state, since there is nothing to show either way; the
    distinction between "no state" and "empty state" matters only to
    the legacy bootstrap facility below, never to this read path.
    """

    validated_document_id = _validate_document_id(document_id)
    opensearch_client = _get_client(client)

    document_metadata = _get_document_metadata(
        document_id=validated_document_id,
        client=opensearch_client,
    )

    country_code = _required_string(
        document_metadata,
        "country_code",
    )

    state = read_contact_state(
        source_directory,
        validated_document_id,
    )

    contacts = state.contacts if state is not None else ()

    return AdminContactListResponse(
        document_id=validated_document_id,
        country_code=country_code,
        contacts=[
            _contact_summary(record)
            for record in contacts
        ],
    )


def _resolve_source_path_for_mutation(
    *,
    source_directory: Path,
    document_metadata: dict[str, Any],
    country_code: str,
) -> Path:
    """The CURRENT persisted source DOCX for one document, resolved
    the same way admin_contact_photos.py already does for every DOCX
    mutation - a read-only OpenSearch-metadata-derived lookup, never a
    reindex."""

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
        raise AdminContactMutationFailedError(
            "The source DOCX could not be unambiguously resolved."
        ) from error

    if resolved.path is None:
        raise AdminContactMutationFailedError(
            "The source DOCX file is missing."
        )

    return resolved.path


def _stage_and_commit_source_document(
    *,
    source_path: Path,
    new_document_bytes: bytes,
) -> None:
    """
    Write, fsync, validate, then atomically replace the persisted
    source DOCX - the same staging discipline
    admin_document_sections.py/admin_contact_photos.py already use for
    every other DOCX mutation (temp file in the same directory, fsync,
    validate, then os.replace). Guarantees zero mutation to
    source_path unless this returns normally.

    The trailing directory fsync is deliberately best-effort: by the
    time os.replace above returns, the new content is already the
    durable content at source_path - raising for a directory-entry
    fsync failure here would escape both this function's own staging
    rollback and the caller's ContactState rollback, leaving a
    committed DOCX mutation with no matching ContactState entry.
    """

    temp_path = _make_temp_docx_path(source_path)

    try:
        temp_path.write_bytes(new_document_bytes)
        _fsync_path(temp_path)
        validate_docx_format(temp_path)
        os.replace(temp_path, source_path)

    except Exception as error:
        temp_path.unlink(missing_ok=True)
        raise AdminContactMutationFailedError(
            f"The updated source document could not be saved: {error}"
        ) from error

    try:
        _fsync_path(source_path.parent)
    except OSError:
        pass


def _restore_source_document(
    *,
    source_path: Path,
    original_bytes: bytes,
) -> None:
    """
    Restore the source DOCX to its exact pre-mutation bytes after a
    DOCX mutation committed but the following ContactState/index
    commit failed. Uses the SAME temp-file + fsync + validate +
    os.replace staging as _stage_and_commit_source_document (never a
    direct, non-atomic write onto the live source_path), so a crash
    or I/O error mid-restore can never leave source_path truncated.
    """

    temp_path = _make_temp_docx_path(source_path)

    try:
        temp_path.write_bytes(original_bytes)
        _fsync_path(temp_path)
        validate_docx_format(temp_path)
        os.replace(temp_path, source_path)

    except Exception as error:
        temp_path.unlink(missing_ok=True)
        raise AdminDocumentRollbackError(
            "A contact mutation failed and the source document could "
            "not be restored to its original state - manual recovery "
            "is required."
        ) from error

    try:
        _fsync_path(source_path.parent)
    except OSError:
        pass


def _resolve_contact_photo(
    record: ContactRecord,
    *,
    source_directory: Path,
    source_path: Path,
    country: str | None,
) -> ContactPhotoPayload | None:
    """
    The authoritative photo bytes for one contact about to be written
    into the rebuilt canonical table - the Admin photo store's own
    copy when this contact has ever had an explicit photo mutation
    (photo_filename set), or otherwise whatever the CURRENT persisted
    source still shows as that contact's own organic photo (see
    contact_document_area.resolve_untracked_contact_photo) - so
    rebuilding the canonical area from ContactState alone never
    silently drops a photo the document still visibly has.
    """

    if record.photo_filename is not None:
        try:
            data = read_contact_photo(
                source_directory, record.photo_filename
            )
        except ContactPhotoStorageError as error:
            raise AdminContactMutationFailedError(
                f"The contact's own stored photo could not be read: "
                f"{error}"
            ) from error

        return ContactPhotoPayload(
            data=data,
            content_type=record.photo_content_type or "image/jpeg",
        )

    try:
        return resolve_untracked_contact_photo(
            source_path,
            contact_person=record.contact_person,
            country=country,
        )
    except ContactAreaError as error:
        raise AdminContactMutationFailedError(str(error)) from error


def _synchronize_source_document(
    *,
    source_directory: Path,
    document_metadata: dict[str, Any],
    country_code: str,
    all_records: tuple[ContactRecord, ...],
) -> tuple[Path, bytes]:
    """
    Rebuild the ENTIRE persisted canonical contact area from the
    complete intended contact list - the one mechanism behind every
    mutation (Add and Delete here, and replace-photo in
    admin_contact_photos.py), so the source DOCX is always the full,
    current ContactState rendered fresh as one standard Word table,
    never a series of independent surgical edits that can drift apart
    from it.

    Always returns (source_path, original_bytes) - the DOCX has always
    been committed by the time this returns, so the caller can restore
    original_bytes if the following ContactState commit fails.
    """

    source_path = _resolve_source_path_for_mutation(
        source_directory=source_directory,
        document_metadata=document_metadata,
        country_code=country_code,
    )
    country = _required_string(document_metadata, "country")
    original_bytes = source_path.read_bytes()

    extracted_contacts = tuple(
        _record_to_extracted_contact(record) for record in all_records
    )
    photos = tuple(
        _resolve_contact_photo(
            record,
            source_directory=source_directory,
            source_path=source_path,
            country=country,
        )
        for record in all_records
    )

    try:
        new_document_bytes = rebuild_canonical_contact_table(
            source_path,
            contacts=extracted_contacts,
            photos=photos,
            country=country,
        )
    except ContactAreaError as error:
        raise AdminContactMutationFailedError(
            f"The contact area could not be synchronized into the "
            f"source document: {error}"
        ) from error

    _stage_and_commit_source_document(
        source_path=source_path,
        new_document_bytes=new_document_bytes,
    )

    return source_path, original_bytes


def add_contact(
    *,
    document_id: str,
    fields: AdminContactWriteRequest,
    source_directory: Path,
    client: OpenSearch | None = None,
    lock_timeout_seconds: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
) -> AdminContactResponse:
    """
    Append one new contact - duplicates (identical field values to an
    existing contact) are explicitly allowed; the new record always
    gets its own fresh, distinct contact_id regardless.

    Also synchronizes the persisted source DOCX with the new contact -
    rebuilding its canonical contact table from the complete current
    contact list (see _synchronize_source_document) - as ONE
    transaction together with the ContactState/index commit: if the
    DOCX commits but the ContactState commit then fails, the DOCX is
    restored to its exact prior bytes before the error propagates. A
    successful Add Contact never leaves the source DOCX unsynchronized
    with ContactState.
    """

    validated_document_id = _validate_document_id(document_id)
    opensearch_client = _get_client(client)

    preliminary_metadata = _get_document_metadata(
        document_id=validated_document_id,
        client=opensearch_client,
    )

    with country_lock(
        source_directory,
        _required_string(preliminary_metadata, "country_code"),
        timeout_seconds=lock_timeout_seconds,
    ):
        country_code, document_metadata = _load_country_code_and_metadata(
            document_id=validated_document_id,
            client=opensearch_client,
            operation="contact_add",
        )

        current_state = read_contact_state(
            source_directory,
            validated_document_id,
        )
        previous_contacts = (
            current_state.contacts
            if current_state is not None
            else ()
        )

        new_record = ContactRecord(
            contact_id=new_contact_id(),
            member_firm=fields.member_firm.strip(),
            contact_person=fields.contact_person.strip(),
            email=fields.email.strip(),
            phone=fields.phone.strip(),
            address=fields.address.strip(),
            website=fields.website.strip(),
        )

        new_contacts = previous_contacts + (new_record,)

        committed_source_path, original_document_bytes = (
            _synchronize_source_document(
                source_directory=source_directory,
                document_metadata=document_metadata,
                country_code=country_code,
                all_records=new_contacts,
            )
        )

        try:
            _apply_contact_state_change(
                document_id=validated_document_id,
                country_code=country_code,
                source_directory=source_directory,
                new_contacts=new_contacts,
                document_metadata=document_metadata,
                client=opensearch_client,
                reset_marker=False,
            )

        except Exception:
            _restore_source_document(
                source_path=committed_source_path,
                original_bytes=original_document_bytes,
            )
            raise

    return AdminContactResponse(
        document_id=validated_document_id,
        country_code=country_code,
        contact_id=new_record.contact_id,
        member_firm=new_record.member_firm,
        contact_person=new_record.contact_person,
        email=new_record.email,
        phone=new_record.phone,
        address=new_record.address,
        website=new_record.website,
    )


def update_contact(
    *,
    document_id: str,
    contact_id: str,
    fields: AdminContactWriteRequest,
    source_directory: Path,
    client: OpenSearch | None = None,
    lock_timeout_seconds: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
) -> AdminContactResponse:
    """Save new field values for one existing contact - its
    contact_id and its position among the document's contacts are both
    preserved."""

    validated_document_id = _validate_document_id(document_id)
    validated_contact_id = contact_id.strip()
    opensearch_client = _get_client(client)

    preliminary_metadata = _get_document_metadata(
        document_id=validated_document_id,
        client=opensearch_client,
    )

    with country_lock(
        source_directory,
        _required_string(preliminary_metadata, "country_code"),
        timeout_seconds=lock_timeout_seconds,
    ):
        country_code, document_metadata = _load_country_code_and_metadata(
            document_id=validated_document_id,
            client=opensearch_client,
            operation="contact_update",
        )

        current_state = read_contact_state(
            source_directory,
            validated_document_id,
        )
        current_contacts = (
            current_state.contacts
            if current_state is not None
            else ()
        )

        position = next(
            (
                index
                for index, record in enumerate(current_contacts)
                if record.contact_id == validated_contact_id
            ),
            None,
        )

        if position is None:
            raise AdminContactNotFoundError(
                document_id=validated_document_id,
                contact_id=validated_contact_id,
            )

        updated_record = ContactRecord(
            contact_id=validated_contact_id,
            member_firm=fields.member_firm.strip(),
            contact_person=fields.contact_person.strip(),
            email=fields.email.strip(),
            phone=fields.phone.strip(),
            address=fields.address.strip(),
            website=fields.website.strip(),
            photo_filename=current_contacts[position].photo_filename,
            photo_content_type=(
                current_contacts[position].photo_content_type
            ),
            photo_sha256=current_contacts[position].photo_sha256,
        )

        new_contacts = (
            current_contacts[:position]
            + (updated_record,)
            + current_contacts[position + 1:]
        )

        _apply_contact_state_change(
            document_id=validated_document_id,
            country_code=country_code,
            source_directory=source_directory,
            new_contacts=new_contacts,
            document_metadata=document_metadata,
            client=opensearch_client,
            reset_marker=False,
        )

    return AdminContactResponse(
        document_id=validated_document_id,
        country_code=country_code,
        contact_id=updated_record.contact_id,
        member_firm=updated_record.member_firm,
        contact_person=updated_record.contact_person,
        email=updated_record.email,
        phone=updated_record.phone,
        address=updated_record.address,
        website=updated_record.website,
    )


def delete_contact(
    *,
    document_id: str,
    contact_id: str,
    source_directory: Path,
    client: OpenSearch | None = None,
    lock_timeout_seconds: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
) -> AdminContactDeleteResponse:
    """
    Remove exactly one contact by its contact_id - every other
    contact for this document is left completely unchanged, including
    its position. Confirmation is a WordPress/B2 concern; this
    performs the authorized deletion directly.
    """

    validated_document_id = _validate_document_id(document_id)
    validated_contact_id = contact_id.strip()
    opensearch_client = _get_client(client)

    preliminary_metadata = _get_document_metadata(
        document_id=validated_document_id,
        client=opensearch_client,
    )

    with country_lock(
        source_directory,
        _required_string(preliminary_metadata, "country_code"),
        timeout_seconds=lock_timeout_seconds,
    ):
        country_code, document_metadata = _load_country_code_and_metadata(
            document_id=validated_document_id,
            client=opensearch_client,
            operation="contact_delete",
        )

        current_state = read_contact_state(
            source_directory,
            validated_document_id,
        )
        current_contacts = (
            current_state.contacts
            if current_state is not None
            else ()
        )

        deleted_record = next(
            (
                record
                for record in current_contacts
                if record.contact_id == validated_contact_id
            ),
            None,
        )

        if deleted_record is None:
            raise AdminContactNotFoundError(
                document_id=validated_document_id,
                contact_id=validated_contact_id,
            )

        new_contacts = tuple(
            record
            for record in current_contacts
            if record.contact_id != validated_contact_id
        )

        committed_source_path, original_document_bytes = (
            _synchronize_source_document(
                source_directory=source_directory,
                document_metadata=document_metadata,
                country_code=country_code,
                all_records=new_contacts,
            )
        )

        try:
            _apply_contact_state_change(
                document_id=validated_document_id,
                country_code=country_code,
                source_directory=source_directory,
                new_contacts=new_contacts,
                document_metadata=document_metadata,
                client=opensearch_client,
                reset_marker=False,
            )

        except Exception:
            _restore_source_document(
                source_path=committed_source_path,
                original_bytes=original_document_bytes,
            )
            raise

    return AdminContactDeleteResponse(
        document_id=validated_document_id,
        country_code=country_code,
        contact_id=validated_contact_id,
    )


def _reseed_contacts_from_current_docx_locked(
    *,
    validated_document_id: str,
    source_directory: Path,
    opensearch_client: OpenSearch,
) -> AdminContactListResponse:
    """
    The real reseed-from-current-DOCX logic - always called with the
    country's lock already held by the caller.

    Split out from reseed_contacts_from_current_docx (mission "ORDER
    8G-B2") so safe_upload_and_index_document's own "identical bytes,
    Admin has changes, confirmed reseed" branch - which is already
    running inside its OWN country_lock at that point - can invoke
    this directly instead of the public, lock-ACQUIRING wrapper below:
    country_lock uses a plain flock() per freshly-opened file
    descriptor, which is not reentrant even within the same process/
    thread, so calling the public wrapper from inside an
    already-held lock for the same country would deadlock (observed
    directly while building this integration).
    """

    country_code, document_metadata = _load_country_code_and_metadata(
        document_id=validated_document_id,
        client=opensearch_client,
        operation="contact_reseed",
    )

    source_filename = _required_string(
        document_metadata,
        "source_filename",
    )
    country = _required_string(
        document_metadata,
        "country",
    )

    try:
        resolved_source = resolve_document_source_path(
            source_root=source_directory,
            country_code=country_code,
            source_filename=source_filename,
        )

    except DocumentSourceConflictError as error:
        raise AdminDocumentLifecycleError(
            "The source DOCX could not be unambiguously resolved."
        ) from error

    if resolved_source.path is None:
        raise AdminDocumentLifecycleError(
            "The source DOCX file is missing."
        )

    parsed_contacts = extract_contacts_from_docx(
        resolved_source.path,
        country=country,
    )

    new_contacts = _build_photo_aware_contact_records(
        source_directory=source_directory,
        docx_path=resolved_source.path,
        contacts=parsed_contacts,
    )

    _apply_contact_state_change(
        document_id=validated_document_id,
        country_code=country_code,
        source_directory=source_directory,
        new_contacts=new_contacts,
        document_metadata=document_metadata,
        client=opensearch_client,
        reset_marker=True,
    )

    return AdminContactListResponse(
        document_id=validated_document_id,
        country_code=country_code,
        contacts=[
            _contact_summary(record)
            for record in new_contacts
        ],
    )


def reseed_contacts_from_current_docx(
    *,
    document_id: str,
    source_directory: Path,
    client: OpenSearch | None = None,
    lock_timeout_seconds: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
) -> AdminContactListResponse:
    """
    Explicitly discard the current structured contact state (and any
    Admin edits it holds) and reseed it fresh from document_id's
    CURRENT on-disk source DOCX (mission "ORDER 8G-B1", section 12).

    safe_upload_and_index_document's own "uploaded bytes are already
    current" short-circuit deliberately performs zero mutation - this
    is the separate, explicit capability B2 calls after the Admin has
    confirmed they want to replace a country's document anyway, even
    though the freshly uploaded bytes happen to be byte-identical to
    what is already active. Nothing about the DOCX file itself changes
    here (there is nothing to change) - only the structured contact
    state, the derived Contact chunk, and the admin-modified marker.

    Acquires the country lock itself - the correct entry point for any
    STANDALONE caller (e.g. a future dedicated endpoint). A caller that
    already holds the lock for this country (safe_upload_and_index_
    document's own identical-bytes branch) must call
    _reseed_contacts_from_current_docx_locked directly instead, never
    this function, to avoid a self-deadlock.
    """

    validated_document_id = _validate_document_id(document_id)
    opensearch_client = _get_client(client)

    preliminary_metadata = _get_document_metadata(
        document_id=validated_document_id,
        client=opensearch_client,
    )

    with country_lock(
        source_directory,
        _required_string(preliminary_metadata, "country_code"),
        timeout_seconds=lock_timeout_seconds,
    ):
        return _reseed_contacts_from_current_docx_locked(
            validated_document_id=validated_document_id,
            source_directory=source_directory,
            opensearch_client=opensearch_client,
        )


def reseed_contact_state_from_parsed_contacts(
    *,
    document_id: str,
    country_code: str,
    source_directory: Path,
    contacts: Sequence[ExtractedContact],
    docx_path: Path | None = None,
) -> None:
    """
    Entirely replace structured contact state from a just-accepted
    DOCX.

    When docx_path is supplied, contact photos are extracted,
    deterministically associated, and persisted before the sidecar
    commit.

    This remains filesystem-only: the caller's DOCX/OpenSearch upload
    transaction has already committed. This function guarantees its
    own state/photo/marker atomicity but does not attempt to undo that
    earlier document commit.

    docx_path remains optional for backward compatibility with callers
    that seed already-parsed contacts without a source DOCX.
    """

    previous_state = read_contact_state(
        source_directory,
        document_id,
    )

    previous_marker = is_admin_modified_since_upload(
        source_directory,
        document_id,
    )

    if docx_path is None:
        new_contacts = tuple(
            _extracted_contact_to_record(contact)
            for contact in contacts
        )
    else:
        new_contacts = _build_photo_aware_contact_records(
            source_directory=source_directory,
            docx_path=docx_path,
            contacts=contacts,
        )

    previous_photo_filenames = _contact_photo_filenames(
        (
            previous_state.contacts
            if previous_state is not None
            else ()
        )
    )

    new_photo_filenames = _contact_photo_filenames(
        new_contacts
    )

    rollback_photo_filenames = (
        new_photo_filenames
        - previous_photo_filenames
    )

    superseded_photo_filenames = (
        previous_photo_filenames
        - new_photo_filenames
    )

    try:
        write_contact_state_atomic(
            source_directory,
            ContactState(
                document_id=document_id,
                country_code=country_code,
                contacts=new_contacts,
            ),
        )

        reset_admin_modified(
            source_directory,
            document_id,
        )

    except Exception as original_error:
        try:
            if previous_state is not None:
                write_contact_state_atomic(
                    source_directory,
                    previous_state,
                )
            else:
                delete_contact_state(
                    source_directory,
                    document_id,
                )

            write_admin_modified_marker(
                source_directory,
                document_id,
                previous_marker,
            )

            for filename in sorted(
                rollback_photo_filenames
            ):
                delete_contact_photo(
                    source_directory,
                    filename,
                )

        except Exception as rollback_error:
            raise AdminDocumentRollbackError(
                "Contact reseed failed and its filesystem rollback "
                "was itself incomplete - manual recovery is required."
            ) from rollback_error

        raise original_error

    # State + marker now authoritatively reference the new records.
    # Superseded files are only garbage at this point.
    for filename in sorted(
        superseded_photo_filenames
    ):
        try:
            delete_contact_photo(
                source_directory,
                filename,
            )
        except ContactPhotoStorageError:
            # Do not convert an already-successful authoritative
            # reseed into a false failure because stale unreferenced
            # garbage could not be removed.
            pass



def apply_structured_contact_state_to_chunks(
    *,
    chunks: list[Any],
    document_id: str,
    source_directory: Path,
) -> list[Any]:
    """
    Mission "ORDER 8G-B1", section 10 - "Refresh must not silently
    replace Admin-edited contacts with stale contact text from the
    DOCX".

    Given the chunk list a plain DOCX reparse just produced (which
    always includes a freshly DOCX-parsed Contact chunk, if any), swap
    that Contact chunk out for one built from the CURRENT structured
    contact state when one already exists - the legal/topic chunks in
    `chunks` are returned completely untouched either way. When no
    structured state exists yet for this document_id (a legacy
    document Reindexed before it was ever bootstrapped or touched by
    Admin Contact CRUD), `chunks` is returned exactly as received - the
    existing parsed-DOCX contact behavior - and no sidecar is created
    merely because this ran.
    """

    state = read_contact_state(source_directory, document_id)

    if state is None:
        return chunks

    non_contact_chunks = [
        chunk
        for chunk in chunks
        if not (
            chunk.legal_topic is None
            and chunk.subsection == "Contact"
        )
    ]

    if not non_contact_chunks and not chunks:
        return chunks

    reference_chunk = chunks[0]

    metadata = DocumentMetadata(
        country=reference_chunk.country,
        country_code=reference_chunk.country_code,
        reference_year=reference_chunk.reference_year,
        language=reference_chunk.language,
        source_filename=reference_chunk.source_filename,
        source_format=reference_chunk.source_format,
    )

    replacement_contact_chunk = build_contact_chunk_for_contacts(
        [
            _record_to_extracted_contact(record)
            for record in state.contacts
        ],
        metadata,
    )

    if replacement_contact_chunk is not None:
        non_contact_chunks.append(replacement_contact_chunk)

    return non_contact_chunks


@dataclass(frozen=True, slots=True)
class ContactBootstrapReport:
    """Result of running the controlled legacy contact bootstrap."""

    documents_seen: int
    contacts_seeded: int
    zero_contact_documents: int
    documents_skipped_existing_state: int
    dry_run: bool


def _default_document_lister(
    *,
    source_directory: Path,
    client: OpenSearch | None,
):
    from app.services.admin_documents import list_indexed_documents

    return list_indexed_documents(
        source_directory=source_directory,
        client=client,
    )


def bootstrap_legacy_contacts(
    *,
    source_directory: Path,
    client: OpenSearch | None = None,
    dry_run: bool = True,
    document_lister=_default_document_lister,
) -> ContactBootstrapReport:
    """
    Seed structured contact state for every currently-indexed document
    that does not already have one (mission "ORDER 8G-B1", section 6).

    Never overwrites an existing sidecar (a document already touched
    by Admin Contact CRUD, or already bootstrapped, is left completely
    alone). Not run automatically at startup or on every deploy -
    callers invoke this explicitly, exactly once, whenever it is
    genuinely needed. document_lister is injectable (defaulting to the
    real admin document catalog), the same dependency-injection
    convention already used throughout this codebase's own admin
    services, so tests can supply a fixed catalog without a real
    OpenSearch aggregation query.
    """

    opensearch_client = _get_client(client)

    catalog = document_lister(
        source_directory=source_directory,
        client=opensearch_client,
    )

    documents_seen = 0
    contacts_seeded = 0
    zero_contact_documents = 0
    documents_skipped_existing_state = 0

    for summary in catalog.documents:
        if not summary.source_file_present:
            continue

        if read_contact_state(
            source_directory,
            summary.document_id,
        ) is not None:
            documents_skipped_existing_state += 1
            continue

        try:
            resolved_source = resolve_document_source_path(
                source_root=source_directory,
                country_code=summary.country_code,
                source_filename=summary.source_filename,
            )

        except DocumentSourceConflictError:
            continue

        if resolved_source.path is None:
            continue

        documents_seen += 1

        parsed_contacts = extract_contacts_from_docx(
            resolved_source.path,
            country=summary.country,
        )

        new_contacts = tuple(
            _extracted_contact_to_record(contact)
            for contact in parsed_contacts
        )

        if new_contacts:
            contacts_seeded += len(new_contacts)
        else:
            zero_contact_documents += 1

        if not dry_run:
            write_contact_state_atomic(
                source_directory,
                ContactState(
                    document_id=summary.document_id,
                    country_code=summary.country_code,
                    contacts=new_contacts,
                ),
            )

    return ContactBootstrapReport(
        documents_seen=documents_seen,
        contacts_seeded=contacts_seeded,
        zero_contact_documents=zero_contact_documents,
        documents_skipped_existing_state=(
            documents_skipped_existing_state
        ),
        dry_run=dry_run,
    )
