"""
Generic, durable "has this document's currently-served information been
changed through Admin since the last accepted DOCX upload?" marker
(mission "ORDER 8G-B1", section 13).

Deliberately not contact-specific: one small, atomic, per-document_id
JSON file - the same atomic-write pattern document_section_state.py
and contact_state.py already establish - meant to be reused verbatim
by every Admin content-mutation surface. Contact Add/Edit/Delete are
its only callers in this mission; a later mission wires Section
Add/Edit/Rename/Delete to the exact same mark_admin_modified() /
reset_admin_modified() calls, never a second, parallel tracking
mechanism.

This module only ever answers "was this document touched by Admin
since its last accepted upload" - it carries no opinion about WHAT was
touched (contacts, sections, or both).
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Final


MARKER_DIRECTORY_NAME: Final[str] = ".admin-state"
MARKER_SUBDIRECTORY_NAME: Final[str] = "modification-markers"

_SCHEMA_VERSION: Final[int] = 1


class AdminModificationMarkerError(RuntimeError):
    """Raised when the marker file is corrupt or unusable."""


@dataclass(frozen=True, slots=True)
class AdminModificationMarker:
    """Whether document_id's currently-served state has been changed
    through Admin since the last accepted DOCX upload/replacement."""

    document_id: str
    modified_since_upload: bool

    def to_json_dict(self) -> dict[str, object]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "document_id": self.document_id,
            "modified_since_upload": self.modified_since_upload,
        }

    @staticmethod
    def from_json_dict(payload: object) -> "AdminModificationMarker":
        if not isinstance(payload, dict):
            raise AdminModificationMarkerError(
                "Modification marker file must contain a JSON object."
            )

        schema_version = payload.get("schema_version")

        if schema_version != _SCHEMA_VERSION:
            raise AdminModificationMarkerError(
                "Unsupported modification marker schema_version: "
                f"{schema_version!r}."
            )

        document_id = payload.get("document_id")
        modified_since_upload = payload.get("modified_since_upload")

        if (
            not isinstance(document_id, str)
            or not document_id
            or not isinstance(modified_since_upload, bool)
        ):
            raise AdminModificationMarkerError(
                "Modification marker file is missing required fields."
            )

        return AdminModificationMarker(
            document_id=document_id,
            modified_since_upload=modified_since_upload,
        )


def _marker_directory(source_directory: Path) -> Path:
    return (
        source_directory
        / MARKER_DIRECTORY_NAME
        / MARKER_SUBDIRECTORY_NAME
    )


def _marker_path(source_directory: Path, document_id: str) -> Path:
    return _marker_directory(source_directory) / f"{document_id}.json"


def is_admin_modified_since_upload(
    source_directory: Path,
    document_id: str,
) -> bool:
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
        payload = json.loads(path.read_text(encoding="utf-8"))

    except (OSError, json.JSONDecodeError) as error:
        raise AdminModificationMarkerError(
            f"Modification marker for {document_id!r} could not be "
            "read."
        ) from error

    return AdminModificationMarker.from_json_dict(
        payload
    ).modified_since_upload


def write_admin_modified_marker(
    source_directory: Path,
    document_id: str,
    modified_since_upload: bool,
) -> None:
    """
    Write the marker file atomically - the one shared primitive behind
    mark_admin_modified/reset_admin_modified and behind restoring a
    prior value on rollback (all three are just "set the marker to a
    known boolean").
    """

    directory = _marker_directory(source_directory)
    directory.mkdir(parents=True, exist_ok=True)

    final_path = _marker_path(source_directory, document_id)

    file_descriptor, temporary_path_str = tempfile.mkstemp(
        prefix=f".{document_id}-",
        suffix=".json.tmp",
        dir=directory,
    )

    temporary_path = Path(temporary_path_str)

    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as handle:
            json.dump(
                AdminModificationMarker(
                    document_id=document_id,
                    modified_since_upload=modified_since_upload,
                ).to_json_dict(),
                handle,
                indent=2,
                sort_keys=True,
            )
            handle.flush()
            os.fsync(handle.fileno())

        os.replace(temporary_path, final_path)

    except BaseException:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass

        raise


def mark_admin_modified(
    source_directory: Path,
    document_id: str,
) -> None:
    """
    Record that document_id's currently-served state has just been
    changed through Admin.

    Called after ANY successful Admin content mutation - Contact
    Add/Edit/Delete now; Section Add/Edit/Rename/Delete in a later
    mission. Idempotent: marking an already-modified document again is
    a harmless overwrite. Never called by an ordinary Refresh/Reindex
    of the same DOCX.
    """

    write_admin_modified_marker(
        source_directory,
        document_id,
        True,
    )


def reset_admin_modified(
    source_directory: Path,
    document_id: str,
) -> None:
    """
    Record that document_id now exactly matches its last accepted DOCX
    upload.

    Called only after a genuinely new, confirmed DOCX
    upload/replacement has fully committed - never after a plain
    Reindex of the same DOCX, and never on a failed mutation.
    """

    write_admin_modified_marker(
        source_directory,
        document_id,
        False,
    )


def delete_admin_modified_marker(
    source_directory: Path,
    document_id: str,
) -> None:
    """
    Remove the marker file entirely.

    Used only when a document_id is fully retired (its identity
    changes on a replacement) - a genuine reset instead overwrites the
    file via reset_admin_modified, it never deletes then recreates it.
    """

    _marker_path(source_directory, document_id).unlink(missing_ok=True)
