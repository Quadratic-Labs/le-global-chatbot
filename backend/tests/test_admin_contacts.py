"""
Tests for structured Admin Contact Management (mission "ORDER 8G-B1").

Mirrors the conventions test_admin_document_sections.py already
established: plain unittest.TestCase, tempfile.TemporaryDirectory() for
source_directory, explicit dependency injection rather than a mocking
framework wherever this codebase already exposes a seam for it, and
unittest.mock.patch only at the one seam it has none for
(document_indexer.py's own module-level `bulk`/
`ensure_legal_documents_index` calls, and docx_parser.py's own
extract_contacts_from_docx for the DOCX-driven reseed/bootstrap paths -
extract_contacts_from_docx's own correctness against a real file is
already exhaustively covered by test_docx_parser.py; these tests only
need SOME deterministic contact list as input).
"""

from __future__ import annotations

import contextlib
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from app.services.admin_contacts import (
    AdminContactMutationFailedError,
    AdminContactNotFoundError,
    add_contact,
    apply_structured_contact_state_to_chunks,
    bootstrap_legacy_contacts,
    delete_contact,
    list_contacts,
    reseed_contact_state_from_parsed_contacts,
    reseed_contacts_from_current_docx,
    update_contact,
)
from app.services.admin_document_lifecycle import (
    AdminDocumentRollbackError,
    reindex_indexed_document,
)
from app.services.admin_document_replacement import (
    safe_upload_and_index_document,
)
from app.services.admin_modification_marker import (
    is_admin_modified_since_upload,
    mark_admin_modified,
)
from app.services.contact_state import (
    ContactRecord,
    ContactState,
    new_contact_id,
    read_contact_state,
    write_contact_state_atomic,
)
from app.models.admin_contacts import AdminContactWriteRequest
from app.models.admin_documents import AdminDocumentListResponse, AdminDocumentSummary
from app.models.document import DocumentChunk
from app.services.docx_parser import ExtractedContact
from app.services.document_indexer import DocumentIndexingResult


def _real_document_id_for(country_code: str, language: str = "en") -> str:
    """
    The one real, deterministic document_id
    build_contact_chunk_for_contacts (and every other chunk builder)
    would compute for this country_code/language - document_id is
    derived solely from country_code + DOCUMENT_FAMILY + language,
    never from anything a test happens to pick, so every fake chunk
    this test file seeds/asserts against must use this exact value,
    never an arbitrary placeholder string.
    """

    from app.services.document_chunk_builder import DocumentMetadata
    from app.services.docx_parser import ExtractedContact as _EC
    from app.services.document_chunk_builder import (
        build_contact_chunk_for_contacts as _probe_builder,
    )

    probe_chunk = _probe_builder(
        [_EC(member_firm="probe")],
        DocumentMetadata(
            country="Probe Country",
            country_code=country_code,
            reference_year=None,
            language=language,
            source_filename="probe.docx",
        ),
    )

    return probe_chunk.document_id


DOCUMENT_ID = _real_document_id_for("GB")
OTHER_DOCUMENT_ID = _real_document_id_for("FR")


def _write_request(
    *,
    member_firm: str = "Example & Partners",
    contact_person: str = "Alex Example",
    email: str = "alex@example.test",
    phone: str = "+1 555 0100",
    address: str = "1 Example Street",
    website: str = "www.example.test",
) -> AdminContactWriteRequest:
    return AdminContactWriteRequest(
        member_firm=member_firm,
        contact_person=contact_person,
        email=email,
        phone=phone,
        address=address,
        website=website,
    )


def _full_contact_record(**overrides: Any) -> ContactRecord:
    defaults = dict(
        contact_id=new_contact_id(),
        member_firm="Example & Partners",
        contact_person="Alex Example",
        email="alex@example.test",
        phone="+1 555 0100",
        address="1 Example Street",
        website="www.example.test",
    )
    defaults.update(overrides)
    return ContactRecord(**defaults)


class FakeContactOpenSearchClient:
    """
    OpenSearch test double for admin_contacts.py.

    Stateful over self.chunks (chunk_id -> source dict) so a mutation's
    real effect can be asserted afterwards, not merely inferred from
    call arguments - mirrors FakeSectionOpenSearchClient's own design
    in test_admin_document_sections.py exactly, generalized to filter
    on subsection.keyword (never legal_topic, which the Contact chunk
    always carries as None).
    """

    def __init__(
        self,
        *,
        document_id: str = DOCUMENT_ID,
        country_code: str = "GB",
        country: str = "United Kingdom",
        source_filename: str = "GB.docx",
        reference_year: int | None = 2026,
        chunks: dict[str, dict[str, Any]] | None = None,
        country_document_ids: list[str] | None = None,
        fail_delete_by_query_calls: int = 0,
        delete_by_query_failure: Exception | None = None,
    ) -> None:
        self.document_id = document_id
        self.country_code = country_code
        self.country = country
        self.source_filename = source_filename
        self.reference_year = reference_year
        self.chunks: dict[str, dict[str, Any]] = dict(chunks or {})
        self.country_document_ids = (
            country_document_ids
            if country_document_ids is not None
            else [document_id]
        )

        self.fail_delete_by_query_calls = fail_delete_by_query_calls
        self.delete_by_query_failure = delete_by_query_failure
        self.delete_by_query_call_count = 0
        self.delete_by_query_calls: list[dict[str, Any]] = []

    def search(self, index: str, body: dict[str, Any]) -> dict[str, Any]:
        del index

        if "sort" in body:
            term = body.get("query", {}).get("term", {})
            requested_document_id = term.get("document_id")
            requested_country_code = term.get("country_code")

            if requested_document_id is not None:
                matching_ids = sorted(
                    chunk_id
                    for chunk_id, chunk in self.chunks.items()
                    if chunk["document_id"] == requested_document_id
                )
            elif requested_country_code is not None:
                # _ensure_no_country_conflict's own country-wide
                # lookup - modeled purely from country_document_ids,
                # never from self.chunks (a country conflict is a
                # catalog-level fact, independent of whether any
                # contact chunk happens to exist yet).
                return {
                    "hits": {
                        "total": {"value": len(self.country_document_ids)},
                        "hits": [
                            {
                                "_id": f"doc-row-{doc_id}",
                                "_source": {
                                    "document_id": doc_id,
                                    "country_code": (
                                        requested_country_code
                                    ),
                                    "country": self.country,
                                    "source_filename": (
                                        self.source_filename
                                    ),
                                    "reference_year": (
                                        self.reference_year
                                    ),
                                },
                                "sort": [f"doc-row-{doc_id}"],
                            }
                            for doc_id in self.country_document_ids
                        ],
                    }
                }
            else:
                matching_ids = []

            return {
                "hits": {
                    "total": {"value": len(matching_ids)},
                    "hits": [
                        {
                            "_id": chunk_id,
                            "_source": self.chunks[chunk_id],
                            "sort": [chunk_id],
                        }
                        for chunk_id in matching_ids
                    ],
                }
            }

        term = body.get("query", {}).get("term", {})
        requested_document_id = term.get("document_id")

        if requested_document_id is not None:
            if requested_document_id != self.document_id:
                return {"hits": {"hits": []}}

            return {
                "hits": {
                    "hits": [
                        {
                            "_source": {
                                "document_id": self.document_id,
                                "source_filename": self.source_filename,
                                "country": self.country,
                                "country_code": self.country_code,
                                "reference_year": self.reference_year,
                            }
                        }
                    ]
                }
            }

        return {"hits": {"hits": []}}

    def delete_by_query(
        self,
        *,
        index: str,
        body: dict[str, Any],
        conflicts: str,
        refresh: bool,
    ) -> dict[str, Any]:
        del index, conflicts, refresh

        self.delete_by_query_call_count += 1

        if (
            self.delete_by_query_call_count
            <= self.fail_delete_by_query_calls
        ):
            raise (
                self.delete_by_query_failure
                if self.delete_by_query_failure is not None
                else RuntimeError(
                    "simulated delete_by_query failure (call #"
                    f"{self.delete_by_query_call_count})."
                )
            )

        query = body["query"]
        filters = query["bool"]["filter"]

        document_id = next(
            clause["term"]["document_id"]
            for clause in filters
            if "document_id" in clause.get("term", {})
        )
        subsection = next(
            (
                clause["term"]["subsection.keyword"]
                for clause in filters
                if "subsection.keyword" in clause.get("term", {})
            ),
            None,
        )

        keep_ids: set[str] = set()

        for clause in query["bool"].get("must_not", []):
            keep_ids.update(clause.get("terms", {}).get("chunk_id", []))

        to_delete = [
            chunk_id
            for chunk_id, chunk in self.chunks.items()
            if chunk["document_id"] == document_id
            and (
                subsection is None
                or chunk.get("subsection") == subsection
            )
            and chunk_id not in keep_ids
        ]

        for chunk_id in to_delete:
            del self.chunks[chunk_id]

        self.delete_by_query_calls.append(
            {
                "document_id": document_id,
                "subsection": subsection,
                "deleted": len(to_delete),
            }
        )

        return {"deleted": len(to_delete), "total": len(to_delete)}


def _bulk_writer(fake_client: FakeContactOpenSearchClient, *, fail_first_n_calls: int = 0):
    call_count = {"n": 0}

    def fake_bulk(client, actions, **kwargs):
        del client, kwargs

        call_count["n"] += 1

        if call_count["n"] <= fail_first_n_calls:
            raise RuntimeError(
                f"simulated OpenSearch bulk failure (call #{call_count['n']})"
            )

        action_list = list(actions)

        for action in action_list:
            fake_client.chunks[action["_id"]] = dict(action["_source"])

        return (len(action_list), [])

    return fake_bulk


@contextlib.contextmanager
def _patched_indexer(fake_client: FakeContactOpenSearchClient, *, fail_bulk: bool = False):
    with patch(
        "app.services.document_indexer.ensure_legal_documents_index"
    ), patch(
        "app.services.document_indexer.bulk",
        side_effect=_bulk_writer(
            fake_client,
            fail_first_n_calls=1 if fail_bulk else 0,
        ),
    ):
        yield


def _seeded_contact_chunk(
    *,
    document_id: str = DOCUMENT_ID,
    country_code: str = "GB",
    country: str = "United Kingdom",
    source_filename: str = "GB.docx",
    content: str = "Member firm: Old Firm\nContact person: Old Person",
) -> dict[str, Any]:
    return {
        "document_id": document_id,
        "country_code": country_code,
        "country": country,
        "source_filename": source_filename,
        "reference_year": 2026,
        "legal_topic": None,
        "document_type": "overview",
        "language": "en",
        "section": f"Employment Law Overview {country}",
        "subsection": "Contact",
        "content": content,
        "content_hash": f"hash-{hash(content)}",
    }


# =========================================================================
# STATE: sidecar read/write/atomic/absent-vs-empty
# =========================================================================


class ContactStateTests(unittest.TestCase):
    def test_absent_state_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            self.assertIsNone(
                read_contact_state(Path(root), DOCUMENT_ID)
            )

    def test_explicit_empty_state_is_not_none(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root)

            write_contact_state_atomic(
                source_directory,
                ContactState(
                    document_id=DOCUMENT_ID,
                    country_code="GB",
                    contacts=(),
                ),
            )

            state = read_contact_state(source_directory, DOCUMENT_ID)

            self.assertIsNotNone(state)
            self.assertEqual(state.contacts, ())

    def test_stable_ordering_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root)

            first = _full_contact_record(member_firm="Firm One")
            second = _full_contact_record(member_firm="Firm Two")

            write_contact_state_atomic(
                source_directory,
                ContactState(
                    document_id=DOCUMENT_ID,
                    country_code="GB",
                    contacts=(first, second),
                ),
            )

            state = read_contact_state(source_directory, DOCUMENT_ID)

            self.assertEqual(
                [c.contact_id for c in state.contacts],
                [first.contact_id, second.contact_id],
            )

    def test_write_is_atomic_no_partial_file_left_on_crash(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root)

            write_contact_state_atomic(
                source_directory,
                ContactState(
                    document_id=DOCUMENT_ID,
                    country_code="GB",
                    contacts=(_full_contact_record(),),
                ),
            )

            state_dir = source_directory / ".admin-state" / "contacts"
            leftover_temp_files = [
                path
                for path in state_dir.iterdir()
                if path.name.endswith(".json.tmp")
            ]

            self.assertEqual(leftover_temp_files, [])


# =========================================================================
# MARKER
# =========================================================================


class AdminModificationMarkerTests(unittest.TestCase):
    def test_absent_marker_defaults_false(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            self.assertFalse(
                is_admin_modified_since_upload(Path(root), DOCUMENT_ID)
            )

    def test_mark_then_reset(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root)

            mark_admin_modified(source_directory, DOCUMENT_ID)
            self.assertTrue(
                is_admin_modified_since_upload(
                    source_directory, DOCUMENT_ID
                )
            )


# =========================================================================
# API: list / add / update / delete
# =========================================================================


class ContactCrudTests(unittest.TestCase):
    def test_list_with_no_state_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root)
            client = FakeContactOpenSearchClient()

            response = list_contacts(
                document_id=DOCUMENT_ID,
                source_directory=source_directory,
                client=client,
            )

            self.assertEqual(response.contacts, [])
            self.assertEqual(response.country_code, "GB")

    def test_add_appends_with_fresh_stable_id(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root)
            client = FakeContactOpenSearchClient()

            with _patched_indexer(client):
                response = add_contact(
                    document_id=DOCUMENT_ID,
                    fields=_write_request(),
                    source_directory=source_directory,
                    client=client,
                )

            self.assertTrue(response.contact_id)
            self.assertEqual(response.member_firm, "Example & Partners")

            state = read_contact_state(source_directory, DOCUMENT_ID)
            self.assertEqual(len(state.contacts), 1)
            self.assertEqual(
                state.contacts[0].contact_id, response.contact_id
            )

            self.assertTrue(
                is_admin_modified_since_upload(
                    source_directory, DOCUMENT_ID
                )
            )

    def test_add_syncs_the_opensearch_contact_chunk(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root)
            client = FakeContactOpenSearchClient()

            with _patched_indexer(client):
                add_contact(
                    document_id=DOCUMENT_ID,
                    fields=_write_request(),
                    source_directory=source_directory,
                    client=client,
                )

            contact_chunks = [
                chunk
                for chunk in client.chunks.values()
                if chunk.get("subsection") == "Contact"
            ]

            self.assertEqual(len(contact_chunks), 1)
            self.assertIn("Example & Partners", contact_chunks[0]["content"])

    def test_zero_one_and_multiple_contacts(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root)
            client = FakeContactOpenSearchClient()

            listing = list_contacts(
                document_id=DOCUMENT_ID,
                source_directory=source_directory,
                client=client,
            )
            self.assertEqual(listing.contacts, [])

            with _patched_indexer(client):
                first = add_contact(
                    document_id=DOCUMENT_ID,
                    fields=_write_request(member_firm="Firm A"),
                    source_directory=source_directory,
                    client=client,
                )

            listing = list_contacts(
                document_id=DOCUMENT_ID,
                source_directory=source_directory,
                client=client,
            )
            self.assertEqual(len(listing.contacts), 1)

            with _patched_indexer(client):
                second = add_contact(
                    document_id=DOCUMENT_ID,
                    fields=_write_request(member_firm="Firm B"),
                    source_directory=source_directory,
                    client=client,
                )
                third = add_contact(
                    document_id=DOCUMENT_ID,
                    fields=_write_request(member_firm="Firm A"),
                    source_directory=source_directory,
                    client=client,
                )

            listing = list_contacts(
                document_id=DOCUMENT_ID,
                source_directory=source_directory,
                client=client,
            )

            self.assertEqual(len(listing.contacts), 3)
            self.assertEqual(
                [c.contact_id for c in listing.contacts],
                [first.contact_id, second.contact_id, third.contact_id],
            )

    def test_duplicate_contacts_have_distinct_ids(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root)
            client = FakeContactOpenSearchClient()

            with _patched_indexer(client):
                first = add_contact(
                    document_id=DOCUMENT_ID,
                    fields=_write_request(),
                    source_directory=source_directory,
                    client=client,
                )
                second = add_contact(
                    document_id=DOCUMENT_ID,
                    fields=_write_request(),
                    source_directory=source_directory,
                    client=client,
                )

            self.assertEqual(first.member_firm, second.member_firm)
            self.assertEqual(first.email, second.email)
            self.assertNotEqual(first.contact_id, second.contact_id)

    def test_update_preserves_id_and_position(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root)
            client = FakeContactOpenSearchClient()

            with _patched_indexer(client):
                first = add_contact(
                    document_id=DOCUMENT_ID,
                    fields=_write_request(member_firm="Firm A"),
                    source_directory=source_directory,
                    client=client,
                )
                second = add_contact(
                    document_id=DOCUMENT_ID,
                    fields=_write_request(member_firm="Firm B"),
                    source_directory=source_directory,
                    client=client,
                )

                update_contact(
                    document_id=DOCUMENT_ID,
                    contact_id=second.contact_id,
                    fields=_write_request(member_firm="Firm B Updated"),
                    source_directory=source_directory,
                    client=client,
                )

            state = read_contact_state(source_directory, DOCUMENT_ID)

            self.assertEqual(
                [c.contact_id for c in state.contacts],
                [first.contact_id, second.contact_id],
            )
            self.assertEqual(state.contacts[0].member_firm, "Firm A")
            self.assertEqual(
                state.contacts[1].member_firm, "Firm B Updated"
            )

    def test_update_stale_contact_id_raises_not_found(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root)
            client = FakeContactOpenSearchClient()

            with self.assertRaises(AdminContactNotFoundError):
                update_contact(
                    document_id=DOCUMENT_ID,
                    contact_id="does-not-exist",
                    fields=_write_request(),
                    source_directory=source_directory,
                    client=client,
                )

    def test_delete_removes_only_the_requested_contact(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root)
            client = FakeContactOpenSearchClient()

            with _patched_indexer(client):
                first = add_contact(
                    document_id=DOCUMENT_ID,
                    fields=_write_request(member_firm="Firm A"),
                    source_directory=source_directory,
                    client=client,
                )
                second = add_contact(
                    document_id=DOCUMENT_ID,
                    fields=_write_request(member_firm="Firm B"),
                    source_directory=source_directory,
                    client=client,
                )

                delete_contact(
                    document_id=DOCUMENT_ID,
                    contact_id=first.contact_id,
                    source_directory=source_directory,
                    client=client,
                )

            state = read_contact_state(source_directory, DOCUMENT_ID)

            self.assertEqual(len(state.contacts), 1)
            self.assertEqual(state.contacts[0].contact_id, second.contact_id)
            self.assertEqual(state.contacts[0].member_firm, "Firm B")

    def test_delete_stale_contact_id_raises_not_found(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root)
            client = FakeContactOpenSearchClient()

            with self.assertRaises(AdminContactNotFoundError):
                delete_contact(
                    document_id=DOCUMENT_ID,
                    contact_id="does-not-exist",
                    source_directory=source_directory,
                    client=client,
                )

    def test_delete_last_contact_removes_stale_contact_chunk(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root)
            client = FakeContactOpenSearchClient()

            with _patched_indexer(client):
                only = add_contact(
                    document_id=DOCUMENT_ID,
                    fields=_write_request(),
                    source_directory=source_directory,
                    client=client,
                )

                self.assertEqual(
                    len(
                        [
                            c
                            for c in client.chunks.values()
                            if c.get("subsection") == "Contact"
                        ]
                    ),
                    1,
                )

                delete_contact(
                    document_id=DOCUMENT_ID,
                    contact_id=only.contact_id,
                    source_directory=source_directory,
                    client=client,
                )

            contact_chunks = [
                c
                for c in client.chunks.values()
                if c.get("subsection") == "Contact"
            ]

            self.assertEqual(contact_chunks, [])

    def test_legal_topic_chunks_are_never_touched(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root)

            client = FakeContactOpenSearchClient(
                chunks={
                    "legal-chunk-1": {
                        "document_id": DOCUMENT_ID,
                        "country_code": "GB",
                        "country": "United Kingdom",
                        "source_filename": "GB.docx",
                        "reference_year": 2026,
                        "legal_topic": "Employment Contracts",
                        "document_type": "overview",
                        "language": "en",
                        "section": "Employment Contracts",
                        "subsection": None,
                        "content": "legal content",
                        "content_hash": "hash-legal",
                    },
                },
            )

            with _patched_indexer(client):
                add_contact(
                    document_id=DOCUMENT_ID,
                    fields=_write_request(),
                    source_directory=source_directory,
                    client=client,
                )

            self.assertIn("legal-chunk-1", client.chunks)
            self.assertEqual(
                client.chunks["legal-chunk-1"]["content"], "legal content"
            )

    def test_country_conflict_blocks_contact_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root)

            client = FakeContactOpenSearchClient(
                country_document_ids=[DOCUMENT_ID, OTHER_DOCUMENT_ID],
            )

            with self.assertRaises(Exception):
                add_contact(
                    document_id=DOCUMENT_ID,
                    fields=_write_request(),
                    source_directory=source_directory,
                    client=client,
                )


# =========================================================================
# ROLLBACK
# =========================================================================


class ContactRollbackTests(unittest.TestCase):
    def test_index_failure_restores_previous_state_and_marker(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root)
            client = FakeContactOpenSearchClient()

            with _patched_indexer(client):
                first = add_contact(
                    document_id=DOCUMENT_ID,
                    fields=_write_request(member_firm="Firm A"),
                    source_directory=source_directory,
                    client=client,
                )

            previous_marker = is_admin_modified_since_upload(
                source_directory, DOCUMENT_ID
            )

            with _patched_indexer(client, fail_bulk=True):
                with self.assertRaises(AdminContactMutationFailedError):
                    add_contact(
                        document_id=DOCUMENT_ID,
                        fields=_write_request(member_firm="Firm B"),
                        source_directory=source_directory,
                        client=client,
                    )

            state = read_contact_state(source_directory, DOCUMENT_ID)

            self.assertEqual(len(state.contacts), 1)
            self.assertEqual(state.contacts[0].contact_id, first.contact_id)
            self.assertEqual(
                is_admin_modified_since_upload(
                    source_directory, DOCUMENT_ID
                ),
                previous_marker,
            )

    def test_state_write_failure_leaves_index_and_marker_untouched(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root)
            client = FakeContactOpenSearchClient()

            with _patched_indexer(client), patch(
                "app.services.admin_contacts.write_contact_state_atomic",
                side_effect=OSError("disk full"),
            ):
                with self.assertRaises(AdminContactMutationFailedError):
                    add_contact(
                        document_id=DOCUMENT_ID,
                        fields=_write_request(),
                        source_directory=source_directory,
                        client=client,
                    )

            self.assertIsNone(
                read_contact_state(source_directory, DOCUMENT_ID)
            )
            self.assertEqual(client.chunks, {})
            self.assertFalse(
                is_admin_modified_since_upload(
                    source_directory, DOCUMENT_ID
                )
            )

    def test_rollback_itself_failing_raises_rollback_error(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root)
            client = FakeContactOpenSearchClient()

            with _patched_indexer(client):
                add_contact(
                    document_id=DOCUMENT_ID,
                    fields=_write_request(),
                    source_directory=source_directory,
                    client=client,
                )

            with _patched_indexer(client, fail_bulk=True):
                with patch(
                    "app.services.admin_contacts.write_contact_state_atomic",
                    side_effect=[None, OSError("rollback also fails")],
                ):
                    with self.assertRaises(AdminDocumentRollbackError):
                        add_contact(
                            document_id=DOCUMENT_ID,
                            fields=_write_request(),
                            source_directory=source_directory,
                            client=client,
                        )


# =========================================================================
# BOOTSTRAP
# =========================================================================


def _fake_document_lister(documents: list[AdminDocumentSummary]):
    def lister(*, source_directory: Path, client) -> AdminDocumentListResponse:
        del source_directory, client

        return AdminDocumentListResponse(
            total=len(documents),
            documents=documents,
        )

    return lister


def _summary(
    *,
    document_id: str,
    country_code: str,
    source_filename: str,
) -> AdminDocumentSummary:
    return AdminDocumentSummary(
        document_id=document_id,
        source_filename=source_filename,
        country=country_code,
        country_code=country_code,
        language="en",
        document_type="overview",
        reference_year=2026,
        chunk_count=1,
        source_file_present=True,
        status="indexed",
    )


class ContactBootstrapTests(unittest.TestCase):
    def test_dry_run_does_not_write_anything(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root)
            (source_directory / "GB.docx").write_bytes(b"docx")

            summaries = [
                _summary(
                    document_id=DOCUMENT_ID,
                    country_code="GB",
                    source_filename="GB.docx",
                )
            ]

            with patch(
                "app.services.admin_contacts.extract_contacts_from_docx",
                return_value=[
                    ExtractedContact(member_firm="Firm A"),
                ],
            ):
                report = bootstrap_legacy_contacts(
                    source_directory=source_directory,
                    client=object(),
                    dry_run=True,
                    document_lister=_fake_document_lister(summaries),
                )

            self.assertTrue(report.dry_run)
            self.assertEqual(report.documents_seen, 1)
            self.assertEqual(report.contacts_seeded, 1)
            self.assertIsNone(
                read_contact_state(source_directory, DOCUMENT_ID)
            )

    def test_wet_run_seeds_state_for_new_documents(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root)
            (source_directory / "GB.docx").write_bytes(b"docx")

            summaries = [
                _summary(
                    document_id=DOCUMENT_ID,
                    country_code="GB",
                    source_filename="GB.docx",
                )
            ]

            with patch(
                "app.services.admin_contacts.extract_contacts_from_docx",
                return_value=[
                    ExtractedContact(member_firm="Firm A"),
                ],
            ):
                report = bootstrap_legacy_contacts(
                    source_directory=source_directory,
                    client=object(),
                    dry_run=False,
                    document_lister=_fake_document_lister(summaries),
                )

            self.assertEqual(report.contacts_seeded, 1)

            state = read_contact_state(source_directory, DOCUMENT_ID)
            self.assertEqual(len(state.contacts), 1)
            self.assertEqual(state.contacts[0].member_firm, "Firm A")

    def test_zero_contact_document_gets_explicit_empty_state(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root)
            (source_directory / "FR.docx").write_bytes(b"docx")

            summaries = [
                _summary(
                    document_id=OTHER_DOCUMENT_ID,
                    country_code="FR",
                    source_filename="FR.docx",
                )
            ]

            with patch(
                "app.services.admin_contacts.extract_contacts_from_docx",
                return_value=[],
            ):
                report = bootstrap_legacy_contacts(
                    source_directory=source_directory,
                    client=object(),
                    dry_run=False,
                    document_lister=_fake_document_lister(summaries),
                )

            self.assertEqual(report.zero_contact_documents, 1)
            self.assertEqual(report.contacts_seeded, 0)

            state = read_contact_state(source_directory, OTHER_DOCUMENT_ID)
            self.assertIsNotNone(state)
            self.assertEqual(state.contacts, ())

    def test_existing_state_is_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root)
            (source_directory / "GB.docx").write_bytes(b"docx")

            existing_record = _full_contact_record(
                member_firm="Admin Edited Firm"
            )
            write_contact_state_atomic(
                source_directory,
                ContactState(
                    document_id=DOCUMENT_ID,
                    country_code="GB",
                    contacts=(existing_record,),
                ),
            )

            summaries = [
                _summary(
                    document_id=DOCUMENT_ID,
                    country_code="GB",
                    source_filename="GB.docx",
                )
            ]

            with patch(
                "app.services.admin_contacts.extract_contacts_from_docx",
                return_value=[
                    ExtractedContact(member_firm="Stale DOCX Firm"),
                ],
            ):
                report = bootstrap_legacy_contacts(
                    source_directory=source_directory,
                    client=object(),
                    dry_run=False,
                    document_lister=_fake_document_lister(summaries),
                )

            self.assertEqual(report.documents_skipped_existing_state, 1)
            self.assertEqual(report.contacts_seeded, 0)

            state = read_contact_state(source_directory, DOCUMENT_ID)
            self.assertEqual(state.contacts[0].member_firm, "Admin Edited Firm")

    def test_legacy_incomplete_contacts_preserved_exactly(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root)
            (source_directory / "GB.docx").write_bytes(b"docx")

            summaries = [
                _summary(
                    document_id=DOCUMENT_ID,
                    country_code="GB",
                    source_filename="GB.docx",
                )
            ]

            with patch(
                "app.services.admin_contacts.extract_contacts_from_docx",
                return_value=[
                    ExtractedContact(
                        contact_person="Alex Example",
                        email="alex@example.test",
                    ),
                ],
            ):
                bootstrap_legacy_contacts(
                    source_directory=source_directory,
                    client=object(),
                    dry_run=False,
                    document_lister=_fake_document_lister(summaries),
                )

            state = read_contact_state(source_directory, DOCUMENT_ID)

            self.assertEqual(len(state.contacts), 1)
            self.assertIsNone(state.contacts[0].member_firm)
            self.assertIsNone(state.contacts[0].phone)
            self.assertEqual(
                state.contacts[0].contact_person, "Alex Example"
            )


class ContactBootstrapIsolatedCorpusTests(unittest.TestCase):
    """
    Mirrors the "ORDER 8G-B0" audit's own real-corpus baseline:
    24 documents, 22 contacts, 2 zero-contact documents (FR, PT) - here
    reproduced against a synthetic, isolated 24-document catalog (never
    the real production corpus), proving the bootstrap facility
    produces exactly this shape given that input.
    """

    def test_matches_the_b0_audit_baseline_shape(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root)

            summaries = []
            contacts_by_country: dict[str, list[ExtractedContact]] = {}

            for index in range(24):
                country_code = f"C{index:02d}"
                summaries.append(
                    _summary(
                        document_id=f"doc_{index:064x}",
                        country_code=country_code,
                        source_filename=f"{country_code}.docx",
                    )
                )
                (
                    source_directory / f"{country_code}.docx"
                ).write_bytes(b"docx")

                # Exactly 2 of the 24 (indices 0 and 1) get zero
                # contacts, matching FR/PT in the real audit baseline.
                contacts_by_country[country_code] = (
                    []
                    if index < 2
                    else [ExtractedContact(member_firm="Firm")]
                )

            def fake_extract(path: Path, country: str):
                del country
                country_code = path.stem
                return contacts_by_country[country_code]

            with patch(
                "app.services.admin_contacts.extract_contacts_from_docx",
                side_effect=fake_extract,
            ):
                report = bootstrap_legacy_contacts(
                    source_directory=source_directory,
                    client=object(),
                    dry_run=False,
                    document_lister=_fake_document_lister(summaries),
                )

            self.assertEqual(report.documents_seen, 24)
            self.assertEqual(report.contacts_seeded, 22)
            self.assertEqual(report.zero_contact_documents, 2)


# =========================================================================
# REFRESH: apply_structured_contact_state_to_chunks
# =========================================================================


def _legal_chunk(document_id: str = DOCUMENT_ID) -> DocumentChunk:
    return DocumentChunk(
        document_id=document_id,
        chunk_id="chunk_" + "c" * 64,
        country="United Kingdom",
        country_code="GB",
        legal_topic="Employment Contracts",
        document_type="overview",
        language="en",
        section="Employment Contracts",
        subsection=None,
        content="legal content",
        source_filename="GB.docx",
        source_format="docx",
        content_hash="hash-legal",
        reference_year=2026,
    )


def _stale_docx_contact_chunk(document_id: str = DOCUMENT_ID) -> DocumentChunk:
    return DocumentChunk(
        document_id=document_id,
        chunk_id="chunk_" + "d" * 64,
        country="United Kingdom",
        country_code="GB",
        legal_topic=None,
        document_type="overview",
        language="en",
        section="Employment Law Overview United Kingdom",
        subsection="Contact",
        content="Member firm: Stale DOCX Firm",
        source_filename="GB.docx",
        source_format="docx",
        content_hash="hash-stale",
        reference_year=2026,
    )


class ApplyStructuredContactStateToChunksTests(unittest.TestCase):
    def test_no_sidecar_leaves_chunks_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root)

            chunks = [_legal_chunk(), _stale_docx_contact_chunk()]

            result = apply_structured_contact_state_to_chunks(
                chunks=list(chunks),
                document_id=DOCUMENT_ID,
                source_directory=source_directory,
            )

            self.assertEqual(result, chunks)

    def test_existing_sidecar_replaces_the_stale_docx_contact_chunk(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root)

            write_contact_state_atomic(
                source_directory,
                ContactState(
                    document_id=DOCUMENT_ID,
                    country_code="GB",
                    contacts=(
                        _full_contact_record(
                            member_firm="Admin Edited Firm"
                        ),
                    ),
                ),
            )

            chunks = [_legal_chunk(), _stale_docx_contact_chunk()]

            result = apply_structured_contact_state_to_chunks(
                chunks=list(chunks),
                document_id=DOCUMENT_ID,
                source_directory=source_directory,
            )

            legal_chunks = [c for c in result if c.subsection != "Contact"]
            contact_chunks = [c for c in result if c.subsection == "Contact"]

            self.assertEqual(len(legal_chunks), 1)
            self.assertEqual(legal_chunks[0].content, "legal content")

            self.assertEqual(len(contact_chunks), 1)
            self.assertIn("Admin Edited Firm", contact_chunks[0].content)
            self.assertNotIn("Stale DOCX Firm", contact_chunks[0].content)

    def test_existing_empty_sidecar_removes_the_docx_contact_chunk(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root)

            write_contact_state_atomic(
                source_directory,
                ContactState(
                    document_id=DOCUMENT_ID,
                    country_code="GB",
                    contacts=(),
                ),
            )

            chunks = [_legal_chunk(), _stale_docx_contact_chunk()]

            result = apply_structured_contact_state_to_chunks(
                chunks=list(chunks),
                document_id=DOCUMENT_ID,
                source_directory=source_directory,
            )

            self.assertEqual(len(result), 1)
            self.assertIsNone(result[0].subsection)


class ReindexPreservesContactStateIntegrationTests(unittest.TestCase):
    """
    Real reindex_indexed_document, proving the wiring (not just the
    pure helper above) actually substitutes the Contact chunk during a
    genuine Refresh.
    """

    def test_refresh_uses_structured_state_not_stale_docx_text(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root)
            (source_directory / "GB.docx").write_bytes(b"docx")

            write_contact_state_atomic(
                source_directory,
                ContactState(
                    document_id=DOCUMENT_ID,
                    country_code="GB",
                    contacts=(
                        _full_contact_record(
                            member_firm="Admin Edited Firm"
                        ),
                    ),
                ),
            )

            client = FakeContactOpenSearchClient(
                chunks={
                    "existing-legal": {
                        "document_id": DOCUMENT_ID,
                        "country_code": "GB",
                        "country": "United Kingdom",
                        "source_filename": "GB.docx",
                        "reference_year": 2026,
                        "legal_topic": "Employment Contracts",
                        "subsection": None,
                        "content": "legal content",
                    },
                },
            )

            def chunk_builder(path: Path) -> list[DocumentChunk]:
                del path
                return [_legal_chunk(), _stale_docx_contact_chunk()]

            captured: dict[str, Any] = {}

            def document_indexer(*, chunks, client=None) -> DocumentIndexingResult:
                del client
                captured["chunks"] = list(chunks)

                return DocumentIndexingResult(
                    index_alias="legal-documents",
                    document_id=chunks[0].document_id,
                    source_filename=chunks[0].source_filename,
                    requested_chunks=len(chunks),
                    indexed_chunks=len(chunks),
                    stale_chunks_deleted=0,
                )

            reindex_indexed_document(
                document_id=DOCUMENT_ID,
                source_directory=source_directory,
                client=client,
                chunk_builder=chunk_builder,
                document_indexer=document_indexer,
            )

            indexed_contact_chunks = [
                c for c in captured["chunks"] if c.subsection == "Contact"
            ]

            self.assertEqual(len(indexed_contact_chunks), 1)
            self.assertIn(
                "Admin Edited Firm", indexed_contact_chunks[0].content
            )
            self.assertNotIn(
                "Stale DOCX Firm", indexed_contact_chunks[0].content
            )


# =========================================================================
# NEW DOCX: reseed_contact_state_from_parsed_contacts +
# safe_upload_and_index_document integration
# =========================================================================


class ReseedContactStateFromParsedContactsTests(unittest.TestCase):
    def test_reseed_overwrites_prior_state_entirely(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root)

            write_contact_state_atomic(
                source_directory,
                ContactState(
                    document_id=DOCUMENT_ID,
                    country_code="GB",
                    contacts=(
                        _full_contact_record(member_firm="Old Firm A"),
                        _full_contact_record(member_firm="Old Firm B"),
                        _full_contact_record(member_firm="Old Firm C"),
                    ),
                ),
            )
            mark_admin_modified(source_directory, DOCUMENT_ID)

            reseed_contact_state_from_parsed_contacts(
                document_id=DOCUMENT_ID,
                country_code="GB",
                source_directory=source_directory,
                contacts=[ExtractedContact(member_firm="New Firm")],
            )

            state = read_contact_state(source_directory, DOCUMENT_ID)

            self.assertEqual(len(state.contacts), 1)
            self.assertEqual(state.contacts[0].member_firm, "New Firm")
            self.assertFalse(
                is_admin_modified_since_upload(
                    source_directory, DOCUMENT_ID
                )
            )

    def test_reseed_with_zero_contacts_clears_prior_state(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root)

            write_contact_state_atomic(
                source_directory,
                ContactState(
                    document_id=DOCUMENT_ID,
                    country_code="GB",
                    contacts=(
                        _full_contact_record(member_firm="Old Firm A"),
                        _full_contact_record(member_firm="Old Firm B"),
                    ),
                ),
            )

            reseed_contact_state_from_parsed_contacts(
                document_id=DOCUMENT_ID,
                country_code="GB",
                source_directory=source_directory,
                contacts=[],
            )

            state = read_contact_state(source_directory, DOCUMENT_ID)

            self.assertIsNotNone(state)
            self.assertEqual(state.contacts, ())


class UploadReplaceReseedsContactsIntegrationTests(unittest.TestCase):
    """
    Real safe_upload_and_index_document, proving a genuine
    upload/replace transaction really reseeds structured contact state
    from the newly-accepted DOCX (not merely the lower-level helper
    above in isolation).
    """

    def _run_upload(
        self,
        *,
        source_directory: Path,
        parsed_contacts: list[ExtractedContact],
        replace_existing: bool,
    ):
        from app.services.document_chunk_builder import DOCUMENT_FAMILY

        del DOCUMENT_FAMILY

        chunk = _legal_chunk()

        def chunk_builder(path: Path) -> list[DocumentChunk]:
            del path
            return [chunk]

        def country_document_lookup(country_code, client):
            del country_code, client
            return []

        def country_document_indexer(*, chunks, client=None):
            del client
            return DocumentIndexingResult(
                index_alias="legal-documents",
                document_id=chunks[0].document_id,
                source_filename=chunks[0].source_filename,
                requested_chunks=len(chunks),
                indexed_chunks=len(chunks),
                stale_chunks_deleted=0,
            )

        with patch(
            "app.services.admin_document_replacement.extract_contacts_from_docx",
            return_value=parsed_contacts,
        ):
            import io

            return safe_upload_and_index_document(
                filename="GB.docx",
                file_stream=io.BytesIO(b"fake docx bytes"),
                source_directory=source_directory,
                processed_directory=source_directory / "processed",
                maximum_bytes=10_000_000,
                country_confirmed=True,
                confirm_warnings=True,
                replace_existing=replace_existing,
                chunk_builder=chunk_builder,
                country_document_lookup=country_document_lookup,
                country_document_indexer=country_document_indexer,
            )

    def test_fresh_upload_seeds_contact_state(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root)

            response = self._run_upload(
                source_directory=source_directory,
                parsed_contacts=[ExtractedContact(member_firm="New Firm")],
                replace_existing=False,
            )

            state = read_contact_state(
                source_directory, response.document_id
            )

            self.assertEqual(len(state.contacts), 1)
            self.assertEqual(state.contacts[0].member_firm, "New Firm")
            self.assertFalse(
                is_admin_modified_since_upload(
                    source_directory, response.document_id
                )
            )


# =========================================================================
# SAME-BYTES CONFIRMED RESEED
# =========================================================================


class ReseedContactsFromCurrentDocxTests(unittest.TestCase):
    def test_confirmed_reseed_discards_admin_edits(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root)
            (source_directory / "GB.docx").write_bytes(b"docx")

            client = FakeContactOpenSearchClient()

            with _patched_indexer(client):
                add_contact(
                    document_id=DOCUMENT_ID,
                    fields=_write_request(member_firm="Admin Edited Firm"),
                    source_directory=source_directory,
                    client=client,
                )

            with patch(
                "app.services.admin_contacts.extract_contacts_from_docx",
                return_value=[
                    ExtractedContact(member_firm="Parsed DOCX Firm"),
                ],
            ), _patched_indexer(client):
                response = reseed_contacts_from_current_docx(
                    document_id=DOCUMENT_ID,
                    source_directory=source_directory,
                    client=client,
                )

            self.assertEqual(len(response.contacts), 1)
            self.assertEqual(
                response.contacts[0].member_firm, "Parsed DOCX Firm"
            )

            state = read_contact_state(source_directory, DOCUMENT_ID)
            self.assertEqual(
                state.contacts[0].member_firm, "Parsed DOCX Firm"
            )
            self.assertFalse(
                is_admin_modified_since_upload(
                    source_directory, DOCUMENT_ID
                )
            )


if __name__ == "__main__":
    unittest.main()
