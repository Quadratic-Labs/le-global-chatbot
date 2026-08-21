
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.routers.admin_contacts import router
from app.services.admin_contact_photos import (
    AdminContactPhotoError,
    AdminContactPhotoNotFoundError,
    read_admin_contact_photo,
    remove_admin_contact_photo,
    replace_admin_contact_photo,
)
from app.services.contact_state import (
    ContactRecord,
    ContactState,
    write_contact_state_atomic,
)


class AdminContactPhotoTests(unittest.TestCase):

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

        write_contact_state_atomic(
            self.root,
            ContactState(
                document_id="doc-test",
                country_code="BE",
                contacts=(
                    ContactRecord(
                        contact_id="contact-test",
                        member_firm="Firm",
                        contact_person="Person",
                        email="p@example.com",
                        phone="+1",
                        address="Address",
                        website="https://example.com",
                    ),
                ),
            ),
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_replace_read_remove(self):
        data = b"\xff\xd8\xff\xe0valid-test-image"

        photo = replace_admin_contact_photo(
            self.root,
            "doc-test",
            "contact-test",
            data=data,
            content_type="image/jpeg",
        )

        self.assertEqual("image/jpeg", photo.content_type)

        loaded = read_admin_contact_photo(
            self.root,
            "doc-test",
            "contact-test",
        )
        self.assertEqual(data, loaded.data)

        self.assertTrue(
            remove_admin_contact_photo(
                self.root,
                "doc-test",
                "contact-test",
            )
        )

        with self.assertRaises(AdminContactPhotoNotFoundError):
            read_admin_contact_photo(
                self.root,
                "doc-test",
                "contact-test",
            )

    def test_fake_image_is_rejected(self):
        with self.assertRaises(AdminContactPhotoError):
            replace_admin_contact_photo(
                self.root,
                "doc-test",
                "contact-test",
                data=b"not-an-image",
                content_type="image/jpeg",
            )

    def test_mime_mismatch_is_rejected(self):
        with self.assertRaises(AdminContactPhotoError):
            replace_admin_contact_photo(
                self.root,
                "doc-test",
                "contact-test",
                data=b"\x89PNG\r\n\x1a\nfake",
                content_type="image/jpeg",
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
