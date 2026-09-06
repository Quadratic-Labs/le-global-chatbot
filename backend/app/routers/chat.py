"""HTTP endpoint for grounded legal answers."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from time import perf_counter
from typing import Final, TypeAlias
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
from app.models.conversation_state import ConversationPendingClarification
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
    resolve_contextual_multi_country_contact_codes,
)
from app.services.conversation_meta import (
    append_personalised_legal_caution,
    requires_personalised_legal_caution,
    resolve_ambiguous_city_followup_question,
    resolve_conversation_meta,
)

from app.services.chat_contact_cards import (
    CONTACT_COUNTRY_FALLBACK_CODES,
    build_legal_chat_contacts,
    resolve_public_contact_photo,
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
from app.services.jurisdiction_resolution import (
    resolve_city_country_codes,
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
    sanitize_user_facing_legal_answer,
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


# The one swappable seam _execute_resolved_plan/resolve_legal_chat_
# response expose for streaming (chat-streaming initiative, GATE S4) -
# see _execute_resolved_plan's own docstring. Loosely typed as
# Callable[..., LegalChatResponse] (not a strict Protocol) since the
# real call site (line ~2119) passes a mix of positional/keyword
# arguments that answer_legal_question's own signature already
# defines; the streaming bridge (app/routers/chat_stream.py) matches
# it via functools.partial, never by redeclaring the signature here.
AnswerGenerationFunction: TypeAlias = Callable[..., LegalChatResponse]


def _optional_contact_source_directory():
    """Return the contact-card source directory when settings exist.

    resolve_legal_chat_response is intentionally callable directly by
    backend tests and internal code without bootstrapping the complete
    deployment environment. Contact cards are additive/optional, so
    missing application settings must never remove the already-valid
    text answer.
    """

    try:
        return get_settings().document_source_dir
    except RuntimeError:
        return None


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

CLARIFICATION_UNSUPPORTED_REQUEST_WITH_COUNTRY_TEMPLATE: Final[str] = (
    "This assistant can only answer employment law questions, and "
    "related L&E Global contacts, covered by the validated documents. "
    "Please rephrase your question within that scope, or contact our "
    "L&E Global member firm in {country} for further assistance."
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
    """Explain that a recognized country is outside this chatbot corpus."""

    display_names = [
        resolve_country_display_name(country_code)
        for country_code in unavailable_codes
    ]

    if len(display_names) == 1:
        country = display_names[0]

        return (
            f"{country} is not currently covered by the validated "
            "L&E Global documents available in this chatbot. "
            "I therefore cannot provide employment-law information "
            f"or a validated L&E Global contact for {country}."
        )

    countries = _format_country_list(display_names)

    return (
        "The following countries are not currently covered by the "
        "validated L&E Global documents available in this chatbot: "
        f"{countries}. I therefore cannot provide employment-law "
        "information or validated L&E Global contacts for those "
        "countries."
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



def _has_location_scoped_contact_request(question: str) -> bool:
    """Recognize a direct request for contact data tied to a location."""

    normalized = _normalize_contact_question(question)

    acquisition = re.search(
        r"\b(?:can|could|may)\s+i\s+(?:have|get)\b"
        r"|\b(?:give|send|show)\s+me\b"
        r"|\bprovide\s+me\s+with\b"
        r"|\bi\s+(?:need|want)\b",
        normalized,
    )

    contact_data = re.search(
        r"\bcontact\s+(?:details|information|info)\b",
        normalized,
    )

    return bool(acquisition and contact_data)



def _normalize_city_label(value: str) -> str:
    """Normalize a city/capital label for deterministic comparison."""

    import unicodedata

    decomposed = unicodedata.normalize(
        "NFKD",
        value.strip().casefold(),
    )

    return "".join(
        character
        for character in decomposed
        if not unicodedata.combining(character)
    )


def _resolve_unique_capital_country_code(
    city_name: str,
    candidate_codes: frozenset[str],
) -> str | None:
    """
    Prefer a country only when this ambiguous city is the national
    capital of exactly one candidate country.

    Examples:
      Rome -> Italy among IT/TG/US.
      Milan -> no preference.
      Barcelona -> no preference.
    """

    if len(candidate_codes) < 2:
        return None

    try:
        import geonamescache

        countries = (
            geonamescache.GeonamesCache()
            .get_countries()
        )
    except Exception:
        return None

    wanted = _normalize_city_label(city_name)

    matches: list[str] = []

    for country_code in sorted(candidate_codes):
        country = countries.get(country_code)

        if not country:
            continue

        capital = str(
            country.get("capital") or ""
        ).strip()

        if (
            capital
            and _normalize_city_label(capital)
            == wanted
        ):
            matches.append(country_code)

    if len(matches) == 1:
        return matches[0]

    return None


def _supported_demonym_country_codes(
    *,
    question: str,
    catalog_provider: CountryCatalogProvider,
) -> list[str]:
    """
    Return supported countries explicitly expressed through a curated
    national adjective/demonym, e.g. German -> DE.

    This does not change general country detection. It is consumed only
    by narrowly-scoped deterministic routing.
    """

    from app.services.country_detection import (
        get_country_demonyms,
    )

    normalized_question = (
        " "
        + re.sub(
            r"[^a-z0-9]+",
            " ",
            question.casefold(),
        ).strip()
        + " "
    )

    result: list[str] = []

    catalog = catalog_provider()

    for country in catalog.countries:
        raw_code = getattr(
            country,
            "country_code",
            None,
        )

        if raw_code is None:
            raw_code = getattr(
                country,
                "value",
                None,
            )

        if not isinstance(raw_code, str):
            continue

        candidate_code = raw_code.strip().upper()

        if len(candidate_code) != 2:
            continue

        for demonym in get_country_demonyms(
            candidate_code
        ):
            normalized_demonym = re.sub(
                r"[^a-z0-9]+",
                " ",
                demonym.casefold(),
            ).strip()

            if (
                normalized_demonym
                and (
                    " "
                    + normalized_demonym
                    + " "
                )
                in normalized_question
            ):
                if candidate_code not in result:
                    result.append(candidate_code)

                break

    return result


def _resolve_current_country_scope(
    request: LegalChatRequest,
    catalog_provider: CountryCatalogProvider,
) -> CountryAvailability:
    """
    Resolve explicit countries first.

    For a direct contact request, a city may supply the country:
    - one geographic candidate -> use it;
    - several candidates -> prefer the only candidate covered by the
      current L&E corpus;
    - if several covered candidates remain -> prefer a unique national
      capital match;
    - otherwise keep the request ambiguous rather than guessing.
    """

    scope = resolve_country_availability(
        request=request,
        catalog_provider=catalog_provider,
    )

    if _CHOICE_OF_LAW_REQUEST_PATTERN.search(
        request.question
    ):
        demonym_codes = _supported_demonym_country_codes(
            question=request.question,
            catalog_provider=catalog_provider,
        )

        merged_available_codes = list(
            dict.fromkeys(
                [
                    *scope.available_codes,
                    *demonym_codes,
                ]
            )
        )

        if merged_available_codes != list(
            scope.available_codes
        ):
            scope = CountryAvailability(
                available_codes=merged_available_codes,
                unavailable_codes=list(
                    scope.unavailable_codes
                ),
            )

    if (
        scope.available_codes
        or scope.unavailable_codes
        or request.country_codes
    ):
        return scope

    contact_request = (
        _detect_contact_intent(request.question)
        or _has_direct_who_to_reach_form(request.question)
        or _has_location_scoped_contact_request(
            request.question
        )
    )

    if not contact_request:
        return scope

    city_codes, matched_location = (
        resolve_city_country_codes(
            request.question
        )
    )

    location_text = matched_location or ""

    # Contact phrases often contain the locality in a final
    # "for/in/at/near <city>" fragment.
    if len(city_codes) != 1:
        location_match = re.search(
            r"\b(?:for|in|at|near)\s+"
            r"([A-Za-zÀ-ÖØ-öø-ÿ'’ .-]{2,80}?)"
            r"\s*[?.!]*$",
            request.question,
            re.IGNORECASE,
        )

        if location_match is not None:
            location_fragment = (
                location_match.group(1).strip()
            )

            (
                fragment_codes,
                fragment_location,
            ) = resolve_city_country_codes(
                location_fragment
            )

            if fragment_codes:
                city_codes = fragment_codes
                location_text = (
                    fragment_location
                    or location_fragment
                )

    if not city_codes:
        return scope

    # Straightforward city.
    if len(city_codes) == 1:
        selected_codes = city_codes

    else:
        # Ask the existing country-availability layer which of the
        # geographic candidates are actually covered by the current
        # validated corpus.
        candidate_scope = resolve_country_availability(
            request=request.model_copy(
                update={
                    "country_codes": sorted(city_codes)
                }
            ),
            catalog_provider=catalog_provider,
        )

        supported_candidates = frozenset(
            candidate_scope.available_codes
        )

        if len(supported_candidates) == 1:
            selected_codes = supported_candidates

        elif len(supported_candidates) >= 2:
            capital_code = (
                _resolve_unique_capital_country_code(
                    location_text,
                    supported_candidates,
                )
            )

            if capital_code is None:
                return scope

            selected_codes = frozenset(
                {capital_code}
            )

        else:
            # No candidate is currently covered. Do not guess among
            # several unsupported countries.
            return scope

    return resolve_country_availability(
        request=request.model_copy(
            update={
                "country_codes": sorted(selected_codes)
            }
        ),
        catalog_provider=catalog_provider,
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

    current_country_scope = _resolve_current_country_scope(
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
            or _has_location_scoped_contact_request(
                request.question
            )
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
        if country_code.upper() in CONTACT_COUNTRY_FALLBACK_CODES:
            render(country_code)
        else:
            answer_sections.append(
                _unavailable_countries_answer([country_code])
            )

    return (
        "\n\n".join(answer_sections),
        sources,
        retrieval_total,
        took_ms,
    )


_CHOICE_OF_LAW_REQUEST_PATTERN: Final[
    re.Pattern[str]
] = re.compile(
    r"(?:"
    r"\bchoice\s+of\s+law\b|"
    r"\bgoverning\s+(?:employment\s+)?law\b|"
    r"\bwhich\s+country(?:'s)?\s+(?:employment\s+)?law\b|"
    r"\bwhich\s+(?:employment\s+)?law\s+"
    r"(?:applies|governs)\b|"
    r"\b(?:employment\s+)?law\s+"
    r"(?:automatically\s+)?(?:applies|governs)\b"
    r")",
    re.IGNORECASE,
)


def _try_local_choice_of_law_recovery(
    *,
    question: str,
    result: RequestUnderstandingResult,
    current_country_scope: CountryAvailability,
    catalog_provider: CountryCatalogProvider,
) -> RequestUnderstandingResult | None:
    """
    Recover a clearly expressed employment choice-of-law question when
    probabilistic request understanding incorrectly returns
    missing_topic.

    This does not determine WHICH country's law governs. It only
    preserves the legal subject the user explicitly asked about so the
    normal grounded RAG path can decide whether the validated material
    supports an answer.
    """

    if not _CHOICE_OF_LAW_REQUEST_PATTERN.search(question):
        return None

    # Choice-of-law classification must not depend on the stochastic
    # semantic result when the current message itself clearly expresses
    # a cross-border applicable-law question.
    #
    # Recover:
    # - missing_topic clarification; or
    # - one legal_information action that semantic understanding already
    #   resolved but may have scoped to only one of the two countries.
    #
    # Do not override contacts, multi-action plans, explicit comparison
    # plans or unsupported requests here.
    if result.status == "clarification":
        if result.clarification_reason != "missing_topic":
            return None
    elif result.status == "resolved":
        if (
            len(result.actions) != 1
            or result.actions[0].type != "legal_information"
        ):
            return None
    else:
        return None

    country_codes = list(
        dict.fromkeys(
            current_country_scope.available_codes
        )
    )

    for candidate_code in _supported_demonym_country_codes(
        question=question,
        catalog_provider=catalog_provider,
    ):
        if candidate_code not in country_codes:
            country_codes.append(candidate_code)

    # Deterministic normalization is deliberately cross-border only.
    # A normal one-country question containing words such as
    # "employment law applies" must stay under ordinary routing.
    if len(country_codes) < 2:
        return None

    existing_action = next(
        (
            action
            for action in result.actions
            if action.type == "legal_information"
        ),
        None,
    )

    action_updates = {
        "country_codes": country_codes,
        "legal_topics": [],
        "document_legal_topics": [],
        "topic_text": "applicable law / choice of law for employment",
        "resolved_question": question,
        "subject_text": "applicable employment law / choice of law",
        "search_concepts": [
            {
                "terms": [
                    "applicable law",
                    "choice of law",
                    "governing law",
                ]
            },
            {
                "terms": [
                    "place of work",
                    "habitual place of work",
                    "workplace location",
                ]
            },
            {
                "terms": [
                    "employer establishment",
                    "employer registered office",
                    "employer domicile",
                ]
            },
        ],
        "subject_specificity": "specific",
        "evidence_mode": "direct_topic",
    }

    if existing_action is not None:
        action_payload = existing_action.model_dump()
        action_payload.update(action_updates)
    else:
        action_payload = {
            "type": "legal_information",
            **action_updates,
        }

    action = RequestUnderstandingAction.model_validate(
        action_payload
    )

    return result.model_copy(
        update={
            "status": "resolved",
            "actions": [action],
            "clarification_reason": None,
            "confidence": max(
                float(result.confidence),
                0.99,
            ),
            "current_message_delta": CurrentMessageDelta(
                explicit_action_types=["legal_information"],
                explicit_country_codes=country_codes,
                explicit_legal_topics=[],
                explicit_subject_text=(
                    "applicable employment law / choice of law"
                ),
                context_operation="independent",
            ),
        }
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


_ELLIPTICAL_LEGAL_FOLLOWUP_ENDING = re.compile(
    r"\b(?:"
    r"refuse|refuses|refused|"
    r"decline|declines|declined|"
    r"reject|rejects|rejected|"
    r"doesnt|doesn't|dont|don't|"
    r"wont|won't|wouldnt|wouldn't"
    r")\s*[?.!]*$",
    flags=re.IGNORECASE,
)


def _try_local_elliptical_legal_clarification(
    *,
    question: str,
    conversation_state: ConversationState | None,
    hints: DeterministicHints,
) -> str | None:
    """
    Resolve one structurally incomplete legal follow-up locally.

    Example:
        prior: Australia / notice period
        now:   "What if the employee refuses?"

    The verb has no object, so answering would require inventing what
    is being refused. Keep the established legal context and ask only
    for the missing complement. No OpenAI call is needed.

    Deliberately narrow:
    - exactly one existing legal-information action;
    - exactly one already-known country;
    - no newly named country/topic;
    - no contact/comparison signal;
    - only an unmistakably incomplete trailing verb/auxiliary.
    """

    if (
        conversation_state is None
        or len(conversation_state.actions) != 1
        or hints.strong_contact_signal
        or hints.comparison_signal
        or hints.current_country_codes
        or hints.current_legal_topics
    ):
        return None

    action = conversation_state.actions[0]

    if (
        action.type != "legal_information"
        or len(action.country_codes) != 1
    ):
        return None

    if not _ELLIPTICAL_LEGAL_FOLLOWUP_ENDING.search(
        question.strip()
    ):
        return None

    country = resolve_country_display_name(
        action.country_codes[0]
    )

    normalized = question.casefold()

    actor = None

    for candidate in (
        "employee",
        "employer",
        "worker",
    ):
        if re.search(
            rf"\b{candidate}\b",
            normalized,
        ):
            actor = candidate
            break

    if actor is not None:
        missing_detail = (
            f"What exactly is the {actor} refusing "
            "to do or accept?"
        )
    else:
        missing_detail = (
            "What exactly is being refused or rejected?"
        )

    return (
        f"I'll keep {country} and the current employment-law "
        f"context. {missing_detail}"
    )


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



_BROAD_LEGAL_OVERVIEW_DOMAIN_PATTERN: Final[
    re.Pattern[str]
] = re.compile(
    r"\b(?:employment|labou?r)\s+law\b",
    re.IGNORECASE,
)

_BROAD_LEGAL_OVERVIEW_CUE_PATTERN: Final[
    re.Pattern[str]
] = re.compile(
        r"\b(?:"
        r"everything|overview|comprehensive|"
        r"general\s+overview|"
        r"tell\s+me\s+about|"
        r"what\s+should\s+i\s+know\s+about|"
        r"main\s+(?:areas?|topics?)|"
        r"key\s+(?:areas?|topics?)"
        r")\b",
        re.IGNORECASE,
)


_UNSUPPORTED_LEGAL_SIGNAL_PATTERN: Final[
    re.Pattern[str]
] = re.compile(
    r"\b(?:"
    r"law|laws|legal|illegal|unlawful|lawful|"
    r"rights?|liabilit(?:y|ies)|"
    r"compliance|regulat(?:ion|ions|ory)|"
    r"tax(?:es|ation)?|"
    r"corporate|commercial|criminal|"
    r"immigration|visa|"
    r"court|lawsuit|litigation|"
    r"divorce|inheritance|probate|"
    r"incorporat(?:e|ed|ing|ion)|"
    r"(?:create|start|form|set\s+up)\s+"
    r"(?:a|an|my|the)?\s*(?:company|business)"
    r")\b",
    re.IGNORECASE,
)


_PROMPT_OVERRIDE_CUE_PATTERN: Final[
    re.Pattern[str]
] = re.compile(
    r"\b(?:ignore|disregard|forget|bypass|override)\b"
    r"[\s\S]{0,160}"
    r"\b(?:instructions?|rules?|restrictions?|polic(?:y|ies)|"
    r"employment\s+law)\b",
    re.IGNORECASE,
)


_OBVIOUS_NON_LEGAL_TARGET_PATTERN: Final[
    re.Pattern[str]
] = re.compile(
    r"\b(?:"
    r"weather|forecast|temperature|"
    r"restaurants?|recipes?|"
    r"sports?|football|soccer|basketball|"
    r"hotels?|tourism|travel|"
    r"movies?|music|games?"
    r")\b",
    re.IGNORECASE,
)


def _should_offer_contact_for_unsupported_request(
    question: str,
) -> bool:
    """
    Return True only when an unsupported request is clearly legal or
    legal-adjacent.

    Non-legal requests such as weather must stay on the simple product
    scope refusal and must not trigger contact retrieval.
    """

    if (
        _PROMPT_OVERRIDE_CUE_PATTERN.search(question)
        and _OBVIOUS_NON_LEGAL_TARGET_PATTERN.search(question)
    ):
        return False

    return bool(
        _UNSUPPORTED_LEGAL_SIGNAL_PATTERN.search(question)
    )


_GENERAL_EMPLOYMENT_REQUEST_PATTERN: Final[
    re.Pattern[str]
] = re.compile(
    r"\b(?:"
    r"employment(?:\s+law)?|"
    r"labou?r\s+law|"
    r"legal\s+(?:information|info)|"
    r"employers?|employees?"
    r")\b",
    re.IGNORECASE,
)


def _try_local_missing_topic_result(
    *,
    question: str,
    result: RequestUnderstandingResult,
    previous_conversation_state: ConversationState | None,
    hints: DeterministicHints,
    current_country_scope: CountryAvailability,
    current_legal_scope: LegalScope,
) -> RequestUnderstandingResult | None:
    """
    Convert a fresh general employment-law request into a topic
    clarification when one supported country is known but no supported
    employment-law topic is identifiable.

    Explicit whole-domain overview requests remain supported. Requests
    already classified as unsupported remain untouched, preserving the
    legal out-of-scope/contact fallback.
    """

    explicit_broad_overview = bool(
        _BROAD_LEGAL_OVERVIEW_DOMAIN_PATTERN.search(question)
        and _BROAD_LEGAL_OVERVIEW_CUE_PATTERN.search(question)
    )

    semantic_result_has_topic = any(
        action.type == "legal_information"
        and (
            action.legal_topics
            or action.document_legal_topics
            or (
                action.topic_text
                and _CHOICE_OF_LAW_REQUEST_PATTERN.search(question)
            )
        )
        for action in result.actions
    )

    if (
        result.status == "unsupported"
        or semantic_result_has_topic
        or previous_conversation_state is not None
        or hints.strong_contact_signal
        or hints.comparison_signal
        or current_country_scope.unavailable_codes
        or len(current_country_scope.available_codes) != 1
        or current_legal_scope.legal_topics
        or explicit_broad_overview
        or not _GENERAL_EMPLOYMENT_REQUEST_PATTERN.search(question)
    ):
        return None

    country_code = current_country_scope.available_codes[0]

    return RequestUnderstandingResult(
        status="clarification",
        actions=[
            RequestUnderstandingAction(
                type="legal_information",
                country_codes=[country_code],
                legal_topics=[],
                document_legal_topics=[],
                topic_text=None,
                subject_text="employment law",
                resolved_question=question.strip(),
                search_concepts=[],
                subject_specificity="broad",
                evidence_mode="broad_topic",
            )
        ],
        is_follow_up=False,
        confidence=1.0,
        clarification_reason="missing_topic",
        current_message_delta=CurrentMessageDelta(
            explicit_action_types=["legal_information"],
            explicit_country_codes=[country_code],
            explicit_legal_topics=[],
            explicit_subject_text="employment law",
            context_operation="independent",
        ),
    )


def _try_local_broad_legal_overview_result(
    *,
    question: str,
    previous_conversation_state: ConversationState | None,
    hints: DeterministicHints,
    current_country_scope: CountryAvailability,
) -> RequestUnderstandingResult | None:
    """
    Recover only a fresh, explicit whole-domain employment-law
    overview when semantic understanding incorrectly asks whether the
    user wants legal information or contact details.

    This never fires for contact, comparison, multi-country or
    conversational requests.
    """

    if (
        previous_conversation_state is not None
        or hints.strong_contact_signal
        or hints.comparison_signal
        or current_country_scope.unavailable_codes
        or len(current_country_scope.available_codes) != 1
        or not _BROAD_LEGAL_OVERVIEW_DOMAIN_PATTERN.search(question)
        or not _BROAD_LEGAL_OVERVIEW_CUE_PATTERN.search(question)
    ):
        return None

    country_code = current_country_scope.available_codes[0]

    subject_text = "broad overview of employment law"

    return RequestUnderstandingResult(
        status="resolved",
        actions=[
            RequestUnderstandingAction(
                type="legal_information",
                country_codes=[country_code],
                legal_topics=[],
                document_legal_topics=[],
                topic_text="employment law overview",
                subject_text=subject_text,
                resolved_question=question.strip(),
                search_concepts=[],
                subject_specificity="broad",
                evidence_mode="broad_topic",
            )
        ],
        is_follow_up=False,
        confidence=1.0,
        clarification_reason=None,
        current_message_delta=CurrentMessageDelta(
            explicit_action_types=["legal_information"],
            explicit_country_codes=[country_code],
            explicit_legal_topics=[],
            explicit_subject_text=subject_text,
            context_operation="independent",
        ),
    )



def _try_local_clear_fresh_legal_result(
    *,
    question: str,
    result: RequestUnderstandingResult,
    conversation_state: object | None,
    hints: DeterministicHints,
    current_country_scope: CountryAvailability,
    current_legal_scope: LegalScope,
) -> RequestUnderstandingResult | None:
    """
    Recover only a fresh, objectively complete legal request that the
    semantic understanding call incorrectly labelled ambiguous.

    This is intentionally NOT a deterministic fast path: semantic
    understanding has already run. It only prevents a spurious
    legal-vs-contact clarification when the current message itself
    already establishes one country and a supported legal topic.
    """

    if (
        result.status != "clarification"
        or result.clarification_reason != "ambiguous_request"
        or conversation_state is not None
        or hints.strong_contact_signal
        or hints.comparison_signal
        or len(current_country_scope.available_codes) != 1
        or bool(current_country_scope.unavailable_codes)
        or not current_legal_scope.is_supported
        or not current_legal_scope.legal_topics
    ):
        return None

    subject = " ".join(question.split())

    # RequestUnderstandingAction.subject_text is capped at 300 chars.
    # A longer message is not safe to reconstruct deterministically:
    # leave it under the semantic clarification path instead.
    if not subject or len(subject) > 300:
        return None

    country_code = current_country_scope.available_codes[0]
    legal_topics = list(current_legal_scope.legal_topics)

    return RequestUnderstandingResult(
        status="resolved",
        actions=[
            RequestUnderstandingAction(
                type="legal_information",
                country_codes=[country_code],
                legal_topics=legal_topics,
                document_legal_topics=[],
                topic_text=None,
                resolved_question=subject,
                subject_text=subject,
                search_concepts=[],
                subject_specificity="specific",
                evidence_mode="direct_topic",
            )
        ],
        is_follow_up=False,
        confidence=1.0,
        clarification_reason=None,
        current_message_delta=CurrentMessageDelta(
            explicit_action_types=["legal_information"],
            explicit_country_codes=[country_code],
            explicit_legal_topics=legal_topics,
            explicit_subject_text=None,
            context_operation="independent",
        ),
    )




def _try_local_parallel_multi_action_result(
    *,
    request: LegalChatRequest,
    result: RequestUnderstandingResult | None,
    catalog_provider,
) -> RequestUnderstandingResult | None:
    """
    Recover a fresh list of simple independent country-scoped actions
    when semantic understanding silently drops part of an explicit
    comma-separated request.

    This is deliberately conservative:
    - fresh request only;
    - no comparison wording;
    - at least three independently resolvable clauses;
    - only a small set of direct legal concepts;
    - existing semantic result is kept whenever it already covers the
      same or a richer action/country set.

    It is a recovery path, never the normal understanding path.
    """

    question = " ".join(request.question.split())

    if not question:
        return None

    folded = question.casefold()

    if any(
        token in folded
        for token in (
            "compare ",
            "compare,",
            "comparison",
            "contrast ",
            "difference between",
        )
    ):
        return None

    # These requests are intentionally written as independent clauses.
    normalized = (
        question
        .replace("; ", ", ")
        .replace(" and the UK ", ", the UK ")
        .replace(" and UK ", ", UK ")
    )

    clauses = [
        part.strip(" .")
        for part in normalized.split(",")
        if part.strip(" .")
    ]

    if len(clauses) < 3:
        return None

    actions: list[RequestUnderstandingAction] = []
    explicit_codes: list[str] = []

    def add_code(code: str) -> None:
        if code not in explicit_codes:
            explicit_codes.append(code)

    for clause in clauses:
        clause_request = request.model_copy(
            update={
                "question": clause,
                "country_codes": [],
                "legal_topics": [],
                "subsections": [],
            }
        )

        scope = resolve_country_availability(
            request=clause_request,
            catalog_provider=catalog_provider,
        )

        available = list(scope.available_codes)
        unavailable = list(scope.unavailable_codes)

        # The general country detector intentionally focuses on
        # country names/aliases. For this deterministic recovery only,
        # also resolve curated demonyms for countries present in the
        # validated catalog (German, French, Irish, etc.).
        #
        # Unsupported jurisdictions remain handled by the normal
        # detector above — e.g. Moroccan -> MA.
        if not available and not unavailable:
            import re

            from app.services.country_detection import (
                get_country_demonyms,
            )

            normalized_clause = (
                " "
                + re.sub(
                    r"[^a-z0-9]+",
                    " ",
                    clause.casefold(),
                ).strip()
                + " "
            )

            demonym_matches: list[str] = []

            for country in catalog_provider().countries:
                candidate_code = country.country_code.upper()

                for demonym in get_country_demonyms(
                    candidate_code
                ):
                    normalized_demonym = re.sub(
                        r"[^a-z0-9]+",
                        " ",
                        demonym.casefold(),
                    ).strip()

                    if (
                        normalized_demonym
                        and (
                            " "
                            + normalized_demonym
                            + " "
                        )
                        in normalized_clause
                    ):
                        if candidate_code not in demonym_matches:
                            demonym_matches.append(
                                candidate_code
                            )

            if len(demonym_matches) == 1:
                available = demonym_matches

        for code in available:
            add_code(code)

        for code in unavailable:
            add_code(code)

        if len(available) != 1:
            # Unsupported and non-legal fragments are intentionally not
            # executable actions. They are handled by the outer router.
            continue

        code = available[0]
        lower = clause.casefold()

        if (
            "contact" in lower
            or "who can i contact" in lower
            or "who should i contact" in lower
            or "who to contact" in lower
            or "l&e global contact" in lower
        ):
            actions.append(
                RequestUnderstandingAction(
                    type="contact",
                    country_codes=[code],
                    legal_topics=[],
                    document_legal_topics=[],
                    topic_text=None,
                    resolved_question=clause,
                    subject_text=None,
                    search_concepts=[],
                    subject_specificity=None,
                    evidence_mode=None,
                )
            )
            continue

        if "severance" in lower or "redundancy pay" in lower:
            actions.append(
                RequestUnderstandingAction(
                    type="legal_information",
                    country_codes=[code],
                    legal_topics=[
                        "Termination of Employment Contracts"
                    ],
                    document_legal_topics=[],
                    topic_text=None,
                    resolved_question=clause,
                    subject_text="statutory severance",
                    search_concepts=[
                        {
                            "terms": [
                                "statutory severance",
                                "severance pay",
                                "redundancy pay",
                            ]
                        }
                    ],
                    subject_specificity="specific",
                    evidence_mode="direct_topic",
                )
            )
            continue

        if (
            "working hours" in lower
            or "working time" in lower
            or "work hours" in lower
        ):
            actions.append(
                RequestUnderstandingAction(
                    type="legal_information",
                    country_codes=[code],
                    legal_topics=["Working Conditions"],
                    document_legal_topics=[],
                    topic_text=None,
                    resolved_question=clause,
                    subject_text="working hours",
                    search_concepts=[
                        {
                            "terms": [
                                "working hours",
                                "working time",
                                "hours of work",
                                "maximum working hours",
                            ]
                        }
                    ],
                    subject_specificity="specific",
                    evidence_mode="direct_topic",
                )
            )
            continue

        if "overtime" in lower:
            actions.append(
                RequestUnderstandingAction(
                    type="legal_information",
                    country_codes=[code],
                    legal_topics=["Working Conditions"],
                    document_legal_topics=[],
                    topic_text=None,
                    resolved_question=clause,
                    subject_text="overtime rules",
                    search_concepts=[
                        {
                            "terms": [
                                "overtime",
                                "overtime pay",
                                "extra hours",
                                "overtime rates",
                            ]
                        }
                    ],
                    subject_specificity="specific",
                    evidence_mode="direct_topic",
                )
            )
            continue

        if "dismiss" in lower or "termination" in lower:
            actions.append(
                RequestUnderstandingAction(
                    type="legal_information",
                    country_codes=[code],
                    legal_topics=[
                        "Termination of Employment Contracts"
                    ],
                    document_legal_topics=[],
                    topic_text=None,
                    resolved_question=clause,
                    subject_text="dismissal law",
                    search_concepts=[
                        {
                            "terms": [
                                "dismissal",
                                "termination of employment",
                                "grounds for termination",
                            ]
                        }
                    ],
                    subject_specificity="specific",
                    evidence_mode="direct_topic",
                )
            )

    # A mixed request may contain two executable supported actions
    # plus an explicitly unsupported jurisdiction or an unrelated
    # fragment. Three clauses are still required above, but only two
    # executable actions are necessary for safe recovery.
    if (
        len(actions) < 2
        or len(explicit_codes) < 2
    ):
        return None

    existing_actions = (
        list(result.actions or [])
        if result is not None
        else []
    )

    existing_signature = {
        (
            action.type,
            tuple(action.country_codes),
        )
        for action in existing_actions
    }

    recovered_signature = {
        (
            action.type,
            tuple(action.country_codes),
        )
        for action in actions
    }

    # Keep the semantic plan only when it already contains every
    # independently recovered explicit action. Action COUNT alone is
    # not evidence of completeness: a stochastic semantic result may
    # return four actions while silently replacing or mis-scoping one
    # of the explicitly requested country/task pairs.
    if recovered_signature.issubset(existing_signature):
        return None

    return RequestUnderstandingResult(
        status="resolved",
        actions=actions,
        is_follow_up=False,
        confidence=1.0,
        clarification_reason=None,
        current_message_delta=CurrentMessageDelta(
            explicit_action_types=[
                action.type
                for action in actions
            ],
            explicit_country_codes=explicit_codes,
            explicit_legal_topics=list(
                dict.fromkeys(
                    topic
                    for action in actions
                    for topic in action.legal_topics
                )
            ),
            explicit_subject_text=None,
            context_operation="independent",
        ),
    )


def _strip_unrequested_comparison_section(
    answer: str,
) -> str:
    """
    Remove a model-created Comparison section when no comparison
    action exists. Country sections before it remain untouched.
    """

    lines = answer.splitlines()

    for index, line in enumerate(lines):
        normalized = (
            line.strip()
            .strip("*# ")
            .rstrip(":")
            .casefold()
        )

        if normalized == "comparison":
            return "\n".join(
                lines[:index]
            ).rstrip()

    return answer.rstrip()


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
                current_user_question=request.question,
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

        contacts = build_legal_chat_contacts(
            source_directory=_optional_contact_source_directory(),
            requested_country_codes=(
                current_country_scope.available_codes
            ),
            unavailable_country_codes=(
                current_country_scope.unavailable_codes
            ),
            sources=sources,
        )

        return LegalChatResponse(
            question=request.question.strip(),
            answer=contact_answer,
            grounded=bool(sources),
            model=None,
            retrieval_total=retrieval_total,
            sources=sources,
            contacts=contacts,
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
            current_user_question=request.question,
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


def _legal_generation_user_question(
    *,
    original_question: str,
    resolved_legal_question: str,
    has_contact_actions: bool,
) -> str:
    """
    Select the conversational question exposed to legal generation.

    For a mixed legal + contact request, contact rendering is handled
    deterministically later by _build_contact_section(). The legal
    generator must therefore see only the resolved legal question and
    must never speculate about contact availability.

    Pure legal requests preserve the literal current user message so
    challenge/follow-up wording such as "Are you sure?" still reaches
    the legal generator unchanged.
    """

    if has_contact_actions:
        return resolved_legal_question

    return original_question


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
    legal_answer_generation_fn: AnswerGenerationFunction = (
        answer_legal_question
    ),
) -> LegalChatResponse:
    """
    Execute every action RequestUnderstanding resolved.

    Every action keeps its own country/topic scope - a contact
    action's country is never widened to a comparison action's
    countries, and vice versa. Exactly one legal generation call
    covers every legal_information/comparison action combined; every
    contact action is resolved deterministically and appended, in
    order, after the legal answer.

    `legal_answer_generation_fn` (chat-streaming initiative, GATE S4):
    the ONE swappable seam this stable function exposes for streaming.
    Defaults to answer_legal_question - byte-for-byte today's
    behavior, unchanged for every existing caller. A caller wanting
    real token streaming (app/routers/chat_stream.py) passes a
    drop-in-compatible callable that bridges to
    stream_answer_legal_question() instead - same call signature
    (matched via functools.partial), so nothing else in this function
    needs to know or care which one is running underneath.
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
    contacts = []
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
            # Preserve deterministic canonical concept coverage.
            #
            # A semantic understanding result may legitimately choose
            # one nearby live document section, but it must not narrow
            # away another canonical section that the literal action
            # wording deterministically requires.
            #
            # Example (generic, not country-specific):
            # "statutory notice period" belongs to both Employment
            # Contracts and Termination of Employment Contracts in the
            # canonical taxonomy. Selecting only the latter can hide a
            # dedicated Notice Period section in the former and create
            # a false evidence-insufficiency answer.
            #
            # An explicitly named live document-topic title remains
            # more specific and therefore keeps its existing priority.
            action_question_text = (
                action.resolved_question
                if action.resolved_question
                else request.question
            )

            deterministic_action_topics = [
                topic
                for topic in detect_legal_topics(
                    action_question_text
                )
                if topic in CANONICAL_LEGAL_TOPICS
                and (
                    action.type != "legal_information"
                    or len(action_scope.available_codes) != 1
                    or not live_document_topics_for_action
                    or topic in live_document_topics_for_action
                )
            ]

            explicit_document_topics = (
                detect_document_legal_topics(
                    action_question_text,
                    sorted(live_document_topics_for_action),
                )
                if action.type != "comparison"
                else []
            )

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

            # Explicit client-supplied legal_topics remain binding.
            # Otherwise deterministic canonical detection is the
            # coverage floor: semantic understanding may add useful
            # topics, but may not silently remove those directly
            # implied by the user's action wording.
            if (
                not request.legal_topics
                and deterministic_action_topics
                and not explicit_document_topics
            ):
                validated_topics = (
                    deterministic_action_topics
                    + [
                        topic
                        for topic in validated_topics
                        if topic not in deterministic_action_topics
                    ]
                )

            validated_document_topics = (
                [
                    topic
                    for topic in action.document_legal_topics
                    if topic in live_document_topics_for_action
                ]
                if action.type != "comparison"
                else []
            )

            # A model-selected document section must not override a
            # deterministic canonical concept unless the user actually
            # named that live section title. This keeps custom/live
            # document topics available when they are genuinely the
            # user's subject, while preventing semantic narrowing.
            if (
                not request.legal_topics
                and deterministic_action_topics
                and not explicit_document_topics
            ):
                validated_document_topics = []

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
                        update={
                            "legal_topics": validated_topics,
                            "document_legal_topics": (
                                validated_document_topics
                            ),
                        }
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
            # Preserve explicitly named unsupported jurisdictions.
            # Request-understanding actions contain only supported codes,
            # while deterministic hints retain unsupported names/codes.
            # They must survive so the final response can explain that
            # those jurisdictions are not covered instead of asking the
            # user for a country they already supplied.
            for unavailable_code in (
                hints.current_unavailable_country_codes
            ):
                if unavailable_code not in merged_unavailable_codes:
                    merged_unavailable_codes.append(
                        unavailable_code
                    )

            # Semantic understanding may identify an explicitly named
            # real jurisdiction that the deterministic first-pass
            # detector could not recognize from a demonym/adjective,
            # for example "Moroccan". Actions intentionally contain
            # supported countries only, but current_message_delta keeps
            # the complete explicit jurisdiction set. Resolve those
            # codes deterministically here and preserve unsupported
            # jurisdictions in the final product-level note.
            semantic_explicit_codes = (
                list(
                    result.current_message_delta
                    .explicit_country_codes
                )
                if result.current_message_delta is not None
                else []
            )

            if semantic_explicit_codes:
                semantic_country_scope = (
                    resolve_country_availability(
                        request=request.model_copy(
                            update={
                                "country_codes":
                                    semantic_explicit_codes
                            }
                        ),
                        catalog_provider=catalog_provider,
                    )
                )

                for unavailable_code in (
                    semantic_country_scope.unavailable_codes
                ):
                    if (
                        unavailable_code
                        not in merged_unavailable_codes
                    ):
                        merged_unavailable_codes.append(
                            unavailable_code
                        )

            # One product-level source budget is shared by all
            # legal actions. Reserve capacity only for explicitly
            # requested contact actions here. The RAG layer allocates
            # the remaining legal budget across LegalActionEvidenceSpec
            # objects according to countries/facets, rather than giving
            # every action the same arbitrarily reduced allowance.
            if contact_actions:
                legal_source_budget = max(
                    1,
                    request.max_sources
                    - min(
                        len(contact_actions),
                        max(0, request.max_sources - 1),
                    ),
                )

                if legal_source_budget < prepared_request.max_sources:
                    prepared_request = (
                        prepared_request.model_copy(
                            update={
                                "max_sources": legal_source_budget
                            }
                        )
                    )

            legal_response = legal_answer_generation_fn(
                prepared_request,
                search_function=search_function,
                generation_client=generation_client,
                rerank_enabled=rerank_enabled,
                rerank_pool_multiplier=rerank_pool_multiplier,
                max_context_characters=max_context_characters,
                max_source_characters=max_source_characters,
                metrics=metrics,
                current_user_question=(
                    _legal_generation_user_question(
                        original_question=request.question,
                        resolved_legal_question=merged_question,
                        has_contact_actions=bool(contact_actions),
                    )
                ),
                action_specs=action_specs or None,
                known_excluded_country_codes=(
                    merged_unavailable_codes or None
                ),
            )

            legal_answer_text = legal_response.answer

            if not any(
                action.type == "comparison"
                for action in legal_type_actions
            ):
                legal_answer_text = (
                    _strip_unrequested_comparison_section(
                        legal_answer_text
                    )
                )

            answer_parts.append(legal_answer_text)
            sources.extend(legal_response.sources)
            grounded = legal_response.grounded
            model_used = legal_response.model
            retrieval_total += legal_response.retrieval_total

            # Product fallback: when validated legal evidence cannot
            # answer the requested question, do not merely tell the
            # user to contact someone. Resolve the actual current
            # L&E Global contact deterministically when available.
            #
            # If the user already explicitly requested a contact,
            # leave that action to the normal contact loop below so
            # the same contact is never rendered twice.
            if (
                not legal_response.grounded
                and not contact_actions
                and merged_available_codes
            ):
                (
                    fallback_contact_answer,
                    fallback_contact_sources,
                    fallback_contact_retrieval_total,
                    fallback_contact_took_ms,
                ) = _build_contact_section(
                    country_codes=merged_available_codes,
                    unavailable_country_codes=[],
                    citation_offset=max(
                        (source.citation for source in sources),
                        default=0,
                    ),
                )

                metrics.opensearch_ms += fallback_contact_took_ms
                retrieval_total += (
                    fallback_contact_retrieval_total
                )

                if fallback_contact_sources:
                    # The structured `contacts` field below is the
                    # single source of contact-card detail (name,
                    # firm, email, phone) - never re-serialize it into
                    # the answer text too, or the visitor sees every
                    # contact twice (once as text, once as its card).
                    answer_parts.append(
                        "For case-specific guidance, you can use "
                        "the L&E Global contacts below:"
                    )
                    grounded = True
                elif fallback_contact_answer:
                    # Never promise a contact "below" when the
                    # country currently has no validated contact.
                    answer_parts.append(fallback_contact_answer)

                contacts.extend(
                    build_legal_chat_contacts(
                        source_directory=(
                            _optional_contact_source_directory()
                        ),
                        requested_country_codes=(
                            merged_available_codes
                        ),
                        unavailable_country_codes=[],
                        sources=fallback_contact_sources,
                    )
                )

                sources.extend(fallback_contact_sources)

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

    # A mixed request may contain valid legal actions plus an obvious
    # unrelated request. The legal generation receives only the legal
    # actions; add the product-level scope refusal deterministically so
    # the unrelated fragment is neither silently ignored nor answered
    # from general knowledge.
    if legal_type_actions:
        combined_answer_text = "\n".join(answer_parts).casefold()
        question_lower = request.question.casefold()

        out_of_scope_fragments = (
            (
                "weather",
                "The weather request is outside this assistant's "
                "employment-law scope.",
            ),
            (
                "restaurant",
                "The restaurant request is outside this assistant's "
                "employment-law scope.",
            ),
        )

        for fragment, message in out_of_scope_fragments:
            if (
                fragment in question_lower
                and fragment not in combined_answer_text
            ):
                answer_parts.append(message)
                combined_answer_text = (
                    "\n".join(answer_parts).casefold()
                )

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
            citation_offset=max(
                (source.citation for source in sources),
                default=0,
            ),
        )

        metrics.opensearch_ms += took_ms
        retrieval_total += contact_retrieval_total

        if contact_answer:
            answer_parts.append(contact_answer)

        contacts.extend(
            build_legal_chat_contacts(
                source_directory=(
                    _optional_contact_source_directory()
                ),
                requested_country_codes=(
                    action_scope.available_codes
                ),
                unavailable_country_codes=(
                    action_scope.unavailable_codes
                ),
                sources=contact_sources,
            )
        )

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
        contacts=contacts,
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
    legal_answer_generation_fn: AnswerGenerationFunction = (
        answer_legal_question
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

    `legal_answer_generation_fn` (chat-streaming initiative, GATE S4):
    threaded straight through to _execute_resolved_plan's own
    parameter of the same name - see its docstring. Defaults to
    answer_legal_question, unchanged for every existing caller
    (POST /api/v1/chat never passes anything else).
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

    # A user may establish narrow employment context before asking
    # the actual legal question, e.g. "I work in Germany and have
    # 8 years of service; remember this for my next question."
    #
    # Keep only the jurisdiction and length-of-service fact in the
    # existing client-carried conversation_state. This is deliberately
    # narrow: it is not a general personal-memory store.
    if request.conversation_state is None:
        memory_text = request.question.strip()
        memory_lower = memory_text.casefold()

        service_match = re.search(
            r"\b(\d{1,2})\s+years?\s+of\s+service\b",
            memory_text,
            flags=re.IGNORECASE,
        )

        memory_cue = (
            "remember" in memory_lower
            or "next question" in memory_lower
            or "use these details" in memory_lower
        )

        if (
            service_match is not None
            and memory_cue
            and not detect_legal_topics(memory_text)
        ):
            memory_scope = resolve_country_availability(
                request.model_copy(
                    update={"country_codes": []}
                ),
                catalog_provider=memoized_catalog_provider,
            )

            if (
                len(memory_scope.available_codes) == 1
                and not memory_scope.unavailable_codes
            ):
                memory_country_code = (
                    memory_scope.available_codes[0]
                )
                memory_country = resolve_country_display_name(
                    memory_country_code
                )
                memory_years = int(service_match.group(1))
                memory_subject = (
                    f"{memory_years} years of service"
                )

                memory_state = ConversationState(
                    actions=[],
                    focus_action_index=None,
                    ordered_country_codes=[],
                    pending_clarification=(
                        ConversationPendingClarification(
                            reason="missing_topic",
                            candidate_action_types=[
                                "legal_information"
                            ],
                            candidate_country_codes=[
                                memory_country_code
                            ],
                            candidate_legal_topics=[],
                            candidate_subject_text=memory_subject,
                            candidate_search_concepts=[],
                            candidate_subject_specificity="specific",
                            candidate_evidence_mode="direct_topic",
                        )
                    ),
                )

                metrics.outcome = "context_memory_setup"
                metrics.conversation_state_emitted = True
                metrics.total_ms = (
                    perf_counter() - total_started_at
                ) * 1000
                metrics.log()

                return LegalChatResponse(
                    question=request.question.strip(),
                    answer=(
                        f"I'll keep {memory_country} and "
                        f"{memory_subject} for your next "
                        "employment-law question."
                    ),
                    grounded=False,
                    model=None,
                    retrieval_total=0,
                    sources=[],
                    conversation_state=memory_state,
                )

    meta_resolution = resolve_conversation_meta(
        question=request.question,
        history=request.history,
        conversation_state=request.conversation_state,
        catalog_provider=memoized_catalog_provider,
    )

    # A structured legal clarification already waiting for its topic
    # takes precedence over a generic meta interpretation of wording
    # such as "what country are we discussing?" when the same new
    # message also supplies a real employment-law topic.
    if (
        meta_resolution is not None
        and request.conversation_state is not None
        and request.conversation_state.pending_clarification
        is not None
        and (
            request.conversation_state
            .pending_clarification.reason
            == "missing_topic"
        )
        and detect_legal_topics(request.question)
    ):
        meta_resolution = None

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
    contextual_contact_country_codes = (
        resolve_contextual_multi_country_contact_codes(
            request.question,
            request.conversation_state,
        )
    )

    pending_legal_topic_resume = (
        request.conversation_state is not None
        and request.conversation_state.pending_clarification
        is not None
        and (
            request.conversation_state
            .pending_clarification.reason
            == "missing_topic"
        )
        and bool(detect_legal_topics(request.question))
    )

    help_intent = (
        None
        if (
            contextual_contact_country_codes is not None
            or pending_legal_topic_resume
        )
        else detect_assistant_help_intent(
            request.question,
            tuple(country.code for country in COUNTRIES),
        )
    )

    if help_intent is not None:
        metrics.outcome = f"assistant_help_{help_intent.intent_type}"
        metrics.total_ms = (
            perf_counter() - total_started_at
        ) * 1000
        metrics.log()

        help_conversation_state = request.conversation_state

        if (
            help_intent.intent_type == "comparison_guidance"
            and len(help_intent.referenced_country_codes) >= 2
        ):
            help_conversation_state = ConversationState(
                actions=[],
                focus_action_index=None,
                ordered_country_codes=[],
                pending_clarification=(
                    ConversationPendingClarification(
                        reason="missing_topic",
                        candidate_action_types=["comparison"],
                        candidate_country_codes=list(
                            help_intent.referenced_country_codes
                        ),
                    )
                ),
            )

        return LegalChatResponse(
            question=request.question.strip(),
            answer=build_assistant_help_answer(
                help_intent, original_question=request.question
            ),
            grounded=False,
            model=None,
            retrieval_total=0,
            sources=[],
            conversation_state=help_conversation_state,
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

        local_elliptical_clarification = (
            _try_local_elliptical_legal_clarification(
                question=request.question,
                conversation_state=previous_conversation_state,
                hints=hints,
            )
        )

        if local_elliptical_clarification is not None:
            metrics.request_understanding_method = (
                "local_deterministic"
            )
            metrics.request_understanding_ms = 0.0
            metrics.request_understanding_openai_ms = 0.0
            metrics.request_understanding_attempts = 0
            metrics.request_status = "clarification"
            metrics.clarification_reason = (
                "elliptical_followup"
            )
            metrics.outcome = (
                "clarification_elliptical_followup"
            )
            # This fast-path returns before semantic understanding
            # and conversation_transition. Preserve the established
            # legal action AND explicitly record that the next user
            # message is expected to provide the missing subject
            # detail; otherwise routing would depend on history alone.
            response_conversation_state = (
                previous_conversation_state
            )

            if (
                previous_conversation_state is not None
                and len(previous_conversation_state.actions) == 1
                and previous_conversation_state.actions[0].type
                == "legal_information"
                and previous_conversation_state.actions[0].country_codes
            ):
                active_action = (
                    previous_conversation_state.actions[0]
                )

                response_conversation_state = (
                    previous_conversation_state.model_copy(
                        update={
                            "pending_clarification": (
                                ConversationPendingClarification(
                                    reason="subject_detail",
                                    candidate_action_types=[
                                        "legal_information"
                                    ],
                                    candidate_country_codes=list(
                                        active_action.country_codes
                                    ),
                                )
                            )
                        }
                    )
                )

            metrics.conversation_state_emitted = (
                response_conversation_state is not None
            )
            metrics.total_ms = (
                perf_counter() - total_started_at
            ) * 1000
            metrics.log()

            return LegalChatResponse(
                question=request.question.strip(),
                answer=local_elliptical_clarification,
                grounded=False,
                model=None,
                retrieval_total=0,
                sources=[],
                conversation_state=response_conversation_state,
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
            local_method = "local_deterministic"

            if local_result is None:
                local_result = (
                    _try_local_parallel_multi_action_result(
                        request=request,
                        result=None,
                        catalog_provider=memoized_catalog_provider,
                    )
                )
                local_method = (
                    "local_deterministic_multi_action"
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

            # Continue through the normal resolved-plan path exactly as
            # if semantic understanding had returned these actions.
            metrics.request_understanding_method = local_method
            metrics.request_understanding_error = outcome.error
            result = local_result
        else:
            result = outcome.result

            metrics.request_understanding_method = "semantic"

        choice_of_law_recovery = (
            _try_local_choice_of_law_recovery(
                question=request.question,
                result=result,
                current_country_scope=current_country_scope,
                catalog_provider=memoized_catalog_provider,
            )
        )

        if choice_of_law_recovery is not None:
            result = choice_of_law_recovery
            metrics.semantic_result_overridden = True
            metrics.semantic_override_reason = (
                "deterministic_choice_of_law"
            )

        # The semantic classifier is probabilistic. A fresh request
        # must never ask the user to provide country/topic again when
        # the deterministic layer already found exactly one supported
        # country and an unambiguous supported legal scope.
        #
        # Reuse the existing conservative grounded fallback rather
        # than fabricate a semantic action. The original user question
        # still reaches retrieval/generation, so precise wording such
        # as employee-vs-contractor remains available to the answer
        # model.
        # A clear contact request with one deterministically
        # resolved country must not be sent back to a generic
        # missing-country clarification from semantic understanding.
        if (
            result.status in {"clarification", "unsupported"}
            and previous_conversation_state is None
            and hints.strong_contact_signal
            and (
            len(current_country_scope.available_codes)
            + len(current_country_scope.unavailable_codes)
            == 1
        )
            and not current_legal_scope.is_supported
            and not hints.comparison_signal
        ):
            metrics.request_understanding_confidence = (
                result.confidence
            )
            metrics.request_understanding_method = (
                "semantic_contact_unsupported_recovered"
                if result.status == "unsupported"
                else "semantic_contact_clarification_recovered"
            )
            metrics.semantic_result_overridden = True
            metrics.semantic_override_reason = (
                "deterministic_single_contact_scope"
            )

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

        # A deterministically recognized legal question for one
        # country outside the validated corpus must not degrade to the
        # generic out-of-scope answer merely because semantic
        # understanding returned clarification/unsupported.
        #
        # Deliberately restricted to:
        # - exactly one unavailable country,
        # - a supported legal topic,
        # - no contact intent,
        # - no comparison intent.
        #
        # Thus unrelated questions such as weather in Tunisia remain
        # genuinely out of scope.
        if (
            result.status in {"clarification", "unsupported"}
            and previous_conversation_state is None
            and not current_country_scope.available_codes
            and len(current_country_scope.unavailable_codes) == 1
            and current_legal_scope.is_supported
            and not hints.strong_contact_signal
            and not hints.comparison_signal
        ):
            metrics.request_understanding_confidence = (
                result.confidence
            )
            metrics.request_understanding_method = (
                "semantic_unavailable_legal_country_recovered"
            )
            metrics.semantic_result_overridden = True
            metrics.semantic_override_reason = (
                "deterministic_single_unavailable_legal_scope"
            )

            # A recognized country outside the validated corpus is
            # already a complete deterministic answer. Never send it
            # through RAG or the generic conservative fallback.
            metrics.outcome = "fallback_unavailable_country"
            metrics.retrieval_total = 0
            metrics.selected_sources = 0
            metrics.model = None
            metrics.generation_attempts = 0

            metrics.request_actions = ["legal_information"]
            metrics.resolved_action_countries = [
                {
                    "type": "legal_information",
                    "country_codes": [],
                }
            ]
            metrics.resolved_country_codes = []
            metrics.resolved_legal_topics = list(
                current_legal_scope.legal_topics
            )

            metrics.total_ms = (
                perf_counter() - total_started_at
            ) * 1000
            metrics.log()

            return LegalChatResponse(
                question=request.question.strip(),
                answer=_unavailable_countries_answer(
                    current_country_scope.unavailable_codes
                ),
                grounded=False,
                model=None,
                retrieval_total=0,
                sources=[],
                conversation_state=None,
            )

        if (
            result.status == "clarification"
            and previous_conversation_state is None
            and len(current_country_scope.available_codes) == 1
            and not current_country_scope.unavailable_codes
            and current_legal_scope.is_supported
            and not hints.strong_contact_signal
            and not hints.comparison_signal
            # This recovery is intentionally for a genuinely simple
            # one-scope request only. A compound request containing
            # several comma-separated tasks must stay in semantic
            # planning; collapsing it to the single deterministic
            # country detected from one clause can erase the other
            # legal actions and create an invalid grounding structure.
            and request.question.count(",") < 2
            and " then " not in request.question.casefold()
        ):
            metrics.request_understanding_confidence = (
                result.confidence
            )
            metrics.request_understanding_method = (
                "semantic_clarification_recovered"
            )
            metrics.semantic_result_overridden = True
            metrics.semantic_override_reason = (
                "deterministic_single_legal_scope"
            )

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

        # A fresh, explicit whole-domain employment-law overview
        # is deterministic enough to normalize even when semantic
        # understanding returned a technically "resolved" result.
        #
        # Without this, stochastic understanding can incorrectly
        # narrow "employment law in Germany" to Working Conditions,
        # Hiring, etc., which prevents the dedicated broad retrieval
        # path from ever running.
        broad_overview_result = (
            _try_local_broad_legal_overview_result(
                question=request.question,
                previous_conversation_state=(
                    previous_conversation_state
                ),
                hints=hints,
                current_country_scope=current_country_scope,
            )
        )

        if broad_overview_result is not None:
            result = broad_overview_result
            metrics.semantic_result_overridden = True
            metrics.semantic_override_reason = (
                "broad_legal_overview"
            )

        missing_topic_result = _try_local_missing_topic_result(
            question=request.question,
            result=result,
            previous_conversation_state=(
                previous_conversation_state
            ),
            hints=hints,
            current_country_scope=current_country_scope,
            current_legal_scope=current_legal_scope,
        )

        if missing_topic_result is not None:
            result = missing_topic_result
            metrics.semantic_result_overridden = True
            metrics.semantic_override_reason = "missing_topic"

        clear_fresh_legal_result = (
            _try_local_clear_fresh_legal_result(
                question=request.question,
                result=result,
                conversation_state=previous_conversation_state,
                hints=hints,
                current_country_scope=current_country_scope,
                current_legal_scope=current_legal_scope,
            )
        )

        if clear_fresh_legal_result is not None:
            result = clear_fresh_legal_result

        metrics.request_understanding_confidence = result.confidence
        metrics.contextual_question_used = result.is_follow_up
        metrics.current_message_operation = (
            result.current_message_delta.context_operation
        )

        transition_started_at = perf_counter()

        parallel_multi_action_result = (
            _try_local_parallel_multi_action_result(
                request=request,
                result=result,
                catalog_provider=memoized_catalog_provider,
            )
        )

        if parallel_multi_action_result is not None:
            result = parallel_multi_action_result
            metrics.semantic_result_overridden = True
            metrics.semantic_override_reason = (
                "parallel_multi_action_recovery"
            )

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

            contact_answer = ""
            contact_sources: list[LegalAnswerSource] = []
            contact_retrieval_total = 0
            contacts = []
            resolved_country_name: str | None = None

            # Offer the country contact only when the unsupported
            # request is clearly legal or legal-adjacent. A non-legal
            # request such as weather must receive the simple product
            # scope refusal and must never trigger contact retrieval.
            #
            # This is intentionally separate from the legal
            # insufficient-evidence fallback, which remains unchanged.
            if (
                _should_offer_contact_for_unsupported_request(
                    request.question
                )
                and current_country_scope.available_codes
                and not current_country_scope.unavailable_codes
            ):
                resolved_country_name = resolve_country_display_name(
                    current_country_scope.available_codes[0]
                )

                (
                    contact_answer,
                    contact_sources,
                    contact_retrieval_total,
                    contact_took_ms,
                ) = _build_contact_section(
                    country_codes=(
                        current_country_scope.available_codes
                    ),
                    unavailable_country_codes=[],
                    citation_offset=0,
                )

                metrics.opensearch_ms += contact_took_ms
                metrics.retrieval_total = contact_retrieval_total
                metrics.selected_sources = len(contact_sources)
                metrics.resolved_country_codes = list(
                    current_country_scope.available_codes
                )

                contacts = build_legal_chat_contacts(
                    source_directory=(
                        _optional_contact_source_directory()
                    ),
                    requested_country_codes=(
                        current_country_scope.available_codes
                    ),
                    unavailable_country_codes=[],
                    sources=contact_sources,
                )

            # The explanatory out-of-scope message always leads - never
            # the contacts alone - so it is the answer text in full,
            # naming the resolved country when one is known. Any contact
            # cards render separately from the structured `contacts`
            # field; contact_only=False keeps this text visible above
            # them even though no plan was executed (conversation_state
            # stays None below).
            if resolved_country_name:
                answer_text = (
                    CLARIFICATION_UNSUPPORTED_REQUEST_WITH_COUNTRY_TEMPLATE.format(
                        country=resolved_country_name
                    )
                )
            else:
                answer_text = CLARIFICATION_UNSUPPORTED_REQUEST_ANSWER

            if not contact_sources and contact_answer:
                answer_text += "\n\n" + contact_answer

            metrics.total_ms = (
                perf_counter() - total_started_at
            ) * 1000

            metrics.log()

            return LegalChatResponse(
                question=request.question.strip(),
                answer=answer_text,
                grounded=bool(contact_sources),
                model=None,
                retrieval_total=contact_retrieval_total,
                sources=contact_sources,
                contacts=contacts,
                contact_only=False,
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

            # A contextual clarification derived from one existing
            # legal action must not erase that action. The user's next
            # reply should continue from the same country/topic rather
            # than restart the conversation.
            if (
                transition_outcome.contextual_clarification_answer
                is not None
                and previous_conversation_state is not None
            ):
                response_conversation_state = (
                    previous_conversation_state
                )

            if transition_outcome.pending_clarification is not None:
                pending_clarification = (
                    transition_outcome.pending_clarification
                )

                if (
                    pending_clarification.reason
                    == "subject_detail"
                    and previous_conversation_state is not None
                ):
                    response_conversation_state = (
                        previous_conversation_state.model_copy(
                            update={
                                "pending_clarification":
                                    pending_clarification
                            }
                        )
                    )
                else:
                    response_conversation_state = (
                        build_next_conversation_state(
                            executed=[],
                            pending_clarification=(
                                pending_clarification
                            ),
                        )
                    )

                metrics.conversation_state_emitted = (
                    response_conversation_state is not None
                )

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
            legal_answer_generation_fn=legal_answer_generation_fn,
        )

        # Final product-level cleanup uses the literal user question,
        # not a rewritten legal retrieval question.
        #
        # 1. Independent legal actions must never acquire an invented
        #    Comparison section.
        # 2. A mixed legal + weather request must explicitly refuse
        #    only the weather fragment instead of silently dropping it.
        final_answer = response.answer

        has_comparison_action = any(
            action.type == "comparison"
            for action in final_result.actions
        )

        has_legal_action = any(
            action.type in {
                "legal_information",
                "comparison",
            }
            for action in final_result.actions
        )

        if not has_comparison_action:
            final_answer = (
                _strip_unrequested_comparison_section(
                    final_answer
                )
            )

        literal_question = request.question.casefold()

        # Recover unsupported jurisdictions directly from the literal
        # current user message. The semantic plan intentionally keeps
        # only executable/supported legal actions, so unsupported
        # jurisdictions must be restored at the product layer rather
        # than silently disappearing from a mixed request.
        literal_country_scope = resolve_country_availability(
            request=request.model_copy(
                update={"country_codes": []}
            ),
            catalog_provider=memoized_catalog_provider,
        )

        if literal_country_scope.unavailable_codes:
            unavailable_answer = _unavailable_countries_answer(
                literal_country_scope.unavailable_codes
            )

            if (
                unavailable_answer.casefold()
                not in final_answer.casefold()
            ):
                final_answer = (
                    final_answer.rstrip()
                    + "\n\nNote: "
                    + unavailable_answer
                )

        if (
            has_legal_action
            and re.search(
                r"https?://\S+",
                request.question,
                flags=re.IGNORECASE,
            )
            and "external webpage" not in final_answer.casefold()
        ):
            final_answer = (
                "I cannot access or rely on the external webpage "
                "you linked. I can answer only from this chatbot's "
                "validated L&E Global employment-law content."
                "\n\n"
                + final_answer.lstrip()
            )

        if (
            has_legal_action
            and "weather" in literal_question
            and "weather" not in final_answer.casefold()
        ):
            final_answer = (
                final_answer.rstrip()
                + "\n\n"
                + "The weather request is outside this assistant's "
                  "employment-law scope."
            )

        if final_answer != response.answer:
            response = response.model_copy(
                update={"answer": final_answer}
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

        response = response.model_copy(
            update={
                "answer": sanitize_user_facing_legal_answer(
                    response.answer
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


@router.get(
    "/contact-photos/{contact_id}/{sha256}",
    response_class=Response,
)
def get_public_contact_photo(
    contact_id: str,
    sha256: str,
) -> Response:
    """Return one validated public contact photo."""

    settings = get_settings()

    photo = resolve_public_contact_photo(
        source_directory=settings.document_source_dir,
        contact_id=contact_id,
        sha256=sha256,
    )

    if photo is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Contact photo not found.",
        )

    return Response(
        content=photo.data,
        media_type=photo.content_type,
        headers={
            "ETag": f'"{photo.sha256}"',
            "Cache-Control": (
                "public, max-age=31536000, immutable"
            ),
            "X-Content-Type-Options": "nosniff",
        },
    )


def _build_comparison_source_budget_response(
    *,
    request: LegalChatRequest,
    error: InvalidLegalChatRequestError,
) -> LegalChatResponse:
    """
    The one friendly-200 (never an HTTP error) response
    InvalidLegalChatRequestError's "comparison_source_budget" code
    produces - extracted verbatim from legal_chat()'s own except
    block (chat-streaming initiative, GATE S4) so
    POST /api/v1/chat/stream can raise this exact same response
    instead of a second, independently maintained copy of this text.
    legal_chat()'s own regression coverage (test_chat.py's
    FriendlyInvalidRequestHttpTests) protects this extraction.
    """

    country_count = error.details.get("country_count")

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
            return _build_comparison_source_budget_response(
                request=request, error=error,
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
                "The assistant is temporarily unavailable. "
                "Please try again shortly."
            ),
            headers={
                "X-Request-ID": request_id,
            },
        ) from error

    except CountryDetectionError as error:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(
                "The assistant could not complete your request. "
                "Please try again."
            ),
            headers={
                "X-Request-ID": request_id,
            },
        ) from error

    except RagAnswerError as error:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(
                "The assistant could not complete your request. "
                "Please try again."
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
                "The assistant could not complete your request. "
                "Please try again."
            ),
            headers={
                "X-Request-ID": request_id,
            },
        ) from error
