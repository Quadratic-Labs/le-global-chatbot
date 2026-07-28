"""API models for the available legal corpus catalog."""

from __future__ import annotations

from pydantic import BaseModel


class LegalCatalogCountry(BaseModel):
    """One country represented in the indexed corpus."""

    country_code: str
    country: str
    chunk_count: int

    class Config:
        extra = "forbid"


class LegalCatalogValue(BaseModel):
    """One legal catalog facet value."""

    value: str
    chunk_count: int

    class Config:
        extra = "forbid"


class LegalCatalogResponse(BaseModel):
    """Countries and legal classifications available for search."""

    countries: list[LegalCatalogCountry]
    legal_topics: list[LegalCatalogValue]
    subsections: list[LegalCatalogValue]

    class Config:
        extra = "forbid"