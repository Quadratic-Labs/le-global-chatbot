"""
Read-only audit of indexed documents' on-disk source paths.

Mission "HOTFIX 0.4.4", section 8: uses the exact same centralized
resolver as list_indexed_documents, Reindex, Replace, and Delete, so
this audit's verdict is always consistent with what the admin UI and
lifecycle operations would themselves decide. Never writes to
OpenSearch or to source_root, never renames a file, never reindexes a
document, never scans source_root for candidate files - country/year/
filename are read exclusively from OpenSearch metadata.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from app.clients.opensearch import get_opensearch_client
from app.services.admin_documents import (
    build_admin_document_catalog_body,
)
from app.services.document_source_resolver import (
    DocumentSourceConflictError,
    resolve_document_source_path,
)
from app.services.opensearch_index import LEGAL_DOCUMENTS_ALIAS


def _extract_metadata_source(
    bucket: dict[str, Any],
) -> dict[str, Any] | None:
    metadata = bucket.get("metadata")

    if not isinstance(metadata, dict):
        return None

    hits_container = metadata.get("hits")

    if not isinstance(hits_container, dict):
        return None

    hits = hits_container.get("hits")

    if not isinstance(hits, list) or not hits:
        return None

    first_hit = hits[0]

    if not isinstance(first_hit, dict):
        return None

    source = first_hit.get("_source")

    return source if isinstance(source, dict) else None


def audit_document_sources(
    *,
    source_root: Path,
) -> dict[str, Any]:
    """
    Read every indexed document's metadata from OpenSearch, resolve
    its on-disk source with the same centralized resolver used
    everywhere else, and return a structured, read-only report.
    """

    client = get_opensearch_client()

    response = client.search(
        index=LEGAL_DOCUMENTS_ALIAS,
        body=build_admin_document_catalog_body(),
    )

    buckets = (
        response.get("aggregations", {})
        .get("documents", {})
        .get("buckets", [])
    )

    documents: list[dict[str, Any]] = []
    indexed_chunks = 0
    resolved_sources = 0
    missing_sources = 0
    conflicts = 0

    for bucket in buckets:
        source = _extract_metadata_source(bucket)
        chunk_count = int(bucket.get("doc_count", 0))
        indexed_chunks += chunk_count

        if source is None:
            documents.append(
                {
                    "document_id": bucket.get("key"),
                    "chunk_count": chunk_count,
                    "resolution": "invalid_metadata",
                }
            )
            continue

        document_id = source.get("document_id")
        country_code = source.get("country_code")
        source_filename = source.get("source_filename")

        if not isinstance(country_code, str) or not country_code:
            documents.append(
                {
                    "document_id": document_id,
                    "chunk_count": chunk_count,
                    "resolution": "invalid_metadata",
                }
            )
            continue

        try:
            resolved = resolve_document_source_path(
                source_root=source_root,
                country_code=country_code,
                source_filename=(
                    source_filename
                    if isinstance(source_filename, str)
                    else None
                ),
            )

        except DocumentSourceConflictError as error:
            conflicts += 1

            documents.append(
                {
                    "document_id": document_id,
                    "country_code": country_code,
                    "source_filename": source_filename,
                    "chunk_count": chunk_count,
                    "resolution": "conflict",
                    "conflicting_paths": [
                        str(path)
                        for path in error.conflicting_paths
                    ],
                }
            )
            continue

        if resolved.path is None:
            missing_sources += 1

            documents.append(
                {
                    "document_id": document_id,
                    "country_code": country_code,
                    "source_filename": source_filename,
                    "chunk_count": chunk_count,
                    "resolution": "missing",
                }
            )
            continue

        resolved_sources += 1

        documents.append(
            {
                "document_id": document_id,
                "country_code": country_code,
                "source_filename": source_filename,
                "chunk_count": chunk_count,
                "resolution": "resolved",
                "resolved_path": str(resolved.path),
                "resolved_origin": resolved.origin,
            }
        )

    return {
        "indexed_documents": len(documents),
        "indexed_chunks": indexed_chunks,
        "resolved_sources": resolved_sources,
        "missing_sources": missing_sources,
        "conflicts": conflicts,
        "documents": documents,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only audit of indexed documents' on-disk source "
            "paths, using the centralized document source resolver."
        )
    )

    parser.add_argument(
        "--source-root",
        required=True,
        type=Path,
        help="The document source directory to resolve paths under.",
    )

    parser.add_argument(
        "--read-only",
        action="store_true",
        required=True,
        help=(
            "Required, explicit acknowledgement that this script "
            "never writes to OpenSearch or to source-root."
        ),
    )

    parser.add_argument(
        "--json-output",
        required=True,
        type=Path,
        help="Path to write the JSON audit report to.",
    )

    arguments = parser.parse_args()

    report = audit_document_sources(
        source_root=arguments.source_root
    )

    arguments.json_output.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    arguments.json_output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"Indexed documents: {report['indexed_documents']}")
    print(f"Indexed chunks: {report['indexed_chunks']}")
    print(f"Resolved sources: {report['resolved_sources']}")
    print(f"Missing sources: {report['missing_sources']}")
    print(f"Conflicts: {report['conflicts']}")
    print(f"JSON report: {arguments.json_output}")

    if report["missing_sources"] or report["conflicts"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
