"""API models for document administration."""

from __future__ import annotations

from pydantic import BaseModel, Field


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
    source_bytes: int | None = None
    updated_at: str | None = None
    status: str

    # Mission "ORDER 8E-A1", sections 29-30: a future UI must be able
    # to render Ready vs Action-required directly from these fields -
    # never by counting how many raw rows share a country_code itself.
    # requires_action is deliberately never derived from document_type
    # (a "comparator" document is never, by itself, a problem - see
    # AdminDocumentCountryConflictReviewRequiredError) - only from how
    # many active documents its own country currently has.
    requires_action: bool = False
    action_reason: str | None = None
    resolution_available: bool = False

    class Config:
        extra = "forbid"


class AdminDocumentListResponse(BaseModel):
    """Indexed documents currently managed by the backend."""

    total: int
    documents: list[AdminDocumentSummary]

    class Config:
        extra = "forbid"


class AdminDocumentStatsResponse(BaseModel):
    """Aggregate counts over the indexed document catalog."""

    total_documents: int
    total_countries: int
    status_counts: dict[str, int]

    # A deduplicated count of distinct countries needing attention -
    # never a raw count of conflicting rows, which would double-count
    # every country in conflict once per extra document (mission
    # "ORDER 8E-A1", section 30).
    countries_requiring_action: int = 0

    class Config:
        extra = "forbid"


class AdminAllowedCountryOption(BaseModel):
    """One country selectable for a manual upload decision."""

    code: str
    name: str

    class Config:
        extra = "forbid"


class AdminCountryConflictCandidate(BaseModel):
    """
    One safe, business-facing candidate document in a country conflict
    review.

    document_id is an internal, opaque identity only - a future UI
    must never require the Admin to read or understand it; it exists
    purely so a CHOOSE_DOCUMENT resolution request can name which
    candidate to keep (mission "ORDER 8E-A1", section 22).
    """

    document_id: str
    source_filename: str
    reference_year: int | None = None
    updated_at: str | None = None
    source_bytes: int | None = None

    class Config:
        extra = "forbid"


class AdminCountryConflictReviewResponse(BaseModel):
    """A read-only, safe review of one country's active conflict."""

    country: str
    country_code: str
    candidates: list[AdminCountryConflictCandidate]

    # Whether strong, generic same-source evidence (see
    # admin_document_conflict_resolution.py) currently makes
    # AUTO_DEDUPLICATE available for this country - CHOOSE_DOCUMENT and
    # REPLACE_WITH_DOCUMENT are always available regardless.
    auto_deduplicate_available: bool

    class Config:
        extra = "forbid"


class AdminCountryConflictResolutionResponse(BaseModel):
    """The outcome of an AUTO_DEDUPLICATE or CHOOSE_DOCUMENT resolution."""

    country_code: str
    resolution_mode: str
    kept_document_id: str
    removed_document_ids: list[str] = Field(default_factory=list)
    stale_chunks_deleted: int

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
    document_family: str

    uploaded_bytes: int
    indexed_chunks: int
    stale_chunks_deleted: int
    replaced_source_file: bool
    replaced_document_ids: list[str] = Field(
        default_factory=list
    )

    class Config:
        extra = "forbid"