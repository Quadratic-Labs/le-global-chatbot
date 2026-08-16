"""HTTP endpoint for grounded legal answers."""

from __future__ import annotations

import re
from collections.abc import Iterator
from time import perf_counter
from typing import Final
from uuid import uuid4

from fastapi import (
    APIRouter,
    Header,
    HTTPException,
    Response,
    status,
)

from app.clients.openai_responses import (
    OpenAIConfigurationError,
    OpenAIResponsesClient,
)
from app.core.config import get_settings
from app.core.country_registry import COUNTRIES
from app.models.catalog import LegalCatalogResponse
from app.models.chat import (
    LegalAnswerSource,
    LegalChatHistoryMessage,
    LegalChatRequest,
    LegalChatResponse,
)
from app.models.conversation_state import ConversationState
from app.services.assistant_help import (
    build_assistant_help_answer,
    detect_assistant_help_intent,
)
from app.services.chat_metrics import (
    LegalChatMetrics,
)
from app.services.conversation_transition import (
    ConversationTransitionError,
    apply_conversation_transition,
    build_next_conversation_state,
)
from app.services.conversation_meta import (
    append_personalised_legal_caution,
    requires_personalised_legal_caution,
    resolve_ambiguous_city_followup_question,
    resolve_conversation_meta,
)

from app.services.country_detection import (
    CountryAvailability,
    CountryCatalogProvider,
    CountryDetectionError,
    is_country_only_followup,
    resolve_country_availability,
    resolve_country_display_name,
)
from app.services.legal_catalog import (
    DocumentLegalTopicsProvider,
    get_document_legal_topics_by_country,
    get_legal_catalog,
)
from app.services.legal_search import (
    LegalSearchError,
    search_contact_chunks,
    search_legal_documents,
)
from app.services.legal_subject_scope import canonicalize_legal_subject
from app.services.legal_topic_detection import (
    CANONICAL_LEGAL_TOPICS,
    LegalScope,
    detect_document_legal_topics,
    detect_legal_topics,
    resolve_legal_scope,
)
from app.services.rag_answer import (
    DEFAULT_MAX_CONTEXT_CHARACTERS,
    DEFAULT_MAX_SOURCE_CHARACTERS,
    InvalidLegalChatRequestError,
    LegalActionEvidenceSpec,
    RagAnswerError,
    SearchFunction,
    TextGenerationClient,
    answer_legal_question,
)
from app.services.request_understanding import (
    CurrentMessageDelta,
    DeterministicHints,
    HistoryTurn,
    RequestUnderstandingAction,
    RequestUnderstandingResult,
    understand_request,
)


router = APIRouter(
    prefix="/api/v1",
    tags=["Legal Chat"],
)


UNAVAILABLE_COUNTRIES_ANSWER_TEMPLATE: Final[str] = (
    "The validated L&E Global corpus does not currently "
    "contain documents for {countries}. Please contact "
    "the relevant L&E Global member firm for "
    "country-specific legal advice. Would you like to see the "
    "countries currently covered?"
)

MISSING_COUNTRY_ANSWER: Final[str] = (
    "Please select or name at least one country so I can answer "
    "from the relevant validated L&E Global documents."
)


# ---------------------------------------------------------------------
# STRONG_CONTACT_INTENT / COUNTRY_SCOPED_REACH_INTENT
#
# These regexes are kept exactly as before, but no longer decide
# anything on their own: RequestUnderstanding is now the primary
# router for every free-text request, and these only ever feed it a
# `strong_contact_signal` hint (see _build_deterministic_hints). A
# country and a legal topic being deterministically resolvable on the
# current question is never, by itself, proof that the whole request
# is understood - that decision is RequestUnderstanding's alone.
# ---------------------------------------------------------------------

# precise_le_global_identification: an identification question,
# anchored at the very start of the (normalized) question AND
# validated all the way to the end - never a general co-occurrence
# anywhere in the sentence. After the structure noun, only end-of-
# question, or "in"/"for"/"covers(ing)"/"serves(ing)" followed by a
# place and then the end of the question, are accepted.
_PRECISE_LE_GLOBAL_IDENTIFICATION_PATTERN: Final[
    re.Pattern[str]
] = re.compile(
    r"^(?:what|which|who|where)\s+(?:(?:is|are)\s+)?(?:the\s+)?"
    r"l&e\s+global\s+"
    r"(?:(?:member\s+firms?|law\s+firms?)(?:\s+contacts?)?"
    r"|offices?|contacts?)"
    r"(?:\s*[?.!]*$"
    r"|\s+(?:in|for|covers?|covering|serves?|serving)"
    r"\s+\S+[\w\s]*[?.!]*$)"
)

# professional_acquisition_request: a professional/firm/legal-counsel
# noun as the immediate object of an explicit acquisition phrasing.
_PROFESSIONAL_ACQUISITION_VERB_PATTERN: Final[str] = (
    r"(?:find\s+me|find\s+us|give\s+me|give\s+us"
    r"|send\s+me|send\s+us"
    r"|connect\s+me\s+with|connect\s+us\s+with"
    r"|put\s+me\s+in\s+touch\s+with|put\s+us\s+in\s+touch\s+with"
    r"|i\s+need|i\s+want"
    r"|i\s+would\s+like\s+to\s+speak\s+(?:with|to)"
    r"|can\s+i\s+(?:have|get)|could\s+i\s+(?:have|get)"
    r"|may\s+i\s+(?:have|get))"
)

_PROFESSIONAL_ACQUISITION_TARGET_PATTERN: Final[str] = (
    r"(?:employment\s+lawyers?|legal\s+counsels?|member\s+firms?"
    r"|law\s+firms?|lawyers?|attorneys?)"
)

_PROFESSIONAL_ACQUISITION_REQUEST_PATTERN: Final[
    re.Pattern[str]
] = re.compile(
    r"\b"
    + _PROFESSIONAL_ACQUISITION_VERB_PATTERN
    + r"\s+(?:an?\s+|the\s+)?"
    + _PROFESSIONAL_ACQUISITION_TARGET_PATTERN
    + r"\b(?!'s)(?=\s*(?:[.?!]|$|\s+(?:in|at|for|from|near|there)\b))"
)

# Shared verb-phrase alternation for every "explicit request" pattern
# below - reused by form 1 (data + of/for + target), form 2 (office +
# data suffix), and form 3 (target + contact as a noun).
_EXPLICIT_REQUEST_VERB_PATTERN: Final[str] = (
    r"(?:give\s+me|send\s+me|provide\s+me\s+with|show\s+me"
    r"|(?:can|could|would)\s+you\s+give\s+me"
    r"|(?:can|could|would)\s+you\s+send\s+me"
    r"|(?:can|could|would)\s+you\s+provide"
    r"|(?:can|could|may)\s+i\s+have"
    r"|(?:can|could|may)\s+i\s+get"
    r"|i\s+need|i\s+want|i\s+would\s+like)"
)

# explicit_contact_data_request, form 1: one of the phrasings above,
# directly followed (only an article/preposition in between) by a
# contact-data expression explicitly linked via "of"/"for" to a
# professional/firm/L&E-Global target.
_EXPLICIT_CONTACT_DATA_TARGET_PATTERN: Final[str] = (
    r"(?:employment\s+lawyers?|legal\s+counsels?|member\s+firms?"
    r"|law\s+firms?|lawyers?|attorneys?|l&e\s+global\s+offices?"
    r"|l&e\s+global)"
)

_CONTACT_DATA_TERM_PATTERN: Final[str] = (
    r"(?:contact\s+details|contact\s+information|contact\s+info"
    r"|email\s+address|email|phone\s+number|phone"
    r"|telephone\s+number|telephone|office\s+address|address"
    r"|website)"
)

_EXPLICIT_CONTACT_DATA_REQUEST_PATTERN: Final[
    re.Pattern[str]
] = re.compile(
    r"\b"
    + _EXPLICIT_REQUEST_VERB_PATTERN
    + r"\s+(?:the\s+|an?\s+)?"
    + _CONTACT_DATA_TERM_PATTERN
    + r"\s+(?:of|for)\s+(?:the\s+|an?\s+)?"
    + _EXPLICIT_CONTACT_DATA_TARGET_PATTERN
    + r"\b(?!'s)"
)

# explicit_contact_data_request, form 1b: the one interrogative
# exception that never contains an "explicit request" verb phrase at
# all - validated as its own complete structure, anchored from the
# very start of the question to its end.
_INTERROGATIVE_CONTACT_DATA_REQUEST_PATTERN: Final[
    re.Pattern[str]
] = re.compile(
    r"^what\s+is\s+the\s+"
    + _CONTACT_DATA_TERM_PATTERN
    + r"\s+(?:of|for)\s+(?:the\s+|an?\s+)?"
    + _EXPLICIT_CONTACT_DATA_TARGET_PATTERN
    + r"(?:\s+(?:in|for)\s+\S+[\w\s]*)?[?.!]*$"
)

# explicit_contact_data_request, form 2: one of the request phrasings
# above, followed anywhere later in the question by "<office> <data
# term>" as its final phrase - a genuine bureau's own coordinates.
_OFFICE_CONTACT_DATA_SUFFIX_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\b"
    + _EXPLICIT_REQUEST_VERB_PATTERN
    + r".*?"
    r"(?<!'s\s)(?<!s'\s)(?<!their\s)(?<!his\s)(?<!her\s)"
    r"\boffices?\s+(?:email|address|details|phone(?:\s+number)?"
    r"|telephone(?:\s+number)?|website)\s*[?.!]*$"
)

# explicit_contact_data_request, form 3: one of the request phrasings
# above, immediately followed (only an article in between) by a
# professional/firm noun and "contact" used as a noun.
_LAWYER_CONTACT_TARGET_PATTERN: Final[str] = (
    r"(?:employment\s+lawyers?|legal\s+counsels?|member\s+firms?"
    r"|law\s+firms?|lawyers?|attorneys?|l&e\s+global)"
)

_LAWYER_CONTACT_TAIL_PATTERN: Final[str] = (
    r"\s+(?:an?\s+|the\s+)?"
    + _LAWYER_CONTACT_TARGET_PATTERN
    + r"\s+contacts?\b(?!'s)"
    r"(?=\s*(?:[.?!]|$|\s+(?:in|at|for|from|near|there)\b))"
)

_LAWYER_CONTACT_ACQUISITION_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\b(?:give|send|provide|show|find)\s+(?:me|us)"
    + _LAWYER_CONTACT_TAIL_PATTERN
    + r"|\b(?:can|could|would)\s+you\s+"
    r"(?:give|send|provide|show|find)(?:\s+(?:me|us))?"
    + _LAWYER_CONTACT_TAIL_PATTERN
    + r"|\b(?:can|could|may)\s+i\s+(?:have|get)"
    + _LAWYER_CONTACT_TAIL_PATTERN
    + r"|\bi\s+(?:need|want|would\s+like)"
    + _LAWYER_CONTACT_TAIL_PATTERN
    + r"|\bput\s+(?:me|us)\s+in\s+touch\s+with"
    + _LAWYER_CONTACT_TAIL_PATTERN
    + r"|\bconnect\s+(?:me|us)\s+with"
    + _LAWYER_CONTACT_TAIL_PATTERN
)

# country_scoped_reach_intent's phrasing half: a direct first-person
# "who/how can I reach ..." form.
_DIRECT_WHO_TO_REACH_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\bwho\s+(?:can|should)\s+i\s+"
    r"(?:contact|speak\s+to|email|call)\b"
    r"|\bhow\s+(?:can|do|should)\s+i\s+"
    r"(?:contact|reach|email|call|speak\s+to)\b"
)

_COMPARISON_SIGNAL_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\bcompar\w*\b|\bversus\b|\bvs\.?\b|\brather\s+than\b"
    r"|\bdiffer\w*\b|\bbetween\b.*\band\b"
)

_CONTACT_TYPOGRAPHIC_APOSTROPHE_PATTERN: Final[
    re.Pattern[str]
] = re.compile(
    "[‘’ʼ]"
)

_CONTACT_WHITESPACE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\s+"
)

# Display-time sanitation only (see the 0.4.2 mission's UK contact
# investigation): some indexed contact chunks have their own Phone
# value repeated as a trailing suffix of the Address line, from
# whatever produced the original chunk content. Reindexing is out of
# scope, so this strips only that exact, already-duplicated suffix at
# answer-build time - never rewrites, reformats, or guesses any other
# part of the address, and only ever touches a contact whose own
# Address line ends with its own Phone value.
_CONTACT_PHONE_LINE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^Phone:[ \t]*(.+)$",
    re.MULTILINE,
)

_CONTACT_ADDRESS_LINE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^Address:[ \t]*(.+)$",
    re.MULTILINE,
)


def _sanitize_contact_content(
    content: str,
) -> str:
    """
    Strip a Phone value repeated as a trailing suffix of the Address
    line, if present - see the module-level comment above.
    """

    phone_match = _CONTACT_PHONE_LINE_PATTERN.search(content)
    address_match = _CONTACT_ADDRESS_LINE_PATTERN.search(content)

    if not phone_match or not address_match:
        return content

    phone_tokens = phone_match.group(1).strip().split()

    if not phone_tokens:
        return content

    phone_suffix_pattern = re.compile(
        r"[,\s]+"
        + r"\s+".join(
            re.escape(token) for token in phone_tokens
        )
        + r"[ \t]*$"
    )

    address_value = address_match.group(1)

    sanitized_address_value, replaced_count = (
        phone_suffix_pattern.subn(
            "",
            address_value,
        )
    )

    if replaced_count == 0 or not sanitized_address_value.strip():
        return content

    start, end = address_match.span(1)

    return (
        content[:start]
        + sanitized_address_value
        + content[end:]
    )


CONTACT_CLARIFICATION_ANSWER: Final[str] = (
    "Which country do you need an L&E Global lawyer contact for?"
)

CONTACT_NOT_FOUND_ANSWER_TEMPLATE: Final[str] = (
    "I could not find a validated L&E Global contact for "
    "{country} in the available documents."
)

CLARIFICATION_LEGAL_MISSING_COUNTRY_ANSWER: Final[str] = (
    "Which country would you like information about?"
)

CLARIFICATION_MISSING_COMPARISON_COUNTRIES_ANSWER: Final[str] = (
    "Which countries would you like to compare?"
)

CLARIFICATION_MISSING_COMPARISON_TOPIC_ANSWER: Final[str] = (
    "Which employment law topic would you like to compare? For "
    "example, termination, working time, leave, remuneration or "
    "employment contracts."
)

CLARIFICATION_AMBIGUOUS_WITH_COUNTRY_TEMPLATE: Final[str] = (
    "Are you looking for employment law information about "
    "{country}, or would you like the contact details of the L&E "
    "Global member firm in {country}?"
)

CLARIFICATION_AMBIGUOUS_REQUEST_ANSWER: Final[str] = (
    "Could you clarify your question? Please specify the country and "
    "the employment law topic - or the contact - you are asking about."
)

CLARIFICATION_UNSUPPORTED_REQUEST_ANSWER: Final[str] = (
    "This assistant can only answer employment law questions, and "
    "related L&E Global contacts, covered by the validated documents. "
    "Please rephrase your question within that scope."
)

CLARIFICATION_EXPLICIT_FILTER_CONFLICT_ANSWER: Final[str] = (
    "Your question appears to concern a different country than the "
    "one specified in this request's country filter. Please clarify "
    "which country you would like this answer for."
)

CLARIFICATION_MISSING_TOPIC_FOR_COUNTRY_TEMPLATE: Final[str] = (
    "What employment law topic would you like information about for "
    "{country}?"
)

CLARIFICATION_MISSING_TOPIC_ANSWER: Final[str] = (
    "What employment law topic would you like information about?"
)


def _format_country_list(
    display_names: list[str],
) -> str:
    """Join country display names into a readable list."""

    if len(display_names) == 1:
        return display_names[0]

    return (
        ", ".join(
            display_names[:-1]
        )
        + " and "
        + display_names[-1]
    )


def _unavailable_countries_answer(
    unavailable_codes: list[str],
) -> str:
    """Build the fallback answer naming the unavailable countries."""

    display_names = [
        resolve_country_display_name(
            country_code
        )
        for country_code in unavailable_codes
    ]

    return UNAVAILABLE_COUNTRIES_ANSWER_TEMPLATE.format(
        countries=_format_country_list(
            display_names
        )
    )


def _iter_recent_user_questions(
    history: list[LegalChatHistoryMessage],
) -> Iterator[str]:
    """
    Yield every user question in history, most recent first.

    Never yields an assistant turn - a historical answer is
    conversational context only, never a source of country or topic
    information.
    """

    for message in reversed(history):
        if message.role != "user":
            continue

        stripped_content = message.content.strip()

        if not stripped_content:
            continue

        yield stripped_content


def _normalize_contact_question(
    question: str,
) -> str:
    """Casefold, normalize curly apostrophes, and collapse whitespace."""

    without_curly_quotes = (
        _CONTACT_TYPOGRAPHIC_APOSTROPHE_PATTERN.sub(
            "'",
            question,
        )
    )

    return _CONTACT_WHITESPACE_PATTERN.sub(
        " ",
        without_curly_quotes.casefold(),
    ).strip()


def _detect_contact_intent(
    question: str,
) -> bool:
    """
    Detect STRONG_CONTACT_INTENT - a hint only (see module docstring),
    never a gate deciding whether RequestUnderstanding runs.
    """

    normalized_question = _normalize_contact_question(
        question
    )

    precise_le_global_identification = bool(
        _PRECISE_LE_GLOBAL_IDENTIFICATION_PATTERN.search(
            normalized_question
        )
    )

    professional_acquisition_request = bool(
        _PROFESSIONAL_ACQUISITION_REQUEST_PATTERN.search(
            normalized_question
        )
    )

    explicit_contact_data_request = bool(
        _EXPLICIT_CONTACT_DATA_REQUEST_PATTERN.search(
            normalized_question
        )
        or _INTERROGATIVE_CONTACT_DATA_REQUEST_PATTERN.search(
            normalized_question
        )
        or _OFFICE_CONTACT_DATA_SUFFIX_PATTERN.search(
            normalized_question
        )
        or _LAWYER_CONTACT_ACQUISITION_PATTERN.search(
            normalized_question
        )
    )

    return bool(
        precise_le_global_identification
        or professional_acquisition_request
        or explicit_contact_data_request
    )


def _has_direct_who_to_reach_form(
    question: str,
) -> bool:
    """
    Detect COUNTRY_SCOPED_REACH_INTENT's phrasing - a hint only (see
    module docstring), never a gate.
    """

    normalized_question = _normalize_contact_question(
        question
    )

    return bool(
        _DIRECT_WHO_TO_REACH_PATTERN.search(
            normalized_question
        )
    )


def _has_comparison_signal(
    question: str,
) -> bool:
    """
    Cheap, generic, informational-only signal that a question might be
    a comparison - never a gate. Used purely to populate one
    deterministic hint field; RequestUnderstanding decides the actual
    routing.
    """

    return bool(
        _COMPARISON_SIGNAL_PATTERN.search(
            question.casefold()
        )
    )


def _build_deterministic_hints(
    request: LegalChatRequest,
    catalog_provider: CountryCatalogProvider,
    document_topic_provider: DocumentLegalTopicsProvider,
) -> tuple[DeterministicHints, CountryAvailability, LegalScope]:
    """
    Build the deterministic hints passed to RequestUnderstanding.

    None of these signals decide anything here - they are computed
    once, attached to the model call as context, and kept available so
    a conservative fallback route remains possible if the model call
    itself fails (see _resolve_conservative_fallback).
    """

    current_country_scope = resolve_country_availability(
        request=request,
        catalog_provider=catalog_provider,
    )

    current_legal_scope = resolve_legal_scope(request)

    recent_user_questions = list(
        _iter_recent_user_questions(request.history)
    )[:3]

    if recent_user_questions:
        combined_history_text = " ".join(recent_user_questions)

        history_country_scope = resolve_country_availability(
            request=request.model_copy(
                update={"question": combined_history_text}
            ),
            catalog_provider=catalog_provider,
        )

        history_country_codes = history_country_scope.available_codes
        history_unavailable_country_codes = (
            history_country_scope.unavailable_codes
        )
        history_legal_topics = detect_legal_topics(
            combined_history_text
        )
    else:
        history_country_codes = []
        history_unavailable_country_codes = []
        history_legal_topics = []

    # Mission "ORDER 8F-A" - one compact, country-scoped aggregation
    # covering every country this request could plausibly concern
    # (current, explicit, and recent-history alike), so the live
    # document-topic vocabulary is available both to the model prompt
    # and to _resolve_conservative_fallback's own deterministic check,
    # without a second OpenSearch call later.
    explicit_country_codes_upper = (
        code.strip().upper()
        for code in request.country_codes
        if code.strip()
    )

    document_topic_country_codes = sorted(
        {
            *current_country_scope.available_codes,
            *explicit_country_codes_upper,
            *history_country_codes,
        }
    )

    current_document_legal_topics = document_topic_provider(
        document_topic_country_codes
    )

    hints = DeterministicHints(
        current_country_codes=current_country_scope.available_codes,
        current_unavailable_country_codes=(
            current_country_scope.unavailable_codes
        ),
        current_legal_topics=current_legal_scope.legal_topics,
        strong_contact_signal=(
            _detect_contact_intent(request.question)
            or _has_direct_who_to_reach_form(request.question)
        ),
        comparison_signal=_has_comparison_signal(request.question),
        history_country_codes=history_country_codes,
        history_unavailable_country_codes=(
            history_unavailable_country_codes
        ),
        history_legal_topics=history_legal_topics,
        explicit_country_codes=list(request.country_codes),
        explicit_legal_topics=list(request.legal_topics),
        explicit_subsections=list(request.subsections),
        current_document_legal_topics=current_document_legal_topics,
    )

    return hints, current_country_scope, current_legal_scope


CONTACT_COUNTRY_FALLBACK_CODES: Final[dict[str, str]] = {
    # Business rule (corrective gate, section 16): Slovakia has no
    # Employment Law Overview of its own yet, so no indexed contact
    # chunk exists for SK either - docx_parser.py extracts contact/
    # member-firm details from that same per-country Overview document
    # (see its own member_firm field), so no Overview means no contact
    # chunk from that path. The client's member firm for Slovakia is
    # reached through the Czech Republic office instead - contact
    # routing only, never a legal-content or jurisdiction substitution
    # (section 18): SK stays SK everywhere else (detection, policy,
    # coverage) - see country_detection.py/admin_country_policy.py,
    # neither of which this mapping touches.
    "SK": "CZ",
}


def _build_contact_section(
    country_codes: list[str],
    unavailable_country_codes: list[str],
    citation_offset: int,
) -> tuple[str, list[LegalAnswerSource], int, float]:
    """
    Build one deterministic contact answer section, never calling
    OpenAI. Citations continue from citation_offset + 1, so a contact
    section appended after a legal answer never collides with the
    legal answer's own citations.

    A requested country with no contact chunk of its own falls back to
    another country's contact chunk only when CONTACT_COUNTRY_
    FALLBACK_CODES names one for it (currently Slovakia only) - the
    section is still labelled with the REQUESTED country's own name;
    only the underlying contact content, and its own country label,
    come from the fallback country, and the answer says so explicitly
    rather than silently presenting Czech contact details as if they
    were Slovakia's own.

    Returns (answer_text, sources, retrieval_total, took_ms) - the
    caller updates shared metrics itself, since this function may be
    invoked once per contact action.
    """

    sources: list[LegalAnswerSource] = []
    answer_sections: list[str] = []
    retrieval_total = 0
    took_ms = 0.0

    requested_codes = [code.upper() for code in country_codes]
    unavailable_codes = [
        code.upper() for code in unavailable_country_codes
    ]

    # An "unavailable" country never had its own contact chunk
    # searched before this fallback existed either: no Overview means
    # no contact chunk from the same document (see docstring above),
    # so searching for that code's own contact content would always
    # come back empty by construction. Only the requested codes
    # themselves (as before) and any fallback TARGET a requested code
    # actually needs go into the real OpenSearch call.
    fallback_targets_needed = {
        CONTACT_COUNTRY_FALLBACK_CODES[code]
        for code in (*requested_codes, *unavailable_codes)
        if code in CONTACT_COUNTRY_FALLBACK_CODES
    }

    search_codes = list(
        dict.fromkeys([*requested_codes, *fallback_targets_needed])
    )

    hits_by_country_code: dict[str, list] = {}

    if search_codes:
        try:
            contact_response = search_contact_chunks(
                country_codes=search_codes
            )
        except LegalSearchError as error:
            raise RagAnswerError(
                "Legal document retrieval failed."
            ) from error

        took_ms = float(contact_response.took_ms)
        retrieval_total += contact_response.total

        for hit in contact_response.hits:
            hits_by_country_code.setdefault(
                hit.country_code.upper(),
                [],
            ).append(hit)

    # A hit already cited under one country_code (e.g. Czechia's own
    # contact chunk) must reuse the SAME citation number, never a new
    # one, when the identical chunk is rendered again through
    # Slovakia's fallback for the same request - found by adversarial
    # review: requesting contact for both SK and CZ together (a
    # realistic combined question) otherwise cited the one underlying
    # Czech chunk twice under two different numbers.
    citation_by_hit_identity: dict[tuple[str, str], int] = {}

    def render(country_code: str) -> None:
        upper_code = country_code.upper()
        display_name = resolve_country_display_name(country_code)
        own_hits = hits_by_country_code.get(upper_code, [])

        source_hits = own_hits
        fallback_preamble: str | None = None

        if not own_hits:
            fallback_code = CONTACT_COUNTRY_FALLBACK_CODES.get(
                upper_code
            )
            fallback_hits = (
                hits_by_country_code.get(fallback_code, [])
                if fallback_code
                else []
            )

            if fallback_hits:
                fallback_display_name = resolve_country_display_name(
                    fallback_code
                )
                source_hits = fallback_hits
                fallback_preamble = (
                    f"No dedicated {display_name} contact is listed "
                    f"yet; {display_name} enquiries are handled by "
                    f"the {fallback_display_name} member firm below."
                )

        if not source_hits:
            answer_sections.append(
                f"{display_name}\n"
                + CONTACT_NOT_FOUND_ANSWER_TEMPLATE.format(
                    country=display_name
                )
            )
            return

        for hit in source_hits:
            hit_identity = (hit.document_id, hit.chunk_id)
            citation = citation_by_hit_identity.get(hit_identity)

            if citation is None:
                citation = citation_offset + len(sources) + 1
                citation_by_hit_identity[hit_identity] = citation

                sources.append(
                    LegalAnswerSource(
                        citation=citation,
                        document_id=hit.document_id,
                        chunk_id=hit.chunk_id,
                        country=hit.country,
                        country_code=hit.country_code,
                        legal_topic=hit.legal_topic,
                        section=hit.section,
                        subsection=hit.subsection,
                        source_filename=hit.source_filename,
                        reference_year=hit.reference_year,
                        score=hit.score,
                    )
                )

            sanitized_content = _sanitize_contact_content(
                hit.content
            )

            body = f"{display_name}\n"

            if fallback_preamble is not None:
                body += f"{fallback_preamble}\n"

            body += f"{sanitized_content} [{citation}]"

            answer_sections.append(body)

    for country_code in country_codes:
        render(country_code)

    for country_code in unavailable_country_codes:
        render(country_code)

    return (
        "\n\n".join(answer_sections),
        sources,
        retrieval_total,
        took_ms,
    )


def _clarification_answer_for(
    result: RequestUnderstandingResult,
) -> str:
    """Map one clarification result to its user-facing answer text."""

    hint_action = result.actions[0] if result.actions else None
    reason = result.clarification_reason

    if reason == "missing_country":
        if hint_action is not None and hint_action.type == "contact":
            return CONTACT_CLARIFICATION_ANSWER

        return CLARIFICATION_LEGAL_MISSING_COUNTRY_ANSWER

    if reason == "missing_comparison_countries":
        return CLARIFICATION_MISSING_COMPARISON_COUNTRIES_ANSWER

    if reason == "missing_comparison_topic":
        return CLARIFICATION_MISSING_COMPARISON_TOPIC_ANSWER

    if reason == "missing_topic":
        hint_country_code = (
            hint_action.country_codes[0]
            if hint_action is not None and hint_action.country_codes
            else None
        )

        if hint_country_code:
            return (
                CLARIFICATION_MISSING_TOPIC_FOR_COUNTRY_TEMPLATE.format(
                    country=resolve_country_display_name(
                        hint_country_code
                    )
                )
            )

        return CLARIFICATION_MISSING_TOPIC_ANSWER

    if reason == "ambiguous_request":
        hint_country_code = (
            hint_action.country_codes[0]
            if hint_action is not None and hint_action.country_codes
            else None
        )

        if hint_country_code:
            return CLARIFICATION_AMBIGUOUS_WITH_COUNTRY_TEMPLATE.format(
                country=resolve_country_display_name(
                    hint_country_code
                )
            )

        return CLARIFICATION_AMBIGUOUS_REQUEST_ANSWER

    return CLARIFICATION_UNSUPPORTED_REQUEST_ANSWER


def _check_explicit_filter_conflict(
    request: LegalChatRequest,
    result: RequestUnderstandingResult,
) -> bool:
    """
    True when the request carried explicit country_codes and the
    understood result names a country outside that explicit set -
    a genuine conflict between text and filter that must be surfaced,
    never silently resolved by picking either side.
    """

    explicit_codes = {
        code.strip().upper()
        for code in request.country_codes
        if code.strip()
    }

    if not explicit_codes:
        return False

    for action in result.actions:
        action_codes = {
            code.upper() for code in action.country_codes
        }

        if action_codes and not action_codes <= explicit_codes:
            return True

    return False


def _try_local_country_only_followup_result(
    *,
    question: str,
    conversation_state: ConversationState | None,
) -> RequestUnderstandingResult | None:
    """
    When RequestUnderstanding fails outright (invalid_response,
    timeout, parsing error, or any other transient failure), a bare
    country-only follow-up ("Peru?") should never degrade to the
    generic conservative fallback - it can be resolved deterministically
    from conversation_state alone, with no further OpenAI call at all
    (mission "CORRECTION FINALE CIBLEE 0.4.2", Correction 2).

    Returns a synthetic, already-correct RequestUnderstandingResult
    (a plain country replacement against the single prior action, with
    every explicit-subject/action field clear) whenever that applies,
    so the caller can treat it exactly like a normal successful
    understanding result and let apply_conversation_transition's own
    existing single-action inheritance handle the rest - never None
    actions, is_follow_up, or current_message_delta invented beyond
    what a country-only message deterministically supports.

    Returns None (the caller must fall through to the existing
    conservative fallback) for anything else: no conversation_state, a
    multi-action state (RULE 5/9's own disambiguation is not
    reproduced here - never guess which action a bare country belongs
    to), a comparison that cannot be inherited below two countries, or
    a message that is not purely a country reference.
    """

    if conversation_state is None or len(conversation_state.actions) != 1:
        return None

    country_codes = is_country_only_followup(question)

    if country_codes is None:
        return None

    previous_action = conversation_state.actions[0]

    if previous_action.type == "comparison" and len(country_codes) < 2:
        return None

    return RequestUnderstandingResult(
        status="resolved",
        actions=[
            RequestUnderstandingAction(
                type=previous_action.type,
                country_codes=country_codes,
                legal_topics=list(previous_action.legal_topics),
                # previous_action.subject_text already folds in
                # whichever of legal_topics/topic_text the original
                # turn actually populated (ConversationActionState
                # has no topic_text field of its own) - carrying it
                # here as topic_text keeps this synthetic action
                # complete per RequestUnderstandingResult's own
                # resolved-action rule even when legal_topics alone
                # is empty. None for a "contact" previous_action,
                # matching that type's own no-subject-matter rule.
                topic_text=previous_action.subject_text,
            )
        ],
        is_follow_up=True,
        confidence=1.0,
        clarification_reason=None,
        current_message_delta=CurrentMessageDelta(
            explicit_action_types=[],
            explicit_country_codes=country_codes,
            explicit_legal_topics=[],
            explicit_subject_text=None,
            context_operation="replace_country",
        ),
    )


def _resolve_conservative_fallback(
    request: LegalChatRequest,
    hints: DeterministicHints,
    current_country_scope: CountryAvailability,
    current_legal_scope: LegalScope,
    metrics: LegalChatMetrics,
    search_function: SearchFunction,
    generation_client: TextGenerationClient | None,
    rerank_enabled: bool,
    rerank_pool_multiplier: int,
    max_context_characters: int,
    max_source_characters: int,
) -> LegalChatResponse:
    """
    Resolve one request whose understanding call failed entirely -
    using only the deterministic hints, and only when they clearly
    describe a complete, single-intention request. Anything less than
    fully clear degrades to a safe clarification - never a partial
    answer presented as complete, never a crash, never the
    documentary-insufficiency message.
    """

    country_resolved = bool(
        current_country_scope.available_codes
        or current_country_scope.unavailable_codes
    )

    unambiguous_single_intent = not (
        hints.strong_contact_signal
        and current_legal_scope.is_supported
    )

    # Mission "ORDER 8F-A", section 10 - an exact, single-country live
    # document-topic title (canonical or Admin-created custom section
    # alike) is a MORE specific deterministic signal than a canonical
    # keyword match, and must be checked first: an understanding-call
    # failure must never force a generic "please specify country and
    # topic" clarification when the question already names one exact,
    # currently-indexed section title outright. Deliberately restricted
    # to a single resolved country (document topics are always one
    # country's own section - never a comparison) and never when a
    # contact signal already claimed the request.
    if (
        not hints.strong_contact_signal
        and len(current_country_scope.available_codes) == 1
    ):
        single_country_code = current_country_scope.available_codes[0]

        resolved_document_topics = detect_document_legal_topics(
            request.question,
            hints.current_document_legal_topics.get(
                single_country_code, []
            ),
        )

        if resolved_document_topics:
            prepared_request = request.model_copy(
                update={
                    "country_codes": (
                        current_country_scope.available_codes
                    ),
                    "legal_topics": resolved_document_topics,
                }
            )

            response = answer_legal_question(
                prepared_request,
                search_function=search_function,
                generation_client=generation_client,
                rerank_enabled=rerank_enabled,
                rerank_pool_multiplier=rerank_pool_multiplier,
                max_context_characters=max_context_characters,
                max_source_characters=max_source_characters,
                metrics=metrics,
                known_excluded_country_codes=(
                    current_country_scope.unavailable_codes or None
                ),
            )

            if current_country_scope.unavailable_codes:
                response = response.model_copy(
                    update={
                        "answer": (
                            response.answer
                            + "\n\nNote: "
                            + _unavailable_countries_answer(
                                current_country_scope.unavailable_codes
                            )
                        ),
                    }
                )

            metrics.request_actions = ["legal_information"]
            metrics.resolved_action_countries = [
                {
                    "type": "legal_information",
                    "country_codes": (
                        current_country_scope.available_codes
                    ),
                }
            ]
            metrics.resolved_country_codes = (
                current_country_scope.available_codes
            )
            metrics.resolved_legal_topics = resolved_document_topics

            return response

    if (
        hints.strong_contact_signal
        and country_resolved
        and not current_legal_scope.is_supported
        and not hints.comparison_signal
    ):
        (
            contact_answer,
            sources,
            retrieval_total,
            took_ms,
        ) = _build_contact_section(
            country_codes=current_country_scope.available_codes,
            unavailable_country_codes=(
                current_country_scope.unavailable_codes
            ),
            citation_offset=0,
        )

        metrics.opensearch_ms += took_ms
        metrics.retrieval_total = retrieval_total
        metrics.selected_sources = len(sources)
        metrics.model = None
        metrics.generation_attempts = 0
        metrics.outcome = (
            "contact_resolved" if sources else "contact_not_found"
        )
        metrics.request_actions = ["contact"]
        metrics.resolved_action_countries = [
            {
                "type": "contact",
                "country_codes": (
                    current_country_scope.available_codes
                ),
            }
        ]
        metrics.resolved_country_codes = (
            current_country_scope.available_codes
        )

        return LegalChatResponse(
            question=request.question.strip(),
            answer=contact_answer,
            grounded=bool(sources),
            model=None,
            retrieval_total=retrieval_total,
            sources=sources,
        )

    if (
        unambiguous_single_intent
        and current_country_scope.available_codes
        and current_legal_scope.is_supported
    ):
        is_comparison = (
            len(current_country_scope.available_codes) >= 2
        )

        prepared_request = request.model_copy(
            update={
                "country_codes": (
                    current_country_scope.available_codes
                ),
                "legal_topics": current_legal_scope.legal_topics,
            }
        )

        response = answer_legal_question(
            prepared_request,
            search_function=search_function,
            generation_client=generation_client,
            rerank_enabled=rerank_enabled,
            rerank_pool_multiplier=rerank_pool_multiplier,
            max_context_characters=max_context_characters,
            max_source_characters=max_source_characters,
            metrics=metrics,
            known_excluded_country_codes=(
                current_country_scope.unavailable_codes or None
            ),
        )

        if current_country_scope.unavailable_codes:
            response = response.model_copy(
                update={
                    "answer": (
                        response.answer
                        + "\n\nNote: "
                        + _unavailable_countries_answer(
                            current_country_scope.unavailable_codes
                        )
                    ),
                }
            )

        metrics.request_actions = [
            "comparison" if is_comparison else "legal_information"
        ]
        metrics.resolved_action_countries = [
            {
                "type": (
                    "comparison" if is_comparison else "legal_information"
                ),
                "country_codes": (
                    current_country_scope.available_codes
                ),
            }
        ]
        metrics.resolved_country_codes = (
            current_country_scope.available_codes
        )
        metrics.resolved_legal_topics = (
            current_legal_scope.legal_topics
        )

        return response

    metrics.clarification_reason = "ambiguous_request"
    metrics.outcome = "clarification_ambiguous_request"

    return LegalChatResponse(
        question=request.question.strip(),
        answer=CLARIFICATION_AMBIGUOUS_REQUEST_ANSWER,
        grounded=False,
        model=None,
        retrieval_total=0,
        sources=[],
    )


def _aggregate_action_country_codes(
    resolved_action_countries: list[dict[str, object]],
) -> list[str]:
    """
    Union, in order, of every action's own resolved country codes -
    kept only for backward compatibility with log consumers reading
    the older flat `resolved_country_codes` field. The per-action
    field is the source of truth for a mixed request.
    """

    aggregated: list[str] = []

    for entry in resolved_action_countries:
        for code in entry.get("country_codes", []):
            if code not in aggregated:
                aggregated.append(code)

    return aggregated


def _aggregate_action_legal_topics(
    resolved_action_topics: list[dict[str, object]],
) -> list[str]:
    """Union, in order, of every action's own resolved legal topics."""

    aggregated: list[str] = []

    for entry in resolved_action_topics:
        for topic in entry.get("legal_topics", []):
            if topic not in aggregated:
                aggregated.append(topic)

    return aggregated


def _execute_resolved_plan(
    request: LegalChatRequest,
    result: RequestUnderstandingResult,
    hints: DeterministicHints,
    metrics: LegalChatMetrics,
    catalog_provider: CountryCatalogProvider,
    search_function: SearchFunction,
    generation_client: TextGenerationClient | None,
    rerank_enabled: bool,
    rerank_pool_multiplier: int,
    max_context_characters: int,
    max_source_characters: int,
) -> LegalChatResponse:
    """
    Execute every action RequestUnderstanding resolved.

    Every action keeps its own country/topic scope - a contact
    action's country is never widened to a comparison action's
    countries, and vice versa. Exactly one legal generation call
    covers every legal_information/comparison action combined; every
    contact action is resolved deterministically and appended, in
    order, after the legal answer.
    """

    contact_actions = result.actions_of_type("contact")
    legal_type_actions = [
        action
        for action in result.actions
        if action.type in ("legal_information", "comparison")
    ]

    resolved_action_countries: list[dict[str, object]] = []
    resolved_action_topics: list[dict[str, object]] = []
    executed: list[tuple[RequestUnderstandingAction, list[str]]] = []

    answer_parts: list[str] = []
    sources: list[LegalAnswerSource] = []
    grounded = False
    model_used: str | None = None
    retrieval_total = 0

    if legal_type_actions:
        merged_available_codes: list[str] = []
        merged_unavailable_codes: list[str] = []
        merged_legal_topics: list[str] = []
        merged_question_parts: list[str] = []
        action_specs: list[LegalActionEvidenceSpec] = []

        for action in legal_type_actions:
            action_scope = resolve_country_availability(
                request=request.model_copy(
                    update={"country_codes": action.country_codes}
                ),
                catalog_provider=catalog_provider,
            )

            for code in action_scope.available_codes:
                if code not in merged_available_codes:
                    merged_available_codes.append(code)

            for code in action_scope.unavailable_codes:
                if code not in merged_unavailable_codes:
                    merged_unavailable_codes.append(code)

            # Mission "ORDER 8F-A", section 7/9 - never trust the
            # model's document_legal_topics blindly: validate against
            # the ACTUAL live legal_topic vocabulary indexed for this
            # action's own resolved countries (never comparison
            # actions - guaranteed empty at the model already, checked
            # again here as belt-and-suspenders).
            live_document_topics_for_action = {
                topic
                for code in action_scope.available_codes
                for topic in hints.current_document_legal_topics.get(
                    code, []
                )
            }

            # Mission "ORDER 8G-A", section 4 - a canonical topic that
            # was renamed away (Rename) is no longer part of a
            # country's live legal_topic vocabulary; a hard filter on
            # it would then retrieve structurally nothing, even though
            # the model still recognized the question as being about
            # that canonical concept in plain language. Only suppress
            # the canonical filter given POSITIVE evidence it is not
            # live (a non-empty live-topic set that specifically omits
            # it) - never merely because no live-topic data is
            # available at all, which proves nothing either way and
            # must fall back to the pre-existing canonical-membership
            # check. Scoped deliberately to single-country legal_
            # information only - comparison behavior (which may
            # legitimately span a country where a canonical topic was
            # never live at all) is untouched, and explicit/dynamic
            # document-topic priority (above) is unaffected either way.
            validated_topics = [
                topic
                for topic in action.legal_topics
                if topic in CANONICAL_LEGAL_TOPICS
                and (
                    action.type != "legal_information"
                    or len(action_scope.available_codes) != 1
                    or not live_document_topics_for_action
                    or topic in live_document_topics_for_action
                )
            ]

            validated_document_topics = (
                [
                    topic
                    for topic in action.document_legal_topics
                    if topic in live_document_topics_for_action
                ]
                if action.type != "comparison"
                else []
            )

            for topic in validated_topics:
                if topic not in merged_legal_topics:
                    merged_legal_topics.append(topic)

            resolved_action_countries.append(
                {
                    "type": action.type,
                    "country_codes": action_scope.available_codes,
                }
            )
            resolved_action_topics.append(
                {
                    "type": action.type,
                    "legal_topics": validated_topics,
                    "document_legal_topics": validated_document_topics,
                    "topic_text": action.topic_text,
                }
            )
            executed.append(
                (
                    action.model_copy(
                        update={"legal_topics": validated_topics}
                    ),
                    action_scope.available_codes,
                )
            )

            question_part = (
                action.resolved_question
                if action.resolved_question
                else request.question
            )

            if question_part not in merged_question_parts:
                merged_question_parts.append(question_part)

            if action_scope.available_codes:
                # Defensive re-canonicalization: action's own
                # subject_text/search_concepts should already be
                # jurisdiction-neutral by this point (canonicalized at
                # RequestUnderstanding's own output, at the client-
                # state boundary, and at conversation_transition's own
                # inheritance step) - this is deliberate belt-and-
                # suspenders, never the only place this is enforced,
                # so the evidence spec that actually reaches retrieval
                # and the insufficient/partial message is never built
                # from an unchecked subject_text.
                canonicalized_subject = canonicalize_legal_subject(
                    subject_text=action.effective_subject_text() or None,
                    search_concepts=action.search_concepts,
                    scoped_country_codes=action_scope.available_codes,
                )

                if canonicalized_subject.changed:
                    metrics.subject_scope_canonicalization_applied = True
                    metrics.search_concepts_canonicalized = True
                    metrics.subject_scope_removed_country_codes = sorted(
                        {
                            *metrics.subject_scope_removed_country_codes,
                            *canonicalized_subject.removed_country_codes,
                        }
                    )

                if canonicalized_subject.subject_became_empty:
                    metrics.subject_empty_after_canonicalization = True

                action_specs.append(
                    LegalActionEvidenceSpec(
                        country_codes=(
                            action_scope.available_codes
                        ),
                        # Explicit legal_topics on the original
                        # request are a binding, canonical-only client
                        # override - matching effective_legal_topics'
                        # override rule above, applied per action -
                        # and take priority over everything else here,
                        # exactly as before "ORDER 8F-A". Only when the
                        # client left legal_topics unset does the new
                        # retrieval-filter priority (mission section 7)
                        # apply: A. an explicit/dynamic document topic
                        # resolved for this action -> filter on its
                        # exact live value(s), never the nearest
                        # canonical guess; B. otherwise, the existing
                        # canonical-topic behavior; C. neither ->
                        # no fabricated hard filter (topic_text-only
                        # free-text retrieval).
                        legal_topics=(
                            list(request.legal_topics)
                            if request.legal_topics
                            else (
                                validated_document_topics
                                if validated_document_topics
                                else validated_topics
                            )
                        ),
                        subject_text=canonicalized_subject.subject_text,
                        search_concepts=(
                            canonicalized_subject.search_concepts
                            or None
                        ),
                        evidence_mode=(
                            action.resolved_evidence_mode()
                        ),
                    )
                )

        # Explicit legal_topics on the original request are a binding
        # retrieval constraint - they override whatever the model
        # inferred for this call.
        effective_legal_topics = (
            list(request.legal_topics)
            if request.legal_topics
            else merged_legal_topics
        )

        merged_question = (
            merged_question_parts[0]
            if len(merged_question_parts) == 1
            else "\n\n".join(merged_question_parts)
        )

        if merged_available_codes:
            prepared_request = request.model_copy(
                update={
                    "country_codes": merged_available_codes,
                    "legal_topics": effective_legal_topics,
                    "question": merged_question,
                }
            )

            # Each legal-type action is retrieved and evidence-graded
            # against only its own country/topic/concepts - never a
            # single "representative" action standing in for a mixed
            # request's other actions (0.4.2 hardening) - see
            # LegalActionEvidenceSpec. Still exactly one combined
            # generation call.
            legal_response = answer_legal_question(
                prepared_request,
                search_function=search_function,
                generation_client=generation_client,
                rerank_enabled=rerank_enabled,
                rerank_pool_multiplier=rerank_pool_multiplier,
                max_context_characters=max_context_characters,
                max_source_characters=max_source_characters,
                metrics=metrics,
                action_specs=action_specs or None,
                known_excluded_country_codes=(
                    merged_unavailable_codes or None
                ),
            )

            answer_parts.append(legal_response.answer)
            sources.extend(legal_response.sources)
            grounded = legal_response.grounded
            model_used = legal_response.model
            retrieval_total += legal_response.retrieval_total

            if merged_unavailable_codes:
                answer_parts.append(
                    "Note: "
                    + _unavailable_countries_answer(
                        merged_unavailable_codes
                    )
                )
        elif merged_unavailable_codes:
            metrics.outcome = "fallback_unavailable_country"
            answer_parts.append(
                _unavailable_countries_answer(
                    merged_unavailable_codes
                )
            )
        else:
            metrics.outcome = "fallback_missing_country"
            answer_parts.append(MISSING_COUNTRY_ANSWER)

    for action in contact_actions:
        action_scope = resolve_country_availability(
            request=request.model_copy(
                update={"country_codes": action.country_codes}
            ),
            catalog_provider=catalog_provider,
        )

        (
            contact_answer,
            contact_sources,
            contact_retrieval_total,
            took_ms,
        ) = _build_contact_section(
            country_codes=action_scope.available_codes,
            unavailable_country_codes=action_scope.unavailable_codes,
            citation_offset=len(sources),
        )

        metrics.opensearch_ms += took_ms
        retrieval_total += contact_retrieval_total

        if contact_answer:
            answer_parts.append(contact_answer)

        sources.extend(contact_sources)

        resolved_action_countries.append(
            {
                "type": "contact",
                "country_codes": action_scope.available_codes,
            }
        )
        executed.append((action, action_scope.available_codes))

        if contact_sources:
            grounded = True

    metrics.request_actions = result.action_types
    metrics.resolved_action_countries = resolved_action_countries
    metrics.resolved_action_topics = resolved_action_topics
    metrics.selected_sources = len(sources)
    metrics.retrieval_total = retrieval_total

    # Backward-compatible flat aggregates (see chat_metrics.py).
    metrics.resolved_country_codes = _aggregate_action_country_codes(
        resolved_action_countries
    )
    metrics.resolved_legal_topics = _aggregate_action_legal_topics(
        resolved_action_topics
    )

    if not legal_type_actions:
        # Pure contact request(s): never any legal generation.
        metrics.model = None
        metrics.generation_attempts = 0
        metrics.repair_triggered = False
        metrics.repair_success = False
        metrics.repair_answer_returned = False
        metrics.outcome = (
            "contact_resolved" if grounded else "contact_not_found"
        )

    try:
        next_conversation_state = build_next_conversation_state(
            executed=executed
        )
    except Exception:
        # Constructing the next conversation_state must never cost
        # the user their already-resolved answer - degrade to
        # carrying no state forward rather than raise.
        next_conversation_state = None

    metrics.conversation_state_emitted = (
        next_conversation_state is not None
    )

    return LegalChatResponse(
        question=request.question.strip(),
        answer="\n\n".join(
            part for part in answer_parts if part
        ),
        grounded=grounded,
        model=model_used,
        retrieval_total=retrieval_total,
        sources=sources,
        conversation_state=next_conversation_state,
    )


def _with_resolved_subject_precision(
    action: RequestUnderstandingAction,
) -> RequestUnderstandingAction:
    """
    Reconcile one action's subject_specificity/evidence_mode via its
    own resolved_subject_precision() - applied once, here, to every
    action (fresh or inherited) before retrieval/generation or
    storage ever reads either field, so a model output that mislabels
    a precise question as broad (real search_concepts like "remote
    work"/"telework" alongside evidence_mode="broad_topic") is never
    trusted over what its own search_concepts actually prove.
    """

    subject_specificity, evidence_mode = (
        action.resolved_subject_precision()
    )

    return action.model_copy(
        update={
            "subject_specificity": subject_specificity,
            "evidence_mode": evidence_mode,
        }
    )


def resolve_legal_chat_response(
    request: LegalChatRequest,
    request_id: str | None = None,
    catalog_provider: CountryCatalogProvider = (
        get_legal_catalog
    ),
    document_topic_provider: DocumentLegalTopicsProvider = (
        get_document_legal_topics_by_country
    ),
    search_function: SearchFunction = (
        search_legal_documents
    ),
    generation_client: (
        TextGenerationClient | None
    ) = None,
    understanding_client: (
        OpenAIResponsesClient | None
    ) = None,
    rerank_enabled: bool = False,
    rerank_pool_multiplier: int = 1,
    max_context_characters: int = (
        DEFAULT_MAX_CONTEXT_CHARACTERS
    ),
    max_source_characters: int = (
        DEFAULT_MAX_SOURCE_CHARACTERS
    ),
) -> LegalChatResponse:
    """
    Resolve one legal-chat request.

    RequestUnderstanding is the primary router for every free-text
    request: deterministic country/topic detection and the
    STRONG_CONTACT_INTENT / COUNTRY_SCOPED_REACH_INTENT regexes only
    ever feed it hints (see _build_deterministic_hints) - they never
    again decide, on their own, that a request is fully understood.

    Exactly one "legal_chat_performance" log event is emitted per
    call, on every path (clarification, resolved, fallback, or error).
    """

    total_started_at = perf_counter()

    metrics = LegalChatMetrics(
        request_id=(
            request_id
            if request_id
            else str(uuid4())
        ),
        question_characters=len(
            request.question
        ),
        max_sources=request.max_sources,
        rerank_enabled=rerank_enabled,
    )

    metrics.history_messages = len(
        request.history
    )
    metrics.history_characters = sum(
        len(message.content)
        for message in request.history
    )

    # Corrective gate, section 9: a bare country-name reply to an
    # ambiguous-city clarification ("Barcelona" -> ask -> "Spain")
    # resumes the ORIGINAL question with that country substituted for
    # the city, rewriting request.question once, right here, before
    # anything else (conversation_meta, hints, RequestUnderstanding)
    # ever sees it - every downstream step then behaves exactly as if
    # the user had asked the resolved question from the start. A
    # no-op (returns None) for every other request.
    resumed_question = resolve_ambiguous_city_followup_question(
        question=request.question,
        history=request.history,
    )

    if resumed_question is not None:
        request = request.model_copy(
            update={"question": resumed_question}
        )

    # Mission "ORDER 5C-GEO", section 25/26: this one request goes on
    # to call resolve_conversation_meta, _build_deterministic_hints
    # (itself up to two calls), understand_request, and
    # _execute_resolved_plan - each independently invoking
    # catalog_provider for what is, within one request, always the
    # exact same real indexed-country catalog. A request-scoped
    # memoization (created fresh here, discarded with this call frame,
    # never a persistent/global cache to invalidate) turns that into a
    # single real catalog fetch per request, not four or more.
    cached_catalog_results: list[LegalCatalogResponse] = []

    def memoized_catalog_provider() -> LegalCatalogResponse:
        if not cached_catalog_results:
            cached_catalog_results.append(catalog_provider())

        return cached_catalog_results[0]

    meta_resolution = resolve_conversation_meta(
        question=request.question,
        history=request.history,
        conversation_state=request.conversation_state,
        catalog_provider=memoized_catalog_provider,
    )

    if meta_resolution is not None:
        metrics.outcome = (
            "conversation_meta_"
            f"{meta_resolution.intent_type}"
        )
        metrics.total_ms = (
            perf_counter() - total_started_at
        ) * 1000
        metrics.log()

        response_state = (
            request.conversation_state
            if meta_resolution.preserve_conversation_state
            else None
        )

        return LegalChatResponse(
            question=request.question.strip(),
            answer=meta_resolution.answer,
            grounded=False,
            model=None,
            retrieval_total=0,
            sources=[],
            conversation_state=response_state,
        )

    # Assistant-help/meta-intent detection runs first, before any
    # other check in this function - _build_deterministic_hints below
    # already calls the OpenSearch-backed legal catalog (via
    # resolve_country_availability), so this must come strictly
    # earlier to guarantee zero OpenSearch calls for a help question
    # (mission "PATCH PRODUIT 0.4.3"). Zero OpenAI calls either: no
    # RequestUnderstanding, no retrieval, no generation on this path.
    # The incoming conversation_state is returned completely
    # unchanged - a help question must never advance, reset, or lose
    # whatever legal action/focus a prior turn had (section 15).
    help_intent = detect_assistant_help_intent(
        request.question,
        tuple(country.code for country in COUNTRIES),
    )

    if help_intent is not None:
        metrics.outcome = f"assistant_help_{help_intent.intent_type}"
        metrics.total_ms = (
            perf_counter() - total_started_at
        ) * 1000
        metrics.log()

        return LegalChatResponse(
            question=request.question.strip(),
            answer=build_assistant_help_answer(
                help_intent, original_question=request.question
            ),
            grounded=False,
            model=None,
            retrieval_total=0,
            sources=[],
            conversation_state=request.conversation_state,
        )

    try:
        (
            hints,
            current_country_scope,
            current_legal_scope,
        ) = _build_deterministic_hints(
            request=request,
            catalog_provider=memoized_catalog_provider,
            document_topic_provider=document_topic_provider,
        )

        history_turns = [
            HistoryTurn(role=message.role, content=message.content)
            for message in request.history
        ]

        previous_conversation_state = request.conversation_state

        metrics.conversation_state_received = (
            previous_conversation_state is not None
        )

        if previous_conversation_state is not None:
            metrics.conversation_state_version = (
                previous_conversation_state.version
            )
            metrics.previous_action_types = [
                action.type
                for action in previous_conversation_state.actions
            ]

            focus_index = (
                previous_conversation_state.focus_action_index
            )

            if focus_index is not None:
                metrics.previous_focus_action = (
                    previous_conversation_state.actions[
                        focus_index
                    ].type
                )

        outcome = understand_request(
            current_question=request.question,
            history=history_turns,
            hints=hints,
            conversation_state=previous_conversation_state,
            catalog_provider=memoized_catalog_provider,
            generation_client=understanding_client,
        )

        metrics.request_understanding_ms = outcome.elapsed_ms
        metrics.request_understanding_openai_ms = outcome.openai_ms
        metrics.request_understanding_attempts = outcome.attempts
        metrics.request_understanding_retry_triggered = (
            outcome.retry_triggered
        )
        metrics.request_understanding_retry_reason = (
            outcome.retry_reason
        )
        metrics.openai_ms += outcome.openai_ms

        if outcome.result is None:
            local_result = _try_local_country_only_followup_result(
                question=request.question,
                conversation_state=previous_conversation_state,
            )

            if local_result is None:
                metrics.request_understanding_method = "fallback"
                metrics.request_understanding_error = outcome.error

                response = _resolve_conservative_fallback(
                    request=request,
                    hints=hints,
                    current_country_scope=current_country_scope,
                    current_legal_scope=current_legal_scope,
                    metrics=metrics,
                    search_function=search_function,
                    generation_client=generation_client,
                    rerank_enabled=rerank_enabled,
                    rerank_pool_multiplier=rerank_pool_multiplier,
                    max_context_characters=max_context_characters,
                    max_source_characters=max_source_characters,
                )

                metrics.total_ms = (
                    perf_counter() - total_started_at
                ) * 1000

                metrics.log()

                return response

            # A bare country-only follow-up, resolved deterministically
            # with no further OpenAI call - proceed exactly as if
            # understanding had itself returned this result.
            metrics.request_understanding_method = (
                "local_deterministic"
            )
            metrics.request_understanding_error = outcome.error
            result = local_result
        else:
            result = outcome.result

            metrics.request_understanding_method = "semantic"

        metrics.request_understanding_confidence = result.confidence
        metrics.contextual_question_used = result.is_follow_up
        metrics.current_message_operation = (
            result.current_message_delta.context_operation
        )

        transition_started_at = perf_counter()

        transition_outcome = apply_conversation_transition(
            result=result,
            conversation_state=previous_conversation_state,
            hints=hints,
            current_question=request.question,
        )

        metrics.conversation_transition_ms = (
            perf_counter() - transition_started_at
        ) * 1000
        metrics.semantic_result_overridden = (
            transition_outcome.semantic_result_overridden
        )
        metrics.semantic_override_reason = (
            transition_outcome.semantic_override_reason
        )
        metrics.context_inheritance_applied = (
            transition_outcome.context_inheritance_applied
        )
        metrics.inherited_action_type = (
            transition_outcome.inherited_action_type
        )
        metrics.inherited_country_replaced = (
            transition_outcome.inherited_country_replaced
        )
        metrics.subject_scope_canonicalization_applied = (
            transition_outcome.subject_canonicalization_applied
        )
        metrics.subject_scope_removed_country_codes = (
            transition_outcome.subject_scope_removed_country_codes
        )
        metrics.inherited_subject_canonicalized = (
            transition_outcome.inherited_subject_canonicalized
        )

        final_result = RequestUnderstandingResult(
            status=transition_outcome.final_status,
            actions=[
                _with_resolved_subject_precision(action)
                for action in transition_outcome.final_actions
            ],
            is_follow_up=result.is_follow_up,
            confidence=result.confidence,
            clarification_reason=(
                transition_outcome.final_clarification_reason
            ),
            current_message_delta=result.current_message_delta,
        )

        metrics.request_status = final_result.status

        if final_result.actions:
            metrics.final_subject_text = any(
                bool(action.subject_text)
                for action in final_result.actions
            )
            metrics.search_concept_groups = sum(
                len(action.search_concepts)
                for action in final_result.actions
            )
            metrics.inherited_legal_topics = [
                topic
                for action in final_result.actions
                for topic in action.legal_topics
            ]

        if _check_explicit_filter_conflict(request, final_result):
            metrics.clarification_reason = "ambiguous_request"
            metrics.request_status = "clarification"
            metrics.outcome = "clarification_ambiguous_request"

            metrics.total_ms = (
                perf_counter() - total_started_at
            ) * 1000

            metrics.log()

            return LegalChatResponse(
                question=request.question.strip(),
                answer=(
                    CLARIFICATION_EXPLICIT_FILTER_CONFLICT_ANSWER
                ),
                grounded=False,
                model=None,
                retrieval_total=0,
                sources=[],
                conversation_state=None,
            )

        if final_result.status == "unsupported":
            metrics.clarification_reason = "unsupported_request"
            metrics.outcome = "clarification_unsupported_request"

            metrics.total_ms = (
                perf_counter() - total_started_at
            ) * 1000

            metrics.log()

            return LegalChatResponse(
                question=request.question.strip(),
                answer=CLARIFICATION_UNSUPPORTED_REQUEST_ANSWER,
                grounded=False,
                model=None,
                retrieval_total=0,
                sources=[],
                conversation_state=None,
            )

        if final_result.status == "clarification":
            metrics.clarification_reason = (
                final_result.clarification_reason
            )

            if transition_outcome.pending_clarification is not None:
                metrics.clarification_options = list(
                    transition_outcome.pending_clarification
                    .candidate_action_types
                )

            unavailable_hint = (
                hints.current_unavailable_country_codes
                or hints.history_unavailable_country_codes
            )

            if (
                final_result.clarification_reason == "missing_country"
                and unavailable_hint
            ):
                answer_text = _unavailable_countries_answer(
                    unavailable_hint
                )
                metrics.outcome = "fallback_unavailable_country"
            elif (
                transition_outcome.contextual_clarification_answer
                is not None
            ):
                answer_text = (
                    transition_outcome.contextual_clarification_answer
                )
                metrics.outcome = (
                    "clarification_"
                    f"{final_result.clarification_reason}"
                )
            else:
                answer_text = _clarification_answer_for(final_result)
                metrics.outcome = (
                    "clarification_"
                    f"{final_result.clarification_reason}"
                )

            response_conversation_state = None

            if transition_outcome.pending_clarification is not None:
                response_conversation_state = (
                    build_next_conversation_state(
                        executed=[],
                        pending_clarification=(
                            transition_outcome.pending_clarification
                        ),
                    )
                )
                metrics.conversation_state_emitted = True

            metrics.total_ms = (
                perf_counter() - total_started_at
            ) * 1000

            metrics.log()

            return LegalChatResponse(
                question=request.question.strip(),
                answer=answer_text,
                grounded=False,
                model=None,
                retrieval_total=0,
                sources=[],
                conversation_state=response_conversation_state,
            )

        response = _execute_resolved_plan(
            request=request,
            result=final_result,
            hints=hints,
            metrics=metrics,
            catalog_provider=memoized_catalog_provider,
            search_function=search_function,
            generation_client=generation_client,
            rerank_enabled=rerank_enabled,
            rerank_pool_multiplier=rerank_pool_multiplier,
            max_context_characters=max_context_characters,
            max_source_characters=max_source_characters,
        )

        if requires_personalised_legal_caution(
            request.question
        ):
            response = response.model_copy(
                update={
                    "answer": (
                        append_personalised_legal_caution(
                            response.answer
                        )
                    )
                }
            )

        metrics.total_ms = (
            perf_counter() - total_started_at
        ) * 1000

        metrics.log()

        return response

    except Exception as error:
        metrics.outcome = "error"
        metrics.error_type = type(error).__name__
        metrics.transition_error = isinstance(
            error, ConversationTransitionError
        )

        metrics.total_ms = (
            perf_counter() - total_started_at
        ) * 1000

        metrics.log()

        raise


@router.post(
    "/chat",
    response_model=LegalChatResponse,
    response_model_exclude_none=True,
)
def legal_chat(
    request: LegalChatRequest,
    response: Response,
    x_request_id: str | None = Header(
        default=None,
        alias="X-Request-ID",
    ),
) -> LegalChatResponse:
    """Generate an answer grounded in validated documents."""

    settings = get_settings()

    request_id = (
        x_request_id.strip()
        if x_request_id
        else str(uuid4())
    )

    response.headers["X-Request-ID"] = request_id

    try:
        return resolve_legal_chat_response(
            request,
            request_id=request_id,
            rerank_enabled=settings.rerank_enabled,
            rerank_pool_multiplier=(
                settings.rerank_pool_multiplier
            ),
            max_context_characters=(
                settings.rag_max_context_characters
            ),
            max_source_characters=(
                settings.rag_max_source_characters
            ),
        )

    except InvalidLegalChatRequestError as error:
        if error.code == "comparison_source_budget":
            country_count = error.details.get(
                "country_count"
            )

            if (
                not isinstance(country_count, int)
                or country_count <= 0
            ):
                country_count = request.max_sources + 1

            source_word = (
                "source"
                if request.max_sources == 1
                else "sources"
            )
            country_word = (
                "country"
                if request.max_sources == 1
                else "countries"
            )

            return LegalChatResponse(
                question=request.question.strip(),
                answer=(
                    f"This comparison includes "
                    f"{country_count} countries, but the "
                    f"current response can cite up to "
                    f"{request.max_sources} {source_word}. "
                    "To keep at least one source for each "
                    f"country, please choose up to "
                    f"{request.max_sources} {country_word} "
                    "or split the comparison into smaller "
                    "groups."
                ),
                grounded=False,
                model=None,
                retrieval_total=0,
                sources=[],
                conversation_state=(
                    request.conversation_state
                ),
            )

        raise HTTPException(
            status_code=(
                status.HTTP_422_UNPROCESSABLE_ENTITY
            ),
            detail=str(
                error
            ),
            headers={
                "X-Request-ID": request_id,
            },
        ) from error

    except OpenAIConfigurationError as error:
        raise HTTPException(
            status_code=(
                status.HTTP_503_SERVICE_UNAVAILABLE
            ),
            detail=(
                "The answer generation service "
                "is not configured."
            ),
            headers={
                "X-Request-ID": request_id,
            },
        ) from error

    except CountryDetectionError as error:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(
                "The country detection service "
                "is temporarily unavailable."
            ),
            headers={
                "X-Request-ID": request_id,
            },
        ) from error

    except RagAnswerError as error:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(
                "The grounded legal answer service "
                "is temporarily unavailable."
            ),
            headers={
                "X-Request-ID": request_id,
            },
        ) from error

    except ConversationTransitionError as error:
        # An unanticipated internal error in the deterministic
        # transition engine - never search, never generate, and never
        # silently fall back to the classifier's own raw (possibly
        # stale-context) result for this request. The internal cause
        # is logged (see metrics.transition_error) but never exposed
        # to the client.
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(
                "The conversation context could not be "
                "processed for this request."
            ),
            headers={
                "X-Request-ID": request_id,
            },
        ) from error
