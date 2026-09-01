"""Consolidated service document_section_state.py; includes former admin_modification_marker.py."""
from __future__ import annotations
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Final
import re
from dataclasses import dataclass, field
from app.core.legal_taxonomy import LEGAL_TOPICS
MARKER_DIRECTORY_NAME: Final[str] = '.admin-state'
MARKER_SUBDIRECTORY_NAME: Final[str] = 'modification-markers'
_MODIFICATION_MARKER_SCHEMA_VERSION: Final[int] = 1

class AdminModificationMarkerError(RuntimeError):
    """Raised when the marker file is corrupt or unusable."""

@dataclass(frozen=True, slots=True)
class AdminModificationMarker:
    """Whether document_id's currently-served state has been changed
    through Admin since the last accepted DOCX upload/replacement."""
    document_id: str
    modified_since_upload: bool

    def to_json_dict(self) -> dict[str, object]:
        return {'schema_version': _MODIFICATION_MARKER_SCHEMA_VERSION, 'document_id': self.document_id, 'modified_since_upload': self.modified_since_upload}

    @staticmethod
    def from_json_dict(payload: object) -> 'AdminModificationMarker':
        if not isinstance(payload, dict):
            raise AdminModificationMarkerError('Modification marker file must contain a JSON object.')
        schema_version = payload.get('schema_version')
        if schema_version != _MODIFICATION_MARKER_SCHEMA_VERSION:
            raise AdminModificationMarkerError(f'Unsupported modification marker schema_version: {schema_version!r}.')
        document_id = payload.get('document_id')
        modified_since_upload = payload.get('modified_since_upload')
        if not isinstance(document_id, str) or not document_id or (not isinstance(modified_since_upload, bool)):
            raise AdminModificationMarkerError('Modification marker file is missing required fields.')
        return AdminModificationMarker(document_id=document_id, modified_since_upload=modified_since_upload)

def _marker_directory(source_directory: Path) -> Path:
    return source_directory / MARKER_DIRECTORY_NAME / MARKER_SUBDIRECTORY_NAME

def _marker_path(source_directory: Path, document_id: str) -> Path:
    return _marker_directory(source_directory) / f'{document_id}.json'

def is_admin_modified_since_upload(source_directory: Path, document_id: str) -> bool:
    """
    False when no marker file exists.

    A document that has never been touched by any Admin mutation since
    it was last (re)uploaded is, by definition, not modified since
    upload - so an absent marker file and an explicit
    modified_since_upload=False marker are equivalent, and this
    function never distinguishes between them.
    """
    path = _marker_path(source_directory, document_id)
    if not path.is_file():
        return False
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as error:
        raise AdminModificationMarkerError(f'Modification marker for {document_id!r} could not be read.') from error
    return AdminModificationMarker.from_json_dict(payload).modified_since_upload

def write_admin_modified_marker(source_directory: Path, document_id: str, modified_since_upload: bool) -> None:
    """
    Write the marker file atomically - the one shared primitive behind
    mark_admin_modified/reset_admin_modified and behind restoring a
    prior value on rollback (all three are just "set the marker to a
    known boolean").
    """
    directory = _marker_directory(source_directory)
    directory.mkdir(parents=True, exist_ok=True)
    final_path = _marker_path(source_directory, document_id)
    file_descriptor, temporary_path_str = tempfile.mkstemp(prefix=f'.{document_id}-', suffix='.json.tmp', dir=directory)
    temporary_path = Path(temporary_path_str)
    try:
        with os.fdopen(file_descriptor, 'w', encoding='utf-8') as handle:
            json.dump(AdminModificationMarker(document_id=document_id, modified_since_upload=modified_since_upload).to_json_dict(), handle, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, final_path)
    except BaseException:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise

def mark_admin_modified(source_directory: Path, document_id: str) -> None:
    """
    Record that document_id's currently-served state has just been
    changed through Admin.

    Called after ANY successful Admin content mutation - Contact
    Add/Edit/Delete now; Section Add/Edit/Rename/Delete in a later
    mission. Idempotent: marking an already-modified document again is
    a harmless overwrite. Never called by an ordinary Refresh/Reindex
    of the same DOCX.
    """
    write_admin_modified_marker(source_directory, document_id, True)

def reset_admin_modified(source_directory: Path, document_id: str) -> None:
    """
    Record that document_id now exactly matches its last accepted DOCX
    upload.

    Called only after a genuinely new, confirmed DOCX
    upload/replacement has fully committed - never after a plain
    Reindex of the same DOCX, and never on a failed mutation.
    """
    write_admin_modified_marker(source_directory, document_id, False)

def delete_admin_modified_marker(source_directory: Path, document_id: str) -> None:
    """
    Remove the marker file entirely.

    Used only when a document_id is fully retired (its identity
    changes on a replacement) - a genuine reset instead overwrites the
    file via reset_admin_modified, it never deletes then recreates it.
    """
    _marker_path(source_directory, document_id).unlink(missing_ok=True)
SECTION_STATE_DIRECTORY_NAME: Final[str] = '.admin-state'
SECTION_EDITS_SUBDIRECTORY_NAME: Final[str] = 'section-edits'
_SCHEMA_VERSION: Final[int] = 1
_SLUG_PATTERN: Final[re.Pattern[str]] = re.compile('[^a-z0-9]+')

class SectionStateError(RuntimeError):
    """Raised when the section-edit state file is corrupt or unusable."""

def section_id_for_legal_topic(legal_topic: str) -> str:
    """
    A controlled, deterministic identifier for one canonical legal
    topic - never derived from client input (mission "ORDER 5C",
    section 16: "pas de raw path provenant du client"). Built once
    from the fixed, small LEGAL_TOPICS tuple, never from a client-
    supplied string.
    """
    slug = _SLUG_PATTERN.sub('_', legal_topic.strip().lower()).strip('_')
    return slug

@dataclass(frozen=True, slots=True)
class SectionEdit:
    """One persisted, currently-effective edited section."""
    legal_topic: str
    section: str
    subsection: str | None
    content: str

@dataclass(frozen=True, slots=True)
class SectionEditState:
    """The full persisted edit state for one document_id."""
    document_id: str
    country_code: str
    sections: dict[str, SectionEdit] = field(default_factory=dict)

    def to_json_dict(self) -> dict[str, object]:
        return {'schema_version': _SCHEMA_VERSION, 'document_id': self.document_id, 'country_code': self.country_code, 'sections': {section_id: {'legal_topic': edit.legal_topic, 'section': edit.section, 'subsection': edit.subsection, 'content': edit.content} for section_id, edit in self.sections.items()}}

    @staticmethod
    def from_json_dict(payload: object) -> 'SectionEditState':
        if not isinstance(payload, dict):
            raise SectionStateError('Section-edit state file must contain a JSON object.')
        schema_version = payload.get('schema_version')
        if schema_version != _SCHEMA_VERSION:
            raise SectionStateError(f'Unsupported section-edit state schema_version: {schema_version!r}.')
        document_id = payload.get('document_id')
        country_code = payload.get('country_code')
        raw_sections = payload.get('sections')
        if not isinstance(document_id, str) or not document_id or (not isinstance(country_code, str)) or (not country_code) or (not isinstance(raw_sections, dict)):
            raise SectionStateError('Section-edit state file is missing required fields.')
        sections: dict[str, SectionEdit] = {}
        for section_id, raw_edit in raw_sections.items():
            if not isinstance(raw_edit, dict):
                raise SectionStateError(f'Section-edit entry {section_id!r} is malformed.')
            legal_topic = raw_edit.get('legal_topic')
            section = raw_edit.get('section')
            subsection = raw_edit.get('subsection')
            content = raw_edit.get('content')
            if not isinstance(legal_topic, str) or not legal_topic or (not isinstance(section, str)) or (not section) or (not (subsection is None or isinstance(subsection, str))) or (not isinstance(content, str)):
                raise SectionStateError(f'Section-edit entry {section_id!r} is malformed.')
            sections[section_id] = SectionEdit(legal_topic=legal_topic, section=section, subsection=subsection, content=content)
        return SectionEditState(document_id=document_id, country_code=country_code, sections=sections)

def _state_directory(source_directory: Path) -> Path:
    return source_directory / SECTION_STATE_DIRECTORY_NAME / SECTION_EDITS_SUBDIRECTORY_NAME

def _state_path(source_directory: Path, document_id: str) -> Path:
    return _state_directory(source_directory) / f'{document_id}.json'

def read_section_edit_state(source_directory: Path, document_id: str) -> SectionEditState | None:
    """None means no section has ever been edited for this document."""
    path = _state_path(source_directory, document_id)
    if not path.is_file():
        return None
    try:
        raw_text = path.read_text(encoding='utf-8')
        payload = json.loads(raw_text)
    except (OSError, json.JSONDecodeError) as error:
        raise SectionStateError(f'Section-edit state file for {document_id!r} could not be read.') from error
    return SectionEditState.from_json_dict(payload)

def write_section_edit_state_atomic(source_directory: Path, state: SectionEditState) -> None:
    """
    Write the full state file for one document_id atomically.

    Mission "ORDER 5C", section 17: write a temporary file in the
    SAME directory as the real target (so os.replace is an atomic
    rename on the same filesystem, never a cross-device copy), flush
    it fully, then os.replace onto the real path - no partial JSON is
    ever visible, and a crash between the write and the replace
    leaves the OLD state file completely intact.
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

def delete_section_edit_state(source_directory: Path, document_id: str) -> None:
    """
    Remove all persisted edits for one document_id - mission "ORDER
    5C", sections 34/38: a confirmed document replace or a successful
    delete must leave zero old edit state behind. Silently a no-op
    when no state file exists (never edited, or already removed).
    """
    _state_path(source_directory, document_id).unlink(missing_ok=True)
