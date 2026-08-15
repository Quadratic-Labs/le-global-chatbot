"""
The ADMIN document-upload country allowlist.

Mission "ORDER 5C": a new business rule restricting which countries a
NEW admin-uploaded document may target, deliberately kept separate
from app/core/country_registry.py. The registry answers "can this
document's country be identified at all?" (a detection capability);
this module answers a strictly narrower, independent question - "is
this country currently accepted for new admin uploads?" - so the
registry can keep recognizing countries outside this list (as
section 7 of the mission explicitly requires) without that implying
they are admin-allowed, and so this list can change without touching
country detection at all.

This is the single source of truth for the 34 allowed codes - no
router, service, or WordPress layer may keep its own copy.
"""

from __future__ import annotations

from typing import Final


ADMIN_ALLOWED_COUNTRY_CODES: Final[frozenset[str]] = frozenset(
    {
        "AR",  # Argentina
        "AU",  # Australia
        "BE",  # Belgium
        "BR",  # Brazil
        "CA",  # Canada
        "CL",  # Chile
        "CN",  # China
        "CO",  # Colombia
        "CZ",  # Czech Republic
        "FR",  # France
        "DE",  # Germany
        "GR",  # Greece
        "ID",  # Indonesia
        "IE",  # Ireland
        "IT",  # Italy
        "IN",  # India
        "JP",  # Japan
        "MX",  # Mexico
        "NL",  # Netherlands
        "NO",  # Norway
        "PE",  # Peru
        "PH",  # Philippines
        "PL",  # Poland
        "PT",  # Portugal
        "RO",  # Romania
        "SG",  # Singapore
        "SK",  # Slovakia
        "ES",  # Spain
        "SE",  # Sweden
        "CH",  # Switzerland
        "TW",  # Taiwan
        "TR",  # Turkey / Türkiye
        "GB",  # UK / United Kingdom
        "US",  # USA / United States
    }
)


def is_admin_country_allowed(country_code: str) -> bool:
    """
    Return whether country_code may be used for a NEW admin upload.

    Deliberately does not validate or normalize country_code itself -
    callers are expected to have already resolved it through
    country_registry.py (which raises its own errors for anything
    unrecognized); this function only ever answers the allowlist
    question for an already-known, already-normalized code.
    """

    return country_code.strip().upper() in ADMIN_ALLOWED_COUNTRY_CODES
