"""
Copy ONLY the documents/contacts/photos listed in the release
compatibility manifest out of the real, live document source
directory into a fresh, isolated snapshot directory - never the
other way around.

This is a READ-ONLY tool with respect to production: it never opens
any file under --production-source-dir for writing, never deletes
anything there, and never mutates ContactState, a photo, or a source
DOCX. It only ever WRITES into --output-dir, which must not already
exist (so a mistaken re-run can never silently merge stale fixture
state with a newer one).

Usage:

    python3 scripts/prepare_release_compatibility_snapshot.py \\
        --production-source-dir /var/lib/le-global-chatbot/documents/source \\
        --manifest scripts/fixtures/release_compatibility_manifest.json \\
        --output-dir /var/tmp/le-global-smoke-fixture/documents/source

The production source directory is typically only readable by the
backend's own container user (uid/gid 10001 per
systemd/README.md) - run this script with whatever privilege is
required to READ it (e.g. sudo), never to write to it.

See docs/RELEASE_COMPATIBILITY.md for the full live-gate recipe this
feeds into.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path


ADMIN_STATE_DIRECTORY_NAME = ".admin-state"
CONTACTS_DIRECTORY_NAME = "contacts"
CONTACT_PHOTOS_DIRECTORY_NAME = "contact-photos"


def load_manifest_documents(manifest_path: Path) -> list[dict]:
    payload = json.loads(manifest_path.read_text())
    documents = payload.get("documents")

    if not isinstance(documents, list) or not documents:
        raise ValueError(f"manifest {manifest_path} has no documents")

    for document in documents:
        for required_field in ("document_id", "source_filename"):
            if not document.get(required_field):
                raise ValueError(
                    f"manifest entry missing required field "
                    f"{required_field!r}: {document!r}"
                )

    return documents


def copy_read_only(source: Path, destination: Path) -> None:
    if not source.exists():
        raise FileNotFoundError(f"expected production file not found: {source}")

    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination, follow_symlinks=False)


def prepare_snapshot(
    production_source_dir: Path,
    manifest_documents: list[dict],
    output_dir: Path,
) -> list[str]:
    """Returns the list of files copied, for the caller to log."""

    if output_dir.exists():
        raise FileExistsError(
            f"output directory already exists, refusing to reuse it: "
            f"{output_dir} (remove it first if this is intentional)"
        )

    copied: list[str] = []

    admin_state_dir = production_source_dir / ADMIN_STATE_DIRECTORY_NAME
    output_admin_state_dir = output_dir / ADMIN_STATE_DIRECTORY_NAME

    for document in manifest_documents:
        document_id = document["document_id"]
        source_filename = document["source_filename"]

        docx_source = production_source_dir / source_filename
        docx_destination = output_dir / source_filename
        copy_read_only(docx_source, docx_destination)
        copied.append(str(docx_source))

        state_source = (
            admin_state_dir / CONTACTS_DIRECTORY_NAME / f"{document_id}.json"
        )
        state_destination = (
            output_admin_state_dir / CONTACTS_DIRECTORY_NAME / f"{document_id}.json"
        )
        copy_read_only(state_source, state_destination)
        copied.append(str(state_source))

        state_payload = json.loads(state_source.read_text())

        for contact in state_payload.get("contacts", []):
            photo_filename = contact.get("photo_filename")

            if not photo_filename:
                continue

            photo_source = (
                admin_state_dir / CONTACT_PHOTOS_DIRECTORY_NAME / photo_filename
            )
            photo_destination = (
                output_admin_state_dir
                / CONTACT_PHOTOS_DIRECTORY_NAME
                / photo_filename
            )
            copy_read_only(photo_source, photo_destination)
            copied.append(str(photo_source))

    return copied


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)

    parser.add_argument(
        "--production-source-dir",
        type=Path,
        required=True,
        help="The real, live document source directory to read from "
        "(read-only). Typically /var/lib/le-global-chatbot/documents/source.",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=(
            Path(__file__).parent
            / "fixtures"
            / "release_compatibility_manifest.json"
        ),
        help="Path to the fixture manifest.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="Destination snapshot directory. Must not already exist.",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_argument_parser()
    arguments = parser.parse_args(argv)

    try:
        manifest_documents = load_manifest_documents(arguments.manifest)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"[ERROR] could not load manifest: {error}", file=sys.stderr)
        return 2

    try:
        copied = prepare_snapshot(
            production_source_dir=arguments.production_source_dir,
            manifest_documents=manifest_documents,
            output_dir=arguments.output_dir,
        )
    except (OSError, FileExistsError, FileNotFoundError) as error:
        print(f"[ERROR] {error}", file=sys.stderr)
        return 1

    print(f"Copied {len(copied)} file(s) into {arguments.output_dir}:")

    for path in copied:
        print(f"  read (never written): {path}")

    print(
        "\nProduction source directory was only ever opened for reading. "
        "Verify with e.g.:\n"
        f"  stat -c '%Y %n' {arguments.production_source_dir}/<file> "
        "before/after, or a checksum comparison."
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
