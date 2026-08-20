"""Deterministic association of parsed contacts with contact photos.

This layer deliberately refuses ambiguous mappings. A missing photo is
safer than assigning the wrong person's image.

It does not persist anything and does not know about OpenSearch, Admin
state, or the chatbot API.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Sequence

from app.services.contact_photos import ContactPhotoCandidate
from app.services.docx_parser import ExtractedContact


_AND_SEPARATOR_RE = re.compile(
    r"\s+\band\b\s+",
    flags=re.IGNORECASE,
)

_EMAIL_SEPARATOR_RE = re.compile(
    r"\s*[,;]\s*",
)


@dataclass(frozen=True, slots=True)
class ContactWithPhoto:
    member_firm: str | None = None
    contact_person: str | None = None
    email: str | None = None
    phone: str | None = None
    address: str | None = None
    website: str | None = None
    photo: ContactPhotoCandidate | None = None


def _without_photo(
    contact: ExtractedContact,
) -> ContactWithPhoto:
    return ContactWithPhoto(
        member_firm=contact.member_firm,
        contact_person=contact.contact_person,
        email=contact.email,
        phone=contact.phone,
        address=contact.address,
        website=contact.website,
        photo=None,
    )


def _with_photo(
    contact: ExtractedContact,
    photo: ContactPhotoCandidate,
) -> ContactWithPhoto:
    return ContactWithPhoto(
        member_firm=contact.member_firm,
        contact_person=contact.contact_person,
        email=contact.email,
        phone=contact.phone,
        address=contact.address,
        website=contact.website,
        photo=photo,
    )


def _split_people(
    value: str | None,
) -> list[str]:
    if not value:
        return []

    parts = [
        part.strip()
        for part in _AND_SEPARATOR_RE.split(
            value.strip()
        )
        if part.strip()
    ]

    return parts


def _clean_email(
    value: str,
) -> str:
    cleaned = value.strip()

    if cleaned.casefold().startswith(
        "mailto:"
    ):
        cleaned = cleaned[7:].strip()

    return cleaned


def _split_emails(
    value: str | None,
) -> list[str]:
    if not value:
        return []

    parts = [
        _clean_email(part)
        for part in _EMAIL_SEPARATOR_RE.split(
            value.strip()
        )
        if part.strip()
    ]

    return [
        part
        for part in parts
        if part
    ]


def _split_combined_contact(
    contact: ExtractedContact,
    photos: Sequence[ContactPhotoCandidate],
) -> list[ContactWithPhoto] | None:
    """Split one combined contact only when cardinality proves the map.

    We require:
      N >= 2 people
      N emails
      N photos

    Ordering is inherited from the deterministic DOCX parser and
    contact-photo extractor. Anything else is treated as ambiguous.
    """

    people = _split_people(
        contact.contact_person
    )
    emails = _split_emails(
        contact.email
    )

    count = len(photos)

    if (
        count < 2
        or len(people) != count
        or len(emails) != count
    ):
        return None

    return [
        ContactWithPhoto(
            member_firm=contact.member_firm,
            contact_person=person,
            email=email,
            phone=contact.phone,
            address=contact.address,
            website=contact.website,
            photo=photo,
        )
        for person, email, photo in zip(
            people,
            emails,
            photos,
            strict=True,
        )
    ]


def associate_contact_photos(
    contacts: Sequence[ExtractedContact],
    photos: Sequence[ContactPhotoCandidate],
) -> list[ContactWithPhoto]:
    """Associate parsed contacts and photos without guessing.

    Supported deterministic cases:

    * zero photos:
        preserve all contacts exactly, without photos;

    * one contact + one photo:
        attach it directly;

    * equal numbers of already-individual contacts/photos:
        map in their established deterministic order;

    * one combined contact + multiple photos:
        split only when people, emails and photos have identical
        cardinality.

    Any other shape is preserved without photo associations.
    """

    contacts_list = list(
        contacts
    )
    photos_list = list(
        photos
    )

    if not contacts_list:
        return []

    if not photos_list:
        return [
            _without_photo(contact)
            for contact in contacts_list
        ]

    # Simplest and strongest association.
    if (
        len(contacts_list) == 1
        and len(photos_list) == 1
    ):
        return [
            _with_photo(
                contacts_list[0],
                photos_list[0],
            )
        ]

    # Existing individual contacts can be paired when exact
    # cardinality establishes a complete one-to-one relationship.
    if (
        len(contacts_list) > 1
        and len(contacts_list)
        == len(photos_list)
    ):
        return [
            _with_photo(
                contact,
                photo,
            )
            for contact, photo in zip(
                contacts_list,
                photos_list,
                strict=True,
            )
        ]

    # Current Belgium-like structure:
    # one parser record contains multiple people/emails.
    if len(contacts_list) == 1:
        split = _split_combined_contact(
            contacts_list[0],
            photos_list,
        )

        if split is not None:
            return split

    # Fail closed: retain business data but make no uncertain
    # photo/person association.
    return [
        _without_photo(contact)
        for contact in contacts_list
    ]
