"""API models for grounded legal answers."""

from __future__ import annotations

from pydantic import BaseModel, Field


class LegalChatRequest(BaseModel):
    """One independent legal question."""

    question: str = Field(
        min_length=2,
        max_length=2000,
        description="Employment law question.",
    )

    country_codes: list[str] = Field(
        default_factory=list,
        description="Optional ISO alpha-2 country filters.",
    )

    legal_topics: list[str] = Field(
        default_factory=list,
        description="Optional canonical legal topic filters.",
    )

    subsections: list[str] = Field(
        default_factory=list,
        description="Optional canonical subsection filters.",
    )

    language: str = Field(
        default="en",
        min_length=2,
        max_length=10,
    )

    reference_year: int | None = Field(
        default=None,
        ge=1900,
        le=2100,
    )

    max_sources: int = Field(
        default=6,
        ge=1,
        le=10,
        description="Maximum retrieved chunks used as context.",
    )

    class Config:
        extra = "forbid"


class LegalAnswerSource(BaseModel):
    """Source supporting the generated legal answer."""

    citation: int

    document_id: str
    chunk_id: str

    country: str
    country_code: str
    legal_topic: str | None

    section: str
    subsection: str | None

    source_filename: str
    reference_year: int | None = None

    score: float

    class Config:
        extra = "forbid"


class LegalChatResponse(BaseModel):
    """Grounded legal answer and its supporting sources."""

    question: str
    answer: str

    grounded: bool
    model: str | None

    retrieval_total: int
    sources: list[LegalAnswerSource]

    class Config:
        extra = "forbid"