"""Consolidated service admin_document_replacement.py; includes former admin_documents.py."""
from __future__ import annotations
import os
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Final
from opensearchpy import OpenSearch
from opensearchpy.exceptions import NotFoundError, OpenSearchException
from app.clients.opensearch import get_opensearch_client
from app.models.admin_documents import AdminDocumentListResponse, AdminDocumentStatsResponse, AdminDocumentSummary, AdminDocumentUploadResponse
from app.models.document import DocumentChunk
from app.services.document_chunk_builder import DOCUMENT_FAMILY
from app.services.document_source_resolver import DocumentSourceConflictError, resolve_document_source_path
from app.services.opensearch_index import LEGAL_DOCUMENTS_ALIAS
import hashlib
import shutil
import tempfile
import uuid
import zipfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import BinaryIO
from opensearchpy.exceptions import OpenSearchException
from app.core.admin_country_policy import ADMIN_ALLOWED_COUNTRY_CODES, is_admin_country_allowed
from app.core.country_registry import CountryMetadataMismatchError, UnknownCountryCodeError, canonical_country_name, normalize_country_code
from app.models.admin_documents import AdminDocumentUploadResponse
from app.services.document_section_state import is_admin_modified_since_upload
from app.services.country_lock import DEFAULT_LOCK_TIMEOUT_SECONDS, country_lock
from app.services.document_chunk_builder import InvalidCountryMarkerValueError, read_country_marker, write_country_marker
from app.services.document_chunk_builder import DOCUMENT_FAMILY, AmbiguousDocumentCountryError, InvalidDocxFormatError, UndeterminableDocumentCountryError, build_document_chunks_from_docx, storage_filename_for_country
from app.services.document_indexer import DocumentIndexingError, DocumentIndexingResult, _fetch_all_chunks, replace_country_document_chunks
from app.services.document_section_state import delete_section_edit_state
from app.services.docx_parser import extract_contacts_from_docx
from app.services.document_source_resolver import DocumentSourceConflictError, resolve_country_source_paths, resolve_document_source_path
from collections.abc import Sequence
from app.core.country_registry import canonical_country_name, normalize_country_code
from app.services.document_chunk_builder import storage_filename_for_country
from app.services.document_indexer import DEFAULT_BULK_CHUNK_SIZE, _delete_country_chunks, _restore_country_snapshot, _snapshot_country_chunks
from typing import Final
from app.core.legal_taxonomy import LEGAL_TOPICS
UPLOAD_READ_SIZE: Final[int] = 1024 * 1024
MAX_ADMIN_DOCUMENTS: Final[int] = 1000
MAX_FILENAME_LENGTH: Final[int] = 255

class InvalidDocumentUploadError(ValueError):
    """Raised when an uploaded document is invalid."""

class InvalidExtensionError(InvalidDocumentUploadError):
    """Raised when the uploaded filename is not a .docx file."""

class DocumentEmptyError(InvalidDocumentUploadError):
    """Raised when the uploaded document has zero bytes."""

class DocumentTooLargeError(InvalidDocumentUploadError):
    """
    Raised when the uploaded document exceeds the configured size
    limit - carries the limit itself so the router can report
    max_bytes/max_mb without the client having to already know it.
    """

    def __init__(self, *, maximum_bytes: int) -> None:
        self.maximum_bytes = maximum_bytes
        self.maximum_megabytes = round(maximum_bytes / (1024 * 1024), 2)
        super().__init__(f'The uploaded DOCX exceeds the configured size limit of {self.maximum_megabytes} MB.')

class DocumentCorruptError(InvalidDocumentUploadError):
    """Raised when the uploaded file is not a genuinely valid DOCX."""

class DocumentParseFailedError(InvalidDocumentUploadError):
    """Raised when a structurally valid DOCX could not be parsed."""

class DocumentCountryUndeterminedError(InvalidDocumentUploadError):
    """Raised when no supported country could be resolved from content."""

class AdminDocumentStorageError(RuntimeError):
    """Raised when a document cannot be persisted safely."""

class AdminDocumentCatalogError(RuntimeError):
    """Raised when indexed documents cannot be listed."""

def _sanitize_filename(filename: str) -> str:
    """
    Validate an uploaded source filename for safety only - never for
    a business naming format (mission "CONTINUATION PATCH 0.4.3",
    section 4). Any filename that is non-empty, ends in .docx, has no
    null byte, no path component, and stays within a reasonable
    length is accepted verbatim - spaces, accents, parentheses,
    dashes, and underscores all included.
    """
    if '\x00' in filename:
        raise InvalidDocumentUploadError('The uploaded filename must not contain a null byte.')
    normalized_filename = filename.strip()
    if not normalized_filename:
        raise InvalidDocumentUploadError('The uploaded document has no filename.')
    if len(normalized_filename) > MAX_FILENAME_LENGTH:
        raise InvalidDocumentUploadError('The uploaded filename is too long.')
    basename = normalized_filename.replace('\\', '/').rsplit('/', maxsplit=1)[-1]
    if basename != normalized_filename:
        raise InvalidDocumentUploadError('The uploaded filename must not contain a path.')
    if basename.startswith('~$'):
        raise InvalidDocumentUploadError('Temporary Microsoft Word files are not accepted.')
    if Path(basename).suffix.casefold() != '.docx':
        raise InvalidExtensionError('Only DOCX documents are accepted.')
    return basename

def _write_upload(file_stream: BinaryIO, destination: Path, maximum_bytes: int) -> int:
    """Stream an uploaded file to disk with a size limit."""
    if maximum_bytes <= 0:
        raise ValueError('maximum_bytes must be greater than zero.')
    try:
        file_stream.seek(0)
    except (AttributeError, OSError):
        pass
    written_bytes = 0
    with destination.open('wb') as output_file:
        while True:
            data = file_stream.read(UPLOAD_READ_SIZE)
            if not data:
                break
            if not isinstance(data, bytes):
                raise InvalidDocumentUploadError('The uploaded document did not contain binary data.')
            written_bytes += len(data)
            if written_bytes > maximum_bytes:
                raise DocumentTooLargeError(maximum_bytes=maximum_bytes)
            output_file.write(data)
    if written_bytes == 0:
        raise DocumentEmptyError('The uploaded DOCX is empty.')
    return written_bytes

def _safe_unlink(path: Path) -> None:
    """Remove a temporary file when it exists."""
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass

def build_admin_document_catalog_body() -> dict[str, Any]:
    """Build the indexed-document aggregation request."""
    return {'size': 0, 'aggs': {'documents': {'terms': {'field': 'document_id', 'size': MAX_ADMIN_DOCUMENTS, 'order': {'_key': 'asc'}}, 'aggs': {'metadata': {'top_hits': {'size': 1, '_source': ['document_id', 'source_filename', 'country', 'country_code', 'language', 'document_type', 'reference_year']}}}}}}

def _extract_metadata_source(bucket: dict[str, Any]) -> dict[str, Any]:
    """Extract document metadata from a top-hits bucket."""
    metadata = bucket.get('metadata')
    if not isinstance(metadata, dict):
        raise AdminDocumentCatalogError('OpenSearch returned invalid document metadata.')
    hits_container = metadata.get('hits')
    if not isinstance(hits_container, dict):
        raise AdminDocumentCatalogError('OpenSearch returned invalid document metadata hits.')
    hits = hits_container.get('hits')
    if not isinstance(hits, list) or not hits or (not isinstance(hits[0], dict)):
        raise AdminDocumentCatalogError('OpenSearch returned no document metadata.')
    source = hits[0].get('_source')
    if not isinstance(source, dict):
        raise AdminDocumentCatalogError('OpenSearch returned invalid document source metadata.')
    return source

def _catalog_required_string(source: dict[str, Any], field: str) -> str:
    """Read one required string metadata field."""
    value = source.get(field)
    if not isinstance(value, str) or not value.strip():
        raise AdminDocumentCatalogError(f'Document metadata field is invalid: {field}')
    return value.strip()

def list_indexed_documents(*, source_directory: Path, client: OpenSearch | None=None) -> AdminDocumentListResponse:
    """Return one administration row per indexed document."""
    opensearch_client = client if client is not None else get_opensearch_client()
    try:
        response = opensearch_client.search(index=LEGAL_DOCUMENTS_ALIAS, body=build_admin_document_catalog_body())
    except OpenSearchException as error:
        raise AdminDocumentCatalogError('OpenSearch document catalog request failed.') from error
    if not isinstance(response, dict):
        raise AdminDocumentCatalogError('OpenSearch returned an invalid response.')
    aggregations = response.get('aggregations')
    if not isinstance(aggregations, dict):
        raise AdminDocumentCatalogError('OpenSearch returned no aggregations.')
    documents_aggregation = aggregations.get('documents')
    if not isinstance(documents_aggregation, dict):
        raise AdminDocumentCatalogError('OpenSearch returned no document aggregation.')
    buckets = documents_aggregation.get('buckets')
    if not isinstance(buckets, list):
        raise AdminDocumentCatalogError('OpenSearch returned invalid document buckets.')
    country_document_counts: dict[str, int] = {}
    for bucket in buckets:
        if not isinstance(bucket, dict):
            continue
        bucket_country_code = _extract_metadata_source(bucket).get('country_code')
        if isinstance(bucket_country_code, str) and bucket_country_code.strip():
            normalized_bucket_code = bucket_country_code.strip().upper()
            country_document_counts[normalized_bucket_code] = country_document_counts.get(normalized_bucket_code, 0) + 1
    documents: list[AdminDocumentSummary] = []
    for bucket in buckets:
        if not isinstance(bucket, dict):
            raise AdminDocumentCatalogError('OpenSearch returned an invalid document bucket.')
        source = _extract_metadata_source(bucket)
        source_filename = _catalog_required_string(source, 'source_filename')
        country_code = _catalog_required_string(source, 'country_code')
        try:
            resolved_source = resolve_document_source_path(source_root=source_directory, country_code=country_code, source_filename=source_filename)
            source_file_present = resolved_source.path is not None
            source_bytes = resolved_source.path.stat().st_size if resolved_source.path is not None else None
            updated_at = datetime.fromtimestamp(resolved_source.path.stat().st_mtime, tz=timezone.utc).isoformat() if resolved_source.path is not None else None
            document_status = 'indexed' if source_file_present else 'indexed_source_missing'
        except DocumentSourceConflictError:
            source_file_present = False
            source_bytes = None
            updated_at = None
            document_status = 'indexed_source_conflict'
        reference_year = source.get('reference_year')
        if reference_year is not None:
            reference_year = int(reference_year)
        document_requires_action = country_document_counts.get(country_code.strip().upper(), 0) > 1
        documents.append(AdminDocumentSummary(document_id=_catalog_required_string(source, 'document_id'), source_filename=source_filename, country=_catalog_required_string(source, 'country'), country_code=_catalog_required_string(source, 'country_code'), language=_catalog_required_string(source, 'language'), document_type=_catalog_required_string(source, 'document_type'), reference_year=reference_year, chunk_count=int(bucket.get('doc_count', 0)), source_file_present=source_file_present, source_bytes=source_bytes, updated_at=updated_at, status=document_status, requires_action=document_requires_action, action_reason='country_conflict' if document_requires_action else None, resolution_available=document_requires_action))
    documents.sort(key=lambda document: (document.country.casefold(), document.source_filename.casefold()))
    return AdminDocumentListResponse(total=len(documents), documents=documents)

def get_admin_document_stats(*, source_directory: Path, client: OpenSearch | None=None) -> AdminDocumentStatsResponse:
    """
    Aggregate counts over the real indexed document catalog.

    Deliberately built on top of list_indexed_documents() rather than
    a second, independent OpenSearch aggregation - one source of truth
    means stats can never silently drift from the list itself (mission
    "ORDER 3": "Le calcul doit venir du catalogue réel. Pas
    d'approximation.").
    """
    catalog = list_indexed_documents(source_directory=source_directory, client=client)
    status_counts: dict[str, int] = {}
    for document in catalog.documents:
        status_counts[document.status] = status_counts.get(document.status, 0) + 1
    return AdminDocumentStatsResponse(total_documents=catalog.total, total_countries=len({document.country_code for document in catalog.documents}), status_counts=status_counts, countries_requiring_action=len({document.country_code for document in catalog.documents if document.requires_action}))

def _sorted_admin_allowed_countries() -> list[dict[str, str]]:
    """
    Every country a NEW admin upload may target, for display in a
    SELECT_COUNTRY/invalid-selection decision - sorted by name for a
    stable, deterministic response.
    """
    return sorted(({'code': code, 'name': canonical_country_name(code)} for code in ADMIN_ALLOWED_COUNTRY_CODES), key=lambda option: option['name'])

@dataclass(frozen=True, slots=True)
class ExistingCountryDocument:
    """One distinct indexed document already active for a country."""
    document_id: str
    source_filename: str
    country: str
    country_code: str
    reference_year: int | None

class AdminDocumentUnexpectedCountryError(ValueError):
    """
    Raised when a caller declared which country it expects this
    upload to resolve to (expected_country_code) and the document's
    own detected country is a different one.

    Used exclusively by the REPLACE_WITH_DOCUMENT conflict-resolution
    path (mission "ORDER 8E-A1", section 35): the Admin is resolving a
    specific country's conflict, and the browser's own claim about
    which country that is must never be trusted blindly - the
    uploaded DOCX's real, freshly-detected country is checked against
    it here, before any mutation, so a mismatched file can never
    silently resolve the wrong country's conflict (or worse, silently
    perform an unrelated fresh upload/replace for whatever country it
    actually turned out to be).
    """

    def __init__(self, *, expected_country_code: str, detected_country_code: str, detected_country: str) -> None:
        self.expected_country_code = expected_country_code
        self.detected_country_code = detected_country_code
        self.detected_country = detected_country
        super().__init__(f"This upload was expected to resolve {expected_country_code}, but the document's own detected country is {detected_country} ({detected_country_code}).")

    def to_detail(self) -> dict[str, object]:
        """Return a structured HTTP 422 payload."""
        return {'code': 'document_unexpected_country', 'message': str(self), 'operation': 'upload', 'expected_country_code': self.expected_country_code, 'detected_country_code': self.detected_country_code, 'detected_country_name': self.detected_country}

class AdminDocumentCountryNotAllowedError(ValueError):
    """
    Raised when a document's country is correctly detected but is not
    on the ADMIN upload allowlist (app.core.admin_country_policy).

    Deliberately a distinct error from DocumentCountryUndeterminedError
    (mission "ORDER 5C": a country the registry could not identify at
    all, and a country identified perfectly but not currently
    accepted for new uploads, are different failures with different
    remediations - conflating them into one generic "undetermined"
    message would hide which one actually happened).
    """

    def __init__(self, *, country: str, country_code: str) -> None:
        self.country = country
        self.country_code = country_code
        super().__init__(f'{country} ({country_code}) is not currently accepted for new document uploads.')

    def to_detail(self) -> dict[str, object]:
        """Return a structured HTTP 422 payload."""
        return {'code': 'document_country_not_allowed', 'message': str(self), 'operation': 'upload', 'country_code': self.country_code, 'country_name': self.country}

class AdminDocumentReplacementRequiredError(ValueError):
    """Raised when an existing country needs explicit admin approval."""

    def __init__(self, *, country: str, country_code: str, existing_documents: Sequence[ExistingCountryDocument], admin_modified: bool=False) -> None:
        self.country = country
        self.country_code = country_code
        self.existing_documents = tuple(existing_documents)
        self.admin_modified = admin_modified
        super().__init__(f'A document already exists for {country}. Confirm replacement to keep the uploaded DOCX as the only active version for this country.')

    def to_detail(self) -> dict[str, object]:
        """
        Return a structured HTTP 409 payload.

        admin_modified (mission "ORDER 8G-B2", section 12) - True when
        ANY existing document for this country has been changed
        through Admin (a section or contact mutation) since its last
        accepted DOCX upload. Lets the ONE existing replacement-
        confirmation dialog compose the additional "this will discard
        your Admin changes" warning, rather than a second, separate
        modal.
        """
        return {'code': 'document_replacement_required', 'message': str(self), 'country': self.country, 'country_code': self.country_code, 'existing_document_ids': [document.document_id for document in self.existing_documents], 'admin_modified': self.admin_modified}

class AdminDocumentAlreadyCurrentError(ValueError):
    """Raised when the uploaded bytes already match the active source."""

    def __init__(self, *, country: str, country_code: str) -> None:
        self.country = country
        self.country_code = country_code
        super().__init__(f'The uploaded DOCX is identical to the current {country} source. No reindexing was performed.')

    def to_detail(self) -> dict[str, object]:
        """Return a structured HTTP 409 payload."""
        return {'code': 'document_already_current', 'message': str(self), 'country': self.country, 'country_code': self.country_code}

class AdminDocumentIdenticalButAdminModifiedError(ValueError):
    """
    Raised instead of AdminDocumentAlreadyCurrentError when the
    uploaded bytes are byte-identical to the active source, but the
    country's document has Admin changes recorded since that source
    was last accepted (mission "ORDER 8G-B2", section 14).

    The ordinary "already up to date" short-circuit would otherwise
    silently end the workflow before the Admin can decide whether to
    discard those changes - this error carries exactly what the
    replacement-confirmation dialog needs to offer that decision
    instead. confirm_contact_reseed=True on a resubmission proceeds
    via reseed_contacts_from_current_docx (never re-touching the DOCX
    bytes, which are already correct).
    """

    def __init__(self, *, country: str, country_code: str, document_id: str) -> None:
        self.country = country
        self.country_code = country_code
        self.document_id = document_id
        super().__init__(f'The uploaded DOCX is identical to the current {country} source, but this document has changes made in the Admin. Confirm to discard those changes and reseed from this DOCX, or cancel to keep them.')

    def to_detail(self) -> dict[str, object]:
        """Return a structured HTTP 409 payload."""
        return {'code': 'document_identical_but_admin_modified', 'message': str(self), 'country': self.country, 'country_code': self.country_code, 'document_id': self.document_id, 'admin_modified': True}

class AdminDocumentWarningConfirmationRequiredError(ValueError):
    """
    Raised when a document parses successfully but its topic coverage
    warrants admin confirmation (confirm_warnings=True) before it is
    indexed - never raised together with AdminDocumentAlreadyCurrentError
    (an identical re-upload is always a no-op, regardless of warnings)
    and never in place of AdminDocumentReplacementRequiredError when no
    warning applies (mission "ORDER 3", section 14: that simpler,
    already-supported contract must stay unchanged when it is the only
    pending decision).
    """

    def __init__(self, *, country: str, country_code: str, warnings: Sequence[TopicCoverageWarning], replacement_required: bool, existing_document_ids: Sequence[str], admin_modified: bool=False) -> None:
        self.country = country
        self.country_code = country_code
        self.warnings = tuple(warnings)
        self.replacement_required = replacement_required
        self.existing_document_ids = tuple(existing_document_ids)
        self.admin_modified = admin_modified
        super().__init__('The document is technically valid but its content requires confirmation before indexing. Set confirm_warnings=true to proceed.')

    def to_detail(self) -> dict[str, object]:
        """
        Return a structured HTTP 409 payload.

        admin_modified (mission "ORDER 8G-B2", section 12) - carried
        here too, not just on AdminDocumentReplacementRequiredError:
        this "combined" warning+replacement path is reachable even
        when a country is admin-modified (a topic-coverage warning and
        a pending replacement can both apply to the same upload), and
        without this field admin.js's adminModifiedWarningHtml() has
        nothing to render here, silently dropping the "this will
        discard your Admin changes" notice on confirm (found via the
        real-Chromium canary, mission "ORDER 8G-B2" section 26).
        """
        return {'code': 'document_warning_confirmation_required', 'message': str(self), 'operation': 'upload', 'country_code': self.country_code, 'country_name': self.country, 'replacement_required': self.replacement_required, 'existing_document_ids': list(self.existing_document_ids), 'admin_modified': self.admin_modified, 'warnings': [{'code': warning.code, 'message': warning.message, 'recognized_topics_count': warning.recognized_topics_count, 'expected_topics_count': warning.expected_topics_count, 'missing_topics': list(warning.missing_topics)} for warning in self.warnings]}

class AdminDocumentCountryConfirmationRequiredError(ValueError):
    """
    Raised when a country was detected - from the DOCX's own content
    or from a previously-persisted DOCX-native marker - but has not
    yet been explicitly confirmed by the Admin.

    Mission "ORDER 8E-A1", section 6: a detected country must never,
    by itself, cause any mutation. Cancel (never re-submitting with
    country_confirmed=true) leaves this upload with zero effect - the
    staged file lives only inside the request's own TemporaryDirectory
    and is discarded automatically.

    Also carries the full admin-allowed country list (mission "ORDER
    8E-A2", section 6): a UI must let the Admin correct a wrong
    detection without IT support, which means offering a "choose a
    different country" dropdown right from this same response, using
    the one authoritative server-side list rather than a second,
    client-invented copy of it.
    """

    def __init__(self, *, country: str, country_code: str, detection_source: str) -> None:
        self.country = country
        self.country_code = country_code
        self.detection_source = detection_source
        super().__init__(f'This document was detected as {country} ({country_code}). Confirm the country before it is processed further.')

    def to_detail(self) -> dict[str, object]:
        """Return a structured HTTP 409 payload."""
        return {'code': 'document_country_confirmation_required', 'message': str(self), 'operation': 'upload', 'country_code': self.country_code, 'country_name': self.country, 'detection_source': self.detection_source, 'allowed_countries': _sorted_admin_allowed_countries()}

class AdminDocumentCountrySelectionRequiredError(ValueError):
    """
    Raised when a technically valid, processable DOCX has no country
    that could be automatically identified from its own content or any
    existing marker.

    Mission "ORDER 8E-A1", section 8: this is never a hard failure any
    more - the Admin may instead pick one explicitly from the allowed
    upload list (selected_country_code), which is itself sufficient
    confirmation - no separate country_confirmed round-trip is needed
    for a country the Admin just explicitly chose.
    """

    def __init__(self) -> None:
        self.allowed_countries = _sorted_admin_allowed_countries()
        super().__init__("This document's country could not be automatically identified. Select the correct country to continue.")

    def to_detail(self) -> dict[str, object]:
        """Return a structured HTTP 409 payload."""
        return {'code': 'document_country_selection_required', 'message': str(self), 'operation': 'upload', 'allowed_countries': self.allowed_countries}

class AdminDocumentCountrySelectionInvalidError(ValueError):
    """
    Raised when an Admin-supplied selected_country_code is not on the
    admin upload allowlist.

    Deliberately distinct from AdminDocumentCountryNotAllowedError
    (mission "ORDER 8E-A1", section 9): that one reports a country
    genuinely *detected* from real content; this one reports a manual
    selection value the server never even attempted to trust as
    content - the request is rejected outright, with zero mutation.
    """

    def __init__(self, *, country_code: str) -> None:
        self.country_code = country_code
        super().__init__(f'{country_code!r} is not a supported country for manual selection.')

    def to_detail(self) -> dict[str, object]:
        """Return a structured HTTP 422 payload."""
        return {'code': 'document_country_selection_invalid', 'message': str(self), 'operation': 'upload', 'country_code': self.country_code, 'allowed_countries': _sorted_admin_allowed_countries()}

@dataclass(frozen=True, slots=True)
class CountryConflictCandidate:
    """One safe, business-facing candidate in a country conflict review."""
    document_id: str
    source_filename: str
    reference_year: int | None
    updated_at: str | None
    source_bytes: int | None

class AdminDocumentCountryConflictReviewRequiredError(ValueError):
    """
    Raised when a country already has more than one active indexed
    document - a broken state the ordinary upload/replace decision
    must never try to blindly resolve.

    Mission "ORDER 8E-A1", section 18: "never guess" - resolving a
    genuine conflict requires the dedicated, generic conflict-
    resolution API (see admin_document_conflict_resolution.py), never
    this upload endpoint silently picking one document to keep.
    """

    def __init__(self, *, country: str, country_code: str, candidates: Sequence[CountryConflictCandidate]) -> None:
        self.country = country
        self.country_code = country_code
        self.candidates = tuple(candidates)
        super().__init__(f'{country} currently has {len(self.candidates)} active documents. Review and resolve this conflict before uploading.')

    def to_detail(self) -> dict[str, object]:
        """Return a structured HTTP 409 payload."""
        return {'code': 'document_country_conflict_review_required', 'message': str(self), 'operation': 'upload', 'country': self.country, 'country_code': self.country_code, 'candidates': [{'document_id': candidate.document_id, 'source_filename': candidate.source_filename, 'reference_year': candidate.reference_year, 'updated_at': candidate.updated_at, 'source_bytes': candidate.source_bytes} for candidate in self.candidates]}
ChunkBuilder = Callable[[Path], list[DocumentChunk]]
CountryDocumentLookup = Callable[[str, OpenSearch | None], list[ExistingCountryDocument]]
CountryDocumentIndexer = Callable[..., DocumentIndexingResult]

def _required_string(source: dict[str, object], field: str) -> str:
    """Read one required string from OpenSearch metadata."""
    value = source.get(field)
    if not isinstance(value, str) or not value.strip():
        raise AdminDocumentStorageError(f'Indexed document metadata is invalid: {field}.')
    return value.strip()

def lookup_existing_country_documents(country_code: str, client: OpenSearch | None=None) -> list[ExistingCountryDocument]:
    """Return distinct indexed documents for one detected country."""
    normalized_country_code = country_code.strip().upper()
    if not normalized_country_code:
        raise ValueError('country_code must not be empty.')
    opensearch_client = client if client is not None else get_opensearch_client()
    try:
        hits = _fetch_all_chunks(client=opensearch_client, field='country_code', value=normalized_country_code)
    except DocumentIndexingError as error:
        raise AdminDocumentStorageError('OpenSearch returned an invalid country lookup response.') from error
    documents_by_id: dict[str, ExistingCountryDocument] = {}
    for hit in hits:
        if not isinstance(hit, dict):
            raise AdminDocumentStorageError('OpenSearch returned an invalid country hit.')
        source = hit.get('_source')
        if not isinstance(source, dict):
            raise AdminDocumentStorageError('OpenSearch returned invalid country metadata.')
        document_id = _required_string(source, 'document_id')
        if document_id in documents_by_id:
            continue
        indexed_country_code = _required_string(source, 'country_code').upper()
        if indexed_country_code != normalized_country_code:
            raise AdminDocumentStorageError('OpenSearch returned metadata for a different country.')
        reference_year = source.get('reference_year')
        if reference_year is not None:
            try:
                reference_year = int(reference_year)
            except (TypeError, ValueError) as error:
                raise AdminDocumentStorageError('Indexed reference_year metadata is invalid.') from error
        documents_by_id[document_id] = ExistingCountryDocument(document_id=document_id, source_filename=_required_string(source, 'source_filename'), country=_required_string(source, 'country'), country_code=indexed_country_code, reference_year=reference_year)
    return [documents_by_id[document_id] for document_id in sorted(documents_by_id)]

def _sha256_file(path: Path) -> str:
    """Return the SHA-256 digest of one file."""
    digest = hashlib.sha256()
    with path.open('rb') as file_handle:
        while True:
            block = file_handle.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()

def _restore_backups(backups: Sequence[tuple[Path, Path]]) -> None:
    """Restore every source path moved to a temporary backup."""
    for original_path, backup_path in reversed(backups):
        if backup_path.exists():
            os.replace(backup_path, original_path)

def _build_conflict_candidates(existing_documents: Sequence[ExistingCountryDocument], *, source_directory: Path, country_code: str) -> list[CountryConflictCandidate]:
    """
    Build the safe, business-facing candidate list for a country
    conflict review - filename/year/last-updated/file size only, never
    a business-meaningful use of document_id (mission "ORDER 8E-A1",
    section 22).

    A per-document source-resolution conflict (two distinct metadata
    fields pointing at two different real files for the very same
    document_id - a different, narrower conflict than the country-
    level one this function itself is building a review for) is
    reported as a simply-absent source file rather than raised, so one
    damaged candidate never prevents the Admin from reviewing the rest.
    """
    candidates: list[CountryConflictCandidate] = []
    for document in existing_documents:
        try:
            resolved = resolve_document_source_path(source_root=source_directory, country_code=country_code, source_filename=document.source_filename)
            path = resolved.path
        except DocumentSourceConflictError:
            path = None
        candidates.append(CountryConflictCandidate(document_id=document.document_id, source_filename=document.source_filename, reference_year=document.reference_year, updated_at=datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat() if path is not None else None, source_bytes=path.stat().st_size if path is not None else None))
    return candidates

def safe_upload_and_index_document(*, filename: str, file_stream: BinaryIO, source_directory: Path, processed_directory: Path, maximum_bytes: int, replace_existing: bool=False, confirm_warnings: bool=False, country_confirmed: bool=False, selected_country_code: str | None=None, resolve_country_conflict: bool=False, confirm_contact_reseed: bool=False, expected_country_code: str | None=None, client: OpenSearch | None=None, chunk_builder: ChunkBuilder=build_document_chunks_from_docx, country_document_lookup: CountryDocumentLookup=lookup_existing_country_documents, country_document_indexer: CountryDocumentIndexer=replace_country_document_chunks, lock_timeout_seconds: float=DEFAULT_LOCK_TIMEOUT_SECONDS) -> AdminDocumentUploadResponse:
    """
    Upload one DOCX with explicit country-level replacement approval.

    file_stream is staged and chunk_builder is invoked exactly once,
    regardless of whether the detected country is brand new or already
    active - a fresh country and a confirmed replacement share the
    exact same write-then-index tail below (country_document_indexer
    tolerates a country with zero prior chunks - see
    replace_country_document_chunks's own snapshot/delete-stale logic -
    so there is no separate "fresh" indexing implementation to fall
    back to). Earlier versions re-staged and re-parsed the upload a
    second time through a delegated legacy implementation for a fresh
    country, which depended on file_stream still being fully re-
    readable after already being consumed once - fragile by
    construction, and never necessary in the first place (mission
    "HOTFIX 0.4.9").

    An existing country is never changed unless replace_existing=True.

    A document that parses successfully but whose topic coverage is
    atypical (see app.services.admin_document_replacement) is not indexed
    unless confirm_warnings=True is also passed - the warning itself
    is always recomputed here, from the real uploaded bytes, never
    trusted from the caller (mission "ORDER 3", section 13).

    Mission "ORDER 8E-A1": a detected country - whether from the DOCX's
    own content or a previously-persisted marker - is never enough to
    proceed on its own; country_confirmed=True is required first
    (AdminDocumentCountryConfirmationRequiredError otherwise). When no
    country can be detected at all, this is no longer a hard failure:
    selected_country_code lets the Admin pick one explicitly from the
    allowed upload list - itself sufficient confirmation, since picking
    is confirming - and that choice is persisted into the DOCX itself
    as a DOCX-native custom property (see docx_country_marker.py)
    before anything else runs, so every later step (chunk parsing,
    already-current comparison, the stored source file) is built from
    that one normalized, marker-carrying candidate rather than the raw
    upload bytes.

    More than one active document for a country is a genuine conflict
    (see AdminDocumentCountryConflictReviewRequiredError) that this
    function refuses to guess through on an ordinary upload.
    resolve_country_conflict=True is the one exception: the dedicated
    REPLACE_WITH_DOCUMENT resolution mode (see
    admin_document_conflict_resolution.py) reuses this exact function
    - same technical validation, same country confirmation, same
    content-suitability warning - to let the Admin supply an
    authoritative DOCX that collapses every existing document for the
    country down to this one, exactly like a confirmed single-document
    replace already does. Never set by the ordinary upload endpoint.

    Every read/decide/mutate step from the country lookup onward runs
    under a per-country lock (country_lock) - never held during
    chunk_builder's own parsing, which does not touch shared state.
    """
    safe_filename = _sanitize_filename(filename)
    try:
        source_directory.mkdir(parents=True, exist_ok=True)
        processed_directory.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='document-upload-', dir=processed_directory) as temporary_directory:
            staged_path = Path(temporary_directory) / safe_filename
            uploaded_bytes = _write_upload(file_stream=file_stream, destination=staged_path, maximum_bytes=maximum_bytes)
            candidate_path = staged_path
            manual_selection_used = False
            if selected_country_code is not None and selected_country_code.strip():
                raw_selected_code = selected_country_code.strip().upper()
                if not is_admin_country_allowed(raw_selected_code):
                    raise AdminDocumentCountrySelectionInvalidError(country_code=raw_selected_code)
                try:
                    normalized_selected_code = normalize_country_code(raw_selected_code)
                except UnknownCountryCodeError as error:
                    raise AdminDocumentCountrySelectionInvalidError(country_code=raw_selected_code) from error
                candidate_directory = Path(temporary_directory) / 'country-marker'
                candidate_directory.mkdir(parents=True, exist_ok=True)
                candidate_path = candidate_directory / safe_filename
                try:
                    write_country_marker(staged_path, candidate_path, country_code=normalized_selected_code, country_name=canonical_country_name(normalized_selected_code))
                except InvalidCountryMarkerValueError as error:
                    raise AdminDocumentCountrySelectionInvalidError(country_code=raw_selected_code) from error
                except (zipfile.BadZipFile, KeyError, OSError) as error:
                    raise DocumentCorruptError(f'DOCX validation failed: {error}') from error
                manual_selection_used = True
            try:
                chunks = chunk_builder(candidate_path)
            except InvalidDocxFormatError as error:
                raise DocumentCorruptError(f'DOCX validation failed: {error}') from error
            except (UndeterminableDocumentCountryError, AmbiguousDocumentCountryError) as error:
                if manual_selection_used:
                    raise DocumentParseFailedError(f'DOCX validation failed: {error}') from error
                raise AdminDocumentCountrySelectionRequiredError() from error
            except CountryMetadataMismatchError as error:
                raise DocumentCountryUndeterminedError(f'DOCX validation failed: {error}') from error
            except Exception as error:
                raise DocumentParseFailedError(f'DOCX validation failed: {error}') from error
            if not chunks:
                raise DocumentParseFailedError('The uploaded DOCX produced no legal chunks.')
            first_chunk = chunks[0]
            country_code = first_chunk.country_code.strip().upper()
            if not is_admin_country_allowed(country_code):
                raise AdminDocumentCountryNotAllowedError(country=first_chunk.country, country_code=country_code)
            if expected_country_code is not None and country_code != expected_country_code.strip().upper():
                raise AdminDocumentUnexpectedCountryError(expected_country_code=expected_country_code.strip().upper(), detected_country_code=country_code, detected_country=first_chunk.country)
            if not (country_confirmed or manual_selection_used):
                raise AdminDocumentCountryConfirmationRequiredError(country=first_chunk.country, country_code=country_code, detection_source='marker' if read_country_marker(candidate_path) is not None else 'content')
            topic_warning = evaluate_topic_coverage(chunks)
            with country_lock(source_directory, country_code, timeout_seconds=lock_timeout_seconds):
                try:
                    existing_documents = country_document_lookup(country_code, client)
                except OpenSearchException as error:
                    raise AdminDocumentStorageError('The existing country catalog could not be checked before upload.') from error
                existing_paths = resolve_country_source_paths(source_root=source_directory, country_code=country_code, source_filenames=[document.source_filename for document in existing_documents])
                unique_document_ids = {document.document_id for document in existing_documents}
                if len(unique_document_ids) > 1 and (not resolve_country_conflict):
                    raise AdminDocumentCountryConflictReviewRequiredError(country=first_chunk.country, country_code=country_code, candidates=_build_conflict_candidates(existing_documents, source_directory=source_directory, country_code=country_code))
                if len(unique_document_ids) == 1 and len(existing_paths) == 1 and (_sha256_file(candidate_path) == _sha256_file(existing_paths[0])):
                    existing_document_id = next(iter(unique_document_ids))
                    if is_admin_modified_since_upload(source_directory, existing_document_id):
                        if not confirm_contact_reseed:
                            raise AdminDocumentIdenticalButAdminModifiedError(country=first_chunk.country, country_code=country_code, document_id=existing_document_id)
                        from app.services.admin_contacts import _reseed_contacts_from_current_docx_locked
                        contacts_response = _reseed_contacts_from_current_docx_locked(validated_document_id=existing_document_id, source_directory=source_directory, opensearch_client=client if client is not None else get_opensearch_client())
                        return AdminDocumentUploadResponse(status='contacts_reseeded', document_id=existing_document_id, source_filename=existing_documents[0].source_filename, country=first_chunk.country, country_code=country_code, reference_year=existing_documents[0].reference_year, document_family=DOCUMENT_FAMILY, uploaded_bytes=uploaded_bytes, indexed_chunks=0, stale_chunks_deleted=0, replaced_source_file=False, replaced_document_ids=[], contact_count=len(contacts_response.contacts))
                    raise AdminDocumentAlreadyCurrentError(country=first_chunk.country, country_code=country_code)
                replacement_pending = bool(existing_documents) and (not (replace_existing or resolve_country_conflict))
                if topic_warning is not None and (not confirm_warnings):
                    raise AdminDocumentWarningConfirmationRequiredError(country=first_chunk.country, country_code=country_code, warnings=[topic_warning], replacement_required=replacement_pending, existing_document_ids=sorted(unique_document_ids), admin_modified=any((is_admin_modified_since_upload(source_directory, document.document_id) for document in existing_documents)))
                if replacement_pending:
                    raise AdminDocumentReplacementRequiredError(country=first_chunk.country, country_code=country_code, existing_documents=existing_documents, admin_modified=any((is_admin_modified_since_upload(source_directory, document.document_id) for document in existing_documents)))
                operation_id = uuid.uuid4().hex
                storage_filename = storage_filename_for_country(country_code)
                final_path = source_directory / storage_filename
                incoming_path = source_directory / f'.{operation_id}.{storage_filename}.incoming'
                backups: list[tuple[Path, Path]] = []
                new_final_installed = False
                shutil.copyfile(candidate_path, incoming_path)
                try:
                    for existing_path in existing_paths:
                        backup_path = existing_path.parent / f'.{operation_id}.{existing_path.name}.backup'
                        os.replace(existing_path, backup_path)
                        backups.append((existing_path, backup_path))
                    os.replace(incoming_path, final_path)
                    new_final_installed = True
                    indexing_result = country_document_indexer(chunks=chunks, client=client)
                except Exception:
                    if new_final_installed:
                        _safe_unlink(final_path)
                    _safe_unlink(incoming_path)
                    _restore_backups(backups)
                    raise
                for _, backup_path in backups:
                    _safe_unlink(backup_path)
                if existing_documents:
                    try:
                        for old_document_id in unique_document_ids:
                            delete_section_edit_state(source_directory, old_document_id)
                    except OSError as error:
                        raise AdminDocumentStorageError('The document was replaced, but its previous section-edit state could not be fully cleared.') from error
                from app.services.admin_contacts import reseed_contact_state_from_parsed_contacts
                from app.services.contact_state import ContactPhotoStorageError
                newly_parsed_contacts = extract_contacts_from_docx(candidate_path, country=first_chunk.country)
                try:
                    reseed_contact_state_from_parsed_contacts(document_id=indexing_result.document_id, country_code=country_code, source_directory=source_directory, contacts=newly_parsed_contacts, docx_path=candidate_path)
                except (OSError, ContactPhotoStorageError) as error:
                    raise AdminDocumentStorageError('The document was uploaded, but its contact state could not be seeded from the new DOCX.') from error
            return AdminDocumentUploadResponse(status='replaced' if existing_documents else 'uploaded', document_id=indexing_result.document_id, source_filename=safe_filename, country=first_chunk.country, country_code=country_code, reference_year=first_chunk.reference_year, document_family=DOCUMENT_FAMILY, uploaded_bytes=uploaded_bytes, indexed_chunks=indexing_result.indexed_chunks, stale_chunks_deleted=indexing_result.stale_chunks_deleted, replaced_source_file=bool(existing_paths), replaced_document_ids=sorted(unique_document_ids), contact_count=len(newly_parsed_contacts))
    except (InvalidDocumentUploadError, AdminDocumentReplacementRequiredError, AdminDocumentAlreadyCurrentError, ValueError):
        raise
    except OSError as error:
        raise AdminDocumentStorageError('The uploaded document could not be persisted safely.') from error
AUTO_DEDUPLICATE = 'AUTO_DEDUPLICATE'
CHOOSE_DOCUMENT = 'CHOOSE_DOCUMENT'
RESOLUTION_MODES = (AUTO_DEDUPLICATE, CHOOSE_DOCUMENT)

class CountryConflictNotFoundError(ValueError):
    """
    Raised when a resolution (or review) is requested for a country
    that does not currently have more than one active document -
    there is nothing to resolve, and this is never silently treated
    as a success.
    """

    def __init__(self, *, country_code: str) -> None:
        self.country_code = country_code
        super().__init__(f'{country_code} is not currently in a conflict state - there is nothing to resolve.')

    def to_detail(self) -> dict[str, object]:
        return {'code': 'country_conflict_not_found', 'message': str(self), 'country_code': self.country_code}

class CountryConflictResolutionError(ValueError):
    """
    Raised for an invalid, stale, or unsupported resolution request -
    e.g. an unknown resolution_mode, an AUTO_DEDUPLICATE request for a
    country whose conflicting records lack strong same-source
    evidence, or a keep_document_id that no longer matches the
    country's current candidates (the conflict state changed between
    the Admin's review and this request - never trusted as still
    valid without revalidating immediately before mutation).
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)

    def to_detail(self) -> dict[str, object]:
        return {'code': 'country_conflict_resolution_invalid', 'message': str(self)}

@dataclass(frozen=True, slots=True)
class CountryConflictReview:
    """A safe, business-facing snapshot of one country's conflict."""
    country_code: str
    country: str
    candidates: tuple[CountryConflictCandidate, ...]
    auto_deduplicate_available: bool

@dataclass(frozen=True, slots=True)
class CountryConflictResolutionResult:
    """The outcome of one successful conflict resolution."""
    country_code: str
    resolution_mode: str
    kept_document_id: str
    removed_document_ids: tuple[str, ...]
    stale_chunks_deleted: int

def _resolved_paths_by_document(existing_documents: Sequence[ExistingCountryDocument], *, source_directory: Path, country_code: str) -> dict[str, Path | None]:
    """Each existing document's own resolved source path, or None."""
    resolved: dict[str, Path | None] = {}
    for document in existing_documents:
        try:
            resolved_source = resolve_document_source_path(source_root=source_directory, country_code=country_code, source_filename=document.source_filename)
            resolved[document.document_id] = resolved_source.path
        except DocumentSourceConflictError:
            resolved[document.document_id] = None
    return resolved

def _auto_deduplicate_keep_document_id(existing_documents: Sequence[ExistingCountryDocument], *, source_directory: Path, country_code: str) -> str | None:
    """
    Return the document_id to keep under AUTO_DEDUPLICATE, or None
    when the strong, generic same-source evidence it requires is
    absent.

    Evidence: every real file resolvable for this country (however
    many distinct on-disk paths that is) shares one identical SHA-256
    digest - proving every conflicting record is backed by the same
    physical, current DOCX content. A document whose own filename does
    not resolve to any of those files is never selected as the one to
    keep, even if it would otherwise win the tie-break below.
    """
    existing_paths = resolve_country_source_paths(source_root=source_directory, country_code=country_code, source_filenames=[document.source_filename for document in existing_documents])
    if not existing_paths:
        return None
    distinct_shas = {_sha256_file(path) for path in existing_paths}
    if len(distinct_shas) != 1:
        return None
    resolved_by_document = _resolved_paths_by_document(existing_documents, source_directory=source_directory, country_code=country_code)
    eligible = [document for document in existing_documents if resolved_by_document.get(document.document_id) is not None]
    if not eligible:
        return None
    canonical_name = storage_filename_for_country(country_code)
    canonical_matches = [document for document in eligible if document.source_filename == canonical_name]
    if len(canonical_matches) == 1:
        return canonical_matches[0].document_id
    with_year = [document for document in eligible if document.reference_year is not None]
    if with_year:
        best_year = max((document.reference_year for document in with_year))
        best_year_ids = sorted((document.document_id for document in with_year if document.reference_year == best_year))
        return best_year_ids[-1]
    return sorted((document.document_id for document in eligible))[-1]

def build_country_conflict_review(country_code: str, *, source_directory: Path, client: OpenSearch | None=None, country_document_lookup=lookup_existing_country_documents) -> CountryConflictReview:
    """
    Return a read-only, safe review of one country's current conflict.

    Raises CountryConflictNotFoundError when the country does not
    currently have more than one active document.
    """
    normalized_code = normalize_country_code(country_code)
    existing_documents = country_document_lookup(normalized_code, client)
    unique_ids = {document.document_id for document in existing_documents}
    if len(unique_ids) <= 1:
        raise CountryConflictNotFoundError(country_code=normalized_code)
    keep_id = _auto_deduplicate_keep_document_id(existing_documents, source_directory=source_directory, country_code=normalized_code)
    return CountryConflictReview(country_code=normalized_code, country=canonical_country_name(normalized_code), candidates=tuple(_build_conflict_candidates(existing_documents, source_directory=source_directory, country_code=normalized_code)), auto_deduplicate_available=keep_id is not None)

def resolve_country_conflict(country_code: str, resolution_mode: str, *, source_directory: Path, keep_document_id: str | None=None, client: OpenSearch | None=None, country_document_lookup=lookup_existing_country_documents, bulk_chunk_size: int=DEFAULT_BULK_CHUNK_SIZE, lock_timeout_seconds: float=DEFAULT_LOCK_TIMEOUT_SECONDS) -> CountryConflictResolutionResult:
    """
    Resolve a country's conflict via AUTO_DEDUPLICATE or
    CHOOSE_DOCUMENT (REPLACE_WITH_DOCUMENT is a distinct upload call -
    see safe_upload_and_index_document's resolve_country_conflict
    flag, which reuses the exact same validation flow as a normal
    upload rather than a second implementation here).

    The conflict is revalidated immediately before mutation, under the
    country's own lock, exactly once - never trusting a client-supplied
    keep_document_id or an earlier review as still current. On any
    indexing failure the previous OpenSearch state is restored exactly,
    and any source file already moved aside is restored too - the
    country is left with precisely one active document only on a
    verified success, never a partial one.
    """
    if resolution_mode not in RESOLUTION_MODES:
        raise CountryConflictResolutionError(f'Unknown resolution_mode: {resolution_mode!r}. Expected one of {RESOLUTION_MODES}.')
    normalized_code = normalize_country_code(country_code)
    opensearch_client = client if client is not None else get_opensearch_client()
    with country_lock(source_directory, normalized_code, timeout_seconds=lock_timeout_seconds):
        existing_documents = country_document_lookup(normalized_code, opensearch_client)
        unique_ids = {document.document_id for document in existing_documents}
        if len(unique_ids) <= 1:
            raise CountryConflictNotFoundError(country_code=normalized_code)
        if resolution_mode == AUTO_DEDUPLICATE:
            resolved_keep_id = _auto_deduplicate_keep_document_id(existing_documents, source_directory=source_directory, country_code=normalized_code)
            if resolved_keep_id is None:
                raise CountryConflictResolutionError(f'AUTO_DEDUPLICATE is not available for {normalized_code} - its conflicting records are not proven to be the same physical document. Use CHOOSE_DOCUMENT or REPLACE_WITH_DOCUMENT instead.')
            if keep_document_id is not None and keep_document_id != resolved_keep_id:
                raise CountryConflictResolutionError('keep_document_id does not match the document AUTO_DEDUPLICATE would keep for this country.')
            effective_keep_id = resolved_keep_id
        else:
            if keep_document_id is None or keep_document_id not in unique_ids:
                raise CountryConflictResolutionError("keep_document_id is not one of this country's current candidates - the conflict state may have changed. Refresh the review and try again.")
            effective_keep_id = keep_document_id
        resolved_by_document = _resolved_paths_by_document(existing_documents, source_directory=source_directory, country_code=normalized_code)
        kept_path = resolved_by_document.get(effective_keep_id)
        operation_id = f'conflict-resolution-{normalized_code}'
        backups: list[tuple[Path, Path]] = []
        snapshot = _snapshot_country_chunks(client=opensearch_client, country_code=normalized_code)
        keep_chunk_ids = [item['_id'] for item in snapshot if item.get('_source', {}).get('document_id') == effective_keep_id]
        if not keep_chunk_ids:
            raise CountryConflictResolutionError('The chosen document has no indexed chunks to keep - refusing to leave the country with zero active documents.')
        try:
            for document in existing_documents:
                if document.document_id == effective_keep_id:
                    continue
                candidate_path = resolved_by_document.get(document.document_id)
                if candidate_path is None or candidate_path == kept_path:
                    continue
                backup_path = candidate_path.parent / f'.{operation_id}.{candidate_path.name}.backup'
                os.replace(candidate_path, backup_path)
                backups.append((candidate_path, backup_path))
            stale_chunks_deleted = _delete_country_chunks(client=opensearch_client, country_code=normalized_code, keep_chunk_ids=keep_chunk_ids)
            remaining = country_document_lookup(normalized_code, opensearch_client)
            remaining_ids = {document.document_id for document in remaining}
            if remaining_ids != {effective_keep_id}:
                raise AdminDocumentStorageError('The country did not end with exactly one active document after resolution.')
        except Exception:
            for original_path, backup_path in reversed(backups):
                if backup_path.exists():
                    os.replace(backup_path, original_path)
            _restore_country_snapshot(client=opensearch_client, snapshot=snapshot, bulk_chunk_size=bulk_chunk_size)
            raise
        for _, backup_path in backups:
            _safe_unlink(backup_path)
        removed_document_ids = sorted(unique_ids - {effective_keep_id})
        for old_document_id in removed_document_ids:
            try:
                delete_section_edit_state(source_directory, old_document_id)
            except OSError as error:
                raise AdminDocumentStorageError("The conflict was resolved, but a previous document's section-edit state could not be fully cleared.") from error
        return CountryConflictResolutionResult(country_code=normalized_code, resolution_mode=resolution_mode, kept_document_id=effective_keep_id, removed_document_ids=tuple(removed_document_ids), stale_chunks_deleted=stale_chunks_deleted)
EXPECTED_TOPICS_COUNT: Final[int] = len(LEGAL_TOPICS)
_STRUCTURE_WARNING_MAX_RECOGNIZED: Final[int] = 5
STRUCTURE_WARNING_CODE: Final[str] = 'structure_warning'
CONTEXT_WARNING_CODE: Final[str] = 'context_warning'

@dataclass(frozen=True, slots=True)
class TopicCoverageWarning:
    """One non-blocking warning about a document's topic coverage."""
    code: str
    message: str
    recognized_topics_count: int
    expected_topics_count: int
    recognized_topics: tuple[str, ...]
    missing_topics: tuple[str, ...]

def recognized_topics_for(chunks: list[DocumentChunk]) -> tuple[str, ...]:
    """
    Every distinct canonical legal topic actually present among a
    document's comparator chunks - never the overview chunks, which
    carry legal_topic=None by design.
    """
    return tuple((topic for topic in LEGAL_TOPICS if any((chunk.document_type == 'comparator' and chunk.legal_topic == topic for chunk in chunks))))

def evaluate_topic_coverage(chunks: list[DocumentChunk]) -> TopicCoverageWarning | None:
    """
    Classify one successfully-parsed document's topic coverage.

    Returns None when recognized_topics_count >= 6 (a strict majority
    of the 11 supported topics) - no warning at all. Otherwise returns
    exactly one warning: CONTEXT_WARNING when zero topics were
    recognized (the document is readable and its country is known, but
    nothing in it matches the product's Employment Law taxonomy at
    all - its relevance is in doubt), STRUCTURE_WARNING when 1-5 were
    recognized (readable, relevant, but atypically thin coverage).
    """
    recognized = recognized_topics_for(chunks)
    recognized_count = len(recognized)
    missing = tuple((topic for topic in LEGAL_TOPICS if topic not in recognized))
    if recognized_count > _STRUCTURE_WARNING_MAX_RECOGNIZED:
        return None
    if recognized_count == 0:
        return TopicCoverageWarning(code=CONTEXT_WARNING_CODE, message=f"The document's country was detected, but none of the document's content matched any of the {EXPECTED_TOPICS_COUNT} supported Employment Law topics. The document may be outside the expected Labour and Employment Law context - review before confirming.", recognized_topics_count=recognized_count, expected_topics_count=EXPECTED_TOPICS_COUNT, recognized_topics=recognized, missing_topics=missing)
    return TopicCoverageWarning(code=STRUCTURE_WARNING_CODE, message=f'The document only covers {recognized_count} of the {EXPECTED_TOPICS_COUNT} supported Employment Law topics, fewer than the usual majority. This may be an atypical or incomplete document - review before confirming.', recognized_topics_count=recognized_count, expected_topics_count=EXPECTED_TOPICS_COUNT, recognized_topics=recognized, missing_topics=missing)
