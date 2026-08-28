"""Tests for the TEST_DOCUMENT_SOURCE_ROOT override seam (GATE 0B-FINAL)."""

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.corpus_paths import DEFAULT_SOURCE_ROOT, resolve_source_root


class ResolveSourceRootTests(unittest.TestCase):
    def test_override_absent_returns_the_default_source_root(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TEST_DOCUMENT_SOURCE_ROOT", None)

            self.assertEqual(
                resolve_source_root(),
                DEFAULT_SOURCE_ROOT,
            )

    def test_override_present_returns_the_overridden_root(self) -> None:
        with patch.dict(
            os.environ,
            {"TEST_DOCUMENT_SOURCE_ROOT": "/var/tmp/some-sanitized-corpus"},
        ):
            self.assertEqual(
                resolve_source_root(),
                Path("/var/tmp/some-sanitized-corpus"),
            )

    def test_override_present_but_empty_still_uses_the_default(self) -> None:
        with patch.dict(
            os.environ,
            {"TEST_DOCUMENT_SOURCE_ROOT": ""},
        ):
            self.assertEqual(
                resolve_source_root(),
                DEFAULT_SOURCE_ROOT,
            )


if __name__ == "__main__":
    unittest.main()
