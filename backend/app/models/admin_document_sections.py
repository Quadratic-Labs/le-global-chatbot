"""API models for admin section editing (mission "ORDER 5C")."""

from __future__ import annotations

from pydantic import BaseModel, Field


class AdminDocumentSectionSummary(BaseModel):
    """One section that actually exists in a document's effective state."""

    section_id: str
    legal_topic: str

    class Config:
        extra = "forbid"


class AdminDocumentSectionListResponse(BaseModel):
    """Every section currently present in one document's effective state."""

    document_id: str
    sections: list[AdminDocumentSectionSummary]

    class Config:
        extra = "forbid"


class AdminDocumentSectionResponse(BaseModel):
    """The current effective content of one document section."""

    document_id: str
    country_code: str
    country_name: str
    section_id: str
    legal_topic: str
    content: str

    class Config:
        extra = "forbid"


class AdminDocumentSectionUpdateRequest(BaseModel):
    """The new effective content an admin wants to save for a section."""

    content: str = Field(min_length=1)

    class Config:
        extra = "forbid"


class AdminDocumentSectionUpdateResponse(BaseModel):
    """Result of successfully saving a new effective section content."""

    document_id: str
    section_id: str
    legal_topic: str
    indexed_chunks: int

    class Config:
        extra = "forbid"


class AdminDocumentSectionAddRequest(BaseModel):
    """A brand-new top-level legal topic an admin wants to add."""

    title: str = Field(min_length=1)
    content: str = Field(min_length=1)
    position: str = Field(min_length=1)

    class Config:
        extra = "forbid"


class AdminDocumentSectionAddResponse(BaseModel):
    """Result of successfully adding a new top-level legal topic."""

    document_id: str
    section_id: str
    legal_topic: str
    indexed_chunks: int

    class Config:
        extra = "forbid"
