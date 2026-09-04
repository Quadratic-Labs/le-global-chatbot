"""
Deterministic conversational intents that do not require legal RAG.

This layer handles natural conversation, assistant capabilities,
catalogue requests, country availability, obvious country spelling
corrections, reset instructions and product-level safeguards.

It never produces substantive legal rules and never treats its own
answers as legal evidence.
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Callable, Final, Literal, Sequence

import pycountry

from app.models.catalog import LegalCatalogResponse
from app.services.country_detection import (
    detect_mentioned_country_codes,
    get_country_name_variants,
    resolve_country_display_name,
    resolve_jurisdiction,
)
from app.services.jurisdiction_resolution import (
    detect_unresolved_location_phrase,
    resolve_city_country_codes,
)
from app.services.legal_topic_detection import (
    CANONICAL_LEGAL_TOPICS,
    detect_legal_topics,
)


ConversationMetaIntent = Literal[
    "greeting",
    "wellbeing",
    "gratitude",
    "acknowledgement",
    "farewell",
    "reset",
    "assistant_identity",
    "assistant_capabilities",
    "capability_followup",
    "comparison_capabilities",
    "supported_countries",
    "targeted_country_availability",
    "supported_legal_topics",
    "country_legal_topics",
    "contact_catalogue",
    "country_suggestion",
    "unsupported_comparison",
    "coverage_list_followup",
    "ambiguous_city_clarification",
    "unknown_locality_clarification",
]


@dataclass(frozen=True, slots=True)
class ConversationMetaResolution:
    intent_type: ConversationMetaIntent
    answer: str
    preserve_conversation_state: bool = True


CatalogProvider = Callable[[], LegalCatalogResponse]


_NON_ALPHANUMERIC: Final[re.Pattern[str]] = re.compile(
    r"[^a-z0-9\s']+"
)
_WHITESPACE: Final[re.Pattern[str]] = re.compile(r"\s+")

_RESET_PHRASES: Final[frozenset[str]] = frozenset(
    {
        "reset",
        "start again",
        "start over",
        "restart",
        "clear the conversation",
        "clear the context",
        "forget my previous question",
        "forget the previous question",
        "forget everything",
        "cancel that",
    }
)

_GREETING_PHRASES: Final[frozenset[str]] = frozenset(
    {
        "hello",
        "hi",
        "hey",
        "good morning",
        "good afternoon",
        "good evening",
        "nice to meet you",
    }
)

_WELLBEING_PHRASES: Final[frozenset[str]] = frozenset(
    {
        "how are you",
        "how are you doing",
        "how is it going",
        "hows it going",
        "are you okay",
    }
)

_GRATITUDE_PHRASES: Final[frozenset[str]] = frozenset(
    {
        "thank you",
        "thanks",
        "thanks a lot",
        "thank you very much",
        "thx",
        "tnx",
        "that was helpful",
        "great thanks",
    }
)

_GRATITUDE_PATTERNS: Final[tuple[re.Pattern[str], ...]] = tuple(
    re.compile(pattern)
    for pattern in (
        r"^(?:thank you|thanks|thx|tnx)(?: a lot| so much| very much)?$",
        r"^(?:thank you|thanks) (?:that|this) was helpful$",
        r"^(?:thank you|thanks) (?:im|i am) fine$",
        r"^(?:thank you|thanks) for (?:the|your) help$",
    )
)

_ACKNOWLEDGEMENT_PATTERNS: Final[tuple[re.Pattern[str], ...]] = tuple(
    re.compile(pattern)
    for pattern in (
        r"^(?:a+ )?(?:ok|okay)$",
        r"^(?:no problem|got it|understood|alright|all right|sounds good)$",
    )
)

_AFFIRMATION_PHRASES: Final[frozenset[str]] = frozenset(
    {
        "yes",
        "yes please",
        "correct",
        "thats right",
        "that is right",
        "exactly",
    }
)

_FAREWELL_PHRASES: Final[frozenset[str]] = frozenset(
    {
        "bye",
        "goodbye",
        "see you",
        "see you later",
        "have a nice day",
    }
)

_FAREWELL_PATTERNS: Final[tuple[re.Pattern[str], ...]] = tuple(
    re.compile(pattern)
    for pattern in (
        r"^(?:(?:thank you|thanks) )?thats all(?: for now)?$",
        r"^(?:thank you|thanks) (?:bye|goodbye)$",
    )
)

_CAPABILITY_FOLLOWUPS: Final[frozenset[str]] = frozenset(
    {
        "what else",
        "and what else",
        "anything else",
        "what else can you do",
        "what other things can you do",
        "tell me more",
        "can you do more",
    }
)

_CAPABILITY_PHRASES: Final[tuple[re.Pattern[str], ...]] = tuple(
    re.compile(pattern)
    for pattern in (
        r"^what (else )?can you (do|help with|answer)$",
        r"^what (else )?can you help( me)? with$",
        r"^how can you help( me)?$",
        r"^how you can help( me)?$",
        r"^can you help me$",
        r"^help me$",
        r"^what can i ask( you)?$",
        r"^what kind of questions can i ask( you)?$",
        r"^tell me what you can do$",
        r"^show me what you can do$",
    )
)

_IDENTITY_PHRASES: Final[tuple[re.Pattern[str], ...]] = tuple(
    re.compile(pattern)
    for pattern in (
        r"^who are you$",
        r"^what are you$",
        r"^what is your role$",
        r"^whats your role$",
        r"^explain( me)? your role$",
        r"^tell me your role$",
        r"^what is this (chatbot|assistant|bot)$",
        r"^what is this (chatbot|assistant|bot) for$",
    )
)

_COMPARISON_CAPABILITY_PATTERNS: Final[
    tuple[re.Pattern[str], ...]
] = tuple(
    re.compile(pattern)
    for pattern in (
        r"^can you do comparisons( too)?$",
        r"^can you compare countries$",
        r"^do you compare countries$",
        r"^how do comparisons work$",
    )
)

_PERSONALISED_LEGAL_PATTERNS: Final[
    tuple[re.Pattern[str], ...]
] = tuple(
    re.compile(pattern)
    for pattern in (
        r"\bshould i (fire|dismiss|terminate|sue)\b",
        r"\bwhat should i do\b",
        r"\btell me exactly what to do\b",
        r"\bwill i win\b",
        r"\bhow much compensation will i receive\b",
        r"\bguarantee that\b",
        r"\bis my employer breaking the law\b",
    )
)

_INTERNAL_COMPARISON_LIMIT_TEMPLATE: Final[str] = (
    "This comparison includes {country_count} countries, but I can "
    "reliably compare up to {limit} countries in one response while "
    "providing at least one cited source for each country. Please "
    "choose up to {limit} countries and I will compare them."
)

_PERSONALISED_LEGAL_CAUTION: Final[str] = (
    "This is general employment-law information based on the "
    "validated L&E Global documents, not advice for a specific case. "
    "The appropriate action can depend on facts that are not available "
    "to this chatbot. For advice on a particular situation, please "
    "consult the relevant L&E Global member firm."
)


def _normalize(value: str) -> str:
    text = unicodedata.normalize("NFKD", value.casefold())

    text = "".join(
        character
        for character in text
        if not unicodedata.combining(character)
    )

    text = text.replace("’", "'")
    text = re.sub(r"\bwhitch\b", "which", text)
    text = re.sub(r"\bu\b", "you", text)
    text = re.sub(r"\bwhats\b", "what is", text)
    text = _NON_ALPHANUMERIC.sub(" ", text)
    text = text.replace("'", " ")
    text = re.sub(r"\bthat\s+s\b", "thats", text)
    text = re.sub(r"\bi\s+m\b", "im", text)
    return _WHITESPACE.sub(" ", text).strip()


def _tokens(text: str) -> set[str]:
    return set(text.split())


def _matches_any_pattern(
    text: str,
    patterns: Sequence[re.Pattern[str]],
) -> bool:
    return any(pattern.fullmatch(text) for pattern in patterns)


def _history_country_suggestion_code(
    history: Sequence[Any],
) -> str | None:
    """Return the country offered by the latest deterministic suggestion."""

    for value in _history_values(history, role="assistant", maximum=4):
        normalized = _normalize(value)

        if not normalized.startswith("did you mean "):
            continue

        country_codes = detect_mentioned_country_codes(value)

        if country_codes:
            return country_codes[0]

    return None


_COVERAGE_LIST_OFFER_MARKER: Final[str] = (
    "would you like to see the countries currently covered"
)


def _history_offered_coverage_list(
    history: Sequence[Any],
) -> bool:
    """
    Return whether the assistant's own last message offered to show
    the currently-covered countries (mission "ORDER 5C-GEO", sections
    3/18) - the exact same marker-phrase-in-history technique
    _history_country_suggestion_code already uses above, so a bare
    "Yes" is interpreted purely by re-reading the conversation's own
    text each turn, never a separate persisted flag that could be
    left stuck on for an unrelated later turn.
    """

    recent_assistant_values = _history_values(
        history, role="assistant", maximum=1
    )

    if not recent_assistant_values:
        return False

    normalized = _normalize(recent_assistant_values[0])

    return _COVERAGE_LIST_OFFER_MARKER in normalized


_AMBIGUOUS_CITY_OFFER_MARKER: Final[str] = (
    "could refer to more than one country i can help with"
)


def resolve_ambiguous_city_followup_question(
    question: str,
    history: Sequence[Any],
) -> str | None:
    """
    Resume an ambiguous-city clarification (corrective gate, section
    9: "Barcelona" -> ambiguity -> ask -> "User: Spain" -> reprendre
    la question avec ES) with a bare country-name reply.

    Same self-expiring, text-only pattern as _history_offered_
    coverage_list above: re-reads the assistant's own last message
    rather than a persisted flag, so this only ever fires immediately
    after that exact clarification and never lingers into an unrelated
    later turn. This module has no access to RAG/retrieval, so it
    never answers the resumed question itself - it only rebuilds the
    effective question text (the originally-ambiguous question, with
    the clarified country's name substituted for the city phrase that
    was ambiguous) for the caller to run through the normal pipeline
    exactly as if the user had asked it that way from the start.

    Returns None whenever there is nothing to resume - including when
    the current message names a country that was never actually
    offered, so a reply naming an unrelated country is never silently
    accepted as resolving someone else's ambiguity.
    """

    recent_assistant_values = _history_values(
        history, role="assistant", maximum=1
    )

    if not recent_assistant_values:
        return None

    last_answer = recent_assistant_values[0]

    if _AMBIGUOUS_CITY_OFFER_MARKER not in _normalize(last_answer):
        return None

    offered_codes = frozenset(
        detect_mentioned_country_codes(last_answer)
    )

    if not offered_codes:
        return None

    detected_codes = detect_mentioned_country_codes(question)

    if len(detected_codes) != 1 or detected_codes[0] not in offered_codes:
        return None

    previous_user_values = _history_values(
        history, role="user", maximum=1
    )

    if not previous_user_values:
        return None

    previous_question = previous_user_values[0]
    jurisdiction = resolve_jurisdiction(previous_question)

    if jurisdiction.matched_location is None:
        return None

    clarified_name = resolve_country_display_name(
        detected_codes[0]
    )

    resolved_question, substitution_count = re.subn(
        rf"(?<!\w){re.escape(jurisdiction.matched_location)}(?!\w)",
        clarified_name,
        previous_question,
        count=1,
        flags=re.IGNORECASE,
    )

    if substitution_count == 0:
        return None

    return resolved_question


def _history_values(
    history: Sequence[Any],
    *,
    role: str | None = None,
    maximum: int = 8,
) -> list[str]:
    values: list[str] = []

    for item in reversed(history):
        item_role = getattr(item, "role", None)
        item_content = getattr(item, "content", None)

        if item_role is None and isinstance(item, dict):
            item_role = item.get("role")
            item_content = item.get("content")

        if role is not None and item_role != role:
            continue

        if not isinstance(item_content, str):
            continue

        stripped = item_content.strip()

        if not stripped:
            continue

        values.append(stripped)

        if len(values) >= maximum:
            break

    return values


def _history_is_about_capabilities(
    history: Sequence[Any],
) -> bool:
    recent = _history_values(history, maximum=6)

    for value in recent:
        normalized = _normalize(value)

        if (
            "employment law" in normalized
            and "compare" in normalized
            and (
                "contact" in normalized
                or "member firm" in normalized
            )
        ):
            return True

        if any(
            pattern.fullmatch(normalized)
            for pattern in _CAPABILITY_PHRASES
        ):
            return True

    return False


def _history_expects_comparison_countries(
    history: Sequence[Any],
) -> bool:
    recent = _history_values(history, maximum=6)

    for value in recent:
        normalized = _normalize(value)

        if any(
            phrase in normalized
            for phrase in (
                "which countries would you like to compare",
                "which employment law topic would you like to compare",
                "name the countries",
            )
        ):
            return True

        if normalized in {
            "compare",
            "i want a comparison",
            "compare countries",
        }:
            return True

    return False


def _preserve_pending_state(
    conversation_state: Any | None,
) -> bool:
    """
    Preserve any existing legal conversation state during a
    non-destructive catalogue, topic or correction response.
    """

    return conversation_state is not None


def _safe_catalog(
    catalog_provider: CatalogProvider,
) -> LegalCatalogResponse | None:
    try:
        return catalog_provider()
    except Exception:
        return None


def _catalog_country_map(
    catalog: LegalCatalogResponse,
) -> dict[str, str]:
    return {
        country.country_code.upper(): country.country
        for country in catalog.countries
    }


def _display_name(
    country_code: str,
    catalog_names: dict[str, str],
) -> str:
    catalog_name = catalog_names.get(country_code.upper())

    if catalog_name:
        return catalog_name

    return resolve_country_display_name(country_code)


def _join_names(values: list[str]) -> str:
    if not values:
        return ""

    if len(values) == 1:
        return values[0]

    if len(values) == 2:
        return f"{values[0]} and {values[1]}"

    return f"{', '.join(values[:-1])}, and {values[-1]}"


def _country_catalogue_answer(
    catalog: LegalCatalogResponse,
) -> str:
    countries = sorted(
        (
            country.country.strip()
            or resolve_country_display_name(
                country.country_code
            )
        )
        for country in catalog.countries
    )

    return (
        f"The validated corpus currently covers {len(countries)} "
        f"countries: {_join_names(countries)}."
    )


def _topics_answer(
    country_name: str | None = None,
) -> str:
    topic_lines = "\n".join(
        f"- {topic}"
        for topic in CANONICAL_LEGAL_TOPICS
    )

    if country_name:
        introduction = (
            f"For {country_name}, you can ask about the following "
            "canonical employment-law topics covered by this chatbot:"
        )
    else:
        introduction = (
            "You can ask about or compare the following canonical "
            "employment-law topics:"
        )

    return (
        f"{introduction}\n{topic_lines}\n\n"
        "Choose one topic and one supported country for a country "
        "answer, or the same topic and at least two supported "
        "countries for a comparison."
    )


def _availability_answer(
    country_codes: Sequence[str],
    catalog: LegalCatalogResponse,
    *,
    comparison: bool,
) -> str:
    catalog_names = _catalog_country_map(catalog)
    active_codes = set(catalog_names)

    available = [
        _display_name(code, catalog_names)
        for code in country_codes
        if code in active_codes
    ]

    unavailable = [
        _display_name(code, catalog_names)
        for code in country_codes
        if code not in active_codes
    ]

    sections: list[str] = []

    if available:
        verb = "is" if len(available) == 1 else "are"

        sections.append(
            f"Yes. {_join_names(available)} {verb} currently "
            "available in the validated corpus."
        )

    if unavailable:
        verb = "is" if len(unavailable) == 1 else "are"

        validity_phrase = (
            "a valid country"
            if len(unavailable) == 1
            else "valid countries"
        )

        sections.append(
            f"{_join_names(unavailable)} "
            f"{verb} {validity_phrase}, but we do not currently have "
            f"validated corpus coverage for "
            f"{'it' if len(unavailable) == 1 else 'them'}."
        )

    if comparison and unavailable:
        sections.append(
            "I cannot produce a reliable comparison while one or "
            "more requested countries are unavailable. Please replace "
            "the unavailable country or ask me to list the countries "
            "currently supported."
        )
    elif unavailable:
        sections.append(
            "Would you like to see the countries currently covered?"
        )
    else:
        sections.append(
            "You can now specify the employment-law topic you want "
            "to explore."
        )

    return " ".join(sections)


def _is_topics_query(text: str) -> bool:
    tokens = _tokens(text)

    topic_words = {
        "topic",
        "topics",
        "subject",
        "subjects",
        "theme",
        "themes",
        "areas",
    }

    if not tokens & topic_words:
        return False

    concise_catalogue_words = {
        "and",
        "the",
        "what",
        "which",
        "about",
        "legal",
        "employment",
        "law",
        *topic_words,
    }

    if tokens <= concise_catalogue_words:
        return True

    explicit_catalogue_words = {
        "list",
        "show",
        "give",
        "all",
        "available",
        "support",
        "supported",
        "cover",
        "compare",
    }

    return bool(
        tokens & explicit_catalogue_words
        or (
            "you" in tokens
            and tokens & {"what", "which", "can", "do"}
        )
    )


def _is_contact_catalogue_query(text: str) -> bool:
    tokens = _tokens(text)

    if not (
        tokens
        & {
            "contact",
            "contacts",
            "firms",
            "lawyers",
        }
    ):
        return False

    return bool(
        tokens
        & {
            "all",
            "available",
            "list",
            "show",
            "which",
        }
    )


def _is_country_catalogue_query(text: str) -> bool:
    tokens = _tokens(text)

    country_words = {
        "country",
        "countries",
        "jurisdiction",
        "jurisdictions",
    }

    if not tokens & country_words:
        return False

    concise_catalogue_words = {
        "and",
        "the",
        "what",
        "which",
        "about",
        *country_words,
    }

    if tokens <= concise_catalogue_words:
        return True

    if "how many" in text:
        return True

    scope_words = {
        "support",
        "supported",
        "cover",
        "covered",
        "available",
        "have",
        "help",
        "list",
        "show",
    }

    return bool(
        tokens & scope_words
        or (
            tokens & {"what", "which", "where"}
            and "you" in tokens
        )
    )


_TARGETED_AVAILABILITY_PATTERNS: Final[tuple[re.Pattern[str], ...]] = tuple(
    re.compile(pattern)
    for pattern in (
        r"\bdo you support\b",
        r"\bdo you cover\b",
        # A plain substring check here would miss the natural
        # "is COUNTRY supported/available" word order (the country
        # name sits between the verb and the adjective) - mirrors
        # assistant_help.py's own _COUNTRIES_TARGETED_PATTERNS regex
        # for the same phrasing, generalized to also match the
        # country-before-verb ordering ("COUNTRY is supported").
        r"\bis\b.*\b(?:supported|available)\b",
        r"\bare\b.*\b(?:supported|available)\b",
        r"\bdo you have (?:data|information|documents)\b",
        r"\bcan you help with\b",
    )
)


def _is_targeted_country_availability(
    text: str,
    country_codes: Sequence[str],
) -> bool:
    if not country_codes:
        return False

    return any(
        pattern.search(text)
        for pattern in _TARGETED_AVAILABILITY_PATTERNS
    )


def _is_country_only(
    text: str,
    country_codes: Sequence[str],
) -> bool:
    working = text

    for country_code in country_codes:
        variants = [
            *get_country_name_variants(country_code),
            resolve_country_display_name(country_code),
        ]

        for variant in variants:
            normalized_variant = _normalize(variant)

            if normalized_variant:
                working = re.sub(
                    rf"(?<!\w){re.escape(normalized_variant)}(?!\w)",
                    " ",
                    working,
                )

    remaining = {
        token
        for token in working.split()
        if token not in {
            "the",
            "for",
            "and",
            "what",
            "about",
            "how",
            "please",
        }
    }

    return not remaining


@lru_cache(maxsize=1)
def _country_typo_candidates() -> tuple[
    tuple[str, str],
    ...
]:
    candidates: dict[tuple[str, str], None] = {}

    for country in pycountry.countries:
        code = country.alpha_2.upper()

        names = {
            country.name,
            getattr(country, "official_name", ""),
            *get_country_name_variants(code),
        }

        for name in names:
            if not name:
                continue

            normalized = _normalize(name)

            if normalized:
                candidates[(normalized, code)] = None

    return tuple(candidates)


def _country_typo_fragment(text: str) -> str | None:
    """Extract a country-like fragment from a natural availability query."""

    patterns = (
        re.compile(
            r"^(?:and )?(?:what|how) about (?P<country>[a-z][a-z ]{2,30})$"
        ),
        re.compile(
            r"^(?:no )?(?:i )?(?:mean|ask about|asked about) "
            r"(?P<country>[a-z][a-z ]{2,30})$"
        ),
        re.compile(
            r"^(?:do|can) you (?:have|support|cover) "
            r"(?:(?:data|information|documents|coverage) )?"
            r"(?:(?:for|on|about|in) )?"
            r"(?P<country>[a-z][a-z ]{2,30})$"
        ),
        re.compile(
            r"^(?:is|are) (?P<country>[a-z][a-z ]{2,30}) "
            r"(?:available|supported|covered)$"
        ),
    )

    for pattern in patterns:
        match = pattern.fullmatch(text)

        if match is not None:
            return match.group("country").strip()

    if len(text.split()) <= 3:
        candidate = " ".join(
            token
            for token in text.split()
            if token
            not in {
                "and",
                "what",
                "about",
                "please",
                "country",
                "the",
                "no",
                "i",
                "mean",
            }
        )

        return candidate or None

    return None


def _suggest_country_code(
    text: str,
) -> str | None:
    candidate_text = _country_typo_fragment(text)

    if not candidate_text:
        return None

    if len(candidate_text.split()) > 3:
        return None

    if not re.fullmatch(
        r"[a-z][a-z\s]{2,30}",
        candidate_text,
    ):
        return None

    best_by_code: dict[str, float] = {}

    for name, code in _country_typo_candidates():
        if abs(len(name) - len(candidate_text)) > 3:
            continue

        score = difflib.SequenceMatcher(
            None,
            candidate_text,
            name,
        ).ratio()

        best_by_code[code] = max(
            score,
            best_by_code.get(code, 0.0),
        )

    ranked = sorted(
        best_by_code.items(),
        key=lambda item: item[1],
        reverse=True,
    )

    if not ranked:
        return None

    best_code, best_score = ranked[0]
    second_score = ranked[1][1] if len(ranked) > 1 else 0.0

    minimum_score = (
        0.86
        if len(candidate_text) <= 4
        else 0.75
    )

    if best_score < minimum_score:
        return None

    if best_score - second_score < 0.07:
        return None

    return best_code


def resolve_conversation_meta(
    *,
    question: str,
    history: Sequence[Any],
    conversation_state: Any | None,
    catalog_provider: CatalogProvider,
) -> ConversationMetaResolution | None:
    """
    Resolve one explicit non-legal conversational intent.

    Returns None whenever the request should continue through normal
    request understanding and legal/contact/comparison routing.
    """

    text = _normalize(question)

    if not text:
        return None

    if text in _RESET_PHRASES:
        return ConversationMetaResolution(
            intent_type="reset",
            answer=(
                "The previous context has been cleared. What "
                "employment-law question would you like to ask?"
            ),
            preserve_conversation_state=False,
        )

    if text in _AFFIRMATION_PHRASES:
        suggested_code = _history_country_suggestion_code(history)

        if suggested_code is not None:
            catalog = _safe_catalog(catalog_provider)

            if catalog is None:
                answer = (
                    "The supported-country catalogue is temporarily "
                    "unavailable. Please try again shortly."
                )
            else:
                answer = _availability_answer(
                    [suggested_code],
                    catalog,
                    comparison=False,
                )

            return ConversationMetaResolution(
                intent_type="targeted_country_availability",
                answer=answer,
                preserve_conversation_state=False,
            )

        if _history_offered_coverage_list(history):
            catalog = _safe_catalog(catalog_provider)

            if catalog is None:
                answer = (
                    "The supported-country catalogue is temporarily "
                    "unavailable. Please try again shortly."
                )
            else:
                answer = _country_catalogue_answer(catalog)

            return ConversationMetaResolution(
                intent_type="coverage_list_followup",
                answer=answer,
                preserve_conversation_state=False,
            )

    if text in _GREETING_PHRASES:
        return ConversationMetaResolution(
            intent_type="greeting",
            answer=(
                "Hello! How can I help you with employment law today?"
            ),
        )

    if text in _WELLBEING_PHRASES:
        return ConversationMetaResolution(
            intent_type="wellbeing",
            answer=(
                "I’m doing well, thank you. I can help with "
                "employment-law information, country comparisons "
                "and L&E Global contacts."
            ),
        )

    if text in _FAREWELL_PHRASES:
        return ConversationMetaResolution(
            intent_type="farewell",
            answer="Goodbye. Have a nice day.",
        )

    if _matches_any_pattern(text, _FAREWELL_PATTERNS):
        return ConversationMetaResolution(
            intent_type="farewell",
            answer="You’re welcome. Goodbye. Have a nice day.",
        )

    if text in _GRATITUDE_PHRASES or _matches_any_pattern(
        text,
        _GRATITUDE_PATTERNS,
    ):
        return ConversationMetaResolution(
            intent_type="gratitude",
            answer=(
                "You’re welcome. Feel free to ask another "
                "employment-law question."
            ),
        )

    if _matches_any_pattern(text, _ACKNOWLEDGEMENT_PATTERNS):
        return ConversationMetaResolution(
            intent_type="acknowledgement",
            answer=(
                "No problem. I’m here when you need employment-law "
                "information."
            ),
        )

    if any(
        pattern.fullmatch(text)
        for pattern in _IDENTITY_PHRASES
    ):
        return ConversationMetaResolution(
            intent_type="assistant_identity",
            answer=(
                "I am the L&E Global employment law assistant. "
                "I answer employment-law questions from validated "
                "L&E Global documents, compare the same legal topic "
                "across supported countries and provide member-firm "
                "contact details. I provide legal information, not "
                "legal advice."
            ),
        )

    if (
        text in _CAPABILITY_FOLLOWUPS
        and _history_is_about_capabilities(history)
    ):
        return ConversationMetaResolution(
            intent_type="capability_followup",
            answer=(
                "I can also list the available countries and legal topics, "
                "explain a topic for one country, "
                "compare the same topic across several countries, "
                "summarise or simplify a previous answer, and provide "
                "the relevant L&E Global member-firm contact details."
            ),
        )

    if any(
        pattern.fullmatch(text)
        for pattern in _COMPARISON_CAPABILITY_PATTERNS
    ):
        return ConversationMetaResolution(
            intent_type="comparison_capabilities",
            answer=(
                "Yes. I can compare the same employment-law topic "
                "across two or more supported countries. Name the "
                "countries and the topic you want to compare."
            ),
        )

    if (
        any(
            pattern.fullmatch(text)
            for pattern in _CAPABILITY_PHRASES
        )
        and not detect_legal_topics(question)
    ):
        return ConversationMetaResolution(
            intent_type="assistant_capabilities",
            answer=(
                "You can ask me for employment-law information about "
                "a supported country, compare the same legal topic "
                "across multiple countries, request L&E Global "
                "member-firm contact details, or ask me to list the "
                "available countries and legal topics."
            ),
        )

    country_codes = tuple(
        detect_mentioned_country_codes(question)
    )

    if _is_topics_query(text):
        if country_codes:
            catalog = _safe_catalog(catalog_provider)

            if catalog is None:
                return ConversationMetaResolution(
                    intent_type="country_legal_topics",
                    answer=(
                        "The country and topic catalogue is temporarily "
                        "unavailable. Please try again shortly."
                    ),
                    preserve_conversation_state=(
                        _preserve_pending_state(
                            conversation_state
                        )
                    ),
                )

            catalog_names = _catalog_country_map(catalog)
            code = country_codes[0]
            country_name = _display_name(
                code,
                catalog_names,
            )

            if code not in catalog_names:
                return ConversationMetaResolution(
                    intent_type="country_legal_topics",
                    answer=(
                        f"{country_name} is a valid country, but it is "
                        "not currently covered by the validated corpus."
                    ),
                    preserve_conversation_state=False,
                )

            return ConversationMetaResolution(
                intent_type="country_legal_topics",
                answer=_topics_answer(country_name),
                preserve_conversation_state=(
                    _preserve_pending_state(
                        conversation_state
                    )
                ),
            )

        return ConversationMetaResolution(
            intent_type="supported_legal_topics",
            answer=_topics_answer(),
            preserve_conversation_state=(
                _preserve_pending_state(
                    conversation_state
                )
            ),
        )

    if _is_contact_catalogue_query(text):
        catalog = _safe_catalog(catalog_provider)

        if catalog is None:
            answer = (
                "The member-firm contact catalogue is temporarily "
                "unavailable. Please try again shortly."
            )
        else:
            country_names = sorted(
                country.country
                for country in catalog.countries
            )

            answer = (
                "Member-firm contact details are available for the "
                "following supported countries: "
                f"{_join_names(country_names)}. Name one country and "
                "I will provide its available contact details."
            )

        return ConversationMetaResolution(
            intent_type="contact_catalogue",
            answer=answer,
            preserve_conversation_state=False,
        )

    comparison_signal = bool(
        _tokens(text)
        & {
            "compare",
            "comparison",
            "versus",
            "vs",
        }
    )

    comparison_context = (
        comparison_signal
        or _history_expects_comparison_countries(
            history
        )
    )

    # A real legal comparison must not be mistaken for a request for
    # the supported-country catalogue merely because formatting
    # wording contains "country"/"jurisdiction" while the legal
    # question also contains a word such as "available".
    #
    # Example:
    # "Compare France and Germany on termination ... where the
    # available information supports it. Structure by country."
    country_catalogue_query = _is_country_catalogue_query(text)

    if (
        country_catalogue_query
        and len(country_codes) >= 2
        and comparison_context
        and detect_legal_topics(question)
    ):
        country_catalogue_query = False

    needs_country_catalog = bool(
        country_catalogue_query
        or _is_targeted_country_availability(
            text,
            country_codes,
        )
        or (
            country_codes
            and comparison_context
        )
        or country_codes
    )

    if needs_country_catalog:
        catalog = _safe_catalog(catalog_provider)

        if catalog is None:
            if (
                country_catalogue_query
                or _is_targeted_country_availability(
                    text,
                    country_codes,
                )
            ):
                return ConversationMetaResolution(
                    intent_type="supported_countries",
                    answer=(
                        "The supported-country catalogue is "
                        "temporarily unavailable. Please try again "
                        "shortly."
                    ),
                    preserve_conversation_state=False,
                )
        else:
            active_codes = {
                country.country_code.upper()
                for country in catalog.countries
            }

            if country_catalogue_query:
                return ConversationMetaResolution(
                    intent_type="supported_countries",
                    answer=_country_catalogue_answer(
                        catalog
                    ),
                    preserve_conversation_state=(
                        _preserve_pending_state(
                            conversation_state
                        )
                    ),
                )

            if _is_targeted_country_availability(
                text,
                country_codes,
            ):
                return ConversationMetaResolution(
                    intent_type=(
                        "targeted_country_availability"
                    ),
                    answer=_availability_answer(
                        country_codes,
                        catalog,
                        comparison=False,
                    ),
                    preserve_conversation_state=False,
                )

            unavailable_codes = [
                code
                for code in country_codes
                if code not in active_codes
            ]

            if (
                len(country_codes) >= 2
                and comparison_context
                and unavailable_codes
                and not detect_legal_topics(question)
            ):
                return ConversationMetaResolution(
                    intent_type="unsupported_comparison",
                    answer=_availability_answer(
                        country_codes,
                        catalog,
                        comparison=True,
                    ),
                    preserve_conversation_state=False,
                )

            if (
                len(country_codes) == 1
                and unavailable_codes
                and _is_country_only(
                    text,
                    country_codes,
                )
            ):
                return ConversationMetaResolution(
                    intent_type=(
                        "targeted_country_availability"
                    ),
                    answer=_availability_answer(
                        country_codes,
                        catalog,
                        comparison=False,
                    ),
                    preserve_conversation_state=False,
                )

    if not country_codes:
        # country_codes is already confirmed empty here, so calling
        # the full resolve_jurisdiction (which starts by re-deriving
        # explicit countries from scratch) would only repeat the
        # exact ~400-pattern scan this function's own country_codes
        # line already ran - a real, measured ~2x-per-call redundancy
        # found by adversarial review. Going straight to the city-only
        # primitives below answers the same question without it, and,
        # since neither primitive ever considers explicit countries at
        # all, an ambiguity found here is structurally guaranteed to
        # be city-caused - a real multi-country comparison (e.g.
        # "Compare France and Germany") is detected via country_codes
        # itself and never reaches this branch in the first place.
        city_codes, matched_location = resolve_city_country_codes(
            question
        )

        if len(city_codes) >= 2:
            candidate_names = sorted(
                resolve_country_display_name(code)
                for code in city_codes
            )

            return ConversationMetaResolution(
                intent_type="ambiguous_city_clarification",
                answer=(
                    f"{matched_location.title()} could "
                    "refer to more than one country I can help with: "
                    f"{_join_names(candidate_names)}. Which country "
                    "do you mean?"
                ),
                preserve_conversation_state=False,
            )

        if not city_codes:
            unresolved_locality = detect_unresolved_location_phrase(
                question
            )

            if unresolved_locality is not None:
                return ConversationMetaResolution(
                    intent_type="unknown_locality_clarification",
                    answer=(
                        f"Which country is {unresolved_locality} "
                        "in? I can help once I know the country."
                    ),
                    preserve_conversation_state=False,
                )

    if not country_codes and not detect_legal_topics(question):
        suggested_code = _suggest_country_code(text)

        if suggested_code is not None:
            catalog = _safe_catalog(catalog_provider)

            catalog_names = (
                _catalog_country_map(catalog)
                if catalog is not None
                else {}
            )

            display_name = _display_name(
                suggested_code,
                catalog_names,
            )

            if suggested_code in catalog_names:
                availability = (
                    f"{display_name} is currently available in the "
                    "validated corpus."
                )
            else:
                availability = (
                    f"{display_name} is not currently available in "
                    "the validated corpus."
                )

            return ConversationMetaResolution(
                intent_type="country_suggestion",
                answer=(
                    f"Did you mean {display_name}? {availability}"
                ),
                preserve_conversation_state=False,
            )

    return None


def requires_personalised_legal_caution(
    question: str,
) -> bool:
    text = _normalize(question)

    return any(
        pattern.search(text)
        for pattern in _PERSONALISED_LEGAL_PATTERNS
    )


def append_personalised_legal_caution(
    answer: str,
) -> str:
    if "not advice for a specific case" in answer.casefold():
        return answer

    return f"{answer.rstrip()}\n\n{_PERSONALISED_LEGAL_CAUTION}"


def build_comparison_country_limit_answer(
    actions: Sequence[Any],
    max_sources: int,
) -> str | None:
    country_count = max(
        (
            len(getattr(action, "country_codes", []))
            for action in actions
            if getattr(action, "type", None) == "comparison"
        ),
        default=0,
    )

    if country_count <= max_sources:
        return None

    return _INTERNAL_COMPARISON_LIMIT_TEMPLATE.format(
        country_count=country_count,
        limit=max_sources,
    )
