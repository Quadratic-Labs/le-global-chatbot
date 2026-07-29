"""API models for indexed document lifecycle operations."""

from __future__ import annotations

from pydantic import BaseModel


class AdminDocumentReindexResponse(BaseModel):
    """Result of reindexing an existing source document."""

    status: str

    previous_document_id: str
    document_id: str
    document_id_changed: bool

    source_filename: str
    country: str
    country_code: str
    reference_year: int | None = None

    indexed_chunks: int
    stale_chunks_deleted: int
    previous_chunks_deleted: int

    class Config:
        extra = "forbid"


class AdminDocumentDeleteResponse(BaseModel):
    """Result of deleting one indexed document."""

    status: str

    document_id: str
    source_filename: str

    deleted_chunks: int
    source_file_deleted: bool

    class Config:
        extra = "forbid"