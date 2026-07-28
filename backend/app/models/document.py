from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class DocumentChunk:
    """Structured legal content ready for OpenSearch indexing."""

    document_id: str
    chunk_id: str

    country: str
    country_code: str
    legal_topic: str | None
    document_type: str
    language: str

    section: str
    subsection: str | None
    content: str

    source_filename: str
    source_format: str
    content_hash: str

    reference_year: int | None = None

    def to_document(self) -> dict[str, Any]:
        """Return an OpenSearch-compatible dictionary."""
        return asdict(self)