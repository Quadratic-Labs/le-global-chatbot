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
from app.models.chat import (
    LegalAnswerSource,
    LegalChatHistoryMessage,
    LegalChatRequest,
    LegalChatResponse,
)
from app.services.chat_metrics import (
    LegalChatMetrics,
)
from app.services.country_detection import (
    CountryAvailability,
    CountryCatalogProvider,
    CountryDetectionError,
    resolve_country_availability,
    resolve_country_display_name,
)
from app.services.legal_catalog import (
    get_legal_catalog,
)
from app.services.legal_search import (
    LegalSearchError,
    search_contact_chunks,
    search_legal_documents,
)
from app.services.legal_topic_detection import (
    CANONICAL_LEGAL_TOPICS,
    LegalScope,
    detect_legal_topics,
    resolve_legal_scope,
)
from app.services.rag_answer import (
    DEFAULT_MAX_CONTEXT_CHARACTERS,
    DEFAULT_MAX_SOURCE_CHARACTERS,
    InvalidLegalChatRequestError,
    RagAnswerError,
    SearchFunction,
    TextGenerationClient,
    answer_legal_question,
)
from app.services.request_understanding import (
    DeterministicHints,
    HistoryTurn,
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
    "country-specific legal advice."
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

    Returns (answer_text, sources, retrieval_total, took_ms) - the
    caller updates shared metrics itself, since this function may be
    invoked once per contact action.
    """

    sources: list[LegalAnswerSource] = []
    answer_sections: list[str] = []
    retrieval_total = 0
    took_ms = 0.0

    if country_codes:
        try:
            contact_response = search_contact_chunks(
                country_codes=country_codes
            )
        except LegalSearchError as error:
            raise RagAnswerError(
                "Legal document retrieval failed."
            ) from error

        took_ms = float(contact_response.took_ms)
        retrieval_total += contact_response.total

        hits_by_country_code: dict[str, list] = {}

        for hit in contact_response.hits:
            hits_by_country_code.setdefault(
                hit.country_code.upper(),
                [],
            ).append(hit)

        for country_code in country_codes:
            country_hits = hits_by_country_code.get(
                country_code.upper(),
                [],
            )

            display_name = resolve_country_display_name(
                country_code
            )

            if not country_hits:
                answer_sections.append(
                    f"{display_name}\n"
                    + CONTACT_NOT_FOUND_ANSWER_TEMPLATE.format(
                        country=display_name
                    )
                )
                continue

            for hit in country_hits:
                citation = citation_offset + len(sources) + 1

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

                answer_sections.append(
                    f"{display_name}\n{hit.content} [{citation}]"
                )

    for country_code in unavailable_country_codes:
        display_name = resolve_country_display_name(
            country_code
        )

        answer_sections.append(
            f"{display_name}\n"
            + CONTACT_NOT_FOUND_ANSWER_TEMPLATE.format(
                country=display_name
            )
        )

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

            validated_topics = [
                topic
                for topic in action.legal_topics
                if topic in CANONICAL_LEGAL_TOPICS
            ]

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
                    "topic_text": action.topic_text,
                }
            )

            question_part = (
                action.resolved_question
                if action.resolved_question
                else request.question
            )

            if question_part not in merged_question_parts:
                merged_question_parts.append(question_part)

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

            legal_response = answer_legal_question(
                prepared_request,
                search_function=search_function,
                generation_client=generation_client,
                rerank_enabled=rerank_enabled,
                rerank_pool_multiplier=rerank_pool_multiplier,
                max_context_characters=max_context_characters,
                max_source_characters=max_source_characters,
                metrics=metrics,
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

    return LegalChatResponse(
        question=request.question.strip(),
        answer="\n\n".join(
            part for part in answer_parts if part
        ),
        grounded=grounded,
        model=model_used,
        retrieval_total=retrieval_total,
        sources=sources,
    )


def resolve_legal_chat_response(
    request: LegalChatRequest,
    request_id: str | None = None,
    catalog_provider: CountryCatalogProvider = (
        get_legal_catalog
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

    try:
        (
            hints,
            current_country_scope,
            current_legal_scope,
        ) = _build_deterministic_hints(
            request=request,
            catalog_provider=catalog_provider,
        )

        history_turns = [
            HistoryTurn(role=message.role, content=message.content)
            for message in request.history
        ]

        outcome = understand_request(
            current_question=request.question,
            history=history_turns,
            hints=hints,
            catalog_provider=catalog_provider,
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

        result = outcome.result

        metrics.request_understanding_method = "semantic"
        metrics.request_understanding_confidence = result.confidence
        metrics.request_status = result.status
        metrics.contextual_question_used = result.is_follow_up

        if _check_explicit_filter_conflict(request, result):
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
            )

        if result.status == "unsupported":
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
            )

        if result.status == "clarification":
            metrics.clarification_reason = result.clarification_reason

            unavailable_hint = (
                hints.current_unavailable_country_codes
                or hints.history_unavailable_country_codes
            )

            if (
                result.clarification_reason == "missing_country"
                and unavailable_hint
            ):
                answer_text = _unavailable_countries_answer(
                    unavailable_hint
                )
                metrics.outcome = "fallback_unavailable_country"
            else:
                answer_text = _clarification_answer_for(result)
                metrics.outcome = (
                    f"clarification_{result.clarification_reason}"
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
            )

        response = _execute_resolved_plan(
            request=request,
            result=result,
            metrics=metrics,
            catalog_provider=catalog_provider,
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

    except Exception as error:
        metrics.outcome = "error"
        metrics.error_type = type(error).__name__

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
