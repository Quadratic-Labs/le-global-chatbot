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
    resolve_legal_scope,
)
from app.services.rag_answer import (
    DEFAULT_MAX_CONTEXT_CHARACTERS,
    DEFAULT_MAX_SOURCE_CHARACTERS,
    NO_INFORMATION_ANSWER,
    InvalidLegalChatRequestError,
    RagAnswerError,
    SearchFunction,
    TextGenerationClient,
    answer_legal_question,
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


# Maximum characters of a previous user question folded into a
# contextualized search question, and how many distinct previous
# questions may be folded in at once - kept short/few since this only
# needs to disambiguate a follow-up, not restate prior turns.
MAX_CONTEXTUAL_PREVIOUS_QUESTION_CHARACTERS: Final[int] = 500
MAX_CONTEXTUAL_PREVIOUS_QUESTIONS: Final[int] = 2


# Contact-intent detection is split into three categories, per the
# mission's own naming:
#
# STRONG_CONTACT_INTENT (see _detect_contact_intent) - precise enough
# to route to the deterministic contact path from the question text
# alone, with no country/topic context needed:
#   - precise_le_global_identification: a direct identification
#     question ("what/which/who/where is/are the L&E Global ...") -
#     never a loose "L&E Global ... appears somewhere with a
#     structure noun" co-occurrence, which used to fire even for
#     "Can the L&E Global member firm terminate an employee?".
#   - professional_acquisition_request: a professional/firm is the
#     actual grammatical object of an explicit acquisition phrasing
#     ("find me AN EMPLOYMENT LAWYER", "I need AN EMPLOYMENT LAWYER")
#     - never merely mentioned alongside it ("find me CASES ABOUT law
#       firms", "I need INFORMATION ABOUT employment lawyers"), and
#     never in possessive form ("an ATTORNEY'S rights").
#   - explicit_contact_data_request: a contact-data expression
#     explicitly linked (via "of"/"for", or as "<office> <data
#     term>") to a professional/firm/L&E-Global/office target - never
#     a bare data expression, and never merely co-occurring anywhere
#     in the sentence with a professional noun (which used to fire
#     for "Show me the law firm's OBLIGATIONS" just because "law
#     firm" and an acquisition verb both appeared somewhere).
#
# COUNTRY_SCOPED_REACH_INTENT (see _has_direct_who_to_reach_form,
# combined by the router with a resolved country and the absence of a
# supported legal topic on the current question) - a direct "who/how
# can I reach ..." form names no professional at all, so it is only
# contact intent when the question is not really about a legal topic
# and a country is otherwise identified ("Who should I email in
# Peru?"), never when a legal topic is also present ("Who should I
# contact regarding dismissal procedure in Australia?" stays legal).

# precise_le_global_identification: an identification question,
# anchored at the very start of the (normalized) question AND
# validated all the way to the end - never a general co-occurrence
# anywhere in the sentence (which used to also fire for "Can the L&E
# Global member firm terminate an employee?"), and never a structure
# noun followed by anything other than a location clause or the end of
# the question (which used to also fire for "What is the L&E Global
# member firm's obligation regarding dismissal?" or "What is the L&E
# Global office policy on overtime?" - both continue past the
# structure noun into unrelated legal content). After the structure
# noun, only end-of-question, or "in"/"for"/"covers(ing)"/"serves(ing)"
# followed by a place and then the end of the question, are accepted.
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
# noun as the immediate object of an explicit acquisition phrasing -
# an optional article, then the noun, then either nothing more, a
# sentence-ending punctuation mark, or a location phrase ("in Peru",
# "there"). Excludes a possessive ("an attorney's ...") and excludes
# the noun being followed by anything else ("cases about ...",
# "information about ...", "'s obligations ...", "policy on ..."),
# since those mean the professional is merely mentioned, not the thing
# being requested.
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
# data suffix), and form 3 (target + contact as a noun). Only these
# phrasings ever introduce a request; a bare mention of a data
# expression or a professional noun elsewhere in the sentence never
# does, regardless of what verb happens to appear nearby.
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
# professional/firm/L&E-Global target. Never a bare data+target
# co-occurrence with no request phrasing at all (which used to also
# fire for "Is the email address of a lawyer personal data?" or "Can
# an employer disclose the phone number of an attorney?" - neither
# contains any of these phrasings).
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
# exception ("What is the phone number for the L&E Global office in
# Peru?") that never contains an "explicit request" verb phrase at
# all - validated as its own complete structure, anchored from the
# very start of the question to its end (an optional trailing "in"/
# "for" + place), exactly like precise_le_global_identification. This
# is what tells it apart from "What rules govern the email address of
# an attorney?", which does not open with "what is the" immediately
# followed by a data term.
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
# term>" as its final phrase ("the Peru office email", "the Australia
# office phone number") - a genuine bureau's own coordinates. The
# request phrasing is what tells "Can I have the Peru office email?"
# apart from "Who may access the Peru office email?" or "What policy
# governs the Peru office address?", neither of which asks to be given
# anything. The grammatical possessive immediately before "office"
# ('s / s' / their / his / her, never an enumerated list of which
# possessor is disallowed) still excludes someone's own workplace
# ("the employee's office address"), and the data term must still end
# the question, excluding a requirement/policy statement ("the office
# address requirement for employment contracts").
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
# professional/firm noun and "contact" used as a noun ("a lawyer
# contact", "an employment lawyer contact") - the professional+contact
# phrase must be the direct object of the request, exactly like
# professional_acquisition_request, never merely co-occurring anywhere
# else in the sentence. This is what tells "Can you give me a lawyer
# contact there?" apart from "Can you show me whether a lawyer contact
# is personal data?" (the object of "show me" is "whether ...", not
# "a lawyer contact") or "Can you provide information about a lawyer
# contact policy?" (the object of "provide" is "information", and the
# noun phrase itself continues into "policy").
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
# "who/how can I reach ..." form. Its own grammatical object already
# is the professional/office to reach, but unlike the phrasings above
# it names no professional at all - so it is never sufficient by
# itself. The router (resolve_legal_chat_response) only treats it as
# contact intent once a country is resolved AND the current question
# carries no supported legal topic.
_DIRECT_WHO_TO_REACH_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\bwho\s+(?:can|should)\s+i\s+"
    r"(?:contact|speak\s+to|email|call)\b"
    r"|\bhow\s+(?:can|do|should)\s+i\s+"
    r"(?:contact|reach|email|call|speak\s+to)\b"
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
    information. Entries whose content is empty after stripping are
    skipped. History already holds at most the three most recently
    validated user turns, so this naturally yields at most three
    questions.
    """

    for message in reversed(history):
        if message.role != "user":
            continue

        stripped_content = message.content.strip()

        if not stripped_content:
            continue

        yield stripped_content


def _build_contextual_question(
    previous_questions: list[str],
    current_question: str,
) -> str:
    """
    Build a short, explicitly-labeled disambiguation string.

    Folds in only the previous user question(s) actually needed to
    resolve a country or topic gap - never an assistant turn, never
    the full history - each capped at
    MAX_CONTEXTUAL_PREVIOUS_QUESTION_CHARACTERS. Used both to try one
    candidate previous question during fallback resolution (called
    with a single-item list) and to build the question actually sent
    to retrieval once the one or two necessary previous questions are
    known.
    """

    clipped_questions = [
        previous_question[
            :MAX_CONTEXTUAL_PREVIOUS_QUESTION_CHARACTERS
        ]
        for previous_question in previous_questions
    ]

    if len(clipped_questions) == 1:
        return (
            "Relevant previous user question:\n"
            f"{clipped_questions[0]}\n\n"
            f"Current question:\n{current_question}"
        )

    bullet_lines = "\n".join(
        f"- {question}" for question in clipped_questions
    )

    return (
        "Relevant previous user questions:\n"
        f"{bullet_lines}\n\n"
        f"Current question:\n{current_question}"
    )


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
    Detect STRONG_CONTACT_INTENT - precise enough to route to the
    deterministic contact path from the question text alone, with no
    country/topic context needed.

    return (
        precise_le_global_identification
        or professional_acquisition_request
        or explicit_contact_data_request
    )

    Deliberately excludes the bare "who/how can I contact/reach ..."
    phrasing (see _has_direct_who_to_reach_form): that one names no
    professional at all, so on its own it is never precise enough -
    the router only treats it as contact intent once combined with a
    resolved country and the absence of a supported legal topic on the
    current question.
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
    Detect COUNTRY_SCOPED_REACH_INTENT's phrasing half only - never
    sufficient by itself. See _DIRECT_WHO_TO_REACH_PATTERN and
    resolve_legal_chat_response, which combines this with a resolved
    country and the absence of a supported legal topic on the current
    question before treating it as contact intent.
    """

    normalized_question = _normalize_contact_question(
        question
    )

    return bool(
        _DIRECT_WHO_TO_REACH_PATTERN.search(
            normalized_question
        )
    )


def _resolve_contact_country_codes(
    request: LegalChatRequest,
    catalog_provider: CountryCatalogProvider,
) -> tuple[CountryAvailability, bool]:
    """
    Resolve the country/countries a contact request concerns.

    Always prefers the current question (or explicit country_codes)
    over the conversation history. Only when the current question
    alone names no country at all does it try each previous user
    question in turn, most recent first, stopping at the first one
    that - combined with the current question - resolves any
    available or unavailable country. Returns whether that history
    fallback was actually used.
    """

    country_scope = resolve_country_availability(
        request=request,
        catalog_provider=catalog_provider,
    )

    if (
        country_scope.available_codes
        or country_scope.unavailable_codes
    ):
        return country_scope, False

    for previous_question in _iter_recent_user_questions(
        request.history
    ):
        contextual_request = request.model_copy(
            update={
                "question": _build_contextual_question(
                    previous_questions=[
                        previous_question,
                    ],
                    current_question=request.question,
                ),
            }
        )

        fallback_scope = resolve_country_availability(
            request=contextual_request,
            catalog_provider=catalog_provider,
        )

        if (
            fallback_scope.available_codes
            or fallback_scope.unavailable_codes
        ):
            return fallback_scope, True

    return country_scope, False


def _build_contact_response(
    country_codes: list[str],
    unavailable_country_codes: list[str],
    metrics: LegalChatMetrics,
) -> LegalChatResponse:
    """
    Build one deterministic contact answer, never calling OpenAI.

    Every requested country either contributes its validated contact
    card (with its own source citation) or an explicit "not found"
    line - never a contact borrowed from a different country, and
    never an invented field.
    """

    sources: list[LegalAnswerSource] = []
    answer_sections: list[str] = []
    retrieval_total = 0

    if country_codes:
        try:
            contact_response = search_contact_chunks(
                country_codes=country_codes
            )

        except LegalSearchError as error:
            raise RagAnswerError(
                "Legal document retrieval failed."
            ) from error

        metrics.opensearch_ms += float(
            contact_response.took_ms
        )

        retrieval_total += contact_response.total

        hits_by_country_code: dict[
            str,
            list,
        ] = {}

        for hit in contact_response.hits:
            hits_by_country_code.setdefault(
                hit.country_code.upper(),
                [],
            ).append(
                hit
            )

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
                citation = len(sources) + 1

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
                    f"{display_name}\n"
                    f"{hit.content} [{citation}]"
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

    metrics.outcome = (
        "contact_resolved"
        if sources
        else "contact_not_found"
    )
    metrics.retrieval_total = retrieval_total
    metrics.selected_sources = len(
        sources
    )
    metrics.model = None
    metrics.generation_attempts = 0
    metrics.repair_triggered = False
    metrics.repair_success = False
    metrics.repair_answer_returned = False

    return LegalChatResponse(
        question="",
        answer="\n\n".join(
            answer_sections
        ),
        grounded=bool(
            sources
        ),
        model=None,
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
    Resolve one legal-chat request, applying scope checks first.

    Retrieval and generation are skipped entirely when every
    mentioned country is outside the corpus, or when the question
    carries no recognized legal topic and is not a general overview
    request. This avoids searching without a meaningful filter and
    citing unrelated passages.

    Exactly one "legal_chat_performance" log event is emitted per
    call, on every path (fallback, success, or error).
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
        if _detect_contact_intent(
            request.question
        ):
            (
                contact_country_scope,
                contact_contextual_question_used,
            ) = _resolve_contact_country_codes(
                request=request,
                catalog_provider=catalog_provider,
            )

            metrics.country_codes = list(
                contact_country_scope.available_codes
            )
            metrics.unavailable_country_codes = list(
                contact_country_scope.unavailable_codes
            )
            metrics.contextual_question_used = (
                contact_contextual_question_used
            )

            if (
                not contact_country_scope.available_codes
                and not contact_country_scope.unavailable_codes
            ):
                metrics.outcome = "contact_clarification"

                metrics.total_ms = (
                    perf_counter() - total_started_at
                ) * 1000

                metrics.log()

                return LegalChatResponse(
                    question=request.question.strip(),
                    answer=CONTACT_CLARIFICATION_ANSWER,
                    grounded=False,
                    model=None,
                    retrieval_total=0,
                    sources=[],
                )

            contact_response = _build_contact_response(
                country_codes=(
                    contact_country_scope.available_codes
                ),
                unavailable_country_codes=(
                    contact_country_scope.unavailable_codes
                ),
                metrics=metrics,
            )

            contact_response = contact_response.model_copy(
                update={
                    "question": request.question.strip(),
                }
            )

            metrics.total_ms = (
                perf_counter() - total_started_at
            ) * 1000

            metrics.log()

            return contact_response

        detection_started_at = perf_counter()

        country_scope = resolve_country_availability(
            request=request,
            catalog_provider=catalog_provider,
        )

        contextual_question_used = False
        necessary_previous_questions: list[str] = []

        if (
            not country_scope.available_codes
            and not country_scope.unavailable_codes
        ):
            for previous_question in (
                _iter_recent_user_questions(
                    request.history
                )
            ):
                contextual_country_request = (
                    request.model_copy(
                        update={
                            "question": (
                                _build_contextual_question(
                                    previous_questions=[
                                        previous_question,
                                    ],
                                    current_question=(
                                        request.question
                                    ),
                                )
                            ),
                        }
                    )
                )

                fallback_country_scope = (
                    resolve_country_availability(
                        request=contextual_country_request,
                        catalog_provider=catalog_provider,
                    )
                )

                if (
                    fallback_country_scope.available_codes
                    or fallback_country_scope.unavailable_codes
                ):
                    country_scope = fallback_country_scope
                    contextual_question_used = True
                    necessary_previous_questions.append(
                        previous_question
                    )
                    break

        metrics.country_detection_ms = (
            perf_counter() - detection_started_at
        ) * 1000

        metrics.country_codes = list(
            country_scope.available_codes
        )
        metrics.unavailable_country_codes = list(
            country_scope.unavailable_codes
        )

        # COUNTRY_SCOPED_REACH_INTENT: a direct "who/how can I reach
        # ..." form names no professional at all, so on its own
        # (_has_direct_who_to_reach_form) it is never precise enough -
        # it only becomes contact intent once a country is resolved
        # (available or unavailable; already computed just above,
        # including any history fallback) AND the current question
        # carries no supported legal topic. The topic check below
        # deliberately reuses the conversation-history-based topic
        # detector reserved for that one purpose: it must reflect the
        # CURRENT question alone, never a contextualized one, since a
        # supported topic on the current question ("Who should I
        # contact regarding dismissal procedure in Australia?") must
        # always stay legal RAG regardless of history. Its result is
        # reused below as the seed for the normal flow's own topic
        # resolution when this branch does not route to Contact, so
        # resolve_legal_scope is never called twice for the same
        # input.
        precomputed_legal_scope = None
        precomputed_topic_detection_ms = 0.0

        if _has_direct_who_to_reach_form(
            request.question
        ):
            topic_detection_started_at = perf_counter()

            precomputed_legal_scope = resolve_legal_scope(
                request
            )

            precomputed_topic_detection_ms = (
                perf_counter()
                - topic_detection_started_at
            ) * 1000

            country_resolved = bool(
                country_scope.available_codes
                or country_scope.unavailable_codes
            )

            if (
                country_resolved
                and not precomputed_legal_scope.is_supported
            ):
                metrics.contextual_question_used = (
                    contextual_question_used
                )

                contact_response = _build_contact_response(
                    country_codes=(
                        country_scope.available_codes
                    ),
                    unavailable_country_codes=(
                        country_scope.unavailable_codes
                    ),
                    metrics=metrics,
                )

                contact_response = (
                    contact_response.model_copy(
                        update={
                            "question": (
                                request.question.strip()
                            ),
                        }
                    )
                )

                metrics.total_ms = (
                    perf_counter() - total_started_at
                ) * 1000

                metrics.log()

                return contact_response

        if (
            country_scope.unavailable_codes
            and not country_scope.available_codes
        ):
            metrics.outcome = (
                "fallback_unavailable_country"
            )
            metrics.contextual_question_used = (
                contextual_question_used
            )

            metrics.total_ms = (
                perf_counter() - total_started_at
            ) * 1000

            metrics.log()

            return LegalChatResponse(
                question=request.question.strip(),
                answer=_unavailable_countries_answer(
                    country_scope.unavailable_codes
                ),
                grounded=False,
                model=None,
                retrieval_total=0,
                sources=[],
            )

        detection_started_at = perf_counter()

        legal_scope = (
            precomputed_legal_scope
            if precomputed_legal_scope is not None
            else resolve_legal_scope(
                request
            )
        )

        if not legal_scope.is_supported:
            for previous_question in (
                _iter_recent_user_questions(
                    request.history
                )
            ):
                contextual_topic_request = (
                    request.model_copy(
                        update={
                            "question": (
                                _build_contextual_question(
                                    previous_questions=[
                                        previous_question,
                                    ],
                                    current_question=(
                                        request.question
                                    ),
                                )
                            ),
                            "country_codes": (
                                country_scope.available_codes
                            ),
                        }
                    )
                )

                fallback_legal_scope = resolve_legal_scope(
                    contextual_topic_request
                )

                if fallback_legal_scope.is_supported:
                    legal_scope = fallback_legal_scope
                    contextual_question_used = True

                    if (
                        previous_question
                        not in necessary_previous_questions
                    ):
                        necessary_previous_questions.append(
                            previous_question
                        )

                    break

        metrics.topic_detection_ms = (
            precomputed_topic_detection_ms
            + (perf_counter() - detection_started_at) * 1000
        )

        metrics.legal_topics = list(
            legal_scope.legal_topics
        )

        if not legal_scope.is_supported:
            metrics.outcome = (
                "fallback_unsupported_topic"
            )
            metrics.contextual_question_used = (
                contextual_question_used
            )

            metrics.total_ms = (
                perf_counter() - total_started_at
            ) * 1000

            metrics.log()

            return LegalChatResponse(
                question=request.question.strip(),
                answer=NO_INFORMATION_ANSWER,
                grounded=False,
                model=None,
                retrieval_total=0,
                sources=[],
            )

        final_contextual_question = (
            _build_contextual_question(
                previous_questions=(
                    necessary_previous_questions[
                        :MAX_CONTEXTUAL_PREVIOUS_QUESTIONS
                    ]
                ),
                current_question=request.question,
            )
            if necessary_previous_questions
            else None
        )

        prepared_request = request.model_copy(
            update={
                "country_codes": (
                    country_scope.available_codes
                ),
                "legal_topics": (
                    legal_scope.legal_topics
                ),
                "question": (
                    final_contextual_question
                    if contextual_question_used
                    and final_contextual_question
                    else request.question
                ),
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

        # The retrieval/generation input above may have used a
        # contextualized question to resolve a follow-up - the
        # response always echoes the user's real original question,
        # never that internal representation.
        response = response.model_copy(
            update={
                "question": request.question.strip(),
            }
        )

        if country_scope.unavailable_codes:
            note = (
                "\n\nNote: "
                + _unavailable_countries_answer(
                    country_scope.unavailable_codes
                )
            )

            response = response.model_copy(
                update={
                    "answer": response.answer + note,
                }
            )

        metrics.contextual_question_used = (
            contextual_question_used
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
