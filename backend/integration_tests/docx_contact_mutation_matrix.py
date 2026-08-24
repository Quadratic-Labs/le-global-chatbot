"""
Real-HTTP, isolated differential matrix for the canonical contact-table
DOCX mutation path (add/update/delete/photo).

Not run by `python -m unittest discover` (see this directory's own
README - the ordinary unit test suite must keep passing with no Docker
at all). This is the automated version of the manual recipe documented
there, extended with the mutation matrix itself.

Why this exists: an incident report (2026-08-24) claimed a real Admin
Contact Add/Update produced a downloaded DOCX Microsoft Word refused to
open. Investigation identified the real document (AU,
doc_d600fa6a...4157, proven from the live WordPress/backend access
logs and source/ContactState mtimes - never assumed), reproduced the
EXACT real operation sequence (add contact with photo -> download ->
delete contact -> download) against the real currently-deployed image
via the real HTTP Admin API, and validated every output with:

  - ZIP CRC integrity
  - well-formed XML on every part
  - every relationship target resolves, no duplicate relationship ids
  - no duplicate wp:docPr (drawing) ids
  - sane w:sectPr placement (single, last body child)
  - python-docx can reopen the result
  - LibreOffice headless can convert the result (run separately - see
    README; not scripted here since it requires libreoffice installed
    on the runner, not inside the backend image)

Across an 11-scenario matrix (A0-A9, including an 8-cycle repeated
add/photo/delete stress run and two large/PNG/JPEG photo variants, not
all kept here for brevity - see the investigation's own report), every
output validated clean. THIS FILE KEEPS THE MATRIX ITSELF as permanent
regression coverage - documenting currently-verified-correct behavior,
not a fix for a confirmed defect: none was found, despite reproducing
the real document and real operation sequence exactly.

Prerequisites: same as this directory's README (an isolated network +
OpenSearch + Redis + candidate backend), PLUS a read-write snapshot
directory containing one representative document + its ContactState +
its original photo, prepared exactly like
scripts/prepare_release_compatibility_snapshot.py does (but read-write,
owned/chmod'd so this script's resets can overwrite it - see
--snapshot-source-dir below). Never point this at a live/production
source directory: it MUTATES whatever --document-id resolves to,
repeatedly, by design.

Usage:

    python3 backend/integration_tests/docx_contact_mutation_matrix.py \\
        --base-url http://127.0.0.1:18101 \\
        --api-key repro-api-key --admin-key repro-admin-key \\
        --document-id doc_... --original-contact-id <hex> \\
        --snapshot-source-dir /var/tmp/some-isolated-dir/documents/source \\
        --original-docx-backup /path/to/a/clean/copy/of/the.docx \\
        --original-state-backup /path/to/a/clean/copy/of/the/ContactState.json \\
        --original-photo-name <contact_id>--<sha256>.jpg
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from lxml import etree


def validate_docx(path: Path) -> list[str]:
    problems: list[str] = []

    try:
        archive = zipfile.ZipFile(path)
    except zipfile.BadZipFile as error:
        return [f"BAD_ZIP: {error}"]

    bad = archive.testzip()
    if bad is not None:
        problems.append(f"CRC_FAILURE: {bad}")

    names = set(archive.namelist())

    if "[Content_Types].xml" not in names:
        problems.append("MISSING_CONTENT_TYPES")

    xml_trees = {}
    for name in names:
        if name.endswith(".xml") or name.endswith(".rels"):
            try:
                xml_trees[name] = etree.fromstring(archive.read(name))
            except Exception as error:
                problems.append(f"MALFORMED_XML[{name}]: {error}")

    for name, tree in xml_trees.items():
        if not name.endswith(".rels"):
            continue

        base_dir = name.replace("_rels/", "")
        base_dir = base_dir.rsplit("/", 1)[0] if "/" in base_dir else ""
        seen_ids: dict[str, str] = {}
        ns = {"r": "http://schemas.openxmlformats.org/package/2006/relationships"}

        for rel in tree.findall("r:Relationship", ns):
            rid = rel.get("Id")
            target = rel.get("Target")
            mode = rel.get("TargetMode", "Internal")

            if rid in seen_ids:
                problems.append(f"DUPLICATE_RELATIONSHIP_ID[{name}]: {rid}")
            seen_ids[rid] = target

            if mode == "External":
                continue

            resolved = target
            if not resolved.startswith("/"):
                if base_dir:
                    resolved = base_dir + "/" + target
                resolved = resolved.replace("word/../", "")
            resolved = resolved.lstrip("/")

            if resolved not in names:
                problems.append(
                    f"MISSING_RELATIONSHIP_TARGET[{name}]: {rid} -> {target}"
                )

    if "word/document.xml" in xml_trees:
        doc = xml_trees["word/document.xml"]
        ns_wp = "{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}"
        docpr_ids = [el.get("id") for el in doc.iter(f"{ns_wp}docPr") if el.get("id")]
        duplicates = {x for x in docpr_ids if docpr_ids.count(x) > 1}
        if duplicates:
            problems.append(f"DUPLICATE_DOCPR_IDS: {sorted(duplicates)}")

        ns_w = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
        body = doc.find(f"{ns_w}body")
        if body is not None:
            direct_sectprs = [c for c in body if c.tag == f"{ns_w}sectPr"]
            if len(direct_sectprs) > 1:
                problems.append(f"MULTIPLE_BODY_LEVEL_SECTPR: {len(direct_sectprs)}")
            if direct_sectprs and body[-1].tag != f"{ns_w}sectPr":
                problems.append("SECTPR_NOT_LAST_BODY_CHILD")

    try:
        from docx import Document

        Document(path)
    except Exception as error:
        problems.append(f"PYTHON_DOCX_CANNOT_REOPEN: {type(error).__name__}: {error}")

    return problems


class MatrixContext:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.headers = {
            "X-API-Key": args.api_key,
            "X-Admin-Key": args.admin_key,
            "Accept": "application/json",
        }
        self.original_photo = (
            Path(args.snapshot_source_dir)
            / ".admin-state/contact-photos"
            / args.original_photo_name
        )
        with open(self.original_photo, "rb") as handle:
            self.original_photo_bytes = handle.read()

    def request(self, method, path, body=None, extra_headers=None, raw=False):
        headers = dict(self.headers)
        if extra_headers:
            headers.update(extra_headers)

        data = None
        if raw and body is not None:
            data = body
        elif body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"

        request = urllib.request.Request(
            self.args.base_url + path, data=data, headers=headers, method=method
        )

        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.read()

    def download(self) -> bytes | None:
        status, body = self.request(
            "GET", f"/api/v1/admin/documents/{self.args.document_id}/download"
        )
        if status != 200:
            print(f"  DOWNLOAD FAILED status={status} body={body[:300]!r}")
            return None
        return body

    def reset_to_clean_baseline(self) -> None:
        docx_target = Path(self.args.snapshot_source_dir) / self.args.docx_filename
        state_target = (
            Path(self.args.snapshot_source_dir)
            / ".admin-state/contacts"
            / f"{self.args.document_id}.json"
        )
        photos_dir = Path(self.args.snapshot_source_dir) / ".admin-state/contact-photos"
        original_photo_target = photos_dir / self.args.original_photo_name

        shutil.copyfile(self.args.original_docx_backup, docx_target)
        shutil.copyfile(self.args.original_state_backup, state_target)

        for extra_photo in glob.glob(str(photos_dir / "*")):
            if os.path.basename(extra_photo) != self.args.original_photo_name:
                os.remove(extra_photo)

        # A prior stage may have deleted or replaced the original
        # contact's photo file entirely (A9/A6) - the ContactState
        # backup above always re-references original_photo_name, so
        # the underlying file must exist again too, or every
        # subsequent stage's rebuild (which re-renders EVERY contact,
        # not just the one being changed) fails reading it.
        if not original_photo_target.exists():
            original_photo_target.write_bytes(self.original_photo_bytes)


NEW_FIELDS = {
    "member_firm": "Matrix Test Firm",
    "contact_person": "Matrix Test Person",
    "email": "matrix-test@example.com",
    "phone": "+1 555 000 9999",
    "address": "1 Matrix Test Street",
    "website": "www.matrix-test.example.com",
}


def run_matrix(ctx: MatrixContext) -> dict[str, bool]:
    results: dict[str, bool] = {}
    args = ctx.args

    def run_stage(label, action):
        print(f"=== {label} ===")
        ctx.reset_to_clean_baseline()
        action()
        body = ctx.download()
        if body is None:
            results[label] = False
            return
        problems = validate_docx_bytes(body, label)
        results[label] = not problems
        print(f"  {label}: {'VALID' if not problems else 'INVALID'}")
        for problem in problems:
            print(f"    - {problem}")

    def validate_docx_bytes(body: bytes, label: str) -> list[str]:
        temp_path = Path(f"/tmp/{label}.docx")
        temp_path.write_bytes(body)
        try:
            return validate_docx(temp_path)
        finally:
            temp_path.unlink(missing_ok=True)

    run_stage("A0_ORIGINAL", lambda: None)

    run_stage(
        "A2_REBUILD_ONLY",
        lambda: ctx.request(
            "PUT",
            f"/api/v1/admin/documents/{args.document_id}/contacts/{args.original_contact_id}",
            {
                "member_firm": args.original_member_firm,
                "contact_person": args.original_contact_person,
                "email": args.original_email,
                "phone": args.original_phone,
                "address": args.original_address,
                "website": args.original_website,
            },
        ),
    )

    def do_add_no_photo():
        status, _ = ctx.request(
            "POST", f"/api/v1/admin/documents/{args.document_id}/contacts", NEW_FIELDS
        )
        print(f"  add status={status}")

    run_stage("A3_ADD_NO_PHOTO", do_add_no_photo)

    def do_add_with_photo():
        status, body = ctx.request(
            "POST", f"/api/v1/admin/documents/{args.document_id}/contacts", NEW_FIELDS
        )
        print(f"  add status={status}")
        contact_id = json.loads(body)["contact_id"]
        status2, _ = ctx.request(
            "PUT",
            f"/api/v1/admin/documents/{args.document_id}/contacts/{contact_id}/photo",
            body=ctx.original_photo_bytes,
            extra_headers={"Content-Type": "image/jpeg"},
            raw=True,
        )
        print(f"  photo status={status2}")
        return contact_id

    run_stage("A4_ADD_WITH_PHOTO", do_add_with_photo)

    def do_update_text():
        fields = {
            "member_firm": args.original_member_firm,
            "contact_person": args.original_contact_person,
            "email": args.original_email,
            "phone": args.original_phone + " (updated)",
            "address": args.original_address,
            "website": args.original_website,
        }
        ctx.request(
            "PUT",
            f"/api/v1/admin/documents/{args.document_id}/contacts/{args.original_contact_id}",
            fields,
        )

    run_stage("A5_UPDATE_TEXT", do_update_text)

    def do_replace_photo():
        status, _ = ctx.request(
            "PUT",
            f"/api/v1/admin/documents/{args.document_id}/contacts/{args.original_contact_id}/photo",
            body=ctx.original_photo_bytes,
            extra_headers={"Content-Type": "image/jpeg"},
            raw=True,
        )
        print(f"  photo replace status={status}")

    run_stage("A6_REPLACE_PHOTO", do_replace_photo)

    def do_delete_photo():
        status, _ = ctx.request(
            "DELETE",
            f"/api/v1/admin/documents/{args.document_id}/contacts/{args.original_contact_id}/photo",
        )
        print(f"  delete photo status={status}")

    run_stage("A9_DELETE_PHOTO_ONLY", do_delete_photo)

    def do_second_update():
        fields1 = dict(NEW_FIELDS)
        ctx.request(
            "PUT",
            f"/api/v1/admin/documents/{args.document_id}/contacts/{args.original_contact_id}",
            {
                "member_firm": args.original_member_firm,
                "contact_person": args.original_contact_person,
                "email": args.original_email,
                "phone": args.original_phone,
                "address": args.original_address,
                "website": args.original_website,
            },
        )
        fields2 = {
            "member_firm": args.original_member_firm,
            "contact_person": args.original_contact_person,
            "email": args.original_email,
            "phone": args.original_phone + " (v2)",
            "address": args.original_address,
            "website": args.original_website,
        }
        ctx.request(
            "PUT",
            f"/api/v1/admin/documents/{args.document_id}/contacts/{args.original_contact_id}",
            fields2,
        )

    run_stage("A7_SECOND_UPDATE", do_second_update)

    # A8: the exact real incident sequence - add w/ photo -> download
    # (pre-delete) -> delete -> download (post-delete)
    print("=== A8a_ADD_PHOTO_PRE_DELETE (mirrors the real incident) ===")
    ctx.reset_to_clean_baseline()
    new_contact_id = do_add_with_photo()
    body = ctx.download()
    problems = validate_docx_bytes(body, "A8a") if body else ["DOWNLOAD_FAILED"]
    results["A8a_ADD_PHOTO_PRE_DELETE"] = not problems
    print(f"  A8a: {'VALID' if not problems else 'INVALID'}")
    for problem in problems:
        print(f"    - {problem}")

    print("=== A8_DELETE (mirrors the real incident's second download) ===")
    status, _ = ctx.request(
        "DELETE",
        f"/api/v1/admin/documents/{args.document_id}/contacts/{new_contact_id}",
    )
    print(f"  delete status={status}")
    body = ctx.download()
    problems = validate_docx_bytes(body, "A8b") if body else ["DOWNLOAD_FAILED"]
    results["A8_DELETE"] = not problems
    print(f"  A8_DELETE: {'VALID' if not problems else 'INVALID'}")
    for problem in problems:
        print(f"    - {problem}")

    return results


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--api-key", required=True)
    parser.add_argument("--admin-key", required=True)
    parser.add_argument("--document-id", required=True)
    parser.add_argument("--original-contact-id", required=True)
    parser.add_argument("--original-member-firm", required=True)
    parser.add_argument("--original-contact-person", required=True)
    parser.add_argument("--original-email", required=True)
    parser.add_argument("--original-phone", required=True)
    parser.add_argument("--original-address", required=True)
    parser.add_argument("--original-website", required=True)
    parser.add_argument(
        "--snapshot-source-dir",
        required=True,
        help="An ISOLATED, read-write documents/source directory - "
        "never a production path. This script mutates it repeatedly.",
    )
    parser.add_argument("--docx-filename", required=True)
    parser.add_argument("--original-docx-backup", required=True, type=Path)
    parser.add_argument("--original-state-backup", required=True, type=Path)
    parser.add_argument("--original-photo-name", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_argument_parser()
    args = parser.parse_args(argv)

    ctx = MatrixContext(args)
    results = run_matrix(ctx)

    print()
    print("=== SUMMARY ===")
    for name, passed in results.items():
        print(f"{name}={'PASS' if passed else 'FAIL'}")

    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
