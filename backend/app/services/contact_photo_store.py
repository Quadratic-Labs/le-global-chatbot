"""Persistent storage for contact photos.

Photos are stored outside the JSON sidecar:

    .admin-state/contact-photos/
        <contact_id>--<sha256>.<extension>

The filename is content-addressed so a replacement can be written
without overwriting the currently referenced image.

This module contains no OpenSearch or HTTP logic.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import tempfile
from typing import Final


PHOTO_STATE_DIRECTORY_NAME: Final[str] = ".admin-state"
PHOTO_STATE_SUBDIRECTORY_NAME: Final[str] = "contact-photos"

_MAX_PHOTO_BYTES: Final[int] = 10 * 1024 * 1024

_ALLOWED_CONTENT_TYPES: Final[dict[str, str]] = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}

_SAFE_CONTACT_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_SAFE_FILENAME_RE = re.compile(
    r"^[A-Za-z0-9._-]+--[0-9a-f]{64}\.(?:jpg|png|webp)$"
)


class ContactPhotoStorageError(RuntimeError):
    """Contact-photo persistence failed or an unsafe reference was used."""


@dataclass(frozen=True, slots=True)
class StoredContactPhoto:
    filename: str
    content_type: str
    sha256: str


def _photo_directory(source_directory: Path) -> Path:
    return (
        Path(source_directory)
        / PHOTO_STATE_DIRECTORY_NAME
        / PHOTO_STATE_SUBDIRECTORY_NAME
    )


def _validate_contact_id(contact_id: str) -> None:
    if (
        not isinstance(contact_id, str)
        or not contact_id
        or not _SAFE_CONTACT_ID_RE.fullmatch(contact_id)
        or contact_id in {".", ".."}
    ):
        raise ContactPhotoStorageError(
            "Unsafe contact_id for photo storage."
        )


def _validate_filename(filename: str) -> None:
    if (
        not isinstance(filename, str)
        or not filename
        or Path(filename).name != filename
        or "/" in filename
        or "\\" in filename
        or not _SAFE_FILENAME_RE.fullmatch(filename)
    ):
        raise ContactPhotoStorageError(
            "Unsafe contact photo filename."
        )


def _extension_for_content_type(content_type: str) -> str:
    extension = _ALLOWED_CONTENT_TYPES.get(content_type)

    if extension is None:
        raise ContactPhotoStorageError(
            "Unsupported contact photo content type."
        )

    return extension


def write_contact_photo_atomic(
    source_directory: Path,
    contact_id: str,
    *,
    data: bytes,
    content_type: str,
) -> StoredContactPhoto:
    """Persist one photo atomically and return its stable metadata.

    A new content hash creates a new filename. Therefore a failed
    replacement cannot overwrite the photo currently referenced by
    contact state.
    """

    _validate_contact_id(contact_id)

    if not isinstance(data, bytes) or not data:
        raise ContactPhotoStorageError(
            "Contact photo data must be non-empty bytes."
        )

    if len(data) > _MAX_PHOTO_BYTES:
        raise ContactPhotoStorageError(
            "Contact photo exceeds the maximum allowed size."
        )

    extension = _extension_for_content_type(content_type)

    digest = hashlib.sha256(data).hexdigest()
    filename = f"{contact_id}--{digest}{extension}"

    _validate_filename(filename)

    directory = _photo_directory(source_directory)

    try:
        directory.mkdir(
            parents=True,
            exist_ok=True,
        )
    except OSError as exc:
        raise ContactPhotoStorageError(
            "Could not create contact photo directory."
        ) from exc

    final_path = directory / filename

    # Content-addressed write: an existing valid object is already the
    # exact logical target. Verify its content before treating it as
    # idempotent.
    if final_path.is_file():
        try:
            existing = final_path.read_bytes()
        except OSError as exc:
            raise ContactPhotoStorageError(
                "Could not read existing contact photo."
            ) from exc

        if hashlib.sha256(existing).hexdigest() == digest:
            return StoredContactPhoto(
                filename=filename,
                content_type=content_type,
                sha256=digest,
            )

    file_descriptor: int | None = None
    temporary_path: Path | None = None

    try:
        file_descriptor, temporary_path_str = tempfile.mkstemp(
            prefix=".contact-photo-",
            suffix=".tmp",
            dir=directory,
        )

        temporary_path = Path(temporary_path_str)

        with os.fdopen(
            file_descriptor,
            "wb",
        ) as stream:
            file_descriptor = None
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())

        os.replace(
            temporary_path,
            final_path,
        )

        temporary_path = None

        # Persist the directory-entry update as strongly as practical.
        try:
            directory_fd = os.open(
                directory,
                os.O_RDONLY,
            )
        except OSError:
            directory_fd = None

        if directory_fd is not None:
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)

        return StoredContactPhoto(
            filename=filename,
            content_type=content_type,
            sha256=digest,
        )

    except OSError as exc:
        raise ContactPhotoStorageError(
            "Could not persist contact photo atomically."
        ) from exc

    finally:
        if file_descriptor is not None:
            try:
                os.close(file_descriptor)
            except OSError:
                pass

        if temporary_path is not None:
            try:
                temporary_path.unlink(
                    missing_ok=True,
                )
            except OSError:
                pass


def read_contact_photo(
    source_directory: Path,
    filename: str,
) -> bytes:
    """Read one stored photo using an opaque validated filename."""

    _validate_filename(filename)

    path = (
        _photo_directory(source_directory)
        / filename
    )

    try:
        data = path.read_bytes()
    except (OSError, FileNotFoundError) as exc:
        raise ContactPhotoStorageError(
            "Stored contact photo does not exist or cannot be read."
        ) from exc

    if not data:
        raise ContactPhotoStorageError(
            "Stored contact photo is empty."
        )

    return data


def delete_contact_photo(
    source_directory: Path,
    filename: str,
) -> None:
    """Delete one stored photo safely; missing files are already deleted."""

    _validate_filename(filename)

    path = (
        _photo_directory(source_directory)
        / filename
    )

    try:
        path.unlink(
            missing_ok=True,
        )
    except OSError as exc:
        raise ContactPhotoStorageError(
            "Could not delete stored contact photo."
        ) from exc
