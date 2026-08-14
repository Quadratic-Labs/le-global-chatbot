"""Mission "ORDER 3", section 17 - per-country lock behavior."""

from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path

from app.services.country_lock import (
    AdminDocumentOperationInProgressError,
    country_lock,
)


class CountryLockTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory()
        root = Path(self._tempdir.name)
        self.source_dir = root / "source"
        self.source_dir.mkdir(parents=True)

    def tearDown(self) -> None:
        self._tempdir.cleanup()

    def test_lock_directory_is_a_child_of_source_never_a_sibling(
        self,
    ) -> None:
        # Mission "ORDER 5": a sibling location (source_dir.parent)
        # requires write access to source_dir's own PARENT directory,
        # which real production does not grant (source_dir itself is
        # the writable bind mount; its parent is root-owned) - every
        # admin operation would fail the first time a lock is
        # acquired on a genuinely fresh deployment. Nesting inside
        # source_dir itself has no such dependency.
        with country_lock(self.source_dir, "CA"):
            pass

        self.assertFalse(
            (self.source_dir.parent / ".admin-locks").exists()
        )
        self.assertTrue(
            (self.source_dir / ".admin-locks" / "CA.lock")
            .exists()
        )
        # Never mistaken for a real source DOCX by anything that
        # lists source_dir's contents - document_source_resolver.py
        # never scans source_dir at all (it resolves specific,
        # expected filenames only), and nothing else does either.
        self.assertEqual(
            [
                path.name
                for path in self.source_dir.iterdir()
                if path.name != ".admin-locks"
            ],
            [],
        )

    def test_different_countries_do_not_block_each_other(self) -> None:
        acquired = []

        with country_lock(self.source_dir, "CL"):
            with country_lock(
                self.source_dir,
                "CO",
                timeout_seconds=1.0,
            ):
                acquired.append("both")

        self.assertEqual(acquired, ["both"])

    def test_same_country_blocks_a_concurrent_holder(self) -> None:
        release_first = threading.Event()
        first_holds_lock = threading.Event()

        def hold_lock() -> None:
            with country_lock(self.source_dir, "CA"):
                first_holds_lock.set()
                release_first.wait(timeout=5)

        thread = threading.Thread(target=hold_lock)
        thread.start()
        self.assertTrue(
            first_holds_lock.wait(timeout=2)
        )

        with self.assertRaises(
            AdminDocumentOperationInProgressError
        ) as context:
            with country_lock(
                self.source_dir,
                "CA",
                timeout_seconds=0.3,
            ):
                pass

        self.assertEqual(context.exception.country_code, "CA")

        release_first.set()
        thread.join(timeout=5)

    def test_lock_is_released_after_a_normal_exit(self) -> None:
        with country_lock(self.source_dir, "CA"):
            pass

        # A second, immediate acquisition must succeed - proves the
        # first one released cleanly rather than leaking.
        acquired = False

        with country_lock(self.source_dir, "CA", timeout_seconds=1.0):
            acquired = True

        self.assertTrue(acquired)

    def test_lock_is_released_even_when_the_block_raises(self) -> None:
        class _Boom(Exception):
            pass

        with self.assertRaises(_Boom):
            with country_lock(self.source_dir, "CA"):
                raise _Boom("simulated failure inside the lock")

        # Mission "ORDER 3", section 17: "Tester la libération du lock
        # sur exception." - a fresh acquisition must succeed promptly,
        # not time out waiting on a lock the failed holder never
        # released.
        acquired = False

        with country_lock(self.source_dir, "CA", timeout_seconds=1.0):
            acquired = True

        self.assertTrue(acquired)

    def test_country_code_is_case_and_whitespace_normalized(
        self,
    ) -> None:
        # "ca" and " CA " must contend for the exact same lock file as
        # "CA" - never three independent locks for one real country.
        with country_lock(self.source_dir, "ca"):
            with self.assertRaises(
                AdminDocumentOperationInProgressError
            ):
                with country_lock(
                    self.source_dir,
                    " CA ",
                    timeout_seconds=0.2,
                ):
                    pass

    def test_lock_file_itself_is_never_deleted(self) -> None:
        # An empty, persistent .lock file per country is expected and
        # normal (section 43: "autorisés uniquement dans le répertoire
        # dédié") - only the OS-level flock is released, not the file.
        with country_lock(self.source_dir, "CA"):
            pass

        lock_path = (
            self.source_dir / ".admin-locks" / "CA.lock"
        )
        self.assertTrue(lock_path.exists())
        self.assertEqual(lock_path.stat().st_size, 0)


if __name__ == "__main__":
    unittest.main()
