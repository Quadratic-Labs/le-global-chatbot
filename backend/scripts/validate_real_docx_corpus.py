"""
Validate real-world DOCX documents against the production ingestion
pipeline, without ever indexing them.

Mission "HOTFIX 0.4.4", Mission 2/2, section 8: runs the exact same
validation, content-based metadata detection, and chunk-building code
path production ingestion uses (validate_docx_format,
metadata_from_content, build_document_chunks_from_docx,
extract_contacts_from_docx) against a directory of real DOCX files,
and reports a structured, read-only verdict per file. Never calls
OpenSearch, never writes to any source_root, never renames a file.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from app.services.document_chunk_builder import (
    build_document_chunks_from_docx,
    metadata_from_content,
    validate_docx_format,
)
from app.services.docx_parser import extract_contacts_from_docx


def validate_one_document(file_path: Path) -> dict[str, Any]:
    """Run the full read-only validation pipeline on one DOCX file."""

    report: dict[str, Any] = {
        "filename": file_path.name,
        "valid_docx": False,
        "country": None,
        "country_code": None,
        "reference_year": None,
        "document_family": None,
        "chunk_count": 0,
        "legal_topics_detected": [],
        "contacts_detected": 0,
        "warnings": [],
        "error": None,
    }

    try:
        validate_docx_format(file_path)
        report["valid_docx"] = True

    except Exception as error:
        report["error"] = f"{type(error).__name__}: {error}"
        return report

    try:
        metadata = metadata_from_content(file_path=file_path)

    except Exception as error:
        report["error"] = f"{type(error).__name__}: {error}"
        return report

    report["country"] = metadata.country
    report["country_code"] = metadata.country_code
    report["reference_year"] = metadata.reference_year
    report["document_family"] = "employment-law-overview"

    try:
        chunks = build_document_chunks_from_docx(
            file_path=file_path,
            country_code=metadata.country_code,
            language=metadata.language,
        )

    except Exception as error:
        report["error"] = f"{type(error).__name__}: {error}"
        return report

    report["chunk_count"] = len(chunks)

    mixed_countries = {
        chunk.country_code for chunk in chunks
    } - {metadata.country_code}

    if mixed_countries:
        report["warnings"].append(
            "Chunks reference other country codes: "
            f"{sorted(mixed_countries)}"
        )

    report["legal_topics_detected"] = sorted(
        {
            chunk.legal_topic
            for chunk in chunks
            if chunk.legal_topic is not None
        }
    )

    if not chunks:
        report["warnings"].append(
            "No legal chunks were produced from this document."
        )

    try:
        contacts = extract_contacts_from_docx(
            file_path=file_path,
            country=metadata.country,
        )

        report["contacts_detected"] = len(contacts)

        if not contacts:
            report["warnings"].append(
                "No contact block was detected in this document."
            )

    except Exception as error:
        report["warnings"].append(
            "Contact extraction failed: "
            f"{type(error).__name__}: {error}"
        )

    return report


def validate_real_docx_corpus(
    *,
    source_dir: Path,
) -> dict[str, Any]:
    """Validate every DOCX file in source_dir, never indexing any of them."""

    files = sorted(source_dir.glob("*.docx"))

    documents = [
        validate_one_document(file_path) for file_path in files
    ]

    successful = [
        document
        for document in documents
        if document["error"] is None
    ]

    failed = [
        document
        for document in documents
        if document["error"] is not None
    ]

    return {
        "source_dir": str(source_dir),
        "files_found": len(files),
        "files_successful": len(successful),
        "files_failed": len(failed),
        "documents": documents,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only validation of real DOCX documents against the "
            "production ingestion pipeline - never indexes anything."
        )
    )

    parser.add_argument(
        "--source-dir",
        required=True,
        type=Path,
        help="Directory containing the real DOCX files to validate.",
    )

    parser.add_argument(
        "--no-index",
        action="store_true",
        required=True,
        help=(
            "Required, explicit acknowledgement that this script "
            "never indexes any document into OpenSearch."
        ),
    )

    parser.add_argument(
        "--json-report",
        required=True,
        type=Path,
        help="Path to write the JSON validation report to.",
    )

    arguments = parser.parse_args()

    report = validate_real_docx_corpus(
        source_dir=arguments.source_dir
    )

    arguments.json_report.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    arguments.json_report.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"Files found: {report['files_found']}")
    print(f"Files successful: {report['files_successful']}")
    print(f"Files failed: {report['files_failed']}")
    print()

    for document in report["documents"]:
        status = "OK" if document["error"] is None else "FAIL"

        print(
            f"[{status}] {document['filename']} -> "
            f"country={document['country']} "
            f"code={document['country_code']} "
            f"year={document['reference_year']} "
            f"chunks={document['chunk_count']} "
            f"topics={len(document['legal_topics_detected'])} "
            f"contacts={document['contacts_detected']}"
        )

        if document["error"]:
            print(f"    ERROR: {document['error']}")

        for warning in document["warnings"]:
            print(f"    WARNING: {warning}")

    print()
    print(f"JSON report: {arguments.json_report}")

    if report["files_failed"] > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
