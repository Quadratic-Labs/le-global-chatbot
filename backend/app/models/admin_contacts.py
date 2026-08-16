"""API models for Admin Contact Management (mission "ORDER 8G-B1")."""

from __future__ import annotations

from pydantic import BaseModel, Field


class AdminContactSummary(BaseModel):
    """
    One persisted contact, as returned to the Admin.

    Fields are nullable here even though a NEW write always requires
    all six (see AdminContactWriteRequest below) - a legacy contact
    parsed from an incomplete real DOCX must remain viewable exactly as
    parsed.
    """

    contact_id: str
    member_firm: str | None = None
    contact_person: str | None = None
    email: str | None = None
    phone: str | None = None
    address: str | None = None
    website: str | None = None

    class Config:
        extra = "forbid"


class AdminContactListResponse(BaseModel):
    """Every contact currently configured for one document/country."""

    document_id: str
    country_code: str
    contacts: list[AdminContactSummary]

    class Config:
        extra = "forbid"


class AdminContactWriteRequest(BaseModel):
    """
    A contact Admin wants to Add or Update.

    All six real business fields are required, non-empty after
    trimming - the future rule this mission's audit (ORDER 8G-B0)
    established: "all real business fields required on Add/Edit; only
    photo is optional" (photo is not part of B1 at all). Duplicates
    (two records with identical field values) are an explicit product
    decision to allow, never rejected here.
    """

    member_firm: str = Field(min_length=1)
    contact_person: str = Field(min_length=1)
    email: str = Field(min_length=1)
    phone: str = Field(min_length=1)
    address: str = Field(min_length=1)
    website: str = Field(min_length=1)

    class Config:
        extra = "forbid"


class AdminContactResponse(BaseModel):
    """Result of successfully adding or updating one contact."""

    document_id: str
    country_code: str
    contact_id: str
    member_firm: str
    contact_person: str
    email: str
    phone: str
    address: str
    website: str

    class Config:
        extra = "forbid"


class AdminContactDeleteResponse(BaseModel):
    """Result of successfully deleting one contact."""

    document_id: str
    country_code: str
    contact_id: str

    class Config:
        extra = "forbid"
