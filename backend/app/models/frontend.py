"""API models used to initialize the chatbot frontend."""

from __future__ import annotations

from pydantic import BaseModel

from app.models.catalog import LegalCatalogResponse


class FrontendLimits(BaseModel):
    """Input limits exposed to the chatbot frontend."""

    question_min_length: int
    question_max_length: int

    max_sources_default: int
    max_sources_min: int
    max_sources_max: int

    class Config:
        extra = "forbid"


class FrontendConfigResponse(BaseModel):
    """Configuration required to initialize the frontend."""

    api_version: str

    default_language: str
    supported_languages: list[str]

    limits: FrontendLimits
    catalog: LegalCatalogResponse

    class Config:
        extra = "forbid"