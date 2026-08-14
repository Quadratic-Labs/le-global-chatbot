"""
Per-country mutual exclusion for admin document lifecycle operations.

Mission "ORDER 3": ORDER 4 will send multiple admin uploads with real
concurrency. Nothing in the existing upload/reindex/delete code
serializes two operations against the same country_code - two
concurrent uploads for the same country could both observe "no
existing document" and both proceed to create one. No new
infrastructure is introduced (no Celery, no queue, no new DB): this is
a plain Linux file lock (fcntl.flock), scoped to one directory that is
never a source-file candidate (document_source_resolver.py only ever
resolves specific, expected filenames - never scans source_root - so
this directory's mere presence never affects source resolution).
"""

from __future__ import annotations

import errno
import fcntl
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


LOCK_DIRECTORY_NAME = ".admin-locks"

DEFAULT_LOCK_TIMEOUT_SECONDS: float = 15.0
_LOCK_POLL_INTERVAL_SECONDS: float = 0.05


class AdminDocumentOperationInProgressError(RuntimeError):
    """Raised when a country's lock cannot be acquired before timeout."""

    def __init__(self, *, country_code: str) -> None:
        self.country_code = country_code

        super().__init__(
            "Another admin operation is already in progress for "
            f"country {country_code!r}. Try again shortly."
        )


def _country_lock_path(
    source_directory: Path,
    country_code: str,
) -> Path:
    """
    <source_directory>/.admin-locks/<CODE>.lock - a child of
    source_directory, never a sibling of it.

    Mission "ORDER 5": a sibling location (source_directory.parent)
    requires write access to source_directory's own PARENT directory,
    which is not guaranteed - real production and a genuinely fresh
    deployment both mount source_directory itself as writable while
    leaving its parent root-owned, causing every admin operation to
    fail with a permission error the first time a lock is acquired.
    source_directory itself is always writable (it is where uploads
    are written), so nesting here has no such dependency.

    This is safe because document_source_resolver.py only ever
    resolves specific, expected filenames - never scans source_root -
    so a `.admin-locks` subdirectory's mere presence never affects
    source resolution (mission "ORDER 3", section 43's own concern:
    "ne doivent jamais être vus comme source document" - satisfied by
    never matching any real, expected filename, not by living
    outside source_directory).
    """

    lock_directory = (
        source_directory / LOCK_DIRECTORY_NAME
    )
    lock_directory.mkdir(parents=True, exist_ok=True)

    return lock_directory / f"{country_code.strip().upper()}.lock"


@contextmanager
def country_lock(
    source_directory: Path,
    country_code: str,
    timeout_seconds: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
) -> Iterator[None]:
    """
    Hold an exclusive lock for one country_code for the lifetime of
    the `with` block.

    Covers the critical section only (check current state, snapshot,
    filesystem mutation, OpenSearch mutation, commit/rollback) - never
    the initial DOCX parsing, which is CPU/IO-bound and does not need
    to be serialized per country. The lock file itself is never
    deleted (an empty, persistent .lock file per country is normal and
    expected - not a document, not a source, not a backup); only the
    OS-level flock is released, always, even on exception, since the
    file is closed in a `finally` block.
    """

    lock_path = _country_lock_path(
        source_directory,
        country_code,
    )

    file_descriptor = os.open(
        lock_path,
        os.O_CREAT | os.O_RDWR,
        0o600,
    )

    deadline = time.monotonic() + timeout_seconds

    try:
        while True:
            try:
                fcntl.flock(
                    file_descriptor,
                    fcntl.LOCK_EX | fcntl.LOCK_NB,
                )

                break

            except OSError as error:
                if error.errno not in (errno.EACCES, errno.EAGAIN):
                    raise

                if time.monotonic() >= deadline:
                    raise AdminDocumentOperationInProgressError(
                        country_code=country_code
                    ) from error

                time.sleep(_LOCK_POLL_INTERVAL_SECONDS)

        try:
            yield

        finally:
            fcntl.flock(
                file_descriptor,
                fcntl.LOCK_UN,
            )

    finally:
        os.close(file_descriptor)
