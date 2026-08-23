"""
Live, read-only backend release/rollback compatibility smoke gate.

This is LAYER 2 of the two-layer release-compatibility contract
documented in docs/RELEASE_COMPATIBILITY.md:

  LAYER 1 (static, no live backend needed):
      wordpress/le-global-chatbot/tests/release-compatibility-contract.py
      Proves WordPress's request construction matches the backend's
      route registrations, from source only.

  LAYER 2 (this file, needs a running candidate backend):
      Proves a specific, running candidate backend can actually SERVE
      the four Admin HTTP boundaries WordPress depends on, against a
      snapshot of CURRENT, real, already-migrated persisted data - not
      just that its routes exist.

Why layer 1 alone is not enough (the incident this file exists for):
candidate-ed292d7 has identical routes, identical request/response
schemas, and identical list_contacts()/get_document_download() service
code to the current backend - a static contract test finds nothing
wrong with it. Its actual incompatibility only shows up as a SILENT
data-completeness gap: its older docx_parser.py cannot recognize the
new canonical Admin-managed contact table
(CONTACT_TABLE_HIDDEN_MARKER, "LE-GLOBAL-CONTACT-TABLE-V1") that a
document's PERSISTED source DOCX already contains once a later
backend generation has rebuilt it - so it fails to build that
document's contact search chunk during indexing, one chunk short of
what a compatible backend indexes, with no HTTP error anywhere. This
is exactly why the DOCUMENT_LIST check below asserts a MINIMUM
chunk_count for the one fixture document flagged
"canonical_table_format" in the manifest, not just HTTP 200: a
same-shaped, same-schema, 200-OK response can still be silently wrong.

This script:

  - Makes ONLY read-only (GET) HTTP requests - it never uploads,
    reindexes, deletes, or mutates a contact or a photo.
  - Never imports anything from app.* - it exercises exactly the HTTP
    boundary WordPress itself calls (see request_backend() in
    class-le-global-chatbot-admin.php: GET, header
    X-API-Key/X-Admin-Key, JSON body).
  - Never prints the API key or admin key it was given, even on
    failure (see _redacted()).

It does NOT start, configure, or tear down the candidate backend, nor
the OpenSearch/Redis it needs, nor prepare the snapshot of
representative persisted data it reads - see
scripts/prepare_release_compatibility_snapshot.py and
scripts/run_release_compatibility_smoke.sh for that, and
docs/RELEASE_COMPATIBILITY.md for the full, reproducible recipe this
repository actually ran to prove this gate catches candidate-ed292d7.

Usage:

    python3 scripts/release_compatibility_smoke.py \\
        --base-url http://127.0.0.1:18001 \\
        --manifest scripts/fixtures/release_compatibility_manifest.json

API_ACCESS_KEY and ADMIN_API_KEY are read from the environment by
default (never required on the command line, so they never appear in
a process listing) - pass --api-key/--admin-key only if you must.

Exit code 0 iff RELEASE_COMPATIBILITY=PASS.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any


DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_MANIFEST_PATH = (
    Path(__file__).parent / "fixtures" / "release_compatibility_manifest.json"
)
DOCX_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument"
    ".wordprocessingml.document"
)
CONTENT_TYPES_ENTRY = "[Content_Types].xml"

GATE_NAMES = (
    "BACKEND_HEALTH",
    "DOCUMENT_LIST",
    "CONTACT_LIST",
    "CONTACT_PHOTO",
    "DOCUMENT_DOWNLOAD",
)


@dataclass(frozen=True)
class GateResult:
    name: str
    passed: bool
    detail: str


class HttpResponse:
    __slots__ = ("status", "body", "headers")

    def __init__(self, status: int, body: bytes, headers: dict[str, str]):
        self.status = status
        self.body = body
        self.headers = headers


def redact(message: str, secrets: list[str]) -> str:
    """Never let a credential reach stdout/stderr, even inside an
    error message echoed back by a misbehaving server."""

    for secret in secrets:
        if secret:
            message = message.replace(secret, "***REDACTED***")
    return message


def http_get(
    url: str,
    headers: dict[str, str],
    timeout: float,
) -> HttpResponse:
    """A plain, unauthenticated-by-default GET - callers add whatever
    headers they need. Raises only for genuine connection-level
    failures (DNS, refused connection, timeout); an HTTP error status
    is returned as a normal HttpResponse, never raised, so callers can
    assert on the exact status a real client would see."""

    request = urllib.request.Request(url, headers=headers, method="GET")

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return HttpResponse(
                status=response.status,
                body=response.read(),
                headers={
                    key.lower(): value
                    for key, value in response.headers.items()
                },
            )
    except urllib.error.HTTPError as error:
        return HttpResponse(
            status=error.code,
            body=error.read(),
            headers={
                key.lower(): value
                for key, value in (error.headers or {}).items()
            },
        )


def parse_json_object(body: bytes) -> dict[str, Any] | None:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None

    return payload if isinstance(payload, dict) else None


def admin_headers(api_key: str | None, admin_key: str | None) -> dict[str, str]:
    """The same two headers request_backend() sends on every real
    WordPress-originated Admin request (class-le-global-chatbot-admin.php)."""

    headers = {"Accept": "application/json"}

    if api_key:
        headers["X-API-Key"] = api_key

    if admin_key:
        headers["X-Admin-Key"] = admin_key

    return headers


def check_backend_health(
    base_url: str,
    timeout: float,
    secrets: list[str],
) -> GateResult:
    name = "BACKEND_HEALTH"

    try:
        response = http_get(
            f"{base_url}/health",
            {"Accept": "application/json"},
            timeout,
        )
    except (urllib.error.URLError, OSError) as error:
        return GateResult(name, False, redact(f"request failed: {error}", secrets))

    if response.status != 200:
        return GateResult(
            name,
            False,
            f"expected HTTP 200, got {response.status}: "
            f"{redact(response.body.decode('utf-8', 'replace')[:300], secrets)}",
        )

    payload = parse_json_object(response.body)

    if payload is None:
        return GateResult(name, False, "response body was not a JSON object")

    if payload.get("status") != "ok":
        return GateResult(
            name,
            False,
            f"service status was not ok: {payload.get('status')!r}",
        )

    dependencies = payload.get("dependencies")

    if not isinstance(dependencies, dict) or not dependencies:
        return GateResult(name, False, "no dependency status reported")

    unacceptable = {
        dependency: status
        for dependency, status in dependencies.items()
        if status != "ok"
    }

    if unacceptable:
        return GateResult(
            name,
            False,
            f"dependency status not acceptable: {unacceptable!r}",
        )

    return GateResult(
        name,
        True,
        f"status=ok, dependencies={dependencies!r}",
    )


def check_document_list(
    base_url: str,
    headers: dict[str, str],
    timeout: float,
    manifest_documents: list[dict[str, Any]],
    secrets: list[str],
) -> GateResult:
    name = "DOCUMENT_LIST"
    url = f"{base_url}/api/v1/admin/documents"

    try:
        response = http_get(url, headers, timeout)
    except (urllib.error.URLError, OSError) as error:
        return GateResult(name, False, redact(f"request failed: {error}", secrets))

    if response.status != 200:
        return GateResult(
            name,
            False,
            f"expected HTTP 200, got {response.status}: "
            f"{redact(response.body.decode('utf-8', 'replace')[:300], secrets)}",
        )

    payload = parse_json_object(response.body)

    if payload is None:
        return GateResult(name, False, "response body was not a JSON object")

    documents = payload.get("documents")

    if not isinstance(documents, list) or not documents:
        return GateResult(name, False, "no documents reported in the catalog")

    by_id = {
        entry.get("document_id"): entry
        for entry in documents
        if isinstance(entry, dict)
    }

    problems: list[str] = []

    for manifest_document in manifest_documents:
        document_id = manifest_document["document_id"]
        entry = by_id.get(document_id)

        if entry is None:
            problems.append(
                f"{document_id} ({manifest_document['country_code']}) "
                "missing from the catalog"
            )
            continue

        expected_min_chunks = manifest_document.get("expected_min_chunk_count")

        if expected_min_chunks is not None:
            actual_chunks = entry.get("chunk_count")

            if (
                not isinstance(actual_chunks, int)
                or actual_chunks < expected_min_chunks
            ):
                problems.append(
                    f"{document_id} ({manifest_document['country_code']}): "
                    f"chunk_count={actual_chunks!r}, expected >= "
                    f"{expected_min_chunks} - this backend's docx parser "
                    "likely cannot read this document's canonical contact "
                    "table (see docs/RELEASE_COMPATIBILITY.md)"
                )

    if problems:
        return GateResult(name, False, "; ".join(problems))

    return GateResult(
        name,
        True,
        f"{len(manifest_documents)}/{len(manifest_documents)} "
        "manifest documents present with acceptable chunk counts",
    )


def check_contact_list(
    base_url: str,
    headers: dict[str, str],
    timeout: float,
    manifest_documents: list[dict[str, Any]],
    secrets: list[str],
) -> GateResult:
    name = "CONTACT_LIST"
    problems: list[str] = []
    checked = 0

    for manifest_document in manifest_documents:
        document_id = manifest_document["document_id"]
        url = (
            f"{base_url}/api/v1/admin/documents/"
            f"{urllib.parse.quote(document_id, safe='')}/contacts"
        )

        try:
            response = http_get(url, headers, timeout)
        except (urllib.error.URLError, OSError) as error:
            problems.append(f"{document_id}: request failed: "
                             f"{redact(str(error), secrets)}")
            continue

        if response.status != 200:
            problems.append(
                f"{document_id}: expected HTTP 200, got {response.status}: "
                f"{redact(response.body.decode('utf-8', 'replace')[:300], secrets)}"
            )
            continue

        payload = parse_json_object(response.body)

        if payload is None:
            problems.append(f"{document_id}: response body was not a JSON object")
            continue

        contacts = payload.get("contacts")

        if not isinstance(contacts, list):
            problems.append(f"{document_id}: 'contacts' was not a list")
            continue

        by_contact_id = {
            contact.get("contact_id"): contact
            for contact in contacts
            if isinstance(contact, dict)
        }

        expected_contacts = manifest_document.get("contacts", [])

        if len(contacts) != len(expected_contacts):
            problems.append(
                f"{document_id}: expected {len(expected_contacts)} "
                f"contact(s), got {len(contacts)}"
            )

        for expected_contact in expected_contacts:
            contact_id = expected_contact["contact_id"]
            actual = by_contact_id.get(contact_id)

            if actual is None:
                problems.append(
                    f"{document_id}: contact {contact_id} missing "
                    "from the response"
                )
                continue

            expected_has_photo = expected_contact.get("has_photo")

            if (
                expected_has_photo is not None
                and actual.get("has_photo") != expected_has_photo
            ):
                problems.append(
                    f"{document_id}: contact {contact_id} has_photo="
                    f"{actual.get('has_photo')!r}, expected "
                    f"{expected_has_photo!r}"
                )

        checked += 1

    if problems:
        return GateResult(name, False, "; ".join(problems))

    return GateResult(
        name,
        True,
        f"{checked}/{len(manifest_documents)} manifest documents parsed "
        "successfully with the expected contacts",
    )


def check_contact_photo(
    base_url: str,
    headers: dict[str, str],
    timeout: float,
    manifest_documents: list[dict[str, Any]],
    secrets: list[str],
) -> GateResult:
    name = "CONTACT_PHOTO"

    targets = [
        (manifest_document["document_id"], contact["contact_id"])
        for manifest_document in manifest_documents
        for contact in manifest_document.get("contacts", [])
        if contact.get("has_photo")
    ]

    if not targets:
        return GateResult(
            name,
            False,
            "the manifest contains no contact flagged has_photo=true "
            "- this gate cannot exercise the photo endpoint at all",
        )

    problems: list[str] = []

    for document_id, contact_id in targets:
        url = (
            f"{base_url}/api/v1/admin/documents/"
            f"{urllib.parse.quote(document_id, safe='')}/contacts/"
            f"{urllib.parse.quote(contact_id, safe='')}/photo"
        )

        try:
            response = http_get(url, headers, timeout)
        except (urllib.error.URLError, OSError) as error:
            problems.append(
                f"{document_id}/{contact_id}: request failed: "
                f"{redact(str(error), secrets)}"
            )
            continue

        if response.status != 200:
            problems.append(
                f"{document_id}/{contact_id}: expected HTTP 200, got "
                f"{response.status}"
            )
            continue

        if not response.body:
            problems.append(f"{document_id}/{contact_id}: empty response body")
            continue

        content_type = response.headers.get("content-type", "")

        if not content_type.startswith("image/"):
            problems.append(
                f"{document_id}/{contact_id}: unexpected content type "
                f"{content_type!r}"
            )
            continue

    if problems:
        return GateResult(name, False, "; ".join(problems))

    return GateResult(
        name,
        True,
        f"{len(targets)}/{len(targets)} manifest photos served "
        "as a non-empty image",
    )


def check_document_download(
    base_url: str,
    headers: dict[str, str],
    timeout: float,
    manifest_documents: list[dict[str, Any]],
    secrets: list[str],
) -> GateResult:
    name = "DOCUMENT_DOWNLOAD"
    problems: list[str] = []

    for manifest_document in manifest_documents:
        document_id = manifest_document["document_id"]
        url = (
            f"{base_url}/api/v1/admin/documents/"
            f"{urllib.parse.quote(document_id, safe='')}/download"
        )

        try:
            response = http_get(url, headers, timeout)
        except (urllib.error.URLError, OSError) as error:
            problems.append(f"{document_id}: request failed: "
                             f"{redact(str(error), secrets)}")
            continue

        if response.status != 200:
            problems.append(
                f"{document_id}: expected HTTP 200, got {response.status}"
            )
            continue

        if not response.body:
            problems.append(f"{document_id}: empty response body")
            continue

        content_type = response.headers.get("content-type", "")

        if content_type.split(";")[0].strip() != DOCX_MEDIA_TYPE:
            problems.append(
                f"{document_id}: unexpected content type {content_type!r}"
            )
            continue

        try:
            with zipfile.ZipFile(BytesIO(response.body)) as archive:
                names = archive.namelist()
        except zipfile.BadZipFile:
            problems.append(
                f"{document_id}: response body is not a valid ZIP/DOCX "
                "container"
            )
            continue

        if CONTENT_TYPES_ENTRY not in names:
            problems.append(
                f"{document_id}: {CONTENT_TYPES_ENTRY} missing from the "
                "downloaded container"
            )
            continue

    if problems:
        return GateResult(name, False, "; ".join(problems))

    return GateResult(
        name,
        True,
        f"{len(manifest_documents)}/{len(manifest_documents)} manifest "
        "documents downloaded as valid, non-empty DOCX containers",
    )


def load_manifest(manifest_path: Path) -> list[dict[str, Any]]:
    payload = json.loads(manifest_path.read_text())
    documents = payload.get("documents")

    if not isinstance(documents, list) or not documents:
        raise ValueError(f"manifest {manifest_path} has no documents")

    return documents


def run_gates(
    base_url: str,
    api_key: str | None,
    admin_key: str | None,
    manifest_documents: list[dict[str, Any]],
    timeout: float,
) -> list[GateResult]:
    secrets = [key for key in (api_key, admin_key) if key]
    headers = admin_headers(api_key, admin_key)
    base_url = base_url.rstrip("/")

    return [
        check_backend_health(base_url, timeout, secrets),
        check_document_list(base_url, headers, timeout, manifest_documents, secrets),
        check_contact_list(base_url, headers, timeout, manifest_documents, secrets),
        check_contact_photo(base_url, headers, timeout, manifest_documents, secrets),
        check_document_download(base_url, headers, timeout, manifest_documents, secrets),
    ]


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)

    parser.add_argument(
        "--base-url",
        required=True,
        help="Base URL of the running candidate backend, e.g. "
        "http://127.0.0.1:18001 (never the production port/hostname).",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=DEFAULT_MANIFEST_PATH,
        help=f"Path to the fixture manifest (default: {DEFAULT_MANIFEST_PATH}).",
    )
    parser.add_argument(
        "--api-key",
        default=None,
        help="X-API-Key value. Defaults to $API_ACCESS_KEY. Prefer the "
        "environment variable so the key never appears in a process "
        "listing.",
    )
    parser.add_argument(
        "--admin-key",
        default=None,
        help="X-Admin-Key value. Defaults to $ADMIN_API_KEY. Prefer the "
        "environment variable so the key never appears in a process "
        "listing.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=f"Per-request timeout in seconds (default: {DEFAULT_TIMEOUT_SECONDS}).",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_argument_parser()
    arguments = parser.parse_args(argv)

    api_key = arguments.api_key or os.environ.get("API_ACCESS_KEY")
    admin_key = arguments.admin_key or os.environ.get("ADMIN_API_KEY")

    try:
        manifest_documents = load_manifest(arguments.manifest)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"[ERROR] could not load manifest: {error}", file=sys.stderr)
        return 2

    results = run_gates(
        base_url=arguments.base_url,
        api_key=api_key,
        admin_key=admin_key,
        manifest_documents=manifest_documents,
        timeout=arguments.timeout,
    )

    secrets = [key for key in (api_key, admin_key) if key]

    for result in results:
        status = "PASS" if result.passed else "FAIL"
        print(f"{result.name}={status}")
        print(f"  {redact(result.detail, secrets)}")

    overall_pass = all(result.passed for result in results)
    print(f"RELEASE_COMPATIBILITY={'PASS' if overall_pass else 'FAIL'}")

    return 0 if overall_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())
