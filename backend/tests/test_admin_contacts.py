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
import hashlib
import tempfile
import unittest
import zipfile
from pathlib import Path
from typing import Any
from unittest.mock import patch

from docx import Document

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
from app.services.admin_contact_photos import replace_admin_contact_photo
from app.services.admin_document_lifecycle import (
    AdminDocumentRollbackError,
    get_document_download,
    reindex_indexed_document,
)
from app.services.admin_document_replacement import (
    safe_upload_and_index_document,
)
from app.services.admin_modification_marker import (
    is_admin_modified_since_upload,
    mark_admin_modified,
)
from app.services.contact_document_area import (
    ContactPhotoPayload,
    rebuild_canonical_contact_table,
)
from app.services.contact_photo_store import write_contact_photo_atomic
from app.services.contact_photos import extract_contact_photo_candidates
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
from app.services.docx_parser import (
    CONTACT_TABLE_HIDDEN_MARKER,
    ExtractedContact,
    extract_contacts_from_docx,
)
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


def _make_png(width: int, height: int, rgb: tuple[int, int, int]) -> bytes:
    """A minimal but well-formed, real-sized RGB PNG - python-docx's
    own image-header parser (used to compute a photo's proportional
    height in the canonical table) needs real dimensions, unlike a
    degenerate single-pixel fixture."""

    import struct
    import zlib

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data))
        )

    signature = b"\x89PNG\r\n\x1a\n"
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))
    image_data = zlib.compress(raw)
    return (
        signature
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", image_data)
        + chunk(b"IEND", b"")
    )


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


def _seed_placeholder_source_docx(
    source_directory: Path, filename: str = "GB.docx"
) -> None:
    """
    A minimal, valid, structurally-empty DOCX - just enough for
    resolve_document_source_path to find a real file on disk. Add/
    Delete Contact always rebuild the persisted source's canonical
    contact table now, regardless of whether this placeholder has any
    prior contact structure at all, so these tests exercise that
    rebuild too (harmlessly, against a document with no real legal
    content) alongside the ContactState/OpenSearch chunk mutation they
    were originally written for.
    """

    document = Document()
    document.add_paragraph("Placeholder document body.")
    document.save(str(source_directory / filename))


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
            _seed_placeholder_source_docx(source_directory)
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
            _seed_placeholder_source_docx(source_directory)
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
            _seed_placeholder_source_docx(source_directory)
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
            _seed_placeholder_source_docx(source_directory)
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
            _seed_placeholder_source_docx(source_directory)
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
            _seed_placeholder_source_docx(source_directory)
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
            _seed_placeholder_source_docx(source_directory)
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
            _seed_placeholder_source_docx(source_directory)

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

    def test_list_exposes_has_photo_true_for_a_contact_with_a_photo(
        self,
    ) -> None:
        """
        The safe photo-presence contract Admin/View needs (mission -
        the diagnosed root cause of "no thumbnail" was a route path
        mismatch, not this, but the Admin list blindly attempting a
        photo fetch for every contact regardless of whether one exists
        is itself worth fixing: no pointless 404 image requests for
        contacts that never had a photo). Never photo_filename or any
        filesystem path.
        """

        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root)
            client = FakeContactOpenSearchClient()

            write_contact_state_atomic(
                source_directory,
                ContactState(
                    document_id=DOCUMENT_ID,
                    country_code="GB",
                    contacts=(
                        ContactRecord(
                            contact_id=new_contact_id(),
                            member_firm="Example & Partners",
                            contact_person="Jane Doe",
                            email="jane@example.com",
                            phone="+1 555 0000",
                            address="1 Example Street",
                            website="https://example.com",
                            photo_filename="deadbeef.jpg",
                            photo_content_type="image/jpeg",
                            photo_sha256="a" * 64,
                        ),
                    ),
                ),
            )

            listing = list_contacts(
                document_id=DOCUMENT_ID,
                source_directory=source_directory,
                client=client,
            )

            self.assertEqual(len(listing.contacts), 1)
            self.assertTrue(listing.contacts[0].has_photo)

            serialized = listing.contacts[0].model_dump()
            self.assertNotIn("photo_filename", serialized)
            self.assertNotIn("photo_content_type", serialized)
            self.assertNotIn("photo_sha256", serialized)

    def test_list_exposes_has_photo_false_for_a_contact_without_a_photo(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root)
            _seed_placeholder_source_docx(source_directory)
            client = FakeContactOpenSearchClient()

            with _patched_indexer(client):
                add_contact(
                    document_id=DOCUMENT_ID,
                    fields=_write_request(),
                    source_directory=source_directory,
                    client=client,
                )

            listing = list_contacts(
                document_id=DOCUMENT_ID,
                source_directory=source_directory,
                client=client,
            )

            self.assertEqual(len(listing.contacts), 1)
            self.assertFalse(listing.contacts[0].has_photo)


# =========================================================================
# DOWNLOAD BYTE-STABILITY (mission "DOCX HARDENING", 2026-08-24)
#
# The invariant this section proves: download is a PURE READ.
# get_document_download() no longer rebuilds/reserializes anything -
# every mutation below already persists its effective DOCX atomically
# before returning, so download's job is just to hand back exactly
# those bytes, unchanged, every time.
# =========================================================================


class DownloadByteStabilityTests(unittest.TestCase):
    def _sha(self, path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def test_persisted_source_sha_equals_download_sha(self) -> None:
        """A: sha256(persisted source) == sha256(download body)."""

        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root)
            _seed_placeholder_source_docx(source_directory)
            client = FakeContactOpenSearchClient()

            with _patched_indexer(client):
                add_contact(
                    document_id=DOCUMENT_ID,
                    fields=_write_request(),
                    source_directory=source_directory,
                    client=client,
                )

            persisted_path = source_directory / "GB.docx"
            download = get_document_download(
                document_id=DOCUMENT_ID,
                source_directory=source_directory,
                client=client,
            )

            self.assertEqual(download.path, persisted_path)
            self.assertEqual(
                self._sha(download.path), self._sha(persisted_path)
            )

    def test_ten_consecutive_downloads_are_byte_identical(self) -> None:
        """B: 10 consecutive downloads of an unchanged document all
        have exactly the same SHA256."""

        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root)
            _seed_placeholder_source_docx(source_directory)
            client = FakeContactOpenSearchClient()

            with _patched_indexer(client):
                add_contact(
                    document_id=DOCUMENT_ID,
                    fields=_write_request(),
                    source_directory=source_directory,
                    client=client,
                )

            hashes = {
                self._sha(
                    get_document_download(
                        document_id=DOCUMENT_ID,
                        source_directory=source_directory,
                        client=client,
                    ).path
                )
                for _ in range(10)
            }

            self.assertEqual(len(hashes), 1)

    def test_download_changes_nothing(self) -> None:
        """C: download changes neither the source file (mtime/size)
        nor ContactState nor the OpenSearch chunk set."""

        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root)
            _seed_placeholder_source_docx(source_directory)
            client = FakeContactOpenSearchClient()

            with _patched_indexer(client):
                add_contact(
                    document_id=DOCUMENT_ID,
                    fields=_write_request(),
                    source_directory=source_directory,
                    client=client,
                )

            persisted_path = source_directory / "GB.docx"
            stat_before = persisted_path.stat()
            state_before = read_contact_state(source_directory, DOCUMENT_ID)
            chunks_before = dict(client.chunks)

            get_document_download(
                document_id=DOCUMENT_ID,
                source_directory=source_directory,
                client=client,
            )

            stat_after = persisted_path.stat()
            state_after = read_contact_state(source_directory, DOCUMENT_ID)

            self.assertEqual(stat_before.st_mtime_ns, stat_after.st_mtime_ns)
            self.assertEqual(stat_before.st_size, stat_after.st_size)
            self.assertEqual(state_before, state_after)
            self.assertEqual(chunks_before, client.chunks)

    def test_download_never_reaches_docx_writing_code(self) -> None:
        """D: download must never invoke materialize_effective_docx,
        python-docx's Document.save, or the canonical contact
        rebuild - patch all three to explode, and prove download
        still succeeds untouched."""

        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root)
            _seed_placeholder_source_docx(source_directory)
            client = FakeContactOpenSearchClient()

            with _patched_indexer(client):
                add_contact(
                    document_id=DOCUMENT_ID,
                    fields=_write_request(),
                    source_directory=source_directory,
                    client=client,
                )

            persisted_bytes = (source_directory / "GB.docx").read_bytes()

            with patch(
                "app.services.contact_document_area."
                "rebuild_canonical_contact_table",
                side_effect=AssertionError(
                    "download must never rebuild the canonical table"
                ),
            ), patch(
                "docx.document.Document.save",
                side_effect=AssertionError(
                    "download must never call Document.save"
                ),
            ):
                download = get_document_download(
                    document_id=DOCUMENT_ID,
                    source_directory=source_directory,
                    client=client,
                )
                downloaded_bytes = download.path.read_bytes()

        self.assertEqual(downloaded_bytes, persisted_bytes)

    def test_source_equals_download_after_add_with_photo(self) -> None:
        """E: after Contact Add + photo, persisted source == downloaded
        bytes."""

        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root)
            _seed_placeholder_source_docx(source_directory)
            client = FakeContactOpenSearchClient()

            with _patched_indexer(client):
                contact = add_contact(
                    document_id=DOCUMENT_ID,
                    fields=_write_request(),
                    source_directory=source_directory,
                    client=client,
                )
                replace_admin_contact_photo(
                    source_directory,
                    DOCUMENT_ID,
                    contact.contact_id,
                    data=_make_png(64, 64, (10, 20, 30)),
                    content_type="image/png",
                    client=client,
                )

            persisted_path = source_directory / "GB.docx"
            download = get_document_download(
                document_id=DOCUMENT_ID,
                source_directory=source_directory,
                client=client,
            )

            self.assertEqual(
                self._sha(download.path), self._sha(persisted_path)
            )

    def test_source_equals_download_after_update(self) -> None:
        """F: after Contact Update, persisted source == downloaded
        bytes."""

        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root)
            _seed_placeholder_source_docx(source_directory)
            client = FakeContactOpenSearchClient()

            with _patched_indexer(client):
                contact = add_contact(
                    document_id=DOCUMENT_ID,
                    fields=_write_request(),
                    source_directory=source_directory,
                    client=client,
                )
                update_contact(
                    document_id=DOCUMENT_ID,
                    contact_id=contact.contact_id,
                    fields=_write_request(member_firm="Updated Firm"),
                    source_directory=source_directory,
                    client=client,
                )

            persisted_path = source_directory / "GB.docx"
            download = get_document_download(
                document_id=DOCUMENT_ID,
                source_directory=source_directory,
                client=client,
            )

            self.assertEqual(
                self._sha(download.path), self._sha(persisted_path)
            )

    def test_source_equals_download_after_photo_replacement(self) -> None:
        """G: after photo replacement, persisted source == downloaded
        bytes."""

        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root)
            _seed_placeholder_source_docx(source_directory)
            client = FakeContactOpenSearchClient()

            with _patched_indexer(client):
                contact = add_contact(
                    document_id=DOCUMENT_ID,
                    fields=_write_request(),
                    source_directory=source_directory,
                    client=client,
                )
                replace_admin_contact_photo(
                    source_directory,
                    DOCUMENT_ID,
                    contact.contact_id,
                    data=_make_png(64, 64, (10, 20, 30)),
                    content_type="image/png",
                    client=client,
                )
                replace_admin_contact_photo(
                    source_directory,
                    DOCUMENT_ID,
                    contact.contact_id,
                    data=_make_png(32, 96, (200, 100, 50)),
                    content_type="image/png",
                    client=client,
                )

            persisted_path = source_directory / "GB.docx"
            download = get_document_download(
                document_id=DOCUMENT_ID,
                source_directory=source_directory,
                client=client,
            )

            self.assertEqual(
                self._sha(download.path), self._sha(persisted_path)
            )

    def test_source_equals_download_after_delete(self) -> None:
        """H: after Contact Delete, persisted source == downloaded
        bytes."""

        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root)
            _seed_placeholder_source_docx(source_directory)
            client = FakeContactOpenSearchClient()

            with _patched_indexer(client):
                first = add_contact(
                    document_id=DOCUMENT_ID,
                    fields=_write_request(member_firm="Firm A"),
                    source_directory=source_directory,
                    client=client,
                )
                add_contact(
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

            persisted_path = source_directory / "GB.docx"
            download = get_document_download(
                document_id=DOCUMENT_ID,
                source_directory=source_directory,
                client=client,
            )

            self.assertEqual(
                self._sha(download.path), self._sha(persisted_path)
            )


# =========================================================================
# UPDATE MUST SYNCHRONIZE THE SOURCE DOCX (not just ContactState)
# =========================================================================


class UpdateContactPersistsToSourceTests(unittest.TestCase):
    """
    Regression coverage for a real bug: update_contact() used to only
    write ContactState/OpenSearch, never calling
    _synchronize_source_document() the way add_contact()/
    delete_contact() both already did - so a real Admin "Update
    Contact" text edit reported success and the ContactState/list
    endpoints reflected the new value, but the actual persisted (and
    therefore downloadable) source DOCX silently kept serving the OLD
    value until some unrelated later mutation happened to trigger a
    fresh rebuild. This was masked before the "DOCX HARDENING" mission
    made download a pure byte read - the OLD download path used to
    call materialize_effective_docx() on every GET, which rebuilt
    fresh from ContactState regardless, hiding the gap.

    test_source_equals_download_after_update above only proves
    download reads exactly what's on disk - it does NOT prove the
    disk copy actually reflects the update, since download and the
    persisted file are the same bytes by construction even when
    update_contact never wrote anything new. These tests inspect the
    actual DOCX content instead of only comparing two reads of
    whatever is already there.
    """

    def _sha(self, path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def test_update_persists_new_value_into_source_docx_and_download(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root)
            _seed_placeholder_source_docx(source_directory)
            client = FakeContactOpenSearchClient()

            with _patched_indexer(client):
                contact = add_contact(
                    document_id=DOCUMENT_ID,
                    fields=_write_request(member_firm="Firm A"),
                    source_directory=source_directory,
                    client=client,
                )
                update_contact(
                    document_id=DOCUMENT_ID,
                    contact_id=contact.contact_id,
                    fields=_write_request(member_firm="Firm B"),
                    source_directory=source_directory,
                    client=client,
                )

            persisted_path = source_directory / "GB.docx"

            persisted_contacts = extract_contacts_from_docx(
                persisted_path, country=None
            )
            self.assertEqual(1, len(persisted_contacts))
            self.assertEqual("Firm B", persisted_contacts[0].member_firm)
            self.assertNotEqual(
                "Firm A", persisted_contacts[0].member_firm,
                "the old value must no longer be the effective contact "
                "value in the persisted document",
            )

            download = get_document_download(
                document_id=DOCUMENT_ID,
                source_directory=source_directory,
                client=client,
            )
            self.assertEqual(
                self._sha(download.path), self._sha(persisted_path)
            )

            downloaded_contacts = extract_contacts_from_docx(
                download.path, country=None
            )
            self.assertEqual(1, len(downloaded_contacts))
            self.assertEqual(
                "Firm B", downloaded_contacts[0].member_firm
            )

    def test_repeated_updates_persist_the_final_value(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root)
            _seed_placeholder_source_docx(source_directory)
            client = FakeContactOpenSearchClient()

            with _patched_indexer(client):
                contact = add_contact(
                    document_id=DOCUMENT_ID,
                    fields=_write_request(member_firm="Firm A"),
                    source_directory=source_directory,
                    client=client,
                )
                update_contact(
                    document_id=DOCUMENT_ID,
                    contact_id=contact.contact_id,
                    fields=_write_request(member_firm="Firm B"),
                    source_directory=source_directory,
                    client=client,
                )
                update_contact(
                    document_id=DOCUMENT_ID,
                    contact_id=contact.contact_id,
                    fields=_write_request(member_firm="Firm C"),
                    source_directory=source_directory,
                    client=client,
                )

            persisted_path = source_directory / "GB.docx"
            persisted_contacts = extract_contacts_from_docx(
                persisted_path, country=None
            )

            self.assertEqual(1, len(persisted_contacts))
            self.assertEqual("Firm C", persisted_contacts[0].member_firm)

            download = get_document_download(
                document_id=DOCUMENT_ID,
                source_directory=source_directory,
                client=client,
            )
            downloaded_contacts = extract_contacts_from_docx(
                download.path, country=None
            )
            self.assertEqual(
                "Firm C", downloaded_contacts[0].member_firm
            )

    def test_update_persists_when_contact_has_no_existing_photo(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root)
            _seed_placeholder_source_docx(source_directory)
            client = FakeContactOpenSearchClient()

            with _patched_indexer(client):
                contact = add_contact(
                    document_id=DOCUMENT_ID,
                    fields=_write_request(
                        member_firm="Firm A", phone="+1 555 0100"
                    ),
                    source_directory=source_directory,
                    client=client,
                )
                update_contact(
                    document_id=DOCUMENT_ID,
                    contact_id=contact.contact_id,
                    fields=_write_request(
                        member_firm="Firm A", phone="+1 555 9999"
                    ),
                    source_directory=source_directory,
                    client=client,
                )

            persisted_path = source_directory / "GB.docx"
            persisted_contacts = extract_contacts_from_docx(
                persisted_path, country=None
            )

            self.assertEqual(1, len(persisted_contacts))
            self.assertEqual("+1 555 9999", persisted_contacts[0].phone)

            photos = extract_contact_photo_candidates(persisted_path)
            self.assertEqual(0, len(photos))

    def test_update_persists_when_contact_already_has_a_photo(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root)
            _seed_placeholder_source_docx(source_directory)
            client = FakeContactOpenSearchClient()

            with _patched_indexer(client):
                contact = add_contact(
                    document_id=DOCUMENT_ID,
                    fields=_write_request(member_firm="Firm A"),
                    source_directory=source_directory,
                    client=client,
                )
                replace_admin_contact_photo(
                    source_directory,
                    DOCUMENT_ID,
                    contact.contact_id,
                    data=_make_png(64, 64, (10, 20, 30)),
                    content_type="image/png",
                    client=client,
                )
                update_contact(
                    document_id=DOCUMENT_ID,
                    contact_id=contact.contact_id,
                    fields=_write_request(member_firm="Firm A Updated"),
                    source_directory=source_directory,
                    client=client,
                )

            persisted_path = source_directory / "GB.docx"
            persisted_contacts = extract_contacts_from_docx(
                persisted_path, country=None
            )

            self.assertEqual(1, len(persisted_contacts))
            self.assertEqual(
                "Firm A Updated", persisted_contacts[0].member_firm
            )

            photos = extract_contact_photo_candidates(persisted_path)
            self.assertEqual(
                1, len(photos),
                "a text-only update must not drop the contact's "
                "already-attached photo",
            )

            download = get_document_download(
                document_id=DOCUMENT_ID,
                source_directory=source_directory,
                client=client,
            )
            self.assertEqual(
                self._sha(download.path), self._sha(persisted_path)
            )


# =========================================================================
# ROLLBACK
# =========================================================================


class ContactRollbackTests(unittest.TestCase):
    def test_index_failure_restores_previous_state_and_marker(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root)
            _seed_placeholder_source_docx(source_directory)
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

    def test_update_index_failure_restores_previous_source_and_state(
        self,
    ) -> None:
        """Mirrors test_index_failure_restores_previous_state_and_marker
        above, for update_contact()'s own new source-DOCX
        synchronization: if the ContactState/index commit fails AFTER
        the DOCX has already been rebuilt with the new value, the DOCX
        must be restored to its exact prior bytes, not left holding
        the new value while ContactState still reports the old one."""

        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root)
            _seed_placeholder_source_docx(source_directory)
            client = FakeContactOpenSearchClient()

            with _patched_indexer(client):
                contact = add_contact(
                    document_id=DOCUMENT_ID,
                    fields=_write_request(member_firm="Firm A"),
                    source_directory=source_directory,
                    client=client,
                )

            persisted_path = source_directory / "GB.docx"
            bytes_before = persisted_path.read_bytes()

            with _patched_indexer(client, fail_bulk=True):
                with self.assertRaises(AdminContactMutationFailedError):
                    update_contact(
                        document_id=DOCUMENT_ID,
                        contact_id=contact.contact_id,
                        fields=_write_request(member_firm="Firm B"),
                        source_directory=source_directory,
                        client=client,
                    )

            self.assertEqual(bytes_before, persisted_path.read_bytes())

            state = read_contact_state(source_directory, DOCUMENT_ID)
            self.assertEqual(state.contacts[0].member_firm, "Firm A")

    def test_rollback_itself_failing_raises_rollback_error(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            source_directory = Path(root)
            _seed_placeholder_source_docx(source_directory)
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


SOURCE_ROOT = Path("/data/documents/source")


class ContactPhotoDeleteSyncTests(unittest.TestCase):
    """
    Mission "FIX ONLY THE REMAINING ADMIN CONTACT <-> SOURCE DOCX
    SYNCHRONIZATION PROBLEMS", requirement 1: deleting a contact must
    remove ONLY that contact's own photo from the persisted source
    DOCX, using the SAME deterministic SHA-256 association already
    proven in contact_document_photos.py - never position, filename,
    or a second, unrelated image-deletion implementation.
    """

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.document_id = _real_document_id_for("AU")

    def _require_copy(self, filename: str) -> Path:
        source = SOURCE_ROOT / filename

        if not source.exists():
            self.skipTest(f"Real corpus source unavailable: {source}")

        original_bytes = source.read_bytes()
        copy_path = self.root / filename
        copy_path.write_bytes(original_bytes)

        self.addCleanup(
            lambda: self.assertEqual(
                original_bytes,
                source.read_bytes(),
                f"{source} was mutated by this test.",
            )
        )

        return copy_path

    def _seed_two_contacts_with_photos(self) -> tuple[str, str]:
        """
        A real AU.docx with Michael Harmer's own real photo, plus a
        second contact added the same way Add Contact would (via
        rebuild_canonical_contact_table), each contact's own photo
        embedded for real, independently. Returns (michael_sha256,
        second_sha256).
        """

        docx_path = self._require_copy("AU.docx")

        michael_photo = extract_contact_photo_candidates(docx_path)[0]
        second_photo_data = _make_png(183, 234, (80, 120, 200))

        jane_contact = ExtractedContact(
            member_firm="Second Firm Pty Ltd",
            contact_person="Jane Secondary",
            email="jane.secondary@secondfirm.com.au",
            phone="+61 2 9000 0000",
            address="100 Test Street, Level 5",
            website="www.secondfirm.com.au",
        )

        new_bytes = rebuild_canonical_contact_table(
            docx_path,
            contacts=(
                ExtractedContact(
                    member_firm="HARMERS WORKPLACE LAWYERS",
                    contact_person="Michael Harmer",
                    email="michael.harmer@harmers.com.au",
                    phone="+61 292 674 322",
                    address="31 Market Street, Level 27 St Martins Tower",
                    website="www.harmers.com.au",
                ),
                jane_contact,
            ),
            photos=(
                ContactPhotoPayload(
                    data=michael_photo.data,
                    content_type=michael_photo.content_type,
                ),
                ContactPhotoPayload(
                    data=second_photo_data, content_type="image/png"
                ),
            ),
            country="Australia",
        )
        docx_path.write_bytes(new_bytes)

        michael_stored = write_contact_photo_atomic(
            self.root,
            "michael-id",
            data=michael_photo.data,
            content_type=michael_photo.content_type,
        )
        second_stored = write_contact_photo_atomic(
            self.root,
            "jane-id",
            data=second_photo_data,
            content_type="image/png",
        )

        write_contact_state_atomic(
            self.root,
            ContactState(
                document_id=self.document_id,
                country_code="AU",
                contacts=(
                    ContactRecord(
                        contact_id="michael-id",
                        member_firm="HARMERS WORKPLACE LAWYERS",
                        contact_person="Michael Harmer",
                        email="michael.harmer@harmers.com.au",
                        phone="+61 292 674 322",
                        address="31 Market Street, Level 27 St Martins Tower",
                        website="www.harmers.com.au",
                        photo_filename=michael_stored.filename,
                        photo_content_type=michael_stored.content_type,
                        photo_sha256=michael_stored.sha256,
                    ),
                    ContactRecord(
                        contact_id="jane-id",
                        member_firm="Second Firm Pty Ltd",
                        contact_person="Jane Secondary",
                        email="jane.secondary@secondfirm.com.au",
                        phone="+61 2 9000 0000",
                        address="100 Test Street, Level 5",
                        website="www.secondfirm.com.au",
                        photo_filename=second_stored.filename,
                        photo_content_type=second_stored.content_type,
                        photo_sha256=second_stored.sha256,
                    ),
                ),
            ),
        )

        return michael_stored.sha256, second_stored.sha256

    def test_delete_contact_with_photo_removes_only_its_own_image(
        self,
    ) -> None:
        docx_path = self.root / "AU.docx"
        michael_sha, jane_sha = self._seed_two_contacts_with_photos()

        client = FakeContactOpenSearchClient(
            document_id=self.document_id,
            country_code="AU",
            country="Australia",
            source_filename="AU.docx",
        )

        with _patched_indexer(client):
            delete_contact(
                document_id=self.document_id,
                contact_id="jane-id",
                source_directory=self.root,
                client=client,
            )

        remaining_shas = {
            c.sha256 for c in extract_contact_photo_candidates(docx_path)
        }
        self.assertIn(
            michael_sha, remaining_shas,
            "the other contact's own photo must remain byte/"
            "functionally intact",
        )
        self.assertNotIn(
            jane_sha, remaining_shas,
            "no stale photo of the deleted contact may remain "
            "anywhere in the DOCX",
        )
        self.assertEqual(1, len(remaining_shas))

        state = read_contact_state(self.root, self.document_id)
        self.assertEqual(1, len(state.contacts))
        self.assertEqual("michael-id", state.contacts[0].contact_id)

    def test_delete_mutation_failure_restores_source_docx_byte_for_byte(
        self,
    ) -> None:
        docx_path = self.root / "AU.docx"
        self._seed_two_contacts_with_photos()

        original_bytes = docx_path.read_bytes()

        client = FakeContactOpenSearchClient(
            document_id=self.document_id,
            country_code="AU",
            country="Australia",
            source_filename="AU.docx",
        )

        with self.assertRaises(AdminContactMutationFailedError):
            with _patched_indexer(client, fail_bulk=True):
                delete_contact(
                    document_id=self.document_id,
                    contact_id="jane-id",
                    source_directory=self.root,
                    client=client,
                )

        self.assertEqual(
            original_bytes, docx_path.read_bytes(),
            "the source DOCX must be restored to its exact original "
            "bytes when the ContactState/index commit fails after the "
            "photo was already removed",
        )

        state = read_contact_state(self.root, self.document_id)
        self.assertEqual(2, len(state.contacts))

    def test_delete_contact_without_a_photo_still_removes_its_text(
        self,
    ) -> None:
        """A contact with no photo_sha256 at all must still have its
        own TEXT block removed from the persisted source DOCX -
        deleting a contact must never leave its name/email/firm behind
        just because it never had a photo."""

        docx_path = self._require_copy("AU.docx")

        write_contact_state_atomic(
            self.root,
            ContactState(
                document_id=self.document_id,
                country_code="AU",
                contacts=(
                    ContactRecord(
                        contact_id="michael-id",
                        member_firm="HARMERS WORKPLACE LAWYERS",
                        contact_person="Michael Harmer",
                        email="michael.harmer@harmers.com.au",
                        phone="+61 292 674 322",
                        address="31 Market Street",
                        website="www.harmers.com.au",
                    ),
                ),
            ),
        )

        client = FakeContactOpenSearchClient(
            document_id=self.document_id,
            country_code="AU",
            country="Australia",
            source_filename="AU.docx",
        )

        with _patched_indexer(client):
            delete_contact(
                document_id=self.document_id,
                contact_id="michael-id",
                source_directory=self.root,
                client=client,
            )

        with zipfile.ZipFile(docx_path) as archive:
            document_xml = archive.read(
                "word/document.xml"
            ).decode("utf-8", errors="ignore")

        self.assertNotIn("Michael Harmer", document_xml)
        self.assertNotIn("michael.harmer@harmers.com.au", document_xml)

        remaining = extract_contacts_from_docx(docx_path, country="Australia")
        self.assertEqual(0, len(remaining))

    def _seed_contact_a_only(self, docx_path: Path) -> str:
        """Contact A (Michael Harmer) exactly as AU.docx's own native
        organic block already reads, with his own real embedded photo
        registered in ContactState - the document's starting state
        before an Admin-added contact B exists at all."""

        michael_photo = extract_contact_photo_candidates(docx_path)[0]
        michael_stored = write_contact_photo_atomic(
            self.root,
            "michael-id",
            data=michael_photo.data,
            content_type=michael_photo.content_type,
        )

        write_contact_state_atomic(
            self.root,
            ContactState(
                document_id=self.document_id,
                country_code="AU",
                contacts=(
                    ContactRecord(
                        contact_id="michael-id",
                        member_firm="HARMERS WORKPLACE LAWYERS",
                        contact_person="Michael Harmer",
                        email="michael.harmer@harmers.com.au",
                        phone="+61 292 674 322",
                        address="31 Market Street",
                        website="www.harmers.com.au",
                        photo_filename=michael_stored.filename,
                        photo_content_type=michael_stored.content_type,
                        photo_sha256=michael_stored.sha256,
                    ),
                ),
            ),
        )

        return michael_stored.sha256

    def _add_contact_b_with_photo_via_real_admin_flow(
        self, client: FakeContactOpenSearchClient
    ) -> tuple[str, bytes]:
        """Exercises the REAL, two-call Admin surface exactly as an
        operator would use it - add_contact() (text only) followed by
        replace_admin_contact_photo() (attaches the photo, rebuilding
        the canonical table again) - never the lower-level
        rebuild_canonical_contact_table() primitive directly.
        Returns (contact_b_id, photo_b_bytes)."""

        jane_photo_data = _make_png(183, 234, (80, 120, 200))

        with _patched_indexer(client):
            response = add_contact(
                document_id=self.document_id,
                fields=_write_request(
                    member_firm="Second Firm Pty Ltd",
                    contact_person="Jane Secondary",
                    email="jane.secondary@secondfirm.com.au",
                    phone="+61 2 9000 0000",
                    address="100 Test Street, Level 5",
                    website="www.secondfirm.com.au",
                ),
                source_directory=self.root,
                client=client,
            )

        with _patched_indexer(client):
            replace_admin_contact_photo(
                self.root,
                self.document_id,
                response.contact_id,
                data=jane_photo_data,
                content_type="image/png",
                client=client,
            )

        return response.contact_id, jane_photo_data

    def test_admin_added_contact_b_fully_removed_from_source_on_delete(
        self,
    ) -> None:
        """The user's mandatory focused test: ADD contact B + photo B
        (via the real two-call Admin surface, onto a document that
        already carries contact A natively), then DELETE contact B,
        then prove directly against the PERSISTED source DOCX that
        contact B's text and photo are gone while contact A's text and
        photo remain - byte-level proof, not ContactState alone."""

        docx_path = self._require_copy("AU.docx")
        michael_sha = self._seed_contact_a_only(docx_path)

        client = FakeContactOpenSearchClient(
            document_id=self.document_id,
            country_code="AU",
            country="Australia",
            source_filename="AU.docx",
        )

        contact_b_id, jane_photo_data = (
            self._add_contact_b_with_photo_via_real_admin_flow(client)
        )
        jane_sha = hashlib.sha256(jane_photo_data).hexdigest()

        # Sanity check before delete: contact B is genuinely present
        # in the persisted source, not merely in ContactState.
        with zipfile.ZipFile(docx_path) as archive:
            pre_delete_xml = archive.read(
                "word/document.xml"
            ).decode("utf-8", errors="ignore")
        self.assertIn("Jane Secondary", pre_delete_xml)
        pre_delete_shas = {
            c.sha256 for c in extract_contact_photo_candidates(docx_path)
        }
        self.assertIn(jane_sha, pre_delete_shas)

        with _patched_indexer(client):
            delete_contact(
                document_id=self.document_id,
                contact_id=contact_b_id,
                source_directory=self.root,
                client=client,
            )

        with zipfile.ZipFile(docx_path) as archive:
            document_xml = archive.read(
                "word/document.xml"
            ).decode("utf-8", errors="ignore")
        remaining_shas = {
            c.sha256 for c in extract_contact_photo_candidates(docx_path)
        }

        contact_b_text_in_source = (
            "Jane Secondary" in document_xml
            or "jane.secondary@secondfirm.com.au" in document_xml
        )
        contact_b_photo_in_source = jane_sha in remaining_shas
        contact_a_text_in_source = (
            "Michael Harmer" in document_xml
            and "michael.harmer@harmers.com.au" in document_xml
        )
        contact_a_photo_in_source = michael_sha in remaining_shas

        print(
            f"CONTACT_B_TEXT_IN_SOURCE="
            f"{'YES' if contact_b_text_in_source else 'NO'}"
        )
        print(
            f"CONTACT_B_PHOTO_IN_SOURCE="
            f"{'YES' if contact_b_photo_in_source else 'NO'}"
        )
        print(
            f"CONTACT_A_TEXT_IN_SOURCE="
            f"{'YES' if contact_a_text_in_source else 'NO'}"
        )
        print(
            f"CONTACT_A_PHOTO_IN_SOURCE="
            f"{'YES' if contact_a_photo_in_source else 'NO'}"
        )

        self.assertFalse(
            contact_b_text_in_source,
            "deleted contact B's own text must not survive in the "
            "persisted source DOCX",
        )
        self.assertFalse(
            contact_b_photo_in_source,
            "deleted contact B's own photo must not survive in the "
            "persisted source DOCX",
        )
        self.assertTrue(
            contact_a_text_in_source,
            "surviving contact A's own text must remain untouched",
        )
        self.assertTrue(
            contact_a_photo_in_source,
            "surviving contact A's own photo must remain untouched",
        )

        remaining_contacts = extract_contacts_from_docx(
            docx_path, country="Australia"
        )
        self.assertEqual(1, len(remaining_contacts))
        self.assertEqual(
            "Michael Harmer", remaining_contacts[0].contact_person
        )

    def test_admin_added_contact_b_delete_failure_restores_source_byte_for_byte(
        self,
    ) -> None:
        """Same admin-added contact B scenario, but the ContactState/
        index commit fails after the source DOCX has already been
        rewritten to remove contact B's text+photo - the source must
        be restored to its EXACT prior bytes (with contact B still
        present), never left half-mutated."""

        docx_path = self._require_copy("AU.docx")
        self._seed_contact_a_only(docx_path)

        client = FakeContactOpenSearchClient(
            document_id=self.document_id,
            country_code="AU",
            country="Australia",
            source_filename="AU.docx",
        )

        contact_b_id, _ = (
            self._add_contact_b_with_photo_via_real_admin_flow(client)
        )

        original_bytes = docx_path.read_bytes()

        with self.assertRaises(AdminContactMutationFailedError):
            with _patched_indexer(client, fail_bulk=True):
                delete_contact(
                    document_id=self.document_id,
                    contact_id=contact_b_id,
                    source_directory=self.root,
                    client=client,
                )

        self.assertEqual(
            original_bytes,
            docx_path.read_bytes(),
            "the source DOCX must be restored to its exact original "
            "bytes (contact B's text and photo both still present) "
            "when the ContactState/index commit fails after the "
            "source rewrite already succeeded",
        )

        state = read_contact_state(self.root, self.document_id)
        self.assertEqual(2, len(state.contacts))
        self.assertIn(
            contact_b_id, {c.contact_id for c in state.contacts}
        )


class AddContactFallbackSynchronizationTests(unittest.TestCase):
    """
    Mission "FIX ONLY THE REMAINING ADMIN CONTACT <-> SOURCE DOCX
    SYNCHRONIZATION PROBLEMS", requirement 2: a document with no
    detected two-box contact area (PT.docx - proven structure-less by
    test_contact_document_area.py's own
    test_fails_closed_with_no_structural_reference_at_all) must never
    leave a successful Add Contact as ContactState-only. add_contact()
    must fall back to contact_document_area.rebuild_canonical_contact_
    table()'s own no-existing-area handling (_default_insertion_anchor)
    and commit that to the source - SOURCE DOCX == CONTACT STATE ==
    OPENSEARCH holds even here. (This docstring previously named
    document_contact_materializer.persist_inline_contact_fallback() -
    that function was superseded by the canonical-table mechanism and
    removed entirely, mission "DOCX HARDENING", 2026-08-24; this test's
    own assertions below already proved the canonical table, not that
    function, was the real fallback in use.)
    """

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.document_id = _real_document_id_for("PT")

    def _require_copy(self, filename: str) -> Path:
        source = SOURCE_ROOT / filename

        if not source.exists():
            self.skipTest(f"Real corpus source unavailable: {source}")

        original_bytes = source.read_bytes()
        copy_path = self.root / filename
        copy_path.write_bytes(original_bytes)

        self.addCleanup(
            lambda: self.assertEqual(
                original_bytes,
                source.read_bytes(),
                f"{source} was mutated by this test.",
            )
        )

        return copy_path

    def _client(self) -> FakeContactOpenSearchClient:
        return FakeContactOpenSearchClient(
            document_id=self.document_id,
            country_code="PT",
            country="Portugal",
            source_filename="PT.docx",
        )

    def test_add_contact_without_structural_area_still_synchronizes_source(
        self,
    ) -> None:
        docx_path = self._require_copy("PT.docx")
        original_bytes = docx_path.read_bytes()

        client = self._client()

        with _patched_indexer(client):
            add_contact(
                document_id=self.document_id,
                fields=_write_request(
                    member_firm="Someone New Lda",
                    contact_person="Someone New",
                    email="new@example.test",
                    phone="+351 21 000 0000",
                    address="Rua Nova 1",
                    website="www.newfirm.test",
                ),
                source_directory=self.root,
                client=client,
            )

        new_bytes = docx_path.read_bytes()

        self.assertNotEqual(
            original_bytes,
            new_bytes,
            "a successful Add Contact must always rewrite the "
            "persisted source DOCX, even when the document has no "
            "two-box contact area to clone the style of - "
            "ContactState alone is never sufficient",
        )

        with zipfile.ZipFile(docx_path) as archive:
            document_xml = archive.read(
                "word/document.xml"
            ).decode("utf-8", errors="ignore")

        self.assertIn("Someone New", document_xml)
        self.assertIn("new@example.test", document_xml)
        self.assertIn(CONTACT_TABLE_HIDDEN_MARKER, document_xml)
        self.assertIn("<w:tbl>", document_xml)

        state = read_contact_state(self.root, self.document_id)
        self.assertEqual(1, len(state.contacts))

    def test_second_add_replaces_rather_than_duplicates_the_fallback_block(
        self,
    ) -> None:
        """Two sequential Add Contact calls against the same
        structure-less document must each leave the source
        synchronized with the FULL current contact list - never
        stacking a second, stale fallback block alongside the first
        (which would silently resurrect a deleted/superseded
        rendering of the earlier contact)."""

        docx_path = self._require_copy("PT.docx")
        client = self._client()

        with _patched_indexer(client):
            add_contact(
                document_id=self.document_id,
                fields=_write_request(
                    member_firm="First Firm Lda",
                    contact_person="First Person",
                    email="first@example.test",
                ),
                source_directory=self.root,
                client=client,
            )

        with _patched_indexer(client):
            add_contact(
                document_id=self.document_id,
                fields=_write_request(
                    member_firm="Second Firm Lda",
                    contact_person="Second Person",
                    email="second@example.test",
                ),
                source_directory=self.root,
                client=client,
            )

        with zipfile.ZipFile(docx_path) as archive:
            document_xml = archive.read(
                "word/document.xml"
            ).decode("utf-8", errors="ignore")

        self.assertIn("First Person", document_xml)
        self.assertIn("Second Person", document_xml)
        self.assertEqual(
            1,
            document_xml.count(CONTACT_TABLE_HIDDEN_MARKER),
            "a second Add Contact must replace the one canonical "
            "table with a fresh rendering of the full contact list, "
            "never append a second one",
        )

        state = read_contact_state(self.root, self.document_id)
        self.assertEqual(2, len(state.contacts))


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
            _seed_placeholder_source_docx(source_directory)

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
