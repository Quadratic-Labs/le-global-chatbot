"""API models for legal document search."""

from __future__ import annotations

from pydantic import BaseModel, Field


class LegalSearchRequest(BaseModel):
    """Legal search criteria received by the API."""

    query: str = Field(
        min_length=2,
        max_length=1000,
        description="Legal question or search expression.",
    )

    country_codes: list[str] = Field(
        default_factory=list,
        description=(
            "Optional ISO alpha-2 country codes, "
            "for example GB, FR, or DE."
        ),
    )

    legal_topics: list[str] = Field(
        default_factory=list,
        description=(
            "Optional canonical legal topics."
        ),
    )

    subsections: list[str] = Field(
        default_factory=list,
        description=(
            "Optional canonical legal subsections."
        ),
    )

    language: str | None = Field(
        default="en",
        min_length=2,
        max_length=10,
        description=(
            "Optional document language filter."
        ),
    )

    reference_year: int | None = Field(
        default=None,
        ge=1900,
        le=2100,
        description=(
            "Optional source document reference year."
        ),
    )

    limit: int = Field(
        default=10,
        ge=1,
        le=50,
        description=(
            "Maximum number of chunks returned."
        ),
    )

    offset: int = Field(
        default=0,
        ge=0,
        le=1000,
        description=(
            "Number of matching chunks to skip."
        ),
    )

    class Config:
        extra = "forbid"


class LegalSearchHit(BaseModel):
    """One legal chunk returned by OpenSearch."""

    score: float

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
    reference_year: int | None = None

    class Config:
        extra = "forbid"


class LegalSearchResponse(BaseModel):
    """Structured legal search response."""

    query: str
    total: int
    limit: int
    offset: int
    took_ms: int
    hits: list[LegalSearchHit]

    class Config:
        extra = "forbid"