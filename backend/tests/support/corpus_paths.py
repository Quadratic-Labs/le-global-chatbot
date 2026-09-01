"""
Test-only resolution of the DOCX source-document root.

Application code always resolves this via the required DOCUMENT_SOURCE_DIR
environment variable (app/core/config.py) - untouched by this module. Tests
that read real corpus files directly, bypassing app config, historically
hardcoded the production path instead. TEST_DOCUMENT_SOURCE_ROOT lets the
backend test suite point those tests at an alternate corpus (for example
the sanitized Gate-0B corpus) without touching application logic; when
unset, resolution is unchanged from the previous hardcoded behavior.
"""

import os
from pathlib import Path

DEFAULT_SOURCE_ROOT = Path("/data/documents/source")


def resolve_source_root() -> Path:
    """Return the DOCX source root tests should read from."""

    override = os.environ.get("TEST_DOCUMENT_SOURCE_ROOT")

    return Path(override) if override else DEFAULT_SOURCE_ROOT
