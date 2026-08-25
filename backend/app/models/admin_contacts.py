"""API models for Admin Contact Management (mission "ORDER 8G-B1")."""

from __future__ import annotations

from pydantic import BaseModel, model_validator


class AdminContactSummary(BaseModel):
    """
    One persisted contact, as returned to the Admin.

    Every field is nullable - both a legacy contact parsed from an
    incomplete real DOCX and a contact saved via Add/Edit can
    genuinely have any of the six business fields empty (see
    AdminContactWriteRequest below).
    """

    contact_id: str
    member_firm: str | None = None
    contact_person: str | None = None
    email: str | None = None
    phone: str | None = None
    address: str | None = None
    website: str | None = None
    has_photo: bool = False

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

    Every one of the six business fields is individually optional - a
    real member-firm contact can genuinely have some fields empty
    (verified directly against production content: France's own
    contact has always had address/website empty, a legitimate
    document state, not missing data to reject). This supersedes an
    earlier mission's "all six required" rule (ORDER 8G-B0) once that
    turned out to reject legitimate real contacts.

    The only validation is a cross-field one: at least one field must
    carry an actual value, so a submission that is entirely blank is
    rejected here with a clear business error - rather than either
    creating a meaningless blank row, or being rejected downstream by
    the internal canonical round-trip validator with a confusing
    "contact count changed" error (an all-blank contact reads back out
    as zero contacts - see ExtractedContact.has_any_field()).

    Duplicates (two records with identical field values) are an
    explicit product decision to allow, never rejected here.
    """

    member_firm: str = ""
    contact_person: str = ""
    email: str = ""
    phone: str = ""
    address: str = ""
    website: str = ""

    class Config:
        extra = "forbid"

    @model_validator(mode="after")
    def _require_at_least_one_field(self) -> "AdminContactWriteRequest":
        if not any(
            value.strip()
            for value in (
                self.member_firm,
                self.contact_person,
                self.email,
                self.phone,
                self.address,
                self.website,
            )
        ):
            raise ValueError(
                "At least one contact field must have a value."
            )

        return self


class AdminContactResponse(BaseModel):
    """Result of successfully adding or updating one contact - every
    field nullable, matching AdminContactSummary (a field submitted
    blank is persisted and echoed back as None, never an empty
    string)."""

    document_id: str
    country_code: str
    contact_id: str
    member_firm: str | None
    contact_person: str | None
    email: str | None
    phone: str | None
    address: str | None
    website: str | None

    class Config:
        extra = "forbid"


class AdminContactDeleteResponse(BaseModel):
    """Result of successfully deleting one contact."""

    document_id: str
    country_code: str
    contact_id: str

    class Config:
        extra = "forbid"
