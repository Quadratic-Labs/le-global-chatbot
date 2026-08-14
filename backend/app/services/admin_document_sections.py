"""
Admin section editing (mission "ORDER 5C").

Country -> Section -> current effective content -> Edit -> Save.

OpenSearch treats every section identically regardless of provenance
(section 2/28: "SECTION = SECTION", no manual-edit flag, no special
retrieval handling) - the only thing this module adds beyond the
existing lifecycle primitives is the durable technical record of
which content is currently effective per section (document_section_
state.py), so a later Reindex/restart never silently reverts an edit.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from opensearchpy import OpenSearch
from opensearchpy.exceptions import OpenSearchException

from app.core.country_registry import canonical_country_name
from app.core.legal_taxonomy import get_canonical_legal_topic
from app.models.admin_document_sections import (
    AdminDocumentSectionListResponse,
    AdminDocumentSectionResponse,
    AdminDocumentSectionRestoreResponse,
    AdminDocumentSectionSummary,
    AdminDocumentSectionUpdateResponse,
)
from app.services.admin_document_lifecycle import (
    AdminDocumentLifecycleError,
    AdminDocumentRollbackError,
    _get_document_metadata,
    _required_string,
    _validate_document_id,
)
from app.services.country_lock import (
    DEFAULT_LOCK_TIMEOUT_SECONDS,
    country_lock,
)
from app.services.docx_parser import ParsedSection, parse_docx_sections
from app.services.document_chunk_builder import (
    DocumentMetadata,
    build_document_chunks,
)
from app.services.document_indexer import (
    DEFAULT_BULK_CHUNK_SIZE,
    DocumentIndexingError,
    _restore_section_snapshot,
    _snapshot_document_chunks,
    replace_document_section_chunks,
)
from app.services.document_source_resolver import (
    DocumentSourceConflictError,
    resolve_document_source_path,
)
from app.services.document_section_state import (
    SectionEdit,
    SectionEditState,
    SectionStateError,
    legal_topic_for_section_id,
    read_section_edit_state,
    section_id_for_legal_topic,
    write_section_edit_state_atomic,
)
from app.services.opensearch_index import LEGAL_DOCUMENTS_ALIAS
from app.clients.opensearch import get_opensearch_client
from app.services.section_splitter import (
    DEFAULT_MAX_CHARS,
    split_parsed_sections,
)


class AdminDocumentSectionNotFoundError(LookupError):
    """Raised when a requested section does not exist in the document."""

    def __init__(self, *, document_id: str, section_id: str) -> None:
        self.document_id = document_id
        self.section_id = section_id

        super().__init__(
            f"Section {section_id!r} was not found in document "
            f"{document_id!r}."
        )

    def to_detail(self) -> dict[str, object]:
        return {
            "code": "document_section_not_found",
            "message": str(self),
            "operation": "section_update",
            "document_id": self.document_id,
            "section_id": self.section_id,
        }


class AdminDocumentSectionInvalidError(ValueError):
    """Raised when the submitted section content cannot be accepted."""

    def __init__(self, *, message: str) -> None:
        super().__init__(message)

    def to_detail(self) -> dict[str, object]:
        return {
            "code": "document_section_invalid",
            "message": str(self),
            "operation": "section_update",
        }


class AdminDocumentSectionUpdateFailedError(RuntimeError):
    """Raised when a section could not be saved safely."""

    def __init__(self, *, message: str) -> None:
        super().__init__(message)

    def to_detail(self) -> dict[str, object]:
        return {
            "code": "document_section_update_failed",
            "message": str(self),
            "operation": "section_update",
        }


def _resolve_current_source_path(
    *,
    document_metadata: dict[str, Any],
    source_directory: Path,
) -> Path:
    country_code = _required_string(
        document_metadata,
        "country_code",
    )
    source_filename = _required_string(
        document_metadata,
        "source_filename",
    )

    try:
        resolved = resolve_document_source_path(
            source_root=source_directory,
            country_code=country_code,
            source_filename=source_filename,
        )

    except DocumentSourceConflictError as error:
        raise AdminDocumentLifecycleError(
            "Multiple distinct source files resolve for this "
            "document."
        ) from error

    if resolved.path is None:
        raise AdminDocumentLifecycleError(
            "The source DOCX backing this document is missing."
        )

    return resolved.path


def _real_topics_from_opensearch(
    *,
    client: OpenSearch,
    document_id: str,
) -> set[str]:
    """Every distinct legal_topic among this document's real chunks now."""

    try:
        response = client.search(
            index=LEGAL_DOCUMENTS_ALIAS,
            body={
                "size": 0,
                "query": {"term": {"document_id": document_id}},
                "aggs": {
                    "topics": {
                        "terms": {
                            "field": "legal_topic",
                            "size": 100,
                        }
                    }
                },
            },
        )

    except OpenSearchException as error:
        raise AdminDocumentLifecycleError(
            "OpenSearch topic lookup failed."
        ) from error

    aggregations = response.get("aggregations")

    if not isinstance(aggregations, dict):
        return set()

    topics_agg = aggregations.get("topics")

    if not isinstance(topics_agg, dict):
        return set()

    buckets = topics_agg.get("buckets")

    if not isinstance(buckets, list):
        return set()

    return {
        bucket["key"]
        for bucket in buckets
        if isinstance(bucket, dict) and bucket.get("key")
    }


def _parsed_sections_for_topic(
    *,
    source_path: Path,
    country: str,
    legal_topic: str,
) -> list[Any]:
    parsed_sections = parse_docx_sections(
        source_path,
        country=country,
    )

    matching = []

    for parsed_section in parsed_sections:
        derived_topic = get_canonical_legal_topic(
            section=parsed_section.section,
            country=country,
        )

        if derived_topic == legal_topic:
            matching.append(parsed_section)

    return matching


def list_effective_sections(
    *,
    document_id: str,
    client: OpenSearch | None = None,
) -> AdminDocumentSectionListResponse:
    """
    Every section that really exists in the document's effective
    state right now - never a fixed list of all 11 taxonomy topics
    (mission "ORDER 5C", section 20).
    """

    document_id = _validate_document_id(document_id)

    opensearch_client = client or get_opensearch_client()

    _get_document_metadata(
        document_id=document_id,
        client=opensearch_client,
    )

    topics = sorted(
        _real_topics_from_opensearch(
            client=opensearch_client,
            document_id=document_id,
        )
    )

    return AdminDocumentSectionListResponse(
        document_id=document_id,
        sections=[
            AdminDocumentSectionSummary(
                section_id=section_id_for_legal_topic(topic),
                legal_topic=topic,
            )
            for topic in topics
        ],
    )


def get_effective_section(
    *,
    document_id: str,
    section_id: str,
    source_directory: Path,
    client: OpenSearch | None = None,
) -> AdminDocumentSectionResponse:
    """
    The current EFFECTIVE content of one section - the last saved
    edit if one exists, otherwise the real DOCX's own current text
    for that section, extracted structurally (never a naive join of
    OpenSearch chunk texts - mission "ORDER 5C", section 22).
    """

    document_id = _validate_document_id(document_id)

    legal_topic = legal_topic_for_section_id(section_id)

    if legal_topic is None:
        raise AdminDocumentSectionNotFoundError(
            document_id=document_id,
            section_id=section_id,
        )

    opensearch_client = client or get_opensearch_client()

    document_metadata = _get_document_metadata(
        document_id=document_id,
        client=opensearch_client,
    )

    country_code = _required_string(document_metadata, "country_code")
    country = canonical_country_name(country_code)

    real_topics = _real_topics_from_opensearch(
        client=opensearch_client,
        document_id=document_id,
    )

    try:
        state = read_section_edit_state(source_directory, document_id)
    except SectionStateError as error:
        raise AdminDocumentLifecycleError(str(error)) from error

    existing_edit = (
        state.sections.get(section_id) if state is not None else None
    )

    if existing_edit is not None:
        content = existing_edit.content

    elif legal_topic in real_topics:
        source_path = _resolve_current_source_path(
            document_metadata=document_metadata,
            source_directory=source_directory,
        )

        matching_sections = _parsed_sections_for_topic(
            source_path=source_path,
            country=country,
            legal_topic=legal_topic,
        )

        if not matching_sections:
            raise AdminDocumentSectionNotFoundError(
                document_id=document_id,
                section_id=section_id,
            )

        content = "\n\n".join(
            section.content for section in matching_sections
        )

    else:
        raise AdminDocumentSectionNotFoundError(
            document_id=document_id,
            section_id=section_id,
        )

    return AdminDocumentSectionResponse(
        document_id=document_id,
        country_code=country_code,
        country_name=country,
        section_id=section_id,
        legal_topic=legal_topic,
        content=content,
    )


def update_effective_section(
    *,
    document_id: str,
    section_id: str,
    new_content: str,
    source_directory: Path,
    client: OpenSearch | None = None,
    lock_timeout_seconds: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
) -> AdminDocumentSectionUpdateResponse:
    """
    Save a new effective content for one existing section.

    Follows the mission's own transaction order (section 26): re-read
    current state under lock, verify the section still exists, build
    the new chunks, mutate OpenSearch first, and only once that has
    fully succeeded, atomically commit the state file - never the
    reverse, so a crash/failure between the two never leaves the
    state file claiming content OpenSearch does not actually have.
    """

    document_id = _validate_document_id(document_id)

    trimmed_content = new_content.strip()

    if not trimmed_content:
        raise AdminDocumentSectionInvalidError(
            message="Section content must not be empty."
        )

    legal_topic = legal_topic_for_section_id(section_id)

    if legal_topic is None:
        raise AdminDocumentSectionNotFoundError(
            document_id=document_id,
            section_id=section_id,
        )

    opensearch_client = client or get_opensearch_client()

    # A preliminary, lock-free read only to discover which country
    # this document belongs to - re-verified again immediately below,
    # once the lock is actually held (mission section 26, steps 1-2).
    preliminary_metadata = _get_document_metadata(
        document_id=document_id,
        client=opensearch_client,
    )
    country_code_for_lock = _required_string(
        preliminary_metadata,
        "country_code",
    )

    with country_lock(
        source_directory,
        country_code_for_lock,
        timeout_seconds=lock_timeout_seconds,
    ):
        document_metadata = _get_document_metadata(
            document_id=document_id,
            client=opensearch_client,
        )

        country_code = _required_string(document_metadata, "country_code")

        if country_code != country_code_for_lock:
            raise AdminDocumentLifecycleError(
                "The document's country changed during the "
                "operation."
            )

        country = canonical_country_name(country_code)
        source_filename = _required_string(
            document_metadata, "source_filename"
        )
        reference_year = document_metadata.get("reference_year")

        real_topics = _real_topics_from_opensearch(
            client=opensearch_client,
            document_id=document_id,
        )

        try:
            state = read_section_edit_state(source_directory, document_id)
        except SectionStateError as error:
            raise AdminDocumentLifecycleError(str(error)) from error

        existing_sections = dict(state.sections) if state is not None else {}
        existing_edit = existing_sections.get(section_id)

        if existing_edit is not None:
            section_label = existing_edit.section
            subsection_label = existing_edit.subsection

        elif legal_topic in real_topics:
            source_path = _resolve_current_source_path(
                document_metadata=document_metadata,
                source_directory=source_directory,
            )

            matching_sections = _parsed_sections_for_topic(
                source_path=source_path,
                country=country,
                legal_topic=legal_topic,
            )

            if not matching_sections:
                raise AdminDocumentSectionNotFoundError(
                    document_id=document_id,
                    section_id=section_id,
                )

            section_label = matching_sections[0].section
            subsection_label = matching_sections[0].subsection

        else:
            raise AdminDocumentSectionNotFoundError(
                document_id=document_id,
                section_id=section_id,
            )

        metadata = DocumentMetadata(
            country=country,
            country_code=country_code,
            reference_year=(
                int(reference_year)
                if isinstance(reference_year, int)
                else None
            ),
            language="en",
            source_filename=source_filename,
        )

        parsed_sections = split_parsed_sections(
            [
                ParsedSection(
                    section=section_label,
                    subsection=subsection_label,
                    content=trimmed_content,
                )
            ],
            max_chars=DEFAULT_MAX_CHARS,
        )

        new_chunks = build_document_chunks(parsed_sections, metadata)

        actual_topics = {chunk.legal_topic for chunk in new_chunks}

        if actual_topics != {legal_topic}:
            raise AdminDocumentSectionUpdateFailedError(
                message=(
                    "The edited content did not resolve back to the "
                    "same section - no change was saved."
                )
            )

        # Captured BEFORE any mutation, scoped to exactly this
        # (document_id, legal_topic) section - the exact pre-edit
        # state this whole transaction rolls back to if anything
        # past this point fails, including a durable state-file
        # commit failure AFTER OpenSearch has already succeeded
        # (mission "ORDER 5C" corrective gate, section 1: an Edit has
        # only two allowed outcomes - fully applied, or exactly as
        # before - never OpenSearch=new with persisted state=old).
        #
        # This read can itself fail (a transient OpenSearch error) -
        # nothing has mutated yet at this point, so it is reported
        # through the exact same structured error type as every other
        # failure in this transaction, never as a raw
        # DocumentIndexingError escaping unwrapped past this function
        # and past the router's own except clauses (corrective gate,
        # section 3: no boundary violation, however early it occurs).
        try:
            pre_edit_snapshot = [
                item
                for item in _snapshot_document_chunks(
                    client=opensearch_client,
                    document_id=document_id,
                )
                if item["_source"].get("legal_topic") == legal_topic
            ]

        except DocumentIndexingError as error:
            raise AdminDocumentSectionUpdateFailedError(
                message=(
                    "The section's current content could not be "
                    "read from the search index before saving."
                )
            ) from error

        try:
            indexing_result = replace_document_section_chunks(
                new_chunks,
                legal_topic,
                client=opensearch_client,
            )

        except DocumentIndexingError as error:
            # replace_document_section_chunks is itself internally
            # atomic - on any failure inside it, it has already
            # restored OpenSearch to exactly its own pre-call state
            # before this exception ever reaches here, so no further
            # OpenSearch rollback is needed; the durable state file
            # (not yet touched at this point) is untouched too.
            raise AdminDocumentSectionUpdateFailedError(
                message=(
                    "The section could not be saved to the search "
                    "index."
                )
            ) from error

        new_state = SectionEditState(
            document_id=document_id,
            country_code=country_code,
            sections={
                **existing_sections,
                section_id: SectionEdit(
                    legal_topic=legal_topic,
                    section=section_label,
                    subsection=subsection_label,
                    content=trimmed_content,
                ),
            },
        )

        try:
            write_section_edit_state_atomic(source_directory, new_state)

        except OSError as state_error:
            # OpenSearch already committed the new section content
            # successfully - it is OpenSearch, not the (untouched,
            # atomically-written) state file, that must now be rolled
            # back, so the two never end up disagreeing.
            try:
                _restore_section_snapshot(
                    client=opensearch_client,
                    document_id=document_id,
                    legal_topic=legal_topic,
                    snapshot=pre_edit_snapshot,
                    bulk_chunk_size=DEFAULT_BULK_CHUNK_SIZE,
                )

            except Exception as rollback_error:
                raise AdminDocumentRollbackError(
                    "The section's durable state could not be "
                    "saved, and rolling the search index back to "
                    "its previous content afterward also failed - "
                    "manual recovery is required."
                ) from rollback_error

            raise AdminDocumentSectionUpdateFailedError(
                message=(
                    "The section could not be saved: its durable "
                    "state could not be written, so the search "
                    "index was rolled back to its previous content."
                )
            ) from state_error

        return AdminDocumentSectionUpdateResponse(
            document_id=document_id,
            section_id=section_id,
            legal_topic=legal_topic,
            indexed_chunks=indexing_result.indexed_chunks,
        )


def restore_effective_section(
    *,
    document_id: str,
    section_id: str,
    source_directory: Path,
    client: OpenSearch | None = None,
    lock_timeout_seconds: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
) -> AdminDocumentSectionRestoreResponse:
    """
    Discard any persisted Edit for exactly this one section and
    restore it to the current source DOCX's own real content -
    mission "ORDER 7C": the only supported path back to the DOCX for a
    single section, so recovering from a bad Edit never requires a
    full document Replace/Delete (which would also discard every
    OTHER section's own edits).

    Every real subsection the DOCX currently has for this legal_topic
    is rebuilt through the exact same chunking pipeline a fresh
    upload/reindex uses (split_parsed_sections + build_document_chunks),
    so restored chunks carry their own true subsection labels - never
    the single borrowed label update_effective_section's one-blob
    Edit uses. Follows the same transaction order as
    update_effective_section (mutate OpenSearch first, then commit the
    state file, rolling OpenSearch back if the state commit fails).

    Idempotent: when section_id has no persisted override already, the
    state file is simply rewritten unchanged and OpenSearch already
    holds - or is harmlessly re-indexed to - exactly the DOCX's own
    content; every OTHER section's own persisted edit is carried
    forward untouched either way.
    """

    document_id = _validate_document_id(document_id)

    legal_topic = legal_topic_for_section_id(section_id)

    if legal_topic is None:
        raise AdminDocumentSectionNotFoundError(
            document_id=document_id,
            section_id=section_id,
        )

    opensearch_client = client or get_opensearch_client()

    preliminary_metadata = _get_document_metadata(
        document_id=document_id,
        client=opensearch_client,
    )
    country_code_for_lock = _required_string(
        preliminary_metadata,
        "country_code",
    )

    with country_lock(
        source_directory,
        country_code_for_lock,
        timeout_seconds=lock_timeout_seconds,
    ):
        document_metadata = _get_document_metadata(
            document_id=document_id,
            client=opensearch_client,
        )

        country_code = _required_string(document_metadata, "country_code")

        if country_code != country_code_for_lock:
            raise AdminDocumentLifecycleError(
                "The document's country changed during the "
                "operation."
            )

        country = canonical_country_name(country_code)
        source_filename = _required_string(
            document_metadata, "source_filename"
        )
        reference_year = document_metadata.get("reference_year")

        source_path = _resolve_current_source_path(
            document_metadata=document_metadata,
            source_directory=source_directory,
        )

        matching_sections = _parsed_sections_for_topic(
            source_path=source_path,
            country=country,
            legal_topic=legal_topic,
        )

        if not matching_sections:
            raise AdminDocumentSectionNotFoundError(
                document_id=document_id,
                section_id=section_id,
            )

        metadata = DocumentMetadata(
            country=country,
            country_code=country_code,
            reference_year=(
                int(reference_year)
                if isinstance(reference_year, int)
                else None
            ),
            language="en",
            source_filename=source_filename,
        )

        parsed_sections = split_parsed_sections(
            matching_sections,
            max_chars=DEFAULT_MAX_CHARS,
        )

        new_chunks = build_document_chunks(parsed_sections, metadata)

        actual_topics = {chunk.legal_topic for chunk in new_chunks}

        if actual_topics != {legal_topic}:
            raise AdminDocumentSectionUpdateFailedError(
                message=(
                    "The document's own content did not resolve back "
                    "to the same section - nothing was restored."
                )
            )

        try:
            state = read_section_edit_state(source_directory, document_id)
        except SectionStateError as error:
            raise AdminDocumentLifecycleError(str(error)) from error

        existing_sections = dict(state.sections) if state is not None else {}

        # Captured BEFORE any mutation, exactly like
        # update_effective_section's own pre-edit snapshot - this
        # transaction rolls back to this if anything past this point
        # fails.
        try:
            pre_restore_snapshot = [
                item
                for item in _snapshot_document_chunks(
                    client=opensearch_client,
                    document_id=document_id,
                )
                if item["_source"].get("legal_topic") == legal_topic
            ]

        except DocumentIndexingError as error:
            raise AdminDocumentSectionUpdateFailedError(
                message=(
                    "The section's current content could not be "
                    "read from the search index before restoring."
                )
            ) from error

        try:
            indexing_result = replace_document_section_chunks(
                new_chunks,
                legal_topic,
                client=opensearch_client,
            )

        except DocumentIndexingError as error:
            raise AdminDocumentSectionUpdateFailedError(
                message=(
                    "The section could not be restored in the "
                    "search index."
                )
            ) from error

        # Discard only this section_id's own override - every other
        # persisted edit for this document is carried forward
        # unchanged, and a section_id that never had an override
        # simply leaves the dict unchanged (idempotent).
        remaining_sections = {
            key: value
            for key, value in existing_sections.items()
            if key != section_id
        }

        new_state = SectionEditState(
            document_id=document_id,
            country_code=country_code,
            sections=remaining_sections,
        )

        try:
            write_section_edit_state_atomic(source_directory, new_state)

        except OSError as state_error:
            try:
                _restore_section_snapshot(
                    client=opensearch_client,
                    document_id=document_id,
                    legal_topic=legal_topic,
                    snapshot=pre_restore_snapshot,
                    bulk_chunk_size=DEFAULT_BULK_CHUNK_SIZE,
                )

            except Exception as rollback_error:
                raise AdminDocumentRollbackError(
                    "The section's durable state could not be "
                    "saved, and rolling the search index back to "
                    "its previous content afterward also failed - "
                    "manual recovery is required."
                ) from rollback_error

            raise AdminDocumentSectionUpdateFailedError(
                message=(
                    "The section could not be restored: its durable "
                    "state could not be written, so the search "
                    "index was rolled back to its previous content."
                )
            ) from state_error

        return AdminDocumentSectionRestoreResponse(
            document_id=document_id,
            section_id=section_id,
            legal_topic=legal_topic,
            indexed_chunks=indexing_result.indexed_chunks,
        )
