"""API models for document administration."""

from __future__ import annotations

from pydantic import BaseModel


class AdminDocumentSummary(BaseModel):
    """One indexed legal document."""

    document_id: str
    source_filename: str

    country: str
    country_code: str
    language: str
    document_type: str
    reference_year: int | None = None

    chunk_count: int
    source_file_present: bool
    status: str

    class Config:
        extra = "forbid"


class AdminDocumentListResponse(BaseModel):
    """Indexed documents currently managed by the backend."""

    total: int
    documents: list[AdminDocumentSummary]

    class Config:
        extra = "forbid"


class AdminDocumentUploadResponse(BaseModel):
    """Result of validating and indexing an uploaded DOCX."""

    status: str

    document_id: str
    source_filename: str

    country: str
    country_code: str
    reference_year: int | None = None

    uploaded_bytes: int
    indexed_chunks: int
    stale_chunks_deleted: int
    replaced_source_file: bool

    class Config:
        extra = "forbid"