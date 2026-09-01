"""Structured contact-card data for the public legal chatbot.

This service is deliberately independent from answer generation:

* no OpenAI/LLM calls;
* no legal retrieval;
* no HTML generation;
* no user-controlled filesystem filename;
* a missing/malformed structured contact state fails closed to no card.

The existing deterministic text contact answer remains authoritative
and unchanged. These objects are an additional frontend representation.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import re
from typing import Iterable
from urllib.parse import quote

from app.models.chat import LegalChatContact
from app.services.contact_state import (
    ContactPhotoStorageError,
    read_contact_photo,
)
from app.services.contact_state import (
    CONTACT_STATE_DIRECTORY_NAME,
    CONTACT_STATE_SUBDIRECTORY_NAME,
    ContactStateError,
    read_contact_state,
)


# Must remain aligned with deterministic contact routing in chat.py.
# In the next HTTP integration gate, chat.py will import this shared
# constant so there is only one source of truth.
CONTACT_COUNTRY_FALLBACK_CODES: dict[str, str] = {
    "SK": "CZ",
}


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class PublicContactPhoto:
    """Resolved photo bytes after sidecar identity/integrity checks."""

    data: bytes
    content_type: str
    sha256: str


def _requested_codes(
    requested_country_codes: Iterable[str],
    unavailable_country_codes: Iterable[str],
) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()

    for value in (
        *requested_country_codes,
        *unavailable_country_codes,
    ):
        if not isinstance(value, str):
            continue

        code = value.strip().upper()

        if not code or code in seen:
            continue

        seen.add(code)
        result.append(code)

    return result


def _source_country_code(source: object) -> str | None:
    value = getattr(source, "country_code", None)

    if not isinstance(value, str) or not value.strip():
        return None

    return value.strip().upper()


def _source_document_id(source: object) -> str | None:
    value = getattr(source, "document_id", None)

    if not isinstance(value, str) or not value.strip():
        return None

    return value.strip()


def _photo_url(
    *,
    contact_id: str,
    sha256: str | None,
) -> str | None:
    if (
        sha256 is None
        or not _SHA256_RE.fullmatch(sha256)
    ):
        return None

    return (
        "/api/v1/contact-photos/"
        f"{quote(contact_id, safe='')}/"
        f"{sha256}"
    )


def build_legal_chat_contacts(
    *,
    source_directory: Path | None,
    requested_country_codes: Iterable[str],
    unavailable_country_codes: Iterable[str],
    sources: Iterable[object],
) -> list[LegalChatContact]:
    """Build deterministic public cards from contact source sidecars.

    `sources` should be the sources produced by the deterministic
    contact-answer builder, not arbitrary legal-answer sources.

    For a fallback such as SK -> CZ, the business data comes from the
    fallback document while the card remains labelled with the user's
    requested jurisdiction, matching the existing text semantics.
    """

    # Structured cards are an optional representation layered on
    # top of the existing deterministic contact answer. Internal
    # callers such as unit tests may deliberately run without the
    # complete application settings/environment; in that case the
    # existing answer must remain usable and cards simply degrade to
    # an empty list.
    if source_directory is None:
        return []

    source_list = list(sources)
    requested_codes = _requested_codes(
        requested_country_codes,
        unavailable_country_codes,
    )

    result: list[LegalChatContact] = []
    emitted_contacts: set[tuple[str, str]] = set()

    for requested_code in requested_codes:
        own_sources = [
            source
            for source in source_list
            if _source_country_code(source) == requested_code
        ]

        selected_sources = own_sources

        if not selected_sources:
            fallback_code = CONTACT_COUNTRY_FALLBACK_CODES.get(
                requested_code
            )

            if fallback_code is not None:
                selected_sources = [
                    source
                    for source in source_list
                    if _source_country_code(source)
                    == fallback_code
                ]

        seen_documents: set[str] = set()

        for source in selected_sources:
            document_id = _source_document_id(source)

            if (
                document_id is None
                or document_id in seen_documents
            ):
                continue

            seen_documents.add(document_id)

            try:
                state = read_contact_state(
                    source_directory,
                    document_id,
                )
            except (OSError, ContactStateError):
                # Cards are an optional structured representation.
                # Never break an otherwise valid legal/contact answer.
                continue

            if state is None:
                continue

            for record in state.contacts:
                identity = (
                    requested_code,
                    record.contact_id,
                )

                if identity in emitted_contacts:
                    continue

                emitted_contacts.add(identity)

                result.append(
                    LegalChatContact(
                        contact_id=record.contact_id,
                        country_code=requested_code,
                        member_firm=record.member_firm,
                        contact_person=record.contact_person,
                        email=record.email,
                        phone=record.phone,
                        address=record.address,
                        website=record.website,
                        photo_url=_photo_url(
                            contact_id=record.contact_id,
                            sha256=record.photo_sha256,
                        ),
                    )
                )

    return result


def _contact_state_directory(
    source_directory: Path,
) -> Path:
    return (
        Path(source_directory)
        / CONTACT_STATE_DIRECTORY_NAME
        / CONTACT_STATE_SUBDIRECTORY_NAME
    )


def resolve_public_contact_photo(
    *,
    source_directory: Path,
    contact_id: str,
    sha256: str,
) -> PublicContactPhoto | None:
    """Resolve a public image through stable contact identity + SHA.

    The caller never provides a filename. The actual filename comes
    only from a validated ContactRecord sidecar.

    The requested SHA must also match both the sidecar metadata and
    the bytes read from disk.
    """

    if (
        not isinstance(contact_id, str)
        or not contact_id
        or "/" in contact_id
        or "\\" in contact_id
    ):
        return None

    if (
        not isinstance(sha256, str)
        or not _SHA256_RE.fullmatch(sha256)
    ):
        return None

    directory = _contact_state_directory(
        source_directory
    )

    if not directory.is_dir():
        return None

    try:
        state_paths = sorted(
            directory.glob("*.json")
        )
    except OSError:
        return None

    for state_path in state_paths:
        document_id = state_path.stem

        try:
            state = read_contact_state(
                source_directory,
                document_id,
            )
        except (OSError, ContactStateError):
            continue

        if state is None:
            continue

        for record in state.contacts:
            if record.contact_id != contact_id:
                continue

            if (
                record.photo_filename is None
                or record.photo_content_type is None
                or record.photo_sha256 is None
            ):
                return None

            if record.photo_sha256 != sha256:
                return None

            try:
                data = read_contact_photo(
                    source_directory,
                    record.photo_filename,
                )
            except ContactPhotoStorageError:
                return None

            actual_sha256 = hashlib.sha256(
                data
            ).hexdigest()

            if actual_sha256 != sha256:
                return None

            return PublicContactPhoto(
                data=data,
                content_type=record.photo_content_type,
                sha256=sha256,
            )

    return None
