"""
Tests for generic country-conflict review and resolution (mission
"ORDER 8E-A1", sections 18/22-28).

Explicitly generic: every scenario here uses invented countries/
document_ids, never Italy - the real Italy case must pass only because
of the same generic evidence these tests exercise, never a special
case for it.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from app.services.admin_document_conflict_resolution import (
    AUTO_DEDUPLICATE,
    CHOOSE_DOCUMENT,
    CountryConflictNotFoundError,
    CountryConflictResolutionError,
    build_country_conflict_review,
    resolve_country_conflict,
)
from app.services.document_section_state import (
    SectionEdit,
    SectionEditState,
    read_section_edit_state,
    section_id_for_legal_topic,
    write_section_edit_state_atomic,
)


class FakeConflictOpenSearch:
    """In-memory OpenSearch double at chunk granularity."""

    def __init__(self) -> None:
        self.chunks: dict[str, dict[str, Any]] = {}
        self.fail_delete_once = False

    def add_chunk(
        self,
        *,
        chunk_id: str,
        document_id: str,
        country_code: str,
        source_filename: str,
        reference_year: int | None = None,
        country: str = "Testland",
    ) -> None:
        self.chunks[chunk_id] = {
            "document_id": document_id,
            "chunk_id": chunk_id,
            "country_code": country_code,
            "country": country,
            "source_filename": source_filename,
            "reference_year": reference_year,
            "language": "en",
            "document_type": "comparator",
        }

    def search(self, *, index: str, body: dict[str, Any]) -> dict[str, Any]:
        del index
        term = body["query"].get("term") or {}

        if "country_code" in term:
            hits = [
                c for c in self.chunks.values()
                if c["country_code"] == term["country_code"]
            ]
        elif "document_id" in term:
            hits = [
                c for c in self.chunks.values()
                if c["document_id"] == term["document_id"]
            ]
        else:
            hits = list(self.chunks.values())

        return {
            "hits": {
                "total": {"value": len(hits)},
                "hits": [
                    {
                        "_id": h["chunk_id"],
                        "_source": h,
                        "sort": [h["chunk_id"]],
                    }
                    for h in sorted(hits, key=lambda c: c["chunk_id"])
                ],
            }
        }

    def delete_by_query(self, **kwargs: Any) -> dict[str, Any]:
        if self.fail_delete_once:
            self.fail_delete_once = False
            raise RuntimeError("simulated OpenSearch failure")

        query = kwargs["body"]["query"]
        must_filters = query["bool"]["filter"]
        must_not = query["bool"].get("must_not") or []
        keep_ids = set()

        for clause in must_not:
            keep_ids.update(clause.get("terms", {}).get("chunk_id", []))

        country_code = None
        for f in must_filters:
            if "term" in f and "country_code" in f["term"]:
                country_code = f["term"]["country_code"]

        deleted = 0
        for chunk_id in list(self.chunks):
            chunk = self.chunks[chunk_id]
            if country_code is not None and chunk["country_code"] != country_code:
                continue
            if chunk_id in keep_ids:
                continue
            del self.chunks[chunk_id]
            deleted += 1

        return {"deleted": deleted}


def _fake_bulk_factory(store: dict[str, dict[str, Any]]):
    def _fake_bulk(*, client, actions, **kwargs):
        del client, kwargs
        count = 0
        for action in actions:
            store[action["_id"]] = dict(action["_source"])
            count += 1
        return count, []

    return _fake_bulk


class ConflictReviewTests(unittest.TestCase):
    def test_review_raises_when_country_is_not_actually_conflicted(
        self,
    ) -> None:
        fake = FakeConflictOpenSearch()
        fake.add_chunk(
            chunk_id="c1",
            document_id="doc_a",
            country_code="ZZ".replace("ZZ", "FR"),
            source_filename="single.docx",
        )

        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root)

            with self.assertRaises(CountryConflictNotFoundError):
                build_country_conflict_review(
                    "FR",
                    source_directory=source_directory,
                    client=fake,
                )

    def test_review_exposes_only_safe_candidate_fields(self) -> None:
        fake = FakeConflictOpenSearch()
        fake.add_chunk(
            chunk_id="c1",
            document_id="doc_a",
            country_code="FR",
            source_filename="France-2024.docx",
            reference_year=2024,
        )
        fake.add_chunk(
            chunk_id="c2",
            document_id="doc_b",
            country_code="FR",
            source_filename="France-legacy.docx",
            reference_year=2026,
        )

        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root)
            (source_directory / "France-2024.docx").write_bytes(
                b"content-a"
            )
            (source_directory / "France-legacy.docx").write_bytes(
                b"content-b-different"
            )

            review = build_country_conflict_review(
                "fr",
                source_directory=source_directory,
                client=fake,
            )

            self.assertEqual(review.country_code, "FR")
            self.assertEqual(len(review.candidates), 2)
            self.assertFalse(review.auto_deduplicate_available)

            filenames = {c.source_filename for c in review.candidates}
            self.assertEqual(
                filenames, {"France-2024.docx", "France-legacy.docx"}
            )

    def test_auto_deduplicate_available_when_content_is_identical(
        self,
    ) -> None:
        fake = FakeConflictOpenSearch()
        fake.add_chunk(
            chunk_id="c1",
            document_id="doc_a",
            country_code="DE",
            source_filename="Germany-old.docx",
        )
        fake.add_chunk(
            chunk_id="c2",
            document_id="doc_b",
            country_code="DE",
            source_filename="DE.docx",
        )

        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root)
            identical_content = b"exact-same-bytes-both-files"
            (source_directory / "Germany-old.docx").write_bytes(
                identical_content
            )
            (source_directory / "DE.docx").write_bytes(
                identical_content
            )

            review = build_country_conflict_review(
                "DE",
                source_directory=source_directory,
                client=fake,
            )

            self.assertTrue(review.auto_deduplicate_available)


class AutoDeduplicateResolutionTests(unittest.TestCase):
    def test_auto_deduplicate_keeps_the_most_recent_and_removes_the_other(
        self,
    ) -> None:
        # Both use distinct, non-canonical legacy names (never the
        # canonical DE.docx) so each document's own resolution stays
        # unambiguous - resolve_document_source_path always also
        # tries the canonical name as a fallback candidate, which
        # would otherwise make either one's own per-document
        # resolution ambiguous purely because the OTHER conflicting
        # record happened to already occupy that canonical path.
        fake = FakeConflictOpenSearch()
        fake.add_chunk(
            chunk_id="c1",
            document_id="doc_old",
            country_code="DE",
            source_filename="Germany-2020.docx",
            reference_year=2020,
        )
        fake.add_chunk(
            chunk_id="c2",
            document_id="doc_new",
            country_code="DE",
            source_filename="Germany-2026.docx",
            reference_year=2026,
        )

        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root)
            identical_content = b"exact-same-bytes"
            old_path = source_directory / "Germany-2020.docx"
            new_path = source_directory / "Germany-2026.docx"
            old_path.write_bytes(identical_content)
            new_path.write_bytes(identical_content)

            write_section_edit_state_atomic(
                source_directory,
                SectionEditState(
                    document_id="doc_old",
                    country_code="DE",
                    sections={
                        section_id_for_legal_topic(
                            "Employment Contracts"
                        ): SectionEdit(
                            legal_topic="Employment Contracts",
                            section="Employment Contracts",
                            subsection=None,
                            content="Stale edit for the removed doc.",
                        ),
                    },
                ),
            )

            result = resolve_country_conflict(
                "de",
                AUTO_DEDUPLICATE,
                source_directory=source_directory,
                client=fake,
            )

            self.assertEqual(result.country_code, "DE")
            self.assertEqual(result.resolution_mode, AUTO_DEDUPLICATE)
            # Neither filename matches the canonical name, so the
            # tie-break falls back to the most recent reference_year.
            self.assertEqual(result.kept_document_id, "doc_new")
            self.assertEqual(
                result.removed_document_ids, ("doc_old",)
            )

            remaining_ids = {
                c["document_id"] for c in fake.chunks.values()
            }
            self.assertEqual(remaining_ids, {"doc_new"})

            # The older duplicate file was safely removed - the kept
            # document's own file remains untouched.
            self.assertFalse(old_path.exists())
            self.assertTrue(new_path.exists())
            self.assertEqual(
                new_path.read_bytes(), identical_content
            )

            # Stale section-edit state for the removed document_id is
            # cleared, exactly like a confirmed upload replace already
            # does.
            self.assertIsNone(
                read_section_edit_state(source_directory, "doc_old")
            )

    def test_auto_deduplicate_refused_without_strong_evidence(
        self,
    ) -> None:
        fake = FakeConflictOpenSearch()
        fake.add_chunk(
            chunk_id="c1",
            document_id="doc_a",
            country_code="JP",
            source_filename="Japan-a.docx",
        )
        fake.add_chunk(
            chunk_id="c2",
            document_id="doc_b",
            country_code="JP",
            source_filename="Japan-b.docx",
        )

        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root)
            (source_directory / "Japan-a.docx").write_bytes(
                b"genuinely different content A"
            )
            (source_directory / "Japan-b.docx").write_bytes(
                b"genuinely different content B, much longer than A"
            )

            with self.assertRaises(CountryConflictResolutionError):
                resolve_country_conflict(
                    "JP",
                    AUTO_DEDUPLICATE,
                    source_directory=source_directory,
                    client=fake,
                )

            # Zero mutation - nothing was touched by the refused
            # request.
            self.assertEqual(len(fake.chunks), 2)
            self.assertTrue(
                (source_directory / "Japan-a.docx").exists()
            )
            self.assertTrue(
                (source_directory / "Japan-b.docx").exists()
            )


class ChooseDocumentResolutionTests(unittest.TestCase):
    def test_choose_document_keeps_the_selected_one(self) -> None:
        fake = FakeConflictOpenSearch()
        fake.add_chunk(
            chunk_id="c1",
            document_id="doc_a",
            country_code="PT",
            source_filename="Portugal-a.docx",
            reference_year=2020,
        )
        fake.add_chunk(
            chunk_id="c2",
            document_id="doc_b",
            country_code="PT",
            source_filename="Portugal-b.docx",
            reference_year=2026,
        )

        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root)
            path_a = source_directory / "Portugal-a.docx"
            path_b = source_directory / "Portugal-b.docx"
            path_a.write_bytes(b"content A")
            path_b.write_bytes(b"content B, genuinely different")

            result = resolve_country_conflict(
                "PT",
                CHOOSE_DOCUMENT,
                source_directory=source_directory,
                keep_document_id="doc_b",
                client=fake,
            )

            self.assertEqual(result.kept_document_id, "doc_b")
            self.assertEqual(result.removed_document_ids, ("doc_a",))

            remaining_ids = {
                c["document_id"] for c in fake.chunks.values()
            }
            self.assertEqual(remaining_ids, {"doc_b"})

            # The non-chosen document's own distinct file is removed;
            # the chosen document's file is left exactly as-is.
            self.assertFalse(path_a.exists())
            self.assertTrue(path_b.exists())
            self.assertEqual(
                path_b.read_bytes(), b"content B, genuinely different"
            )

    def test_choose_document_rejects_a_stale_document_id(self) -> None:
        fake = FakeConflictOpenSearch()
        fake.add_chunk(
            chunk_id="c1",
            document_id="doc_a",
            country_code="PL",
            source_filename="Poland-a.docx",
        )
        fake.add_chunk(
            chunk_id="c2",
            document_id="doc_b",
            country_code="PL",
            source_filename="Poland-b.docx",
        )

        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root)
            (source_directory / "Poland-a.docx").write_bytes(b"a")
            (source_directory / "Poland-b.docx").write_bytes(b"b")

            with self.assertRaises(CountryConflictResolutionError):
                resolve_country_conflict(
                    "PL",
                    CHOOSE_DOCUMENT,
                    source_directory=source_directory,
                    # Not one of the two current candidates - proves
                    # the conflict state is revalidated immediately
                    # before mutation, never trusted from an earlier
                    # client-side review.
                    keep_document_id="doc_stale_from_an_old_review",
                    client=fake,
                )

            self.assertEqual(len(fake.chunks), 2)

    def test_resolution_refused_when_country_is_no_longer_conflicted(
        self,
    ) -> None:
        # Simulates a race: the Admin's review was taken when the
        # country had 2 documents, but by the time they submit their
        # resolution choice, something else already collapsed it to 1.
        fake = FakeConflictOpenSearch()
        fake.add_chunk(
            chunk_id="c1",
            document_id="doc_a",
            country_code="NO",
            source_filename="Norway.docx",
        )

        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root)
            (source_directory / "Norway.docx").write_bytes(b"content")

            with self.assertRaises(CountryConflictNotFoundError):
                resolve_country_conflict(
                    "NO",
                    CHOOSE_DOCUMENT,
                    source_directory=source_directory,
                    keep_document_id="doc_a",
                    client=fake,
                )


class ResolutionRollbackTests(unittest.TestCase):
    def test_opensearch_failure_restores_files_and_index_state(
        self,
    ) -> None:
        fake = FakeConflictOpenSearch()
        fake.add_chunk(
            chunk_id="c1",
            document_id="doc_a",
            country_code="SE",
            source_filename="Sweden-a.docx",
        )
        fake.add_chunk(
            chunk_id="c2",
            document_id="doc_b",
            country_code="SE",
            source_filename="Sweden-b.docx",
        )
        fake.fail_delete_once = True

        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root)
            path_a = source_directory / "Sweden-a.docx"
            path_b = source_directory / "Sweden-b.docx"
            path_a.write_bytes(b"content A")
            path_b.write_bytes(b"content B different")

            with patch(
                "app.services.document_indexer.bulk",
                new=_fake_bulk_factory(fake.chunks),
            ):
                with self.assertRaises(RuntimeError):
                    resolve_country_conflict(
                        "SE",
                        CHOOSE_DOCUMENT,
                        source_directory=source_directory,
                        keep_document_id="doc_b",
                        client=fake,
                    )

            # Both source files restored to their original locations.
            self.assertTrue(path_a.exists())
            self.assertTrue(path_b.exists())
            self.assertEqual(path_a.read_bytes(), b"content A")

            # Both documents' chunks still present - nothing lost.
            remaining_ids = {
                c["document_id"] for c in fake.chunks.values()
            }
            self.assertEqual(remaining_ids, {"doc_a", "doc_b"})


class SingleComparatorIsNeverAConflictTests(unittest.TestCase):
    """
    Mission "ORDER 8E-A1", section 27/28: document_type=comparator is
    never itself invalid - the only rule is "active document count per
    country <= 1". A single comparator-only document must never be
    treated as requiring action or as a resolvable conflict.
    """

    def test_single_comparator_document_has_no_conflict_to_resolve(
        self,
    ) -> None:
        fake = FakeConflictOpenSearch()
        fake.add_chunk(
            chunk_id="c1",
            document_id="doc_solo",
            country_code="SG",
            source_filename="Singapore.docx",
        )
        # (document_type on the fake chunk is always "comparator" by
        # construction - see FakeConflictOpenSearch.add_chunk.)

        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root)

            with self.assertRaises(CountryConflictNotFoundError):
                build_country_conflict_review(
                    "SG",
                    source_directory=source_directory,
                    client=fake,
                )

            with self.assertRaises(CountryConflictNotFoundError):
                resolve_country_conflict(
                    "SG",
                    AUTO_DEDUPLICATE,
                    source_directory=source_directory,
                    client=fake,
                )


class ConcurrentResolutionTests(unittest.TestCase):
    """
    Mission "ORDER 8E-A1", section 36 - two simultaneous resolution
    attempts for the same country must serialize through the existing,
    already-proven country_lock (see test_country_lock.py) rather than
    ever both mutating at once. This test uses the real filesystem
    lock and real threads, not a mock, to prove it empirically for the
    new conflict-resolution path specifically.
    """

    def test_two_concurrent_choose_document_calls_never_both_succeed(
        self,
    ) -> None:
        import threading
        import time

        fake = FakeConflictOpenSearch()
        fake.add_chunk(
            chunk_id="c1",
            document_id="doc_a",
            country_code="CO",
            source_filename="Colombia-a.docx",
        )
        fake.add_chunk(
            chunk_id="c2",
            document_id="doc_b",
            country_code="CO",
            source_filename="Colombia-b.docx",
        )

        original_delete = fake.delete_by_query

        def slow_delete(**kwargs):
            # Widen the race window so a genuinely concurrent second
            # call has time to reach its own revalidation while the
            # first is still mid-mutation.
            time.sleep(0.15)
            return original_delete(**kwargs)

        fake.delete_by_query = slow_delete

        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root)
            (source_directory / "Colombia-a.docx").write_bytes(b"a")
            (source_directory / "Colombia-b.docx").write_bytes(b"b")

            results: list[Any] = []
            errors: list[Exception] = []

            def attempt(keep_id: str) -> None:
                try:
                    results.append(
                        resolve_country_conflict(
                            "CO",
                            CHOOSE_DOCUMENT,
                            source_directory=source_directory,
                            keep_document_id=keep_id,
                            client=fake,
                        )
                    )
                except Exception as error:
                    errors.append(error)

            first_thread = threading.Thread(
                target=attempt, args=("doc_a",)
            )
            second_thread = threading.Thread(
                target=attempt, args=("doc_b",)
            )

            first_thread.start()
            time.sleep(0.02)
            second_thread.start()
            first_thread.join(timeout=5)
            second_thread.join(timeout=5)

            # Exactly one call actually mutated state (the lock
            # serialized them); the other either failed cleanly
            # (CountryConflictNotFoundError, since the country was no
            # longer conflicted by the time it got the lock) or timed
            # out waiting - never both succeeding, and never a
            # corrupted state with zero or two active documents.
            self.assertEqual(len(results), 1)

            remaining_ids = {
                c["document_id"] for c in fake.chunks.values()
            }
            self.assertEqual(len(remaining_ids), 1)


if __name__ == "__main__":
    unittest.main()
