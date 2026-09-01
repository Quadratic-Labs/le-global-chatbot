"""Consolidated service contact_state.py; includes former contact_photo_store.py."""
from __future__ import annotations
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import tempfile
from typing import Final
import json
import uuid
PHOTO_STATE_DIRECTORY_NAME: Final[str] = '.admin-state'
PHOTO_STATE_SUBDIRECTORY_NAME: Final[str] = 'contact-photos'
_MAX_PHOTO_BYTES: Final[int] = 10 * 1024 * 1024
_ALLOWED_CONTENT_TYPES: Final[dict[str, str]] = {'image/jpeg': '.jpg', 'image/png': '.png', 'image/webp': '.webp'}
_SAFE_CONTACT_ID_RE = re.compile('^[A-Za-z0-9._-]+$')
_SAFE_FILENAME_RE = re.compile('^[A-Za-z0-9._-]+--[0-9a-f]{64}\\.(?:jpg|png|webp)$')

class ContactPhotoStorageError(RuntimeError):
    """Contact-photo persistence failed or an unsafe reference was used."""

@dataclass(frozen=True, slots=True)
class StoredContactPhoto:
    filename: str
    content_type: str
    sha256: str

def _photo_directory(source_directory: Path) -> Path:
    return Path(source_directory) / PHOTO_STATE_DIRECTORY_NAME / PHOTO_STATE_SUBDIRECTORY_NAME

def _validate_contact_id(contact_id: str) -> None:
    if not isinstance(contact_id, str) or not contact_id or (not _SAFE_CONTACT_ID_RE.fullmatch(contact_id)) or (contact_id in {'.', '..'}):
        raise ContactPhotoStorageError('Unsafe contact_id for photo storage.')

def _validate_filename(filename: str) -> None:
    if not isinstance(filename, str) or not filename or Path(filename).name != filename or ('/' in filename) or ('\\' in filename) or (not _SAFE_FILENAME_RE.fullmatch(filename)):
        raise ContactPhotoStorageError('Unsafe contact photo filename.')

def _extension_for_content_type(content_type: str) -> str:
    extension = _ALLOWED_CONTENT_TYPES.get(content_type)
    if extension is None:
        raise ContactPhotoStorageError('Unsupported contact photo content type.')
    return extension

def write_contact_photo_atomic(source_directory: Path, contact_id: str, *, data: bytes, content_type: str) -> StoredContactPhoto:
    """Persist one photo atomically and return its stable metadata.

    A new content hash creates a new filename. Therefore a failed
    replacement cannot overwrite the photo currently referenced by
    contact state.
    """
    _validate_contact_id(contact_id)
    if not isinstance(data, bytes) or not data:
        raise ContactPhotoStorageError('Contact photo data must be non-empty bytes.')
    if len(data) > _MAX_PHOTO_BYTES:
        raise ContactPhotoStorageError('Contact photo exceeds the maximum allowed size.')
    extension = _extension_for_content_type(content_type)
    digest = hashlib.sha256(data).hexdigest()
    filename = f'{contact_id}--{digest}{extension}'
    _validate_filename(filename)
    directory = _photo_directory(source_directory)
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ContactPhotoStorageError('Could not create contact photo directory.') from exc
    final_path = directory / filename
    if final_path.is_file():
        try:
            existing = final_path.read_bytes()
        except OSError as exc:
            raise ContactPhotoStorageError('Could not read existing contact photo.') from exc
        if hashlib.sha256(existing).hexdigest() == digest:
            return StoredContactPhoto(filename=filename, content_type=content_type, sha256=digest)
    file_descriptor: int | None = None
    temporary_path: Path | None = None
    try:
        file_descriptor, temporary_path_str = tempfile.mkstemp(prefix='.contact-photo-', suffix='.tmp', dir=directory)
        temporary_path = Path(temporary_path_str)
        with os.fdopen(file_descriptor, 'wb') as stream:
            file_descriptor = None
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, final_path)
        temporary_path = None
        try:
            directory_fd = os.open(directory, os.O_RDONLY)
        except OSError:
            directory_fd = None
        if directory_fd is not None:
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        return StoredContactPhoto(filename=filename, content_type=content_type, sha256=digest)
    except OSError as exc:
        raise ContactPhotoStorageError('Could not persist contact photo atomically.') from exc
    finally:
        if file_descriptor is not None:
            try:
                os.close(file_descriptor)
            except OSError:
                pass
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass

def read_contact_photo(source_directory: Path, filename: str) -> bytes:
    """Read one stored photo using an opaque validated filename."""
    _validate_filename(filename)
    path = _photo_directory(source_directory) / filename
    try:
        data = path.read_bytes()
    except (OSError, FileNotFoundError) as exc:
        raise ContactPhotoStorageError('Stored contact photo does not exist or cannot be read.') from exc
    if not data:
        raise ContactPhotoStorageError('Stored contact photo is empty.')
    return data

def delete_contact_photo(source_directory: Path, filename: str) -> None:
    """Delete one stored photo safely; missing files are already deleted."""
    _validate_filename(filename)
    path = _photo_directory(source_directory) / filename
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        raise ContactPhotoStorageError('Could not delete stored contact photo.') from exc
CONTACT_STATE_DIRECTORY_NAME: Final[str] = '.admin-state'
CONTACT_STATE_SUBDIRECTORY_NAME: Final[str] = 'contacts'
SCHEMA_VERSION: Final[int] = 1

class ContactStateError(RuntimeError):
    """Raised when the contact state file is corrupt or unusable."""

def new_contact_id() -> str:
    """
    A fresh, opaque, stable contact identifier.

    Never recomputed from a contact's own business fields: the product
    explicitly allows two contacts to share every field verbatim, so
    an identity derived from field content would collide for exactly
    that legitimate case. Assigned once, when a contact enters
    structured state (Add, or legacy bootstrap) - Edit never changes
    it.
    """
    return uuid.uuid4().hex

@dataclass(frozen=True, slots=True)
class ContactRecord:
    """
    One persisted contact.

    Every business field may be missing - a legacy contact parsed from
    an incomplete real DOCX (e.g. missing a phone number) must remain
    readable exactly as parsed; only the Admin write path enforces
    "all six fields required" (see app.models.admin_contacts), never
    this storage layer.
    """
    contact_id: str
    member_firm: str | None = None
    contact_person: str | None = None
    email: str | None = None
    phone: str | None = None
    address: str | None = None
    website: str | None = None
    photo_filename: str | None = None
    photo_content_type: str | None = None
    photo_sha256: str | None = None

    def to_json_dict(self) -> dict[str, object]:
        return {'contact_id': self.contact_id, 'member_firm': self.member_firm, 'contact_person': self.contact_person, 'email': self.email, 'phone': self.phone, 'address': self.address, 'website': self.website, 'photo_filename': self.photo_filename, 'photo_content_type': self.photo_content_type, 'photo_sha256': self.photo_sha256}

    @staticmethod
    def from_json_dict(payload: object) -> 'ContactRecord':
        if not isinstance(payload, dict):
            raise ContactStateError('One persisted contact entry must be a JSON object.')
        contact_id = payload.get('contact_id')
        if not isinstance(contact_id, str) or not contact_id:
            raise ContactStateError('A persisted contact is missing its contact_id.')

        def _optional_string(field_name: str) -> str | None:
            value = payload.get(field_name)
            if value is None:
                return None
            if not isinstance(value, str):
                raise ContactStateError(f'Contact field {field_name!r} must be a string or null.')
            return value
        photo_filename = _optional_string('photo_filename')
        photo_content_type = _optional_string('photo_content_type')
        photo_sha256 = _optional_string('photo_sha256')
        photo_values = (photo_filename, photo_content_type, photo_sha256)
        has_any_photo_metadata = any((value is not None for value in photo_values))
        has_all_photo_metadata = all((value is not None for value in photo_values))
        if has_any_photo_metadata and (not has_all_photo_metadata):
            raise ContactStateError('Persisted contact photo metadata must be either fully present or fully absent.')
        if photo_filename is not None:
            if Path(photo_filename).name != photo_filename or '/' in photo_filename or '\\' in photo_filename:
                raise ContactStateError('Persisted contact photo filename is unsafe.')
            if photo_content_type not in {'image/jpeg', 'image/png', 'image/webp'}:
                raise ContactStateError('Persisted contact photo content type is unsupported.')
            if photo_sha256 is None or len(photo_sha256) != 64 or any((character not in '0123456789abcdef' for character in photo_sha256)):
                raise ContactStateError('Persisted contact photo SHA-256 is invalid.')
        return ContactRecord(contact_id=contact_id, member_firm=_optional_string('member_firm'), contact_person=_optional_string('contact_person'), email=_optional_string('email'), phone=_optional_string('phone'), address=_optional_string('address'), website=_optional_string('website'), photo_filename=photo_filename, photo_content_type=photo_content_type, photo_sha256=photo_sha256)

@dataclass(frozen=True, slots=True)
class ContactState:
    """
    The full persisted contact state for one document_id.

    An explicitly empty `contacts` tuple is a legitimate, valid state -
    a document with zero configured contacts - entirely distinct from
    no ContactState existing at all for a document_id at all
    (read_contact_state returns None for that case, never an empty
    ContactState).
    """
    document_id: str
    country_code: str
    contacts: tuple[ContactRecord, ...] = ()

    def to_json_dict(self) -> dict[str, object]:
        return {'schema_version': SCHEMA_VERSION, 'document_id': self.document_id, 'country_code': self.country_code, 'contacts': [contact.to_json_dict() for contact in self.contacts]}

    @staticmethod
    def from_json_dict(payload: object) -> 'ContactState':
        if not isinstance(payload, dict):
            raise ContactStateError('Contact state file must contain a JSON object.')
        schema_version = payload.get('schema_version')
        if schema_version != SCHEMA_VERSION:
            raise ContactStateError(f'Unsupported contact state schema_version: {schema_version!r}.')
        document_id = payload.get('document_id')
        country_code = payload.get('country_code')
        raw_contacts = payload.get('contacts')
        if not isinstance(document_id, str) or not document_id or (not isinstance(country_code, str)) or (not country_code) or (not isinstance(raw_contacts, list)):
            raise ContactStateError('Contact state file is missing required fields.')
        contacts = tuple((ContactRecord.from_json_dict(item) for item in raw_contacts))
        contact_ids = [contact.contact_id for contact in contacts]
        if len(contact_ids) != len(set(contact_ids)):
            raise ContactStateError('Contact state file has duplicate contact_id values.')
        return ContactState(document_id=document_id, country_code=country_code, contacts=contacts)

def _state_directory(source_directory: Path) -> Path:
    return source_directory / CONTACT_STATE_DIRECTORY_NAME / CONTACT_STATE_SUBDIRECTORY_NAME

def _state_path(source_directory: Path, document_id: str) -> Path:
    return _state_directory(source_directory) / f'{document_id}.json'

def read_contact_state(source_directory: Path, document_id: str) -> ContactState | None:
    """
    None means no structured contact state has ever been created for
    this document_id - distinct from an explicit contacts=[] state,
    which is a real, persisted ContactState this function returns like
    any other.
    """
    path = _state_path(source_directory, document_id)
    if not path.is_file():
        return None
    try:
        raw_text = path.read_text(encoding='utf-8')
        payload = json.loads(raw_text)
    except (OSError, json.JSONDecodeError) as error:
        raise ContactStateError(f'Contact state file for {document_id!r} could not be read.') from error
    return ContactState.from_json_dict(payload)

def write_contact_state_atomic(source_directory: Path, state: ContactState) -> None:
    """
    Write the full contact state file for one document_id atomically.

    Same temp-file-in-the-same-directory + fsync + os.replace pattern
    already established by document_section_state.py's own
    write_section_edit_state_atomic - no partial JSON is ever visible,
    and a crash between the write and the replace leaves the OLD state
    file (or its absence) completely intact.
    """
    directory = _state_directory(source_directory)
    directory.mkdir(parents=True, exist_ok=True)
    final_path = _state_path(source_directory, state.document_id)
    file_descriptor, temporary_path_str = tempfile.mkstemp(prefix=f'.{state.document_id}-', suffix='.json.tmp', dir=directory)
    temporary_path = Path(temporary_path_str)
    try:
        with os.fdopen(file_descriptor, 'w', encoding='utf-8') as handle:
            json.dump(state.to_json_dict(), handle, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, final_path)
    except BaseException:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise

def delete_contact_state(source_directory: Path, document_id: str) -> None:
    """
    Remove all persisted contact state for one document_id.

    Silently a no-op when no state file exists. Used only when a
    document_id is fully retired (its identity changes on a
    replacement) - a genuine reseed instead overwrites the file with a
    new ContactState via write_contact_state_atomic, it never deletes
    then recreates it.
    """
    _state_path(source_directory, document_id).unlink(missing_ok=True)
