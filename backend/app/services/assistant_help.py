"""
Deterministic detection and response-building for questions about the
assistant itself - identity, capabilities, supported topics/countries,
comparison and contact guidance, usage examples, sources, and
limitations. Never the RAG pipeline: zero OpenAI calls, zero
OpenSearch calls, no document retrieval, no generation.

Reuses the project's existing static configuration - CANONICAL_LEGAL_
TOPICS (legal_topic_taxonomy.py) and COUNTRIES (country_registry.py) -
rather than a second, independent list, so a help response updates
automatically whenever a topic or country is added there. country_
detection.get_legal_catalog() is deliberately NOT used here: it is a
live OpenSearch aggregation query, and this module must never call
OpenSearch at all, even to describe scope.

Detection is intentionally NOT a general "is this about the chatbot"
classifier - mission "PATCH PRODUIT 0.4.3" explicitly forbids broad
fuzzy matching that could divert a real legal question. Each category
requires a tight anchor (an explicit "you"/"your"/"this chatbot"
reference, or - for comparison/contact - the shape of a request
actually directed at the assistant) precisely so that a real legal
question sharing surface vocabulary ("the role of trade unions",
"what topics must be discussed with a works council", "can an
employer compare employee salaries") is never intercepted. See
_FALSE_POSITIVE examples exercised in the test suite for the exact
boundary this module must hold.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final, Literal, Sequence

from app.core.country_registry import COUNTRIES, canonical_country_name
from app.services.country_detection import (
    detect_mentioned_country_codes,
    get_country_demonyms,
    get_country_name_variants,
    resolve_country_display_name,
)
from app.services.legal_topic_detection import (
    CANONICAL_LEGAL_TOPICS,
    detect_legal_topics,
)

AssistantHelpIntentType = Literal[
    "assistant_identity",
    "assistant_capabilities",
    "supported_legal_topics",
    "supported_countries",
    "comparison_capabilities",
    "comparison_guidance",
    "contact_capabilities",
    "question_examples",
    "source_policy",
    "assistant_limitations",
]


@dataclass(frozen=True, slots=True)
class AssistantHelpIntent:
    """
    One deterministically-recognized question about the assistant
    itself, never a legal question.

    `referenced_country_codes` is populated only when the question
    itself named specific countries relevant to the intent (a
    targeted "Do you cover Spain?" or a comparison naming two
    countries with no topic yet) - never a guess at country scope
    the user did not state. `referenced_topic` is reserved for a
    future intent that names a specific topic; no current intent
    type populates it.
    """

    intent_type: AssistantHelpIntentType
    referenced_country_codes: tuple[str, ...] = ()
    referenced_topic: str | None = None
    requested_examples: bool = False
    requested_comparison_help: bool = False


_CONTRACTION_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\b(whats|what's)\b"
)
_CANT_PATTERN: Final[re.Pattern[str]] = re.compile(r"\b(cant|can't)\b")
_BARE_U_PATTERN: Final[re.Pattern[str]] = re.compile(r"\bu\b")
_PUNCTUATION_PATTERN: Final[re.Pattern[str]] = re.compile(r"[^\w\s']")
_WHITESPACE_PATTERN: Final[re.Pattern[str]] = re.compile(r"\s+")


def _normalize(question: str) -> str:
    """
    Lowercase, unify curly/straight apostrophes and the "what's"/
    "whats" and "can't"/"cant" contraction pairs into one canonical
    form each, spell out the bare "u" text-speak abbreviation as
    "you", strip all other punctuation, and collapse whitespace - so
    every pattern below only ever needs to spell out one form.
    """

    text = question.casefold()
    text = text.replace("’", "'")
    text = _CONTRACTION_PATTERN.sub("what is", text)
    text = _CANT_PATTERN.sub("cannot", text)
    text = _PUNCTUATION_PATTERN.sub(" ", text)
    text = text.replace("'", " ")
    text = _WHITESPACE_PATTERN.sub(" ", text).strip()
    text = _BARE_U_PATTERN.sub("you", text)
    return text


def _compile_all(patterns: Sequence[str]) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(p) for p in patterns)


# --- assistant_identity --------------------------------------------

_IDENTITY_PATTERNS: Final[tuple[re.Pattern[str], ...]] = _compile_all(
    (
        r"\bwho are you\b",
        r"\bwhat are you\b",
        r"\bwhat is this (chatbot|assistant|bot)\b",
        r"\bwhat (chatbot|assistant|bot) is this\b",
        r"\bwhat is your (role|purpose)\b",
        r"\bwhy are you here\b",
        r"\bwhat do you do\b",
    )
)


def _is_identity_question(text: str) -> bool:
    return any(pattern.search(text) for pattern in _IDENTITY_PATTERNS)


# --- assistant_capabilities -----------------------------------------

_CAPABILITIES_PATTERNS: Final[tuple[re.Pattern[str], ...]] = _compile_all(
    (
        r"\bwhat can you (do|answer|help)\b",
        r"\bwhat can i ask( you)?\b",
        r"\bhow (?:can you|you can) help\b",
        r"\bshow me what you can do\b",
        r"\bwhat do you know about\b",
        r"\bhelp me use this (chatbot|assistant|bot)\b",
    )
)

_CAPABILITIES_BARE_HELP: Final[frozenset[str]] = frozenset(
    {"help", "help me"}
)

_CAPABILITY_WH_WORDS: Final[frozenset[str]] = frozenset(
    {"what", "which", "how"}
)
_CAPABILITY_QUESTION_WORDS: Final[frozenset[str]] = frozenset(
    {"question", "questions", "ask"}
)
_CAPABILITY_VERBS: Final[frozenset[str]] = frozenset(
    {"can", "answer", "help"}
)


def _is_capabilities_question(text: str) -> bool:
    if any(pattern.search(text) for pattern in _CAPABILITIES_PATTERNS):
        return True

    if text in _CAPABILITIES_BARE_HELP:
        return True

    tokens = set(text.split())

    return (
        bool(tokens & _CAPABILITY_WH_WORDS)
        and "you" in tokens
        and bool(tokens & _CAPABILITY_QUESTION_WORDS)
        and bool(tokens & _CAPABILITY_VERBS)
    )


# --- supported_legal_topics -----------------------------------------

_TOPICS_EXPLICIT_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    _compile_all((r"\blist the available topics\b",))
)

_TOPICS_WH_WORDS: Final[frozenset[str]] = frozenset({"what", "which"})
_TOPICS_TOPIC_WORDS: Final[frozenset[str]] = frozenset(
    {
        "topic",
        "topics",
        "theme",
        "themes",
        "subject",
        "subjects",
        "law",
        "laws",
    }
)
_TOPICS_SCOPE_VERBS: Final[frozenset[str]] = frozenset(
    {"cover", "available", "answer", "explain", "ask", "help"}
)


def _is_topics_question(text: str) -> bool:
    if any(
        pattern.search(text) for pattern in _TOPICS_EXPLICIT_PATTERNS
    ):
        return True

    tokens = set(text.split())

    return (
        bool(tokens & _TOPICS_WH_WORDS)
        and bool(tokens & _TOPICS_TOPIC_WORDS)
        and bool(tokens & _TOPICS_SCOPE_VERBS)
    )


# --- supported_countries --------------------------------------------

_COUNTRIES_EXPLICIT_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    _compile_all(
        (
            r"\bwhere can you answer employment law questions\b",
            r"\blist the countries\b",
        )
    )
)

_COUNTRIES_WH_WORDS: Final[frozenset[str]] = frozenset(
    {"what", "which", "where"}
)
_COUNTRIES_COUNTRY_WORDS: Final[frozenset[str]] = frozenset(
    {"country", "countries", "jurisdiction", "jurisdictions"}
)
_COUNTRIES_SCOPE_WORDS: Final[frozenset[str]] = frozenset(
    {"cover", "available", "supported", "support"}
)

_COUNTRIES_TARGETED_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    _compile_all(
        (
            r"\bdo you cover\b",
            r"\bcan you answer\b",
            r"\bis .+ (supported|available)\b",
        )
    )
)


def _is_countries_general_question(text: str) -> bool:
    if any(
        pattern.search(text) for pattern in _COUNTRIES_EXPLICIT_PATTERNS
    ):
        return True

    tokens = set(text.split())

    return (
        bool(tokens & _COUNTRIES_WH_WORDS)
        and bool(tokens & _COUNTRIES_COUNTRY_WORDS)
        and bool(tokens & _COUNTRIES_SCOPE_WORDS | {"you"} & tokens)
    )


def _is_countries_targeted_question(text: str) -> bool:
    return any(
        pattern.search(text) for pattern in _COUNTRIES_TARGETED_PATTERNS
    )


# --- comparison_capabilities / comparison_guidance -------------------

_COMPARISON_EXPLICIT_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    _compile_all(
        (
            r"\bcan you compare\b",
            r"\bwhat (countries|comparisons|topics) can you compare\b",
            r"\bwhich topics can be compared\b",
            r"\bwhat comparisons can you make\b",
            r"\bhow (do |does )?comparisons? works?\b",
            r"\bhow can i compare countries\b",
            r"\bcompare which countries\b",
            r"\bwhat (do i need|is required)"
            r"( to provide)? for a comparison\b",
            r"\bcan you make a multi.?country comparison\b",
            r"\bwhat happens if one country has no information\b",
            r"\bcan you compare countries if one document is "
            r"incomplete\b",
            r"\bdo comparisons use the same sources\b",
            r"\bhow reliable are the comparisons\b",
            r"\bcan you compare different topics\b",
            r"\bcan you compare more than two countries\b",
        )
    )
)

_COMPARISON_LIMITS_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    _compile_all(
        (
            r"\bwhat happens if one country has no information\b",
            r"\bcan you compare countries if one document is "
            r"incomplete\b",
            r"\bdo comparisons use the same sources\b",
            r"\bhow reliable are the comparisons\b",
            r"\bcan you compare different topics\b",
            r"\bcan you compare more than two countries\b",
        )
    )
)


_COMPARISON_CONNECTOR_WORDS: Final[frozenset[str]] = frozenset(
    {
        "compare",
        "comparing",
        "comparison",
        "can",
        "you",
        "i",
        "please",
        "the",
        "a",
        "an",
        "and",
        "with",
        "between",
        "vs",
        "versus",
        "or",
        "to",
    }
)


def _is_bare_country_comparison(
    text: str, country_codes: Sequence[str]
) -> bool:
    """
    True only when, after removing every mentioned country's own name/
    demonym and a small set of connector words, nothing substantive
    remains - "Compare Spain and Peru" is bare; "Compare employment
    rules between Spain and Poland" is not (real, if vague, subject
    content survives) and must never be treated as meta guidance.
    """

    working_text = text

    for code in country_codes:
        for variant in (
            get_country_name_variants(code) + get_country_demonyms(code)
        ):
            working_text = re.sub(
                rf"(?<!\w){re.escape(variant.casefold())}(?!\w)",
                " ",
                working_text,
            )

    remaining_tokens = [
        token
        for token in working_text.split()
        if token not in _COMPARISON_CONNECTOR_WORDS
    ]

    return not remaining_tokens


def _is_comparison_trigger(
    text: str, country_codes: Sequence[str]
) -> bool:
    if any(
        pattern.search(text) for pattern in _COMPARISON_EXPLICIT_PATTERNS
    ):
        return True

    return "compare" in text.split() and len(country_codes) >= 2


def _is_comparison_limits_question(text: str) -> bool:
    return any(
        pattern.search(text) for pattern in _COMPARISON_LIMITS_PATTERNS
    )


# --- contact_capabilities --------------------------------------------

_CONTACT_TRIGGER_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    _compile_all(
        (
            r"\bcan you (provide|give)( me)? "
            r"(a )?(law firm |member firm )?contacts?\b",
            r"\bwhat contact (information|details) can you give\b",
            r"\bcan i ask for (member firm )?contacts\b",
            r"\bwhich contacts do you have\b",
            r"\bhow do i get (the |a )?contact\b",
        )
    )
)


def _is_contact_trigger(text: str) -> bool:
    return any(
        pattern.search(text) for pattern in _CONTACT_TRIGGER_PATTERNS
    )


# --- question_examples ------------------------------------------------

_EXAMPLES_PATTERNS: Final[tuple[re.Pattern[str], ...]] = _compile_all(
    (
        r"\bgive( me)?( a| an)? (comparison |contact )?examples?\b",
        r"\bshow( me)? example questions?\b",
        r"\bhow should i ask a question\b",
        r"\bhow do i use this (chatbot|assistant|bot)\b",
        r"\bwhat is a good question\b",
        r"\bhow should i formulate my request\b",
    )
)


def _is_examples_question(text: str) -> bool:
    return any(pattern.search(text) for pattern in _EXAMPLES_PATTERNS)


# --- source_policy / assistant_limitations ---------------------------

_SOURCES_PATTERNS: Final[tuple[re.Pattern[str], ...]] = _compile_all(
    (
        r"\bwhat sources? (do )?you use\b",
        r"\bwhere does your information come from\b",
        r"\bdo you use the internet\b",
        r"\bare your answers legal advice\b",
        r"\bis this legal advice\b",
        r"\bcan you answer from your own knowledge\b",
    )
)

_LIMITATIONS_PATTERNS: Final[tuple[re.Pattern[str], ...]] = _compile_all(
    (
        r"\bwhat are your limitations\b",
        r"\bwhat cannot you answer\b",
        r"\bcan you answer questions outside employment law\b",
        r"\bcan you invent an answer if information is missing\b",
    )
)


def _is_sources_question(text: str) -> bool:
    return any(pattern.search(text) for pattern in _SOURCES_PATTERNS)


def _is_limitations_question(text: str) -> bool:
    return any(pattern.search(text) for pattern in _LIMITATIONS_PATTERNS)


# --- main dispatcher --------------------------------------------------


def detect_assistant_help_intent(
    question: str,
    supported_country_codes: Sequence[str],
) -> AssistantHelpIntent | None:
    """
    Deterministically recognize a question about the assistant itself
    - never a real legal question. Returns None whenever the question
    should continue to the normal legal/contact/comparison pipeline,
    including when it superficially resembles a help question but
    names a real legal topic or a targeted country/contact request
    (see the module docstring).
    """

    text = _normalize(question)

    if not text:
        return None

    country_codes = tuple(detect_mentioned_country_codes(question))

    if _is_identity_question(text):
        return AssistantHelpIntent(intent_type="assistant_identity")

    if _is_topics_question(text):
        # Checked ahead of _is_capabilities_question: a question
        # explicitly naming "topics"/"themes" (e.g. "What employment
        # law topics can you answer questions about?") also happens
        # to satisfy that function's own broad "can/answer/help +
        # question word" fallback - the explicit topics word makes
        # the request for the 11-topic list unambiguous, and must win.
        return AssistantHelpIntent(intent_type="supported_legal_topics")

    if _is_capabilities_question(text):
        return AssistantHelpIntent(
            intent_type="assistant_capabilities",
            referenced_country_codes=country_codes,
        )

    if _is_countries_targeted_question(text) and len(country_codes) == 1:
        return AssistantHelpIntent(
            intent_type="supported_countries",
            referenced_country_codes=country_codes,
        )

    if _is_comparison_trigger(text, country_codes):
        if len(country_codes) >= 2:
            if detect_legal_topics(question) or not (
                _is_bare_country_comparison(text, country_codes)
            ):
                # A real comparison - an explicit topic was detected,
                # or real (if vague) subject content survives once
                # the countries themselves are stripped out ("employment
                # rules", "annual bonus scheme") - never meta, regardless
                # of the surface "compare" wording.
                return None

            return AssistantHelpIntent(
                intent_type="comparison_guidance",
                referenced_country_codes=country_codes,
            )

        if country_codes:
            # Exactly one country alongside "compare" is too
            # ambiguous to treat as bare meta guidance - it may be a
            # contextual follow-up relying on conversation history
            # this module never sees (e.g. "how does that compare
            # with Spain?"). Let RequestUnderstanding decide.
            return None

        return AssistantHelpIntent(intent_type="comparison_capabilities")

    if _is_countries_general_question(text):
        return AssistantHelpIntent(intent_type="supported_countries")

    if _is_contact_trigger(text):
        if country_codes:
            # A targeted contact request - never meta, the existing
            # contact pipeline resolves it with real coordinates.
            return None

        return AssistantHelpIntent(intent_type="contact_capabilities")

    if _is_examples_question(text):
        return AssistantHelpIntent(
            intent_type="question_examples", requested_examples=True
        )

    if _is_sources_question(text):
        return AssistantHelpIntent(intent_type="source_policy")

    if _is_limitations_question(text):
        return AssistantHelpIntent(intent_type="assistant_limitations")

    return None


# --- response building -------------------------------------------------

ASSISTANT_IDENTITY_ANSWER: Final[str] = (
    "I am the L&E Global employment law assistant. I use validated "
    "L&E Global documents to answer employment-law questions, "
    "compare legal topics across supported countries, and provide "
    "contact details for L&E Global member firms. I provide legal "
    "information, not legal advice."
)

ASSISTANT_CAPABILITIES_ANSWER: Final[str] = (
    "You can ask me for employment-law information about a "
    "supported country, compare the same legal topic across "
    "multiple countries, or request the contact details of an L&E "
    "Global member firm. When possible, include a country and a "
    "legal topic in your question.\n\n"
    "For example:\n"
    "- \"Explain overtime rules in Spain.\"\n"
    "- \"Compare termination notice in Australia and Peru.\"\n"
    "- \"Give me the contact details in the United Kingdom.\""
)

def _build_capabilities_answer(
    country_codes: Sequence[str],
) -> str:
    """
    The assistant_capabilities answer, naming every country the
    question itself mentioned - never a documentary-insufficiency
    message, since this is a meta question about what the assistant
    can do, not a real request for a country's legal content (mission
    "HOTFIX 0.4.4", section 2.6).
    """

    if not country_codes:
        return ASSISTANT_CAPABILITIES_ANSWER

    display_names = [
        resolve_country_display_name(code) for code in country_codes
    ]

    countries_phrase = " and ".join(display_names)

    return (
        f"For {countries_phrase}, I can provide employment-law "
        "information from the validated L&E Global documents, "
        f"compare {countries_phrase} with other supported countries "
        "on the same legal topic, and give you the contact details "
        "of the local L&E Global member firm. Ask me a specific "
        "employment-law question to get started, for example: "
        f"\"Explain termination notice in {display_names[0]}.\""
    )


COMPARISON_CAPABILITIES_ANSWER: Final[str] = (
    "I can compare the same employment-law topic across two or more "
    "supported countries. Please name the countries and one "
    "specific legal topic, for example: 'Compare termination notice "
    "in Spain and Peru.'"
)

COMPARISON_LIMITS_ANSWER: Final[str] = (
    "I compare each country independently using its validated L&E "
    "Global documents. If one country does not contain enough "
    "information for the requested topic, I will clearly indicate "
    "that rather than infer or invent a rule."
)

CONTACT_CAPABILITIES_ANSWER: Final[str] = (
    "I can provide the available L&E Global member-firm contact "
    "details for a supported country, such as the firm name, "
    "contact person, email, telephone, address and website. Please "
    "specify the country."
)

QUESTION_EXAMPLES_ANSWER: Final[str] = (
    "You can ask questions such as:\n\n"
    "- \"Explain overtime rules in Spain.\"\n"
    "- \"Can an employer dismiss an employee on sick leave in "
    "Peru?\"\n"
    "- \"Compare fixed-term contracts in the United Kingdom and "
    "Australia.\"\n"
    "- \"Give me the contact details in Spain.\"\n\n"
    "For the most precise answer, include the country, the legal "
    "topic and the specific issue you want to understand."
)

SOURCE_POLICY_ANSWER: Final[str] = (
    "I answer using the validated L&E Global employment-law "
    "documents available in this chatbot. I do not rely on "
    "unrelated external information for legal answers."
)

ASSISTANT_LIMITATIONS_ANSWER: Final[str] = (
    "I am limited to employment-law information and L&E Global "
    "member-firm contacts covered by the validated documents. I do "
    "not provide legal advice, and I will indicate when the "
    "documents do not contain enough information."
)


def _build_topics_answer() -> str:
    topic_list = ", ".join(CANONICAL_LEGAL_TOPICS)

    return (
        f"I can help with the following employment-law topics: "
        f"{topic_list}. You can ask about one country or compare the "
        "same topic across several supported countries.\n\n"
        "If the validated documents do not contain enough direct "
        "information for a specific question, I will say so."
    )


def _build_countries_general_answer() -> str:
    country_list = ", ".join(
        sorted(canonical_country_name(country.code) for country in COUNTRIES)
    )

    return (
        f"I currently cover the following countries: {country_list}."
    )


def _build_countries_targeted_answer(country_code: str) -> str:
    supported_codes = {country.code for country in COUNTRIES}
    display_name = resolve_country_display_name(country_code)

    if country_code in supported_codes:
        return (
            f"Yes. I can answer employment-law questions about "
            f"{display_name} using the validated L&E Global "
            "documents available in this chatbot."
        )

    return (
        f"I do not currently have validated L&E Global documents "
        f"for {display_name}."
    )


def _build_comparison_guidance_answer(country_codes: Sequence[str]) -> str:
    display_names = [
        resolve_country_display_name(code) for code in country_codes
    ]
    countries_phrase = " and ".join(display_names)

    return (
        f"Yes. I can compare {countries_phrase}. Which employment-law "
        "topic would you like to compare? For example: overtime, "
        "termination notice, fixed-term contracts or employee "
        "benefits."
    )


def build_assistant_help_answer(
    intent: AssistantHelpIntent, *, original_question: str
) -> str:
    """
    The complete, final answer text for one detected AssistantHelpIntent
    - deterministic, no OpenAI/OpenSearch call, never the documentary
    disclaimer automatically attached to a RAG answer.
    """

    if intent.intent_type == "assistant_identity":
        return ASSISTANT_IDENTITY_ANSWER

    if intent.intent_type == "assistant_capabilities":
        return _build_capabilities_answer(intent.referenced_country_codes)

    if intent.intent_type == "supported_legal_topics":
        return _build_topics_answer()

    if intent.intent_type == "supported_countries":
        if intent.referenced_country_codes:
            return _build_countries_targeted_answer(
                intent.referenced_country_codes[0]
            )

        return _build_countries_general_answer()

    if intent.intent_type == "comparison_guidance":
        return _build_comparison_guidance_answer(
            intent.referenced_country_codes
        )

    if intent.intent_type == "comparison_capabilities":
        if _is_comparison_limits_question(_normalize(original_question)):
            return COMPARISON_LIMITS_ANSWER

        return COMPARISON_CAPABILITIES_ANSWER

    if intent.intent_type == "contact_capabilities":
        return CONTACT_CAPABILITIES_ANSWER

    if intent.intent_type == "question_examples":
        return QUESTION_EXAMPLES_ANSWER

    if intent.intent_type == "source_policy":
        return SOURCE_POLICY_ANSWER

    if intent.intent_type == "assistant_limitations":
        return ASSISTANT_LIMITATIONS_ANSWER

    raise AssertionError(
        f"Unhandled AssistantHelpIntentType: {intent.intent_type!r}"
    )
