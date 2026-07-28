import json
import sys
from collections import Counter
from pathlib import Path

from app.services.document_chunk_builder import (
    build_document_chunks_from_docx,
)


def main() -> None:
    if len(sys.argv) not in {3, 4}:
        raise SystemExit(
            "Usage: python -m scripts.inspect_document_chunks "
            "<path-to-docx> <country-code> [output-json]"
        )

    file_path = Path(sys.argv[1])
    country_code = sys.argv[2]

    output_path = (
        Path(sys.argv[3])
        if len(sys.argv) == 4
        else None
    )

    chunks = build_document_chunks_from_docx(
        file_path=file_path,
        country_code=country_code,
    )

    document_ids = {
        chunk.document_id
        for chunk in chunks
    }

    chunk_ids = [
        chunk.chunk_id
        for chunk in chunks
    ]

    content_hashes = [
        chunk.content_hash
        for chunk in chunks
    ]

    document_type_counts = Counter(
        chunk.document_type
        for chunk in chunks
    )

    legal_topic_counts = Counter(
        chunk.legal_topic
        for chunk in chunks
        if chunk.legal_topic is not None
    )

    duplicate_chunk_ids = [
        chunk_id
        for chunk_id, count in Counter(
            chunk_ids
        ).items()
        if count > 1
    ]

    duplicate_content_hashes = [
        content_hash
        for content_hash, count in Counter(
            content_hashes
        ).items()
        if count > 1
    ]

    print(
        f"File: {file_path.name}"
    )
    print(
        f"Total chunks: {len(chunks)}"
    )
    print(
        "Overview chunks: "
        f"{document_type_counts.get('overview', 0)}"
    )
    print(
        "Comparator chunks: "
        f"{document_type_counts.get('comparator', 0)}"
    )
    print(
        f"Legal topics found: {len(legal_topic_counts)}"
    )
    print(
        f"Unique document IDs: {len(document_ids)}"
    )
    print(
        f"Unique chunk IDs: {len(set(chunk_ids))}"
    )
    print(
        "Duplicate chunk IDs: "
        f"{len(duplicate_chunk_ids)}"
    )
    print(
        "Duplicate content hashes: "
        f"{len(duplicate_content_hashes)}"
    )

    print()
    print("Chunks by legal topic:")

    for legal_topic, count in sorted(
        legal_topic_counts.items()
    ):
        print(
            f"- {legal_topic}: {count}"
        )

    if duplicate_chunk_ids:
        raise RuntimeError(
            "Duplicate chunk IDs detected: "
            f"{duplicate_chunk_ids}"
        )

    if output_path is None:
        return

    output_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    output_data = {
        "summary": {
            "source_filename": file_path.name,
            "total_chunks": len(chunks),
            "overview_chunks": document_type_counts.get(
                "overview",
                0,
            ),
            "comparator_chunks": document_type_counts.get(
                "comparator",
                0,
            ),
            "legal_topics_found": len(
                legal_topic_counts
            ),
            "unique_document_ids": len(
                document_ids
            ),
            "unique_chunk_ids": len(
                set(chunk_ids)
            ),
            "duplicate_chunk_ids": duplicate_chunk_ids,
            "duplicate_content_hashes": (
                duplicate_content_hashes
            ),
            "chunks_by_legal_topic": dict(
                sorted(
                    legal_topic_counts.items()
                )
            ),
        },
        "chunks": [
            chunk.to_document()
            for chunk in chunks
        ],
    }

    output_path.write_text(
        json.dumps(
            output_data,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print()
    print(
        "Chunk inspection written to: "
        f"{output_path.resolve()}"
    )


if __name__ == "__main__":
    main()