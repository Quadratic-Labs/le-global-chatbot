"""
Release/rollback compatibility smoke gate - the STATIC half.

Incident this guards against: a backend rollback (or forward-deploy)
can report GET /health == 200 while still being functionally
incompatible with the currently-deployed WordPress Admin, because
/health never touches ContactState, persisted DOCX files, or the
Contact API's own request/route shape at all. Concretely, backend
commit candidate-ed292d7 lacks docx_parser.py's canonical-table read
tier and contact_document_area.py's rebuild mechanism entirely (see
git history) - so any document whose PERSISTED SOURCE DOCX had
already been migrated to the canonical table format by a later
backend generation cannot have its contacts mutated under ed292d7
without failing structurally, even though ed292d7 itself is healthy
in isolation and its own list_contacts()/route registrations are
byte-identical to the current backend's.

This file proves, from BOTH sides' own real source - never a live
network call, never a server start, never a reindex/reseed, never any
OpenSearch/ContactState/DOCX mutation - that the CURRENT WordPress
Admin's request construction for the four routes this mission's smoke
gate covers (list documents, list contacts, contact photo, document
download) matches the CURRENT backend's own route registration
exactly. This is the same "verify both sides of a contract from
source, in one monorepo, without a live call" convention this test
suite already established: see test-contact-photo-crud-contract.py's
own test_photo_urls_use_the_same_documents_path_prefix_as_contacts,
paired with backend/tests/test_admin_contact_photos.py's
test_photo_route_paths_share_the_documents_prefix.

What this file does NOT prove (documented, not silently assumed):
request/response BODY schema drift that a route rename would not
cause, or any BEHAVIORAL difference in business logic once a request
reaches the route. A rollback candidate should also pass a live,
READ-ONLY smoke run (list documents, list contacts, contact photo,
document download, backend/OpenSearch/Redis health) against a real
running instance before being trusted - see this file's own
docstring reference in the release runbook. This file is the part of
that gate that can run in CI on every commit, with no live backend
required at all.
"""

import re
import unittest
from pathlib import Path

WP_ROOT = Path("wordpress/le-global-chatbot")
PHP = (WP_ROOT / "includes/class-le-global-chatbot-admin.php").read_text()

BACKEND_ROOT = Path("backend")
ADMIN_DOCUMENTS_ROUTER = (
    BACKEND_ROOT / "app/routers/admin_documents.py"
).read_text()
ADMIN_CONTACTS_ROUTER = (
    BACKEND_ROOT / "app/routers/admin_contacts.py"
).read_text()
ADMIN_LIFECYCLE_ROUTER = (
    BACKEND_ROOT / "app/routers/admin_document_lifecycle.py"
).read_text()


def _php_constant(name: str) -> str:
    match = re.search(
        r"private const " + re.escape(name) + r"\s*=\s*\(?\s*'([^']+)'",
        PHP,
    )
    assert match is not None, f"{name} constant not found in PHP source"
    return match.group(1)


def _router_prefix(router_source: str) -> str:
    match = re.search(r'prefix\s*=\s*"([^"]+)"', router_source)
    assert match is not None, "router prefix not found"
    return match.group(1)


class ReleaseCompatibilitySmokeGateContract(unittest.TestCase):
    def setUp(self) -> None:
        self.documents_path = _php_constant("DOCUMENTS_PATH")

    def test_documents_path_constant_matches_the_backend_router_prefix(
        self,
    ) -> None:
        """WordPress builds every one of these four urls from ONE
        constant, DOCUMENTS_PATH - proving it matches the backend's
        own prefix + "/documents" once locks in all four at once."""

        self.assertEqual(self.documents_path, "/api/v1/admin/documents")
        self.assertEqual(
            _router_prefix(ADMIN_DOCUMENTS_ROUTER), "/api/v1/admin"
        )
        self.assertRegex(
            ADMIN_DOCUMENTS_ROUTER,
            r'@router\.get\(\s*"/documents"',
        )

    # --- 1. List documents -------------------------------------------

    def test_list_documents_is_a_get_to_documents_path(self) -> None:
        self.assertRegex(
            PHP,
            r"request_backend\(\s*'GET',\s*self::DOCUMENTS_PATH\s*,",
        )
        self.assertRegex(
            ADMIN_DOCUMENTS_ROUTER,
            r'@router\.get\(\s*"/documents"',
        )

    # --- 2. List contacts ----------------------------------------------

    def test_list_contacts_is_a_get_to_documents_path_plus_contacts(
        self,
    ) -> None:
        self.assertRegex(
            PHP,
            r"self::DOCUMENTS_PATH\s*\.\s*'/'\s*\.\s*rawurlencode\(\$document_id\)\s*\.\s*'/contacts'",
        )
        self.assertRegex(
            _router_prefix(ADMIN_CONTACTS_ROUTER)
            + ADMIN_CONTACTS_ROUTER,
            r"/api/v1/admin",
        )
        self.assertIn(
            _router_prefix(ADMIN_CONTACTS_ROUTER), self.documents_path
        )
        self.assertRegex(
            ADMIN_CONTACTS_ROUTER,
            r'@router\.get\(\s*"/documents/\{document_id\}/contacts"',
        )

    # --- 3. Contact photo ------------------------------------------

    def test_contact_photo_is_a_get_to_documents_path_plus_contacts_photo(
        self,
    ) -> None:
        self.assertRegex(
            PHP,
            (
                r"self::DOCUMENTS_PATH\s*\.\s*'/'\s*\.\s*"
                r"rawurlencode\(\$document_id\)\s*\.\s*'/contacts/'\s*\.\s*"
                r"rawurlencode\(\$contact_id\)\s*\.\s*'/photo'"
            ),
        )
        self.assertRegex(
            ADMIN_CONTACTS_ROUTER,
            (
                r'@router\.get\(\s*'
                r'"/documents/\{document_id\}/contacts/\{contact_id\}/photo"'
            ),
        )

    # --- 4. Document download ---------------------------------------

    def test_download_is_a_get_to_documents_path_plus_download(
        self,
    ) -> None:
        self.assertRegex(
            PHP,
            (
                r"self::DOCUMENTS_PATH\s*\n?\s*\.\s*'/'\s*\n?\s*\.\s*"
                r"rawurlencode\(\$document_id\)\s*\n?\s*\.\s*'/download'"
            ),
        )
        self.assertEqual(
            _router_prefix(ADMIN_LIFECYCLE_ROUTER), "/api/v1/admin"
        )
        self.assertRegex(
            ADMIN_LIFECYCLE_ROUTER,
            r'"/documents/\{document_id\}/download"',
        )

    # --- Fifth check: the fixed generic failure messages this
    # mission's incident narrative quoted verbatim actually come from
    # THESE exact call sites - locks in that "The contacts could not
    # be loaded." / "The document identifier is invalid." are real,
    # current strings, not stale references in this test's own
    # comments.

    def test_incident_narrative_messages_are_still_the_real_current_strings(
        self,
    ) -> None:
        self.assertIn("The contacts could not be loaded.", PHP)
        self.assertIn("The document identifier is invalid.", PHP)


if __name__ == "__main__":
    unittest.main()
