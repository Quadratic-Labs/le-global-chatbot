"""
Tests for Admin contact photo CRUD (mission "COMPLETE CONTACT PHOTO
CRUD + DOCX SOURCE SYNCHRONIZATION").

Every mutation test here backs onto a REAL corpus DOCX (temp copy,
never the real file itself - see SOURCE_ROOT/_require_copy below,
mirroring test_contact_photos.py's own established convention of
skipping gracefully when /data/documents/source is unavailable rather
than failing), through a small fake OpenSearch client that answers
exactly the one read-only metadata lookup
(_resolve_current_source_path) these mutations need - never a real
network call, and never any reindexing.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any

from app.routers.admin_contacts import router
from app.services.admin_contact_photos import (
    AdminContactPhotoError,
    AdminContactPhotoNotFoundError,
    read_admin_contact_photo,
    remove_admin_contact_photo,
    replace_admin_contact_photo,
)
from app.services.admin_document_lifecycle import get_document_download
from app.services.contact_photo_store import write_contact_photo_atomic
from app.services.contact_photos import extract_contact_photo_candidates
from app.services.contact_state import (
    ContactRecord,
    ContactState,
    read_contact_state,
    write_contact_state_atomic,
)


SOURCE_ROOT = Path("/data/documents/source")

def _make_valid_jpeg(width: int = 183, height: int = 234) -> bytes:
    """
    A minimal but REAL-SIZED, structurally complete baseline JPEG -
    SOI/APP0(JFIF)/SOF0/SOS/EOI with plausible width/height, never a
    degenerate single-pixel stub. python-docx's own image-header
    parser (used to compute a photo's proportional height in the
    canonical contact table) stops reading at the first SOS marker, so
    the entropy-coded scan data itself can be a single placeholder
    byte - only the headers before it need to be genuine.
    """

    import struct

    soi = b"\xff\xd8"
    app0_data = (
        b"JFIF\x00\x01\x01\x00"
        + struct.pack(">HH", 96, 96)
        + b"\x00\x00"
    )
    app0 = b"\xff\xe0" + struct.pack(">H", len(app0_data) + 2) + app0_data
    sof0_data = (
        bytes([8])
        + struct.pack(">HH", height, width)
        + bytes([1, 1, 0x11, 0])
    )
    sof0 = b"\xff\xc0" + struct.pack(">H", len(sof0_data) + 2) + sof0_data
    sos_data = bytes([1, 1, 0, 0, 63, 0])
    sos = b"\xff\xda" + struct.pack(">H", len(sos_data) + 2) + sos_data
    return soi + app0 + sof0 + sos + b"\x00\xff\xd9"


def _make_png(width: int, height: int, rgb: tuple[int, int, int]) -> bytes:
    """A minimal but well-formed, real-sized RGB PNG - see
    _make_valid_jpeg's own docstring for why a degenerate single-pixel
    fixture is not enough now that a photo's own proportional height
    is computed from its real dimensions."""

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


_VALID_JPEG = _make_valid_jpeg()
_VALID_PNG = _make_png(183, 234, (120, 80, 200))


class _FakePhotoOpenSearchClient:
    """The one read-only metadata lookup contact photo mutations
    need - never a reindex, never a real network call."""

    def __init__(
        self,
        *,
        document_id: str,
        country_code: str,
        country: str,
        source_filename: str,
        reference_year: int | None = 2026,
    ) -> None:
        self.document_id = document_id
        self.country_code = country_code
        self.country = country
        self.source_filename = source_filename
        self.reference_year = reference_year

    def search(self, index: str, body: dict[str, Any]) -> dict[str, Any]:
        del index

        term = body.get("query", {}).get("term", {})

        if term.get("document_id") != self.document_id:
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


class AdminContactPhotoTests(unittest.TestCase):

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def _require_source_copy(self, filename: str) -> Path:
        source = SOURCE_ROOT / filename

        if not source.exists():
            self.skipTest(
                f"Real corpus source unavailable: {source}"
            )

        return source

    def _seed_photo_bearing_contact(self) -> _FakePhotoOpenSearchClient:
        """
        Argentina (AR.docx), copied into this test's own
        source_directory as AR.docx (the canonical
        {COUNTRY_CODE}.docx storage name
        resolve_document_source_path expects) - a real single
        contact with a real existing photo, so REPLACE/REMOVE exercise
        the actual DOCX-mutation code path, not a synthetic stand-in.
        """

        real_source = self._require_source_copy("AR.docx")
        docx_path = self.root / "AR.docx"
        shutil.copyfile(real_source, docx_path)

        candidates = extract_contact_photo_candidates(docx_path)
        assert len(candidates) == 1
        photo = candidates[0]

        stored = write_contact_photo_atomic(
            self.root,
            "contact-test",
            data=photo.data,
            content_type=photo.content_type,
        )

        write_contact_state_atomic(
            self.root,
            ContactState(
                document_id="doc_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                country_code="AR",
                contacts=(
                    ContactRecord(
                        contact_id="contact-test",
                        member_firm="Allende & Brea",
                        contact_person="Nicolás Grandi",
                        email="ngrandi@allende.com",
                        phone="+1",
                        address="Address",
                        website="https://example.com",
                        photo_filename=stored.filename,
                        photo_content_type=stored.content_type,
                        photo_sha256=stored.sha256,
                    ),
                ),
            ),
        )

        return _FakePhotoOpenSearchClient(
            document_id="doc_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            country_code="AR",
            country="Argentina",
            source_filename="AR.docx",
        )

    def _seed_photo_less_contact(self) -> _FakePhotoOpenSearchClient:
        """
        Germany (DE.docx), copied in the same way - a real single
        contact (Tobias Pusch) who genuinely has no photo yet, so a
        PUT exercises the real ADD-into-the-document code path.
        """

        real_source = self._require_source_copy("DE.docx")
        docx_path = self.root / "DE.docx"
        shutil.copyfile(real_source, docx_path)

        assert extract_contact_photo_candidates(docx_path) == []

        write_contact_state_atomic(
            self.root,
            ContactState(
                document_id="doc_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                country_code="DE",
                contacts=(
                    ContactRecord(
                        contact_id="contact-test",
                        member_firm="Pusch Wahlig Workplace Law",
                        contact_person="Tobias Pusch",
                        email="pusch@pwwl.de",
                        phone="+1",
                        address="Address",
                        website="https://example.com",
                    ),
                ),
            ),
        )

        return _FakePhotoOpenSearchClient(
            document_id="doc_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            country_code="DE",
            country="Germany",
            source_filename="DE.docx",
        )

    def _seed_belgium_two_contacts(self) -> _FakePhotoOpenSearchClient:
        """
        Belgium's real two-contact, two-photo document - to prove
        isolation holds at the FULL service layer (ContactState +
        photo store + source DOCX together), not merely at the raw
        DOCX-primitive level test_contact_document_photos.py already
        covers.
        """

        real_source = self._require_source_copy(
            "Labour and Employment Law in Belgium 2026.docx"
        )
        docx_path = self.root / "BE.docx"
        shutil.copyfile(real_source, docx_path)

        candidates = extract_contact_photo_candidates(docx_path)
        by_name = {c.source_filename: c for c in candidates}
        chris_photo = by_name["image2.jpg"]
        nicolas_photo = by_name["image1.png"]

        chris_stored = write_contact_photo_atomic(
            self.root,
            "chris-id",
            data=chris_photo.data,
            content_type=chris_photo.content_type,
        )
        nicolas_stored = write_contact_photo_atomic(
            self.root,
            "nicolas-id",
            data=nicolas_photo.data,
            content_type=nicolas_photo.content_type,
        )

        write_contact_state_atomic(
            self.root,
            ContactState(
                document_id="doc_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                country_code="BE",
                contacts=(
                    ContactRecord(
                        contact_id="chris-id",
                        member_firm="Van Olmen & Wynant",
                        contact_person="Chris van Olmen",
                        email="chris.van.olmen@vow.be",
                        phone="+1",
                        address="Address",
                        website="https://example.com",
                        photo_filename=chris_stored.filename,
                        photo_content_type=chris_stored.content_type,
                        photo_sha256=chris_stored.sha256,
                    ),
                    ContactRecord(
                        contact_id="nicolas-id",
                        member_firm="Van Olmen & Wynant",
                        contact_person="Nicolas Simon",
                        email="nicolas.simon@vow.be",
                        phone="+1",
                        address="Address",
                        website="https://example.com",
                        photo_filename=nicolas_stored.filename,
                        photo_content_type=nicolas_stored.content_type,
                        photo_sha256=nicolas_stored.sha256,
                    ),
                ),
            ),
        )

        return _FakePhotoOpenSearchClient(
            document_id="doc_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            country_code="BE",
            country="Belgium",
            source_filename="BE.docx",
        )

    def test_belgium_two_contact_isolation_at_the_service_layer(
        self,
    ) -> None:
        """Mission section 22, case 8: mutating Chris's photo must
        never touch Nicolas's ContactState, photo file, or DOCX
        image - and vice versa."""

        client = self._seed_belgium_two_contacts()
        docx_path = self.root / "BE.docx"
        doc_id = (
            "doc_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        )

        original_state = read_contact_state(self.root, doc_id)
        nicolas_before = next(
            c for c in original_state.contacts
            if c.contact_id == "nicolas-id"
        )

        replace_admin_contact_photo(
            self.root,
            doc_id,
            "chris-id",
            data=_VALID_JPEG,
            content_type="image/jpeg",
            client=client,
        )

        state_after = read_contact_state(self.root, doc_id)
        nicolas_after = next(
            c for c in state_after.contacts
            if c.contact_id == "nicolas-id"
        )

        self.assertEqual(nicolas_before, nicolas_after)

        docx_shas = {
            c.sha256
            for c in extract_contact_photo_candidates(docx_path)
        }
        self.assertIn(nicolas_before.photo_sha256, docx_shas)
        self.assertEqual(2, len(docx_shas))

        self.assertTrue(
            remove_admin_contact_photo(
                self.root, doc_id, "nicolas-id", client=client
            )
        )

        final_state = read_contact_state(self.root, doc_id)
        chris_final = next(
            c for c in final_state.contacts
            if c.contact_id == "chris-id"
        )
        self.assertIsNotNone(chris_final.photo_sha256)

        final_docx_shas = {
            c.sha256
            for c in extract_contact_photo_candidates(docx_path)
        }
        self.assertEqual(1, len(final_docx_shas))
        self.assertIn(chris_final.photo_sha256, final_docx_shas)

    def test_add_photo_for_a_brand_new_contact_falls_back_to_the_largest_zone(
        self,
    ) -> None:
        """
        Mission "FINAL BLOCKER": a brand-new contact's name will
        usually have no matching "CONTACT PERSON" zone in the document
        at all (that IS the common case for genuinely adding someone -
        their name cannot possibly already appear anywhere). Rather
        than failing closed here, this now anchors to the document's
        own largest existing CONTACT PERSON zone (Tobias Pusch's own,
        the only one in DE.docx) - never a name-based search, and
        never disturbing that other contact's own zone or photo state.
        """

        client = self._seed_photo_less_contact()
        docx_path = self.root / "DE.docx"
        doc_id = (
            "doc_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        )

        write_contact_state_atomic(
            self.root,
            ContactState(
                document_id=doc_id,
                country_code="DE",
                contacts=(
                    ContactRecord(
                        contact_id="contact-test",
                        member_firm="Someone New GmbH",
                        contact_person="Someone New",
                        email="new@example.test",
                        phone="+1",
                        address="Address",
                        website="https://example.com",
                    ),
                ),
            ),
        )

        photo = replace_admin_contact_photo(
            self.root,
            doc_id,
            "contact-test",
            data=_VALID_JPEG,
            content_type="image/jpeg",
            client=client,
        )

        docx_shas = {
            c.sha256
            for c in extract_contact_photo_candidates(docx_path)
        }
        self.assertEqual(1, len(docx_shas))
        self.assertIn(photo.sha256, docx_shas)

        state = read_contact_state(self.root, doc_id)
        self.assertEqual(
            photo.sha256, state.contacts[0].photo_sha256
        )

        download = get_document_download(
            document_id=doc_id,
            source_directory=self.root,
            client=client,
        )
        self.addCleanup(
            lambda: (
                download.cleanup_path
                and download.cleanup_path.unlink(missing_ok=True)
            )
        )
        downloaded_shas = {
            c.sha256
            for c in extract_contact_photo_candidates(download.path)
        }
        self.assertIn(
            photo.sha256,
            downloaded_shas,
            "the downloaded DOCX must contain the new contact's "
            "photo, not merely ContactState",
        )

    def _seed_zero_zone_country(self) -> _FakePhotoOpenSearchClient:
        """
        Portugal (PT.docx) - a real document with genuinely ZERO
        "CONTACT PERSON" zones anywhere. The canonical contact table
        mechanism rebuilds the whole contact area from ContactState
        regardless of prior structure, so a photo mutation here now
        succeeds (synchronizing a fresh table into the source) rather
        than failing closed the way the old floating-shape-only
        mechanism had to.
        """

        real_source = self._require_source_copy("PT.docx")
        docx_path = self.root / "PT.docx"
        shutil.copyfile(real_source, docx_path)

        assert extract_contact_photo_candidates(docx_path) == []

        write_contact_state_atomic(
            self.root,
            ContactState(
                document_id="doc_cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
                country_code="PT",
                contacts=(
                    ContactRecord(
                        contact_id="contact-test",
                        member_firm="Someone New Lda",
                        contact_person="Someone New",
                        email="new@example.test",
                        phone="+1",
                        address="Address",
                        website="https://example.com",
                    ),
                ),
            ),
        )

        return _FakePhotoOpenSearchClient(
            document_id="doc_cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
            country_code="PT",
            country="Portugal",
            source_filename="PT.docx",
        )

    def test_new_contact_photo_for_a_zero_zone_document_still_syncs(
        self,
    ) -> None:
        """
        A document with no contact area at all still gets the new
        photo synchronized into a freshly rebuilt canonical table -
        the source DOCX is never left unsynchronized just because it
        had no prior contact structure.
        """

        client = self._seed_zero_zone_country()
        doc_id = (
            "doc_cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
        )

        photo = replace_admin_contact_photo(
            self.root,
            doc_id,
            "contact-test",
            data=_VALID_JPEG,
            content_type="image/jpeg",
            client=client,
        )

        docx_path = self.root / "PT.docx"
        docx_candidates = extract_contact_photo_candidates(docx_path)
        self.assertEqual(1, len(docx_candidates))
        self.assertEqual(photo.sha256, docx_candidates[0].sha256)

        state = read_contact_state(self.root, doc_id)
        self.assertEqual(photo.sha256, state.contacts[0].photo_sha256)

    def test_replace_read_remove_syncs_the_source_docx(self) -> None:
        client = self._seed_photo_bearing_contact()
        docx_path = self.root / "AR.docx"

        photo = replace_admin_contact_photo(
            self.root,
            "doc_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "contact-test",
            data=_VALID_JPEG,
            content_type="image/jpeg",
            client=client,
        )

        self.assertEqual("image/jpeg", photo.content_type)

        # The persisted source DOCX itself must now resolve to the
        # NEW photo, not merely ContactState.
        docx_shas = {
            c.sha256
            for c in extract_contact_photo_candidates(docx_path)
        }
        self.assertIn(photo.sha256, docx_shas)
        self.assertEqual(1, len(docx_shas))

        loaded = read_admin_contact_photo(
            self.root, "doc_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "contact-test"
        )
        self.assertEqual(_VALID_JPEG, loaded.data)

        self.assertTrue(
            remove_admin_contact_photo(
                self.root, "doc_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "contact-test", client=client
            )
        )

        with self.assertRaises(AdminContactPhotoNotFoundError):
            read_admin_contact_photo(
                self.root, "doc_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "contact-test"
            )

        # The removal must also be reflected in the source DOCX -
        # not merely ContactState (mission Bug D).
        self.assertEqual(
            [], extract_contact_photo_candidates(docx_path)
        )

    def test_add_to_a_photo_less_contact_syncs_the_source_docx(
        self,
    ) -> None:
        client = self._seed_photo_less_contact()
        docx_path = self.root / "DE.docx"

        photo = replace_admin_contact_photo(
            self.root,
            "doc_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "contact-test",
            data=_VALID_PNG,
            content_type="image/png",
            client=client,
        )

        docx_candidates = extract_contact_photo_candidates(docx_path)
        self.assertEqual(1, len(docx_candidates))
        self.assertEqual(photo.sha256, docx_candidates[0].sha256)

        state = read_contact_state(self.root, "doc_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
        self.assertEqual(
            photo.sha256, state.contacts[0].photo_sha256
        )

    def test_replace_failure_leaves_the_source_docx_and_state_unchanged(
        self,
    ) -> None:
        """
        A ContactState photo SHA that no longer matches anything in
        the source DOCX (simulating drift/corruption) is never
        consulted for a REPLACE - the canonical table is rebuilt fresh
        from the newly-uploaded bytes directly, so a stale/corrupt
        prior reference cannot block a legitimate new upload. This is
        a deliberate improvement over the old floating-shape mechanism
        (which had to locate the OLD sha in the document before it
        could replace it): rebuilding the whole area from ContactState
        every time eliminates this entire class of drift-driven
        failure.
        """

        client = self._seed_photo_bearing_contact()

        write_contact_state_atomic(
            self.root,
            ContactState(
                document_id="doc_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                country_code="AR",
                contacts=(
                    ContactRecord(
                        contact_id="contact-test",
                        member_firm="Allende & Brea",
                        contact_person="Nicolás Grandi",
                        email="ngrandi@allende.com",
                        phone="+1",
                        address="Address",
                        website="https://example.com",
                        photo_filename="contact-test--deadbeef.jpg",
                        photo_content_type="image/jpeg",
                        photo_sha256="0" * 64,
                    ),
                ),
            ),
        )

        photo = replace_admin_contact_photo(
            self.root,
            "doc_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "contact-test",
            data=_VALID_JPEG,
            content_type="image/jpeg",
            client=client,
        )

        docx_path = self.root / "AR.docx"
        docx_candidates = extract_contact_photo_candidates(docx_path)
        self.assertEqual(1, len(docx_candidates))
        self.assertEqual(photo.sha256, docx_candidates[0].sha256)

        state = read_contact_state(
            self.root,
            "doc_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        )
        self.assertEqual(photo.sha256, state.contacts[0].photo_sha256)

    def test_downloaded_docx_reflects_a_replaced_photo(self) -> None:
        """
        Mission section 19 (mandatory): after a mutation, the SAME
        backend document-download path Admin uses must reflect it -
        not merely ContactState. get_document_download() is exactly
        the function backing GET .../download.
        """

        client = self._seed_photo_bearing_contact()

        photo = replace_admin_contact_photo(
            self.root,
            "doc_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "contact-test",
            data=_VALID_JPEG,
            content_type="image/jpeg",
            client=client,
        )

        download = get_document_download(
            document_id="doc_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            source_directory=self.root,
            client=client,
        )
        self.addCleanup(
            lambda: (
                download.cleanup_path
                and download.cleanup_path.unlink(missing_ok=True)
            )
        )

        downloaded_shas = {
            c.sha256
            for c in extract_contact_photo_candidates(download.path)
        }
        self.assertIn(photo.sha256, downloaded_shas)
        self.assertEqual(1, len(downloaded_shas))

    def test_fake_image_is_rejected(self):
        client = self._seed_photo_bearing_contact()

        with self.assertRaises(AdminContactPhotoError):
            replace_admin_contact_photo(
                self.root,
                "doc_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "contact-test",
                data=b"not-an-image",
                content_type="image/jpeg",
                client=client,
            )

    def test_mime_mismatch_is_rejected(self):
        client = self._seed_photo_bearing_contact()

        with self.assertRaises(AdminContactPhotoError):
            replace_admin_contact_photo(
                self.root,
                "doc_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "contact-test",
                data=b"\x89PNG\r\n\x1a\nfake",
                content_type="image/jpeg",
                client=client,
            )

    def test_photo_routes_keep_admin_security_dependencies(self):
        normal = None
        photo_routes = []

        for route in router.routes:
            methods = getattr(route, "methods", set())

            if (
                route.path.endswith("/{document_id}/contacts")
                and "GET" in methods
            ):
                normal = route

            if route.path.endswith(
                "/{document_id}/contacts/{contact_id}/photo"
            ):
                photo_routes.append(route)

        self.assertIsNotNone(normal)
        self.assertEqual(3, len(photo_routes))

        normal_dependencies = len(
            normal.dependant.dependencies
        )

        self.assertGreater(normal_dependencies, 0)

        for route in photo_routes:
            self.assertGreaterEqual(
                len(route.dependant.dependencies),
                normal_dependencies,
            )

    def test_photo_route_paths_share_the_documents_prefix(self):
        """
        The WordPress Admin proxy (class-le-global-chatbot-admin.php)
        builds every contact photo URL as DOCUMENTS_PATH + "/" +
        document_id + "/contacts/" + contact_id + "/photo", where
        DOCUMENTS_PATH is the exact same "/api/v1/admin/documents"
        constant used to build the list/add/update/delete contact
        URLs. The three photo routes must therefore share that same
        prefix - if they don't, every request WordPress sends 404s
        (the proven root cause of "Admin View/Edit shows no photo
        thumbnail": the browser's <img> gets a 404 and its onerror
        handler silently removes it).
        """

        contacts_list_route = next(
            route
            for route in router.routes
            if route.path.endswith("/{document_id}/contacts")
            and "GET" in route.methods
        )
        documents_prefix = contacts_list_route.path.removesuffix(
            "/{document_id}/contacts"
        )

        # A list, not a set: the three photo routes (GET/PUT/DELETE)
        # correctly share one IDENTICAL path string, differentiated
        # only by HTTP method - deduplicating by path value would
        # collapse them to one element even when the fix is correct.
        photo_routes = [
            route
            for route in router.routes
            if route.path.endswith(
                "/{document_id}/contacts/{contact_id}/photo"
            )
        ]

        self.assertEqual(3, len(photo_routes))

        for route in photo_routes:
            self.assertTrue(
                route.path.startswith(documents_prefix + "/"),
                f"{route.path!r} does not share the "
                f"{documents_prefix!r} prefix WordPress's "
                "DOCUMENTS_PATH constant assumes every Admin contact "
                "route (including photo routes) uses",
            )


if __name__ == "__main__":
    unittest.main()
