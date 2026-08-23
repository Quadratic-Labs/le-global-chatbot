"""
Unit tests for scripts/release_compatibility_smoke.py.

These tests never touch the network or a real backend - every HTTP
call the module would make is replaced with a canned response via
unittest.mock, exactly mirroring the response shapes the real
candidate-a3cb8e1/candidate-ed292d7 live runs actually produced (see
docs/RELEASE_COMPATIBILITY.md for that live proof; this file protects
the SMOKE RUNNER'S OWN parsing/validation logic against regressing,
independent of any live backend being reachable).

Run directly:

    python3 scripts/tests/test_release_compatibility_smoke.py
"""

from __future__ import annotations

import io
import json
import sys
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))

import release_compatibility_smoke as smoke  # noqa: E402


def make_zip_bytes(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return buffer.getvalue()


VALID_DOCX_BYTES = make_zip_bytes(
    {"[Content_Types].xml": b"<Types/>", "word/document.xml": b"<doc/>"}
)
DOCX_MISSING_CONTENT_TYPES_BYTES = make_zip_bytes(
    {"word/document.xml": b"<doc/>"}
)

MANIFEST_DOCUMENTS = [
    {
        "document_id": "doc_au",
        "country_code": "AU",
        "source_filename": "AU.docx",
        "expected_min_chunk_count": 60,
        "contacts": [{"contact_id": "contact-au-1", "has_photo": True}],
    },
    {
        "document_id": "doc_cl",
        "country_code": "CL",
        "source_filename": "CL.docx",
        "contacts": [{"contact_id": "contact-cl-1", "has_photo": False}],
    },
]


def response(
    status: int = 200,
    body: bytes = b"",
    headers: dict[str, str] | None = None,
) -> smoke.HttpResponse:
    return smoke.HttpResponse(status=status, body=body, headers=headers or {})


def health_body(status="ok", opensearch="ok", redis="ok") -> bytes:
    return json.dumps(
        {
            "status": status,
            "service": "le-global-backend",
            "dependencies": {"opensearch": opensearch, "redis": redis},
        }
    ).encode()


def document_list_body(chunk_counts: dict[str, int]) -> bytes:
    return json.dumps(
        {
            "total": len(chunk_counts),
            "documents": [
                {
                    "document_id": document_id,
                    "source_filename": f"{document_id}.docx",
                    "chunk_count": chunk_count,
                }
                for document_id, chunk_count in chunk_counts.items()
            ],
        }
    ).encode()


def contact_list_body(document_id: str, contacts: list[dict]) -> bytes:
    return json.dumps(
        {
            "document_id": document_id,
            "country_code": "XX",
            "contacts": contacts,
        }
    ).encode()


class BackendHealthTests(unittest.TestCase):
    def test_success_response_parsing(self):
        with patch.object(
            smoke, "http_get", return_value=response(200, health_body())
        ):
            result = smoke.check_backend_health("http://x", 5, [])

        self.assertTrue(result.passed)

    def test_non_200_failure(self):
        with patch.object(
            smoke, "http_get", return_value=response(503, health_body())
        ):
            result = smoke.check_backend_health("http://x", 5, [])

        self.assertFalse(result.passed)
        self.assertIn("503", result.detail)

    def test_malformed_json(self):
        with patch.object(
            smoke, "http_get", return_value=response(200, b"not json")
        ):
            result = smoke.check_backend_health("http://x", 5, [])

        self.assertFalse(result.passed)

    def test_degraded_dependency_fails(self):
        with patch.object(
            smoke,
            "http_get",
            return_value=response(200, health_body(status="degraded", redis="unavailable")),
        ):
            result = smoke.check_backend_health("http://x", 5, [])

        self.assertFalse(result.passed)

    def test_connection_error_is_caught_not_raised(self):
        import urllib.error

        with patch.object(
            smoke, "http_get", side_effect=urllib.error.URLError("refused")
        ):
            result = smoke.check_backend_health("http://x", 5, [])

        self.assertFalse(result.passed)


class DocumentListTests(unittest.TestCase):
    def test_success_response_parsing(self):
        body = document_list_body({"doc_au": 60, "doc_cl": 49})

        with patch.object(smoke, "http_get", return_value=response(200, body)):
            result = smoke.check_document_list(
                "http://x", {}, 5, MANIFEST_DOCUMENTS, []
            )

        self.assertTrue(result.passed)

    def test_non_200_failure(self):
        with patch.object(
            smoke, "http_get", return_value=response(500, b"boom")
        ):
            result = smoke.check_document_list(
                "http://x", {}, 5, MANIFEST_DOCUMENTS, []
            )

        self.assertFalse(result.passed)

    def test_malformed_json(self):
        with patch.object(
            smoke, "http_get", return_value=response(200, b"{not json")
        ):
            result = smoke.check_document_list(
                "http://x", {}, 5, MANIFEST_DOCUMENTS, []
            )

        self.assertFalse(result.passed)

    def test_no_documents(self):
        body = json.dumps({"total": 0, "documents": []}).encode()

        with patch.object(smoke, "http_get", return_value=response(200, body)):
            result = smoke.check_document_list(
                "http://x", {}, 5, MANIFEST_DOCUMENTS, []
            )

        self.assertFalse(result.passed)

    def test_chunk_count_below_expected_minimum_fails(self):
        """The exact assertion that caught candidate-ed292d7 live: same
        HTTP 200, same schema, one document's chunk_count short of the
        known-good baseline."""

        body = document_list_body({"doc_au": 59, "doc_cl": 49})

        with patch.object(smoke, "http_get", return_value=response(200, body)):
            result = smoke.check_document_list(
                "http://x", {}, 5, MANIFEST_DOCUMENTS, []
            )

        self.assertFalse(result.passed)
        self.assertIn("doc_au", result.detail)
        self.assertIn("59", result.detail)

    def test_missing_manifest_document_fails(self):
        body = document_list_body({"doc_cl": 49})

        with patch.object(smoke, "http_get", return_value=response(200, body)):
            result = smoke.check_document_list(
                "http://x", {}, 5, MANIFEST_DOCUMENTS, []
            )

        self.assertFalse(result.passed)
        self.assertIn("doc_au", result.detail)


class ContactListTests(unittest.TestCase):
    def test_success_response_parsing(self):
        def fake_get(url, headers, timeout):
            if "doc_au" in url:
                return response(
                    200,
                    contact_list_body(
                        "doc_au", [{"contact_id": "contact-au-1", "has_photo": True}]
                    ),
                )
            return response(
                200,
                contact_list_body(
                    "doc_cl", [{"contact_id": "contact-cl-1", "has_photo": False}]
                ),
            )

        with patch.object(smoke, "http_get", side_effect=fake_get):
            result = smoke.check_contact_list(
                "http://x", {}, 5, MANIFEST_DOCUMENTS, []
            )

        self.assertTrue(result.passed)

    def test_contacts_endpoint_failure(self):
        with patch.object(
            smoke, "http_get", return_value=response(502, b"bad gateway")
        ):
            result = smoke.check_contact_list(
                "http://x", {}, 5, MANIFEST_DOCUMENTS, []
            )

        self.assertFalse(result.passed)

    def test_has_photo_mismatch_fails(self):
        def fake_get(url, headers, timeout):
            if "doc_au" in url:
                return response(
                    200,
                    contact_list_body(
                        "doc_au",
                        [{"contact_id": "contact-au-1", "has_photo": False}],
                    ),
                )
            return response(
                200,
                contact_list_body(
                    "doc_cl", [{"contact_id": "contact-cl-1", "has_photo": False}]
                ),
            )

        with patch.object(smoke, "http_get", side_effect=fake_get):
            result = smoke.check_contact_list(
                "http://x", {}, 5, MANIFEST_DOCUMENTS, []
            )

        self.assertFalse(result.passed)


class ContactPhotoTests(unittest.TestCase):
    def test_success_response_parsing(self):
        with patch.object(
            smoke,
            "http_get",
            return_value=response(
                200, b"\xff\xd8\xff", {"content-type": "image/jpeg"}
            ),
        ):
            result = smoke.check_contact_photo(
                "http://x", {}, 5, MANIFEST_DOCUMENTS, []
            )

        self.assertTrue(result.passed)

    def test_empty_body_fails(self):
        with patch.object(
            smoke,
            "http_get",
            return_value=response(200, b"", {"content-type": "image/jpeg"}),
        ):
            result = smoke.check_contact_photo(
                "http://x", {}, 5, MANIFEST_DOCUMENTS, []
            )

        self.assertFalse(result.passed)

    def test_invalid_content_type_fails(self):
        with patch.object(
            smoke,
            "http_get",
            return_value=response(
                200, b"<html>not a photo</html>", {"content-type": "text/html"}
            ),
        ):
            result = smoke.check_contact_photo(
                "http://x", {}, 5, MANIFEST_DOCUMENTS, []
            )

        self.assertFalse(result.passed)

    def test_no_photo_fixture_in_manifest_fails_loudly(self):
        no_photo_manifest = [
            {
                "document_id": "doc_cl",
                "country_code": "CL",
                "source_filename": "CL.docx",
                "contacts": [{"contact_id": "contact-cl-1", "has_photo": False}],
            }
        ]

        result = smoke.check_contact_photo(
            "http://x", {}, 5, no_photo_manifest, []
        )

        self.assertFalse(result.passed)


class DocumentDownloadTests(unittest.TestCase):
    def test_success_response_parsing(self):
        with patch.object(
            smoke,
            "http_get",
            return_value=response(
                200, VALID_DOCX_BYTES, {"content-type": smoke.DOCX_MEDIA_TYPE}
            ),
        ):
            result = smoke.check_document_download(
                "http://x", {}, 5, MANIFEST_DOCUMENTS, []
            )

        self.assertTrue(result.passed)

    def test_empty_body_fails(self):
        with patch.object(
            smoke,
            "http_get",
            return_value=response(200, b"", {"content-type": smoke.DOCX_MEDIA_TYPE}),
        ):
            result = smoke.check_document_download(
                "http://x", {}, 5, MANIFEST_DOCUMENTS, []
            )

        self.assertFalse(result.passed)

    def test_non_zip_bytes_fails(self):
        with patch.object(
            smoke,
            "http_get",
            return_value=response(
                200, b"not a zip file at all", {"content-type": smoke.DOCX_MEDIA_TYPE}
            ),
        ):
            result = smoke.check_document_download(
                "http://x", {}, 5, MANIFEST_DOCUMENTS, []
            )

        self.assertFalse(result.passed)

    def test_zip_missing_content_types_xml_fails(self):
        with patch.object(
            smoke,
            "http_get",
            return_value=response(
                200,
                DOCX_MISSING_CONTENT_TYPES_BYTES,
                {"content-type": smoke.DOCX_MEDIA_TYPE},
            ),
        ):
            result = smoke.check_document_download(
                "http://x", {}, 5, MANIFEST_DOCUMENTS, []
            )

        self.assertFalse(result.passed)

    def test_unexpected_content_type_fails(self):
        with patch.object(
            smoke,
            "http_get",
            return_value=response(
                200, VALID_DOCX_BYTES, {"content-type": "application/zip"}
            ),
        ):
            result = smoke.check_document_download(
                "http://x", {}, 5, MANIFEST_DOCUMENTS, []
            )

        self.assertFalse(result.passed)


class RedactionTests(unittest.TestCase):
    def test_secrets_never_appear_in_diagnostic_output(self):
        secret_admin_key = "super-secret-admin-key-xyz"
        secret_api_key = "super-secret-api-key-abc"

        message = (
            f"request failed talking to backend with X-Admin-Key: "
            f"{secret_admin_key} and X-API-Key: {secret_api_key}"
        )

        redacted = smoke.redact(message, [secret_admin_key, secret_api_key])

        self.assertNotIn(secret_admin_key, redacted)
        self.assertNotIn(secret_api_key, redacted)

    def test_health_check_redacts_secrets_in_error_body(self):
        secret_admin_key = "super-secret-admin-key-xyz"

        with patch.object(
            smoke,
            "http_get",
            return_value=response(
                500, f"error, key was {secret_admin_key}".encode()
            ),
        ):
            result = smoke.check_backend_health(
                "http://x", 5, [secret_admin_key]
            )

        self.assertNotIn(secret_admin_key, result.detail)


class OverallResultTests(unittest.TestCase):
    def test_any_individual_fail_produces_non_zero_result(self):
        results = [
            smoke.GateResult("BACKEND_HEALTH", True, "ok"),
            smoke.GateResult("DOCUMENT_LIST", False, "chunk_count too low"),
            smoke.GateResult("CONTACT_LIST", True, "ok"),
            smoke.GateResult("CONTACT_PHOTO", True, "ok"),
            smoke.GateResult("DOCUMENT_DOWNLOAD", True, "ok"),
        ]

        self.assertFalse(all(result.passed for result in results))

    def test_all_pass_produces_release_compatibility_pass(self):
        results = [
            smoke.GateResult(name, True, "ok") for name in smoke.GATE_NAMES
        ]

        self.assertTrue(all(result.passed for result in results))

    def test_main_exit_code_matches_overall_result(self):
        import tempfile

        manifest_payload = {"documents": MANIFEST_DOCUMENTS}

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as manifest_file:
            json.dump(manifest_payload, manifest_file)
            manifest_path = manifest_file.name

        def all_pass_get(url, headers, timeout):
            if url.endswith("/health"):
                return response(200, health_body())
            if url.endswith("/api/v1/admin/documents"):
                return response(
                    200, document_list_body({"doc_au": 60, "doc_cl": 49})
                )
            if url.endswith("/contacts"):
                document_id = "doc_au" if "doc_au" in url else "doc_cl"
                contacts = (
                    [{"contact_id": "contact-au-1", "has_photo": True}]
                    if document_id == "doc_au"
                    else [{"contact_id": "contact-cl-1", "has_photo": False}]
                )
                return response(200, contact_list_body(document_id, contacts))
            if url.endswith("/photo"):
                return response(200, b"\xff\xd8\xff", {"content-type": "image/jpeg"})
            if url.endswith("/download"):
                return response(
                    200, VALID_DOCX_BYTES, {"content-type": smoke.DOCX_MEDIA_TYPE}
                )
            raise AssertionError(f"unexpected URL in test: {url}")

        try:
            with patch.object(smoke, "http_get", side_effect=all_pass_get):
                exit_code = smoke.main(
                    [
                        "--base-url",
                        "http://x",
                        "--manifest",
                        manifest_path,
                        "--api-key",
                        "test-key-fixture",
                        "--admin-key",
                        "test-key-fixture",
                    ]
                )
        finally:
            Path(manifest_path).unlink()

        self.assertEqual(exit_code, 0)

    def test_main_exit_code_non_zero_when_a_gate_fails(self):
        import tempfile

        manifest_payload = {"documents": MANIFEST_DOCUMENTS}

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as manifest_file:
            json.dump(manifest_payload, manifest_file)
            manifest_path = manifest_file.name

        try:
            with patch.object(
                smoke, "http_get", return_value=response(503, b"down")
            ):
                exit_code = smoke.main(
                    [
                        "--base-url",
                        "http://x",
                        "--manifest",
                        manifest_path,
                        "--api-key",
                        "test-key-fixture",
                        "--admin-key",
                        "test-key-fixture",
                    ]
                )
        finally:
            Path(manifest_path).unlink()

        self.assertEqual(exit_code, 1)


if __name__ == "__main__":
    unittest.main()
