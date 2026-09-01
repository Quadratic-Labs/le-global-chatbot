"""
Deterministic conversation-transition engine.

Runs once, after RequestUnderstanding has already produced a validated
RequestUnderstandingResult, and before any retrieval or generation.
Never calls OpenAI itself - it only reconciles the classifier's own
result with the structured state of the last successful turn
(conversation_state), using nothing but current_message_delta's
explicit signal and simple structural rules. It exists specifically so
that a trivial, single-active-action country change ("Peru?") never
depends on the model correctly reconstructing the whole conversation's
topic from raw history text every single call - see the module
docstring in request_understanding.py and the 0.4.2 mission's defect
A/B/C findings for why that dependency was unreliable.

conversation_state is client-supplied, already schema-validated by
FastAPI at the request boundary, but its *content* is still untrusted:
this module only ever reads routing metadata out of it (action type,
country, subject, search concepts) - never legal content, never a
contact, and never a `resolved_question` string handed straight to
retrieval (see RULE 4 below, and the 0.4.2 mission's rectificatif C).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field, replace

from app.models.conversation_state import (
    MAX_COUNTRY_CODES_PER_ACTION,
    ConversationActionState,
    ConversationPendingClarification,
    ConversationSearchConcept,
    ConversationState,
)
from app.services.country_detection import (
    detect_mentioned_country_codes,
    get_country_name_variants,
    is_country_only_followup,
    resolve_country_display_name,
)
from app.services.legal_subject_scope import (
    CanonicalSearchConcept,
    canonicalize_legal_subject,
)
from app.services.country_detection import (
    resolve_city_country_codes,
)
from app.services.request_understanding import (
    CurrentMessageDelta,
    DeterministicHints,
    RequestUnderstandingAction,
    RequestUnderstandingResult,
)


_SAME_SUBJECT_FOLLOWUP_PATTERNS = tuple(
    re.compile(pattern)
    for pattern in (
        r"^(?:now )?(?:please )?(?:(?:give|show|tell)(?: me)? )?"
        r"(?:the )?same (?:information|info|details|rules|topic|subject|thing)"
        r"(?: for| in| about)?$",
        r"^(?:and )?(?:now )?(?:please )?do (?:the )?same"
        r"(?: for| in| about)?$",
        r"^(?:and )?(?:now )?(?:please )?(?:the )?same"
        r"(?: for| in| about)$",
    )
)


def _normalize_followup_text(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value.casefold())
    without_diacritics = "".join(
        character
        for character in decomposed
        if not unicodedata.combining(character)
    )
    return " ".join(re.sub(r"[^a-z0-9]+", " ", without_diacritics).split())


_EXPLICIT_SAME_SUBJECT_JURISDICTION_PATTERN = re.compile(
    r"^\s*(?:and\s+)?how\s+does\s+"
    r"(?:this|that|(?:the\s+)?same\s+"
    r"(?:issue|rule|topic|subject|thing))\s+"
    r"work\s+"
    r"(?:in\s+.+?|under\s+.+?\s+law)"
    r"\s*[?.!]*\s*$",
    re.IGNORECASE,
)


def _same_subject_country_followup(
    question: str,
) -> list[str] | None:
    """Recognize a one-country request that reuses the prior subject."""

    country_codes = detect_mentioned_country_codes(question)

    if len(country_codes) != 1:
        return None

    if _EXPLICIT_SAME_SUBJECT_JURISDICTION_PATTERN.fullmatch(
        question
    ):
        return country_codes

    working = question

    for variant in (
        *get_country_name_variants(country_codes[0]),
        resolve_country_display_name(country_codes[0]),
    ):
        working = re.sub(
            rf"(?<!\w){re.escape(variant)}(?!\w)",
            " ",
            working,
            flags=re.IGNORECASE,
        )

    normalized = _normalize_followup_text(working)

    if any(
        pattern.fullmatch(normalized)
        for pattern in _SAME_SUBJECT_FOLLOWUP_PATTERNS
    ):
        return country_codes

    return None



_LEGAL_CHALLENGE_FOLLOWUP_PATTERNS = tuple(
    re.compile(pattern)
    for pattern in (
        r"^(?:are|were) you sure$",
        r"^(?:is|was) that (?:really )?(?:true|correct|legal)$",
        r"^(?:really|seriously)$",
        r"^(?:can|could|would) you "
        r"(?:confirm|verify)(?: it| that| this)?$",
        r"^(?:please )?(?:confirm|verify)(?: it| that| this)?$",
        r"^just (?:say|answer) (?:yes|no)$",
        r"^(?:i am|i m) sure(?: that| this)? "
        r"(?:is )?legal(?: just)? (?:say|answer) yes$",
        r"^(?:i am|i m) sure(?: it| this| that)? "
        r"(?:is )?(?:true|correct)(?: just)? "
        r"(?:say|answer) yes$",
    )
)


def _is_legal_challenge_followup(
    question: str,
) -> bool:
    """
    Recognize a short confirmation/challenge that has no legal
    subject of its own and therefore must keep one established
    legal context rather than create a new contact/topic intent.
    """

    normalized = _normalize_followup_text(question)

    return any(
        pattern.fullmatch(normalized)
        for pattern in _LEGAL_CHALLENGE_FOLLOWUP_PATTERNS
    )


def _correct_delta_for_legal_challenge_followup(
    *,
    result: RequestUnderstandingResult,
    conversation_state: ConversationState,
    hints: DeterministicHints,
    current_question: str | None,
) -> RequestUnderstandingResult:
    """
    Preserve one established legal action for conversational
    challenges such as "Are you sure?" or "Just say yes".

    The current message contributes no new country/topic/action.
    Deterministic contact/comparison/country/topic signals always
    win, so a genuine new request is never swallowed here.
    """

    if (
        current_question is None
        or len(conversation_state.actions) != 1
        or conversation_state.actions[0].type
        != "legal_information"
        or hints.strong_contact_signal
        or hints.comparison_signal
        or hints.current_country_codes
        or hints.current_legal_topics
        or not _is_legal_challenge_followup(
            current_question
        )
    ):
        return result

    return result.model_copy(
        update={
            "is_follow_up": True,
            "current_message_delta": CurrentMessageDelta(
                explicit_action_types=[],
                explicit_country_codes=[],
                explicit_legal_topics=[],
                explicit_subject_text=None,
                context_operation="continue",
            ),
        }
    )


def _explicit_same_subject_country_codes(
    *,
    result: RequestUnderstandingResult,
    current_question: str | None,
) -> list[str] | None:
    """
    Resolve a one-country explicit jurisdiction replacement that
    reuses the previous legal subject.

    Country-name forms such as "same for Spain" are handled by the
    deterministic detector.

    Adjectival/demonym forms such as:
        "How does the same issue work under Spanish law?"
    may not be represented by detect_mentioned_country_codes itself.
    For that narrow explicit legal-scope shape, reuse the single
    country already resolved by RequestUnderstanding instead of
    maintaining a second demonym registry in this module.
    """

    if current_question is None:
        return None

    deterministic = _same_subject_country_followup(
        current_question
    )

    if deterministic is not None:
        return deterministic

    if not _EXPLICIT_SAME_SUBJECT_JURISDICTION_PATTERN.fullmatch(
        current_question
    ):
        return None

    delta = result.current_message_delta

    if delta is None:
        return None

    semantic_codes = [
        code.strip().upper()
        for code in delta.explicit_country_codes
        if code.strip()
    ]

    semantic_codes = list(
        dict.fromkeys(semantic_codes)
    )

    if len(semantic_codes) != 1:
        return None

    return semantic_codes


_CONTEXTUAL_CONTACT_REQUEST_PATTERN = re.compile(
    r"\b(?:give|send|provide|show)\s+(?:me|us)\b"
    r"|\b(?:can|could|would)\s+you\s+"
    r"(?:give|send|provide|show)\b"
    r"|\b(?:can|could|may)\s+i\s+(?:have|get)\b"
    r"|\bi\s+(?:need|want|would\s+like)\b"
    r"|\bwho\s+(?:can|should)\s+i\s+"
    r"(?:contact|reach|email|call)\b"
    r"|\bhow\s+(?:can|do|should)\s+i\s+"
    r"(?:contact|reach|email|call)\b",
    re.IGNORECASE,
)

_CONTEXTUAL_CONTACT_OBJECT_PATTERN = re.compile(
    r"\b(?:contacts?|contact\s+(?:details?|information|info)"
    r"|member\s+firms?|lawyers?)\b",
    re.IGNORECASE,
)

_CONTEXTUAL_COUNTRY_GROUP_PATTERN = re.compile(
    r"\b(?:both|these|those|all|the)\b"
    r".{0,30}\bcountr(?:y|ies)\b",
    re.IGNORECASE,
)

_CONTEXTUAL_PRONOUN_GROUP_PATTERN = re.compile(
    r"\b(?:both|all)\s+of\s+(?:them|these|those)\b",
    re.IGNORECASE,
)

_CONTEXTUAL_CONTACT_COUNT_PATTERN = re.compile(
    r"\b(?:both|all\s+(?:two|three|four|five|six))\s+contacts?\b"
    r"|\bcontacts?\s+(?:for|from)\s+"
    r"(?:both|all\s+(?:two|three|four|five|six))\b",
    re.IGNORECASE,
)

_CONTEXTUAL_NUMBER_WORDS = {
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
}


def resolve_contextual_multi_country_contact_codes(
    question: str,
    conversation_state: ConversationState | None,
) -> list[str] | None:
    """
    Resolve an explicit contextual contact request against the
    countries already carried by the structured conversation state.

    None means this is not a contextual multi-country contact request.
    [] means it is contextual, but the requested cardinality is
    ambiguous against the current state and must not be guessed.
    A non-empty list is the exact ordered country scope to use.
    """

    if conversation_state is None:
        return None

    ordered_codes: list[str] = []

    def add_codes(values: list[str]) -> None:
        for value in values:
            code = value.strip().upper()

            if code and code not in ordered_codes:
                ordered_codes.append(code)

    add_codes(list(conversation_state.ordered_country_codes))

    if len(ordered_codes) < 2:
        ordered_codes = []

        for action in conversation_state.actions:
            add_codes(list(action.country_codes))

    if len(ordered_codes) < 2:
        return None

    normalized = _normalize_followup_text(question)

    if not _CONTEXTUAL_CONTACT_REQUEST_PATTERN.search(normalized):
        return None

    if not _CONTEXTUAL_CONTACT_OBJECT_PATTERN.search(normalized):
        return None

    has_context_reference = bool(
        _CONTEXTUAL_COUNTRY_GROUP_PATTERN.search(normalized)
        or _CONTEXTUAL_PRONOUN_GROUP_PATTERN.search(normalized)
        or _CONTEXTUAL_CONTACT_COUNT_PATTERN.search(normalized)
    )

    if not has_context_reference:
        return None

    requested_count: int | None = None

    if re.search(r"\bboth\b", normalized):
        requested_count = 2
    else:
        for word, count in _CONTEXTUAL_NUMBER_WORDS.items():
            if re.search(
                rf"\b(?:all\s+)?{word}\b",
                normalized,
            ):
                requested_count = count
                break

    if (
        requested_count is not None
        and requested_count != len(ordered_codes)
    ):
        # Example: "both" with a three-country state.
        # Never arbitrarily select two countries.
        return []

    return ordered_codes


def _correct_result_for_contextual_multi_country_contact(
    *,
    result: RequestUnderstandingResult,
    conversation_state: ConversationState,
    current_question: str | None,
) -> RequestUnderstandingResult:
    """
    A request such as "give me the contacts for both countries"
    explicitly changes the active objective to Contact while reusing
    the already-known multi-country scope.

    The structured state, not the language model, is authoritative for
    which countries "both/all/these countries" refers to.
    """

    if current_question is None:
        return result

    country_codes = resolve_contextual_multi_country_contact_codes(
        current_question,
        conversation_state,
    )

    if country_codes is None or not country_codes:
        return result

    return RequestUnderstandingResult(
        status="resolved",
        actions=[
            RequestUnderstandingAction(
                type="contact",
                country_codes=country_codes,
            )
        ],
        is_follow_up=True,
        confidence=1.0,
        clarification_reason=None,
        current_message_delta=CurrentMessageDelta(
            explicit_action_types=["contact"],
            explicit_country_codes=country_codes,
            explicit_legal_topics=[],
            explicit_subject_text=None,
            context_operation="change_action",
        ),
    )


class ConversationTransitionError(RuntimeError):
    """
    Raised when the transition engine hits a genuinely unexpected
    internal error - never for one of the explicitly modeled, safe
    fallback cases (no conversation_state at all, a comparison that
    cannot be inherited below two countries, and so on), which already
    return a defined TransitionOutcome directly, with no exception at
    all. 0.4.2 hardening: silently trusting the classifier's own raw
    result after an unanticipated bug in this engine could mean acting
    on a country/subject this engine was specifically supposed to
    correct - an explicit, rare 502 is safer than that. The router
    converts this into a controlled HTTP 502 (see routers/chat.py);
    its own message is generic and never echoes the internal cause.
    """


@dataclass(frozen=True, slots=True)
class TransitionOutcome:
    """
    The deterministically-reconciled routing decision for one request.

    `final_status`/`final_actions`/`final_clarification_reason` are
    what the router must actually execute - they may differ from
    `result.status`/`result.actions` whenever this engine overrode the
    classifier (see `semantic_result_overridden`). `pending_clarification`
    is set only when `final_status == "clarification"` and the
    clarification is contextual (multi-action selection or a country
    reference ambiguous between two or more candidates from
    conversation_state) - never for the classifier's own generic
    clarifications, which carry no candidates to offer.
    """

    final_status: str
    final_actions: list[RequestUnderstandingAction]
    final_clarification_reason: str | None
    pending_clarification: ConversationPendingClarification | None
    semantic_result_overridden: bool
    semantic_override_reason: str | None
    context_inheritance_applied: bool
    inherited_action_type: str | None
    inherited_country_replaced: bool
    contextual_clarification_answer: str | None = None
    # Jurisdiction-neutral-subject observability - see chat_metrics.py.
    subject_canonicalization_applied: bool = False
    subject_scope_removed_country_codes: list[str] = field(
        default_factory=list
    )
    inherited_subject_canonicalized: bool = False


def _build_resolved_question(
    *,
    action_type: str,
    country_codes: list[str],
    subject_text: str,
) -> str:
    """
    Deterministically build a self-contained question from a
    country/subject pair - never a fragile find-and-replace of a
    country name inside a previous sentence (RULE 4).
    """

    display_names = [
        resolve_country_display_name(code) for code in country_codes
    ]

    if action_type == "comparison":
        countries = " and ".join(display_names)

        return (
            f"Compare {countries} regarding this employment "
            f"law issue: {subject_text}."
        )

    country = (
        display_names[0] if display_names else "the requested country"
    )

    return (
        f"For {country}, answer this employment law question: "
        f"{subject_text}."
    )


@dataclass(frozen=True, slots=True)
class _InheritedAction:
    action: RequestUnderstandingAction
    subject_became_empty: bool
    canonicalization_applied: bool
    removed_country_codes: list[str]


def _inherit_action(
    previous_action: ConversationActionState,
    *,
    country_codes: list[str],
) -> _InheritedAction:
    """
    Rebuild a full RequestUnderstandingAction from one prior
    conversation_state action, keeping its type/subject/topics intact
    and using only the given (possibly new) country_codes.

    0.4.2 jurisdiction-neutral-subject hardening: a bare country
    follow-up ("Peru?") only ever changes country_codes here - never
    subject_text/search_concepts themselves - so if the OLD country
    was ever baked into either (RequestUnderstanding's own output is
    canonicalized at the source, but a state built before this
    hardening existed, or a replayed/tampered client state, is not
    trusted either), it would otherwise silently survive into the NEW
    country's resolved_question, retrieval query, and insufficient/
    partial message. Canonicalizing here against the union of the
    previous action's own country_codes and the new ones is a second,
    defensive pass - idempotent when the source was already clean.

    `subject_became_empty` (always False for a contact action, which
    never carries a subject) tells the caller the inherited subject
    carried no transferable legal content at all once its geographic
    scope was stripped out (e.g. a legacy or tampered state whose
    entire subject_text was just the old country's name) - the caller
    must turn this into a targeted clarification, never silently fall
    back to the action's broad legal_topics as if that were the
    subject the user actually asked about (RULE: never a silent
    general search - see the mission's Phase 13).
    """

    canonicalized = canonicalize_legal_subject(
        subject_text=previous_action.subject_text,
        search_concepts=previous_action.search_concepts,
        scoped_country_codes=previous_action.country_codes,
        additional_country_codes=country_codes,
    )

    subject_text = canonicalized.subject_text
    search_concepts = canonicalized.search_concepts

    # A concept group whose every term was purely geographic (e.g.
    # the old country's own name, no other legal content) can be
    # dropped entirely during canonicalization even though
    # subject_text itself survives untouched. A precise subject must
    # never be broadened just because its concepts disappeared here -
    # rebuild a single concept group directly from the surviving
    # canonical subject_text (the same precise wording already used
    # for retrieval/generation, never an invented synonym), keeping
    # subject_specificity/evidence_mode exactly as inherited.
    if not search_concepts and subject_text:
        search_concepts = [CanonicalSearchConcept(terms=[subject_text])]

    resolved_question = (
        _build_resolved_question(
            action_type=previous_action.type,
            country_codes=country_codes,
            subject_text=subject_text,
        )
        if subject_text and previous_action.type != "contact"
        else None
    )

    action = RequestUnderstandingAction(
        type=previous_action.type,
        country_codes=country_codes,
        legal_topics=list(previous_action.legal_topics),
        # A prior turn can resolve with legal_topics == [] when the
        # model conveyed the subject only via topic_text - a field
        # ConversationActionState does not persist on its own,
        # folding it into subject_text instead. Falling back to the
        # (guaranteed non-empty here, since subject_became_empty is
        # handled by the caller before this action is ever used)
        # canonicalized subject_text keeps this inherited action
        # complete per RequestUnderstandingResult's own resolved
        # legal_information/comparison rule, without changing
        # anything when legal_topics is already present.
        topic_text=(
            subject_text if not previous_action.legal_topics else None
        ),
        resolved_question=resolved_question,
        subject_text=subject_text,
        search_concepts=[
            {"terms": list(concept.terms)}
            for concept in search_concepts
        ],
        subject_specificity=previous_action.subject_specificity,
        evidence_mode=previous_action.evidence_mode,
    )

    return _InheritedAction(
        action=action,
        subject_became_empty=canonicalized.subject_became_empty,
        canonicalization_applied=canonicalized.changed,
        removed_country_codes=canonicalized.removed_country_codes,
    )


def _missing_topic_clarification(
    *,
    action_type: str,
    country_codes: list[str],
) -> TransitionOutcome:
    """
    The inherited subject carried no transferable legal content once
    its geographic scope was stripped - ask for the topic explicitly,
    naming the new country, rather than silently answering the whole
    broad topic area as if that were what was actually asked (see
    _inherit_action's own docstring and the mission's Phase 13).
    Never reaches OpenSearch or generation - this returns directly.
    """

    country_phrase = (
        " and ".join(
            resolve_country_display_name(code)
            for code in country_codes
        )
        if country_codes
        else "that country"
    )

    contextual_answer = (
        "What employment law topic would you like information "
        f"about for {country_phrase}?"
    )

    return TransitionOutcome(
        final_status="clarification",
        final_actions=[],
        final_clarification_reason="missing_topic",
        pending_clarification=ConversationPendingClarification(
            reason="missing_topic",
            candidate_action_types=[action_type],
            candidate_country_codes=country_codes,
        ),
        semantic_result_overridden=True,
        semantic_override_reason="inherited_subject_became_empty",
        context_inheritance_applied=False,
        inherited_action_type=None,
        inherited_country_replaced=False,
        contextual_clarification_answer=contextual_answer,
    )


def _merge_country_codes(
    existing: list[str],
    additional: list[str],
) -> list[str]:
    merged = list(existing)

    for code in additional:
        if code not in merged:
            merged.append(code)

    return merged[:MAX_COUNTRY_CODES_PER_ACTION]


def _passthrough(
    result: RequestUnderstandingResult,
) -> TransitionOutcome:
    """No conversation_state, or nothing this engine needs to do -
    trust the classifier's own result exactly as given."""

    pending_clarification = None

    if (
        result.status == "clarification"
        and result.clarification_reason == "missing_country"
        and len(result.actions) == 1
        and result.actions[0].type == "contact"
    ):
        pending_clarification = ConversationPendingClarification(
            reason="missing_country",
            candidate_action_types=["contact"],
            candidate_country_codes=[],
        )
        contact_missing_country_pending = True
        del contact_missing_country_pending

    return TransitionOutcome(
        final_status=result.status,
        final_actions=list(result.actions),
        final_clarification_reason=result.clarification_reason,
        pending_clarification=pending_clarification,
        semantic_result_overridden=False,
        semantic_override_reason=None,
        context_inheritance_applied=False,
        inherited_action_type=None,
        inherited_country_replaced=False,
    )


def _describe_action_for_clarification(
    action: ConversationActionState,
) -> str:
    """A short noun phrase naming one candidate action, for a
    contextual clarification question - never the action's full
    resolved_question, never legal content."""

    if action.type == "contact":
        return "the local member firm contact"

    subject = action.subject_text or "that information"

    return f"the {subject}"


def _join_with_final_connector(
    labels: list[str],
    *,
    final_connector: str,
) -> str:
    if len(labels) == 1:
        return labels[0]

    if len(labels) == 2:
        return f"{labels[0]}, {labels[1]}, {final_connector}"

    return ", ".join(labels[:-1]) + f", {labels[-1]}, {final_connector}"


def _multi_action_clarification(
    conversation_state: ConversationState,
    *,
    reason: str,
    new_country_codes: list[str] | None = None,
) -> TransitionOutcome:
    """
    RULE 5 / RULE 9 safety net: conversation_state names more than one
    action and the current message did not clearly select one - ask,
    listing the real candidates, never guessing.
    """

    candidate_types = [
        action.type for action in conversation_state.actions
    ]
    candidate_country_codes = sorted(
        {
            code
            for action in conversation_state.actions
            for code in action.country_codes
        }
    )

    labels = [
        _describe_action_for_clarification(action)
        for action in conversation_state.actions
    ]

    if new_country_codes and labels:
        country_phrase = " and ".join(
            resolve_country_display_name(code)
            for code in new_country_codes
        )
        labels[0] = f"{labels[0]} for {country_phrase}"

    final_connector = (
        "or both" if len(labels) == 2 else "or all of them"
    )

    contextual_answer = (
        "Would you like "
        + _join_with_final_connector(
            labels, final_connector=final_connector
        )
        + "?"
    )

    return TransitionOutcome(
        final_status="clarification",
        final_actions=[],
        final_clarification_reason=reason,
        pending_clarification=ConversationPendingClarification(
            reason=reason,
            candidate_action_types=candidate_types,
            candidate_country_codes=candidate_country_codes,
        ),
        semantic_result_overridden=True,
        semantic_override_reason=(
            "multiple_prior_actions_without_explicit_selection"
        ),
        context_inheritance_applied=False,
        inherited_action_type=None,
        inherited_country_replaced=False,
        contextual_clarification_answer=contextual_answer,
    )


def _country_reference_clarification(
    conversation_state: ConversationState,
    *,
    action_type: str,
) -> TransitionOutcome:
    """
    RULE 9 / defect F: a single active action whose own conversation_state
    entry names more than one candidate country (e.g. right after a
    comparison) and the message picks the action but not which country -
    "Do you mean the contact in Peru or in Spain?", never the generic
    missing_country wording.
    """

    candidate_country_codes = (
        list(conversation_state.ordered_country_codes)
        if conversation_state.ordered_country_codes
        else sorted(
            {
                code
                for action in conversation_state.actions
                for code in action.country_codes
            }
        )
    )

    action_label = (
        "contact" if action_type == "contact" else "information"
    )

    countries_phrase = " or ".join(
        f"in {resolve_country_display_name(code)}"
        for code in candidate_country_codes
    )

    contextual_answer = (
        f"Do you mean the {action_label} {countries_phrase}?"
    )

    return TransitionOutcome(
        final_status="clarification",
        final_actions=[],
        final_clarification_reason="ambiguous_reference",
        pending_clarification=ConversationPendingClarification(
            reason="ambiguous_reference",
            candidate_action_types=[action_type],
            candidate_country_codes=candidate_country_codes,
        ),
        semantic_result_overridden=True,
        semantic_override_reason="ambiguous_country_reference",
        context_inheritance_applied=False,
        inherited_action_type=None,
        inherited_country_replaced=False,
        contextual_clarification_answer=contextual_answer,
    )


def _canonicalize_conversation_state(
    conversation_state: ConversationState,
) -> ConversationState:
    """
    Canonicalize every action's subject_text/search_concepts against
    its own country_codes, immediately after Pydantic validation and
    before any of this module's own logic reads them.

    conversation_state is client-supplied - never trusted for its
    *content* even though FastAPI already validated its *shape* (see
    the module docstring). Without this upfront pass, a jurisdiction
    baked into subject_text by a state built before this hardening
    existed (or a replayed/tampered client state) would still leak
    into a clarification's own descriptive phrase (see
    _describe_action_for_clarification) even on paths that never
    reach _inherit_action's own defensive canonicalization at all.

    Deliberately never collapses a subject to None here, even when
    canonicalize_legal_subject reports subject_became_empty: doing so
    would silently discard that signal before _inherit_action - the
    only place that actually decides whether this action is being
    inherited right now - gets a chance to turn it into a targeted
    missing_topic clarification (Phase 13). Left as the original,
    still-contaminated text in that one rare case instead, so the
    later, authoritative canonicalization pass is what detects and
    acts on the empty result, never this earlier defensive one.
    """

    canonicalized_actions: list[ConversationActionState] = []
    any_changed = False

    for action in conversation_state.actions:
        canonicalized = canonicalize_legal_subject(
            subject_text=action.subject_text,
            search_concepts=action.search_concepts,
            scoped_country_codes=action.country_codes,
        )

        if not canonicalized.changed or canonicalized.subject_became_empty:
            canonicalized_actions.append(action)
            continue

        any_changed = True
        canonicalized_actions.append(
            action.model_copy(
                update={
                    "subject_text": canonicalized.subject_text,
                    "search_concepts": [
                        ConversationSearchConcept(terms=concept.terms)
                        for concept in canonicalized.search_concepts
                    ],
                }
            )
        )

    if not any_changed:
        return conversation_state

    return conversation_state.model_copy(
        update={"actions": canonicalized_actions}
    )


def _correct_delta_for_country_only_followup(
    *,
    result: RequestUnderstandingResult,
    conversation_state: ConversationState,
    current_question: str | None,
) -> RequestUnderstandingResult:
    """
    A bare country-only follow-up ("Peru?", "What about Peru?") must
    always be treated as a pure country replacement against a single
    prior action - even when RequestUnderstanding's own delta claims
    an explicit new subject/action for it, a real, observed model
    behavior when the prior action's own subject was itself
    uninformative (e.g. just the old country's name - see
    legal_subject_scope.py). That claimed subject is not actually
    present in the user's own current message, so it must never be
    trusted over what the message deterministically is (mission
    "CORRECTION FINALE CIBLEE 0.4.2", Correction 1).

    Only ever corrects the delta - never result.actions, which
    _apply_transition's own single-action branch does not consult for
    this decision anyway (it is fully replaced by _inherit_action's
    own output). Never touches a multi-action state (RULE 5/9's own
    disambiguation already handles that case untouched) or a message
    that also carries its own legal-subject content, which
    is_country_only_followup already excludes.
    """

    if (
        current_question is None
        or len(conversation_state.actions) != 1
    ):
        return result

    country_codes = is_country_only_followup(current_question)

    if country_codes is None:
        return result

    return result.model_copy(
        update={
            "current_message_delta": CurrentMessageDelta(
                explicit_action_types=[],
                explicit_country_codes=country_codes,
                explicit_legal_topics=[],
                explicit_subject_text=None,
                context_operation="replace_country",
            )
        }
    )


def _correct_delta_for_same_subject_country_followup(
    *,
    result: RequestUnderstandingResult,
    conversation_state: ConversationState,
    hints: DeterministicHints,
    current_question: str | None,
) -> RequestUnderstandingResult:
    """Make "same information for X" reuse one prior subject locally."""

    if (
        current_question is None
        or len(conversation_state.actions) != 1
        or conversation_state.actions[0].type == "comparison"
        or hints.strong_contact_signal
        or hints.current_legal_topics
    ):
        return result

    country_codes = _explicit_same_subject_country_codes(
        result=result,
        current_question=current_question,
    )

    if country_codes is None:
        return result

    return result.model_copy(
        update={
            "current_message_delta": CurrentMessageDelta(
                explicit_action_types=[],
                explicit_country_codes=country_codes,
                explicit_legal_topics=[],
                explicit_subject_text=None,
                context_operation="replace_country",
            )
        }
    )



def _correct_subject_detail_followup(
    *,
    result: RequestUnderstandingResult,
    conversation_state: ConversationState,
    hints: DeterministicHints,
    current_question: str | None,
) -> RequestUnderstandingResult:
    """Resume one server-requested subject clarification safely."""

    pending = conversation_state.pending_clarification

    if (
        pending is None
        or pending.reason != "subject_detail"
        or current_question is None
        or result.status != "clarification"
        or len(conversation_state.actions) != 1
        or hints.strong_contact_signal
        or hints.comparison_signal
    ):
        return result

    previous = conversation_state.actions[0]

    if previous.type != "legal_information":
        return result

    if (
        pending.candidate_action_types
        != ["legal_information"]
        or set(pending.candidate_country_codes)
        != set(previous.country_codes)
    ):
        return result

    delta = result.current_message_delta

    # Never steal an explicit jurisdiction/action switch.
    if delta.explicit_country_codes:
        return result

    if (
        delta.explicit_action_types
        and set(delta.explicit_action_types)
        != {"legal_information"}
    ):
        return result

    subject = (
        delta.explicit_subject_text
        or current_question.strip()
    )

    words = re.findall(
        r"[a-z0-9]+",
        subject.casefold(),
    )

    # Still vague -> continue clarifying instead of guessing.
    if len(words) < 4:
        return result

    subject = subject[:300]

    search_concepts = (
        [
            ConversationSearchConcept(
                terms=list(concept.terms)
            )
            for concept in previous.search_concepts
        ]
        if previous.search_concepts
        else [
            ConversationSearchConcept(
                terms=[subject]
            )
        ]
    )

    action = RequestUnderstandingAction(
        type="legal_information",
        country_codes=list(previous.country_codes),
        legal_topics=list(previous.legal_topics),
        topic_text=(
            None
            if previous.legal_topics
            else subject[:200]
        ),
        resolved_question=_build_resolved_question(
            action_type="legal_information",
            country_codes=list(previous.country_codes),
            subject_text=subject,
        ),
        subject_text=subject,
        search_concepts=search_concepts,
        subject_specificity="specific",
        evidence_mode=previous.evidence_mode,
    )

    return RequestUnderstandingResult(
        status="resolved",
        actions=[action],
        is_follow_up=True,
        confidence=result.confidence,
        clarification_reason=None,
        current_message_delta=CurrentMessageDelta(
            explicit_action_types=[
                "legal_information"
            ],
            explicit_country_codes=[],
            explicit_legal_topics=list(previous.legal_topics),
            explicit_subject_text=subject,
            context_operation="change_subject",
        ),
    )

_INCIDENTAL_TRAVEL_PATTERN = re.compile(
    r"\b(?:"
    r"booked|booking|book|"
    r"trip|travel|travelling|traveling|travelled|traveled|"
    r"holiday|vacation|"
    r"visit|visiting|visited|"
    r"flight|flying|"
    r"going|go"
    r")\b",
    re.IGNORECASE,
)

_VACATION_CONTEXT_PATTERN = re.compile(
    r"\b(?:"
    r"vacation|holiday|annual\s+leave|paid\s+leave|"
    r"leave\s+request|trip|travel"
    r")\b",
    re.IGNORECASE,
)

_EXPLICIT_LEGAL_SCOPE_PATTERN = re.compile(
    r"\b(?:"
    r"under\s+.+?\s+law|"
    r"(?:employment|labour|labor)\s+law|"
    r"jurisdiction|legal\s+rules?|"
    r"work(?:ing)?\s+in|"
    r"employ(?:ed|ment)\s+in"
    r")\b",
    re.IGNORECASE,
)


def _correct_delta_for_incidental_travel_followup(
    *,
    result: RequestUnderstandingResult,
    conversation_state: ConversationState,
    hints: DeterministicHints,
    current_question: str | None,
) -> RequestUnderstandingResult:
    """
    Keep the active employment-law jurisdiction when a country is
    merely a travel destination in an existing vacation/leave issue.
    """

    if (
        current_question is None
        or len(conversation_state.actions) != 1
        or conversation_state.actions[0].type != "legal_information"
        or hints.strong_contact_signal
    ):
        return result

    action = conversation_state.actions[0]

    prior_parts = [
        action.subject_text or "",
        *action.legal_topics,
    ]

    for concept in action.search_concepts:
        prior_parts.extend(concept.terms)

    prior_scope = " ".join(prior_parts)

    if not _VACATION_CONTEXT_PATTERN.search(prior_scope):
        return result

    if not _INCIDENTAL_TRAVEL_PATTERN.search(current_question):
        return result

    # Real jurisdiction instructions always win.
    if _EXPLICIT_LEGAL_SCOPE_PATTERN.search(current_question):
        return result

    country_codes = detect_mentioned_country_codes(
        current_question
    )

    # Several newly mentioned countries are genuinely ambiguous.
    if len(country_codes) > 1:
        return result

    return result.model_copy(
        update={
            "current_message_delta": CurrentMessageDelta(
                explicit_action_types=[],
                explicit_country_codes=[],
                explicit_legal_topics=[],
                explicit_subject_text=None,
                context_operation="continue",
            )
        }
    )


def _correct_delta_for_pressure_challenge_followup(
    *,
    result: RequestUnderstandingResult,
    conversation_state: ConversationState,
    hints: DeterministicHints,
    current_question: str | None,
) -> RequestUnderstandingResult:
    """
    Preserve one active legal question when the user merely challenges,
    pressures or asks the assistant to rubber-stamp the previous
    proposition.

    Examples:
      "I'm sure. Just say yes."
      "I'm sure this is legal. Just say yes."
      "Trust me, just say yes."
      "I know it's true. Confirm it."

    These messages add no legal subject or jurisdiction. They must not
    create a subject-detail clarification, and they must never weaken
    grounding or false-premise resistance.
    """

    if (
        current_question is None
        or len(conversation_state.actions) != 1
        or conversation_state.actions[0].type != "legal_information"
        or conversation_state.pending_clarification is not None
        or hints.strong_contact_signal
        or hints.comparison_signal
        or hints.current_legal_topics
        or detect_mentioned_country_codes(current_question)
    ):
        return result

    normalized = re.sub(
        r"[^a-z0-9]+",
        " ",
        current_question.casefold(),
    ).strip()

    patterns = (
        (
            r"(?:i m|im|i am) sure"
            r"(?: (?:this|that|it) is "
            r"(?:legal|true|correct))?"
            r"(?: just)?"
            r"(?: say yes| confirm(?: it)?)?"
        ),
        r"just say yes",
        r"trust me(?: just)? say yes",
        (
            r"i know"
            r"(?: it s| its| it is)?"
            r" (?:legal|true|correct)"
            r"(?: just)?"
            r"(?: say yes| confirm(?: it)?)?"
        ),
    )

    if not any(
        re.fullmatch(pattern, normalized)
        for pattern in patterns
    ):
        return result

    return result.model_copy(
        update={
            "current_message_delta": CurrentMessageDelta(
                explicit_action_types=[],
                explicit_country_codes=[],
                explicit_legal_topics=[],
                explicit_subject_text=None,
                context_operation="continue",
            )
        }
    )



def _resolve_contact_missing_country_pending(
    *,
    conversation_state: ConversationState,
    hints: DeterministicHints,
    current_question: str | None,
) -> TransitionOutcome | None:
    """
    Consume a country/city reply to an earlier contact missing-country
    clarification. A city is accepted only when it maps to one country.
    """

    pending = conversation_state.pending_clarification

    if (
        pending is None
        or pending.reason != "missing_country"
        or len(pending.candidate_action_types) != 1
        or pending.candidate_action_types[0] != "contact"
    ):
        return None

    if hints.current_legal_topics or hints.comparison_signal:
        return None

    candidate_codes = list(
        dict.fromkeys(
            [
                *hints.current_country_codes,
                *hints.current_unavailable_country_codes,
            ]
        )
    )

    if (
        not candidate_codes
        and current_question is not None
    ):
        city_codes, _ = resolve_city_country_codes(current_question)

        if len(city_codes) == 1:
            candidate_codes = sorted(city_codes)

    if len(candidate_codes) != 1:
        return None

    return TransitionOutcome(
        final_status="resolved",
        final_actions=[
            RequestUnderstandingAction(
                type="contact",
                country_codes=candidate_codes,
            )
        ],
        final_clarification_reason=None,
        pending_clarification=None,
        semantic_result_overridden=True,
        semantic_override_reason=(
            "contact_missing_country_resolved"
        ),
        context_inheritance_applied=False,
        inherited_action_type="contact",
        inherited_country_replaced=True,
    )


def _correct_delta_for_unavailable_legal_country_switch(
    *,
    result: RequestUnderstandingResult,
    conversation_state: ConversationState,
    hints: DeterministicHints,
) -> RequestUnderstandingResult:
    """
    Preserve an explicit legal jurisdiction switch when the newly
    requested country is recognized but is outside the validated
    document corpus.

    RequestUnderstanding is intentionally restricted to supported
    country codes, so for e.g. "What is the notice period in
    Tunisia?" it may correctly emit context_operation="replace_country"
    while either leaving explicit_country_codes empty or incorrectly
    retaining a previously supported country code. The deterministic
    current-message unavailable-country detection is authoritative
    for this narrow legal jurisdiction-switch case.

    Without this correction, conversation transition interprets that
    empty list as "keep the previous country", which can silently turn
    a Tunisia question into an Italy answer.

    The semantic status itself is deliberately not authoritative
    here. A real legal jurisdiction switch to a country outside the
    corpus may be classified as resolved, clarification or
    unsupported. What is authoritative is the deterministic
    combination below.

    This correction is deliberately narrow:
    - the current message must deterministically name exactly one
      unavailable country and a supported legal topic;
    - semantic context_operation may be independent, ambiguous,
      replace_country, change_subject or similar because this field is
      probabilistic; only an already-normalized "continue" or an
      additive "add_country" is protected from replacement;
    - exactly one unavailable current country must be known;
    - the current message must contain a supported legal topic;
    - contact/comparison requests are excluded.

    It runs after incidental-travel correction, so a travel
    destination such as "I will go to Tunisia" remains a continuation
    of the existing legal jurisdiction rather than becoming one.
    """

    delta = result.current_message_delta

    if (
        # "continue" is authoritative here: the incidental-travel
        # correction runs before this helper and deliberately changes
        # destination-only messages such as "I will go to Tunisia"
        # into a continuation of the existing jurisdiction.
        delta.context_operation in {"continue", "add_country"}
        or len(hints.current_unavailable_country_codes) != 1
        or not hints.current_legal_topics
        or hints.strong_contact_signal
        or hints.comparison_signal
        or (
            delta.explicit_action_types
            and set(delta.explicit_action_types)
            != {"legal_information"}
        )
    ):
        return result

    # _apply_transition deliberately uses two different mechanics:
    #
    # - with ONE previous legal action, a country continuation is
    #   inherited only when the delta contains no explicit new
    #   action/subject;
    #
    # - with SEVERAL previous actions, an explicit action type is
    #   required to select which previous action is being continued.
    #
    # Normalize accordingly instead of forcing the same delta shape
    # on both paths.
    single_legal_action = (
        len(conversation_state.actions) == 1
        and conversation_state.actions[0].type
        == "legal_information"
    )

    if single_legal_action:
        normalized_action_types = []
        normalized_legal_topics = []
        normalized_subject_text = None
    else:
        normalized_action_types = [
            "legal_information"
        ]
        normalized_legal_topics = list(
            delta.explicit_legal_topics
        )
        normalized_subject_text = (
            delta.explicit_subject_text
        )

    return result.model_copy(
        update={
            "current_message_delta": delta.model_copy(
                update={
                    "explicit_action_types": (
                        normalized_action_types
                    ),
                    "explicit_country_codes": list(
                        hints.current_unavailable_country_codes
                    ),
                    "explicit_legal_topics": (
                        normalized_legal_topics
                    ),
                    "explicit_subject_text": (
                        normalized_subject_text
                    ),
                    "context_operation": "replace_country",
                }
            )
        }
    )


def apply_conversation_transition(
    *,
    result: RequestUnderstandingResult,
    conversation_state: ConversationState | None,
    hints: DeterministicHints,
    current_question: str | None = None,
) -> TransitionOutcome:
    """
    Reconcile one validated RequestUnderstandingResult with the
    structured state of the last successful turn.

    Only ever engages for status="resolved" results carrying zero
    explicit new subject/action/topic and exactly one country-level
    change against a single focused prior action (RULE 3), or a
    structurally unresolved multi-action prior state (RULE 5/9) - every
    other case (a genuinely new subject, action, or comparison; no
    prior state at all; an independent message) passes the classifier's
    own result through untouched, since RequestUnderstanding is already
    the primary router and this engine only ever corrects the one
    narrow class of transition it cannot reliably self-correct call to
    call (see the module docstring).

    `current_question`, when given, additionally lets this engine
    deterministically override a spurious explicit subject/action the
    classifier claimed for what the raw message actually shows is a
    bare country-only follow-up - see
    _correct_delta_for_country_only_followup.
    """

    if conversation_state is None:
        return _passthrough(result)

    canonicalized_state = _canonicalize_conversation_state(
        conversation_state
    )

    contact_pending_outcome = (
        _resolve_contact_missing_country_pending(
            conversation_state=canonicalized_state,
            hints=hints,
            current_question=current_question,
        )
    )

    if contact_pending_outcome is not None:
        return contact_pending_outcome

    result = _correct_delta_for_country_only_followup(
        result=result,
        conversation_state=canonicalized_state,
        current_question=current_question,
    )
    result = _correct_delta_for_same_subject_country_followup(
        result=result,
        conversation_state=canonicalized_state,
        hints=hints,
        current_question=current_question,
    )

    result = _correct_delta_for_incidental_travel_followup(
        result=result,
        conversation_state=canonicalized_state,
        hints=hints,
        current_question=current_question,
    )
    result = (
        _correct_delta_for_unavailable_legal_country_switch(
            result=result,
            conversation_state=canonicalized_state,
            hints=hints,
        )
    )

    subject_detail_result = _correct_subject_detail_followup(
        result=result,
        conversation_state=canonicalized_state,
        hints=hints,
        current_question=current_question,
    )
    subject_detail_overridden = (
        subject_detail_result is not result
    )
    result = subject_detail_result

    result = _correct_delta_for_pressure_challenge_followup(
        result=result,
        conversation_state=canonicalized_state,
        hints=hints,
        current_question=current_question,
    )

    result = _correct_delta_for_legal_challenge_followup(
        result=result,
        conversation_state=canonicalized_state,
        hints=hints,
        current_question=current_question,
    )

    transition_hints = hints

    if (
        current_question is not None
        and len(canonicalized_state.actions) == 1
        and canonicalized_state.actions[0].type != "comparison"
        and not hints.strong_contact_signal
        and _explicit_same_subject_country_codes(
            result=result,
            current_question=current_question,
        ) is not None
    ):
        # A deterministic one-country legal-scope replacement is
        # stronger than an erroneous generic comparison hint.
        transition_hints = replace(
            hints,
            comparison_signal=False,
        )

    result = _correct_result_for_contextual_multi_country_contact(
        result=result,
        conversation_state=canonicalized_state,
        current_question=current_question,
    )

    try:
        outcome = _apply_transition(
            result=result,
            conversation_state=canonicalized_state,
            hints=transition_hints,
            current_question=current_question,
        )

        # Structural guarantee: every contextual legal clarification
        # must explicitly describe what the next user message is
        # expected to resolve. History remains useful context, but
        # conversation routing must never depend on history alone.
        if (
            outcome.final_status == "clarification"
            and outcome.contextual_clarification_answer is not None
            and outcome.pending_clarification is None
            and len(canonicalized_state.actions) == 1
            and canonicalized_state.actions[0].type
            == "legal_information"
        ):
            active_action = canonicalized_state.actions[0]

            outcome = replace(
                outcome,
                pending_clarification=(
                    ConversationPendingClarification(
                        reason="subject_detail",
                        candidate_action_types=[
                            "legal_information"
                        ],
                        candidate_country_codes=list(
                            active_action.country_codes
                        ),
                    )
                ),
            )

        if subject_detail_overridden:
            outcome = replace(
                outcome,
                semantic_result_overridden=True,
                semantic_override_reason=(
                    "subject_detail_clarification_resolved"
                ),
                context_inheritance_applied=True,
                inherited_action_type="legal_information",
                inherited_country_replaced=False,
            )

        if canonicalized_state is conversation_state:
            return outcome

        client_state_removed_codes = {
            code
            for original, canonicalized in zip(
                conversation_state.actions,
                canonicalized_state.actions,
            )
            if original.subject_text != canonicalized.subject_text
            for code in original.country_codes
        }

        if not client_state_removed_codes:
            return replace(
                outcome, subject_canonicalization_applied=True
            )

        return replace(
            outcome,
            subject_canonicalization_applied=True,
            subject_scope_removed_country_codes=sorted(
                {
                    *outcome.subject_scope_removed_country_codes,
                    *client_state_removed_codes,
                }
            ),
        )
    except Exception as error:
        # RULE 8, hardened: every explicitly modeled, safe case (no
        # conversation_state, a comparison that cannot be inherited
        # below two countries, and so on) already returns its own
        # defined TransitionOutcome inside _apply_transition without
        # raising at all - anything that reaches this except is by
        # definition unanticipated. Never silently fall back to the
        # classifier's own raw result here: that could mean acting on
        # a country/subject this engine exists specifically to
        # correct. Fail loudly instead, as a controlled 502 (see
        # routers/chat.py) - a rare, explicit failure is safer than a
        # response silently built on a stale or wrong reconciliation.
        raise ConversationTransitionError(
            "conversation_transition failed unexpectedly."
        ) from error


def _apply_transition(
    *,
    result: RequestUnderstandingResult,
    conversation_state: ConversationState,
    hints: DeterministicHints,
    current_question: str | None = None,
) -> TransitionOutcome:
    delta = result.current_message_delta
    previous_actions = conversation_state.actions

    if not previous_actions:
        return _passthrough(result)

    explicit_country_codes = list(delta.explicit_country_codes)
    has_new_action = bool(delta.explicit_action_types)
    has_new_subject = bool(
        delta.explicit_subject_text or delta.explicit_legal_topics
    )
    strong_new_contact_signal = (
        hints.strong_contact_signal
        and "contact" not in [action.type for action in previous_actions]
    )

    unambiguous_single_intent = (
        not has_new_action
        and not has_new_subject
        and not strong_new_contact_signal
        and not hints.comparison_signal
    )

    if len(previous_actions) == 1:
        previous_action = previous_actions[0]

        if (
            delta.context_operation
            in ("continue", "replace_country", "add_country")
            and unambiguous_single_intent
        ):
            if delta.context_operation == "continue":
                final_country_codes = list(
                    previous_action.country_codes
                )
                country_replaced = False
            elif delta.context_operation == "add_country":
                final_country_codes = _merge_country_codes(
                    previous_action.country_codes,
                    explicit_country_codes,
                )
                country_replaced = bool(explicit_country_codes)
            else:
                final_country_codes = (
                    explicit_country_codes
                    if explicit_country_codes
                    else list(previous_action.country_codes)
                )
                country_replaced = bool(explicit_country_codes)

            if (
                previous_action.type == "comparison"
                and len(final_country_codes) < 2
            ):
                # A comparison can never be inherited down to fewer
                # than two countries - fall through to the
                # classifier's own result rather than emit an invalid
                # comparison.
                return _passthrough(result)

            inherited = _inherit_action(
                previous_action,
                country_codes=final_country_codes,
            )

            if inherited.subject_became_empty:
                return _missing_topic_clarification(
                    action_type=previous_action.type,
                    country_codes=final_country_codes,
                )

            if (
                delta.context_operation == "continue"
                and current_question
                and previous_action.type != "contact"
            ):
                followup_text = " ".join(
                    current_question.strip().split()
                )[:240]

                subject_text = (
                    inherited.action.effective_subject_text()
                )[:260]

                country_phrase = " and ".join(
                    resolve_country_display_name(code)
                    for code in final_country_codes
                )

                inherited = replace(
                    inherited,
                    action=inherited.action.model_copy(
                        update={
                            "resolved_question": (
                                f"For {country_phrase}, answer this "
                                "conversational follow-up: "
                                f"{followup_text}. "
                                "The established employment-law "
                                f"subject is: {subject_text}."
                            )
                        }
                    ),
                )

            return TransitionOutcome(
                final_status="resolved",
                final_actions=[inherited.action],
                final_clarification_reason=None,
                pending_clarification=None,
                semantic_result_overridden=True,
                semantic_override_reason=(
                    "single_active_action_country_continuation"
                ),
                context_inheritance_applied=True,
                inherited_action_type=previous_action.type,
                inherited_country_replaced=country_replaced,
                subject_canonicalization_applied=(
                    inherited.canonicalization_applied
                ),
                subject_scope_removed_country_codes=(
                    inherited.removed_country_codes
                ),
                inherited_subject_canonicalized=(
                    inherited.canonicalization_applied
                ),
            )

        if (
            delta.context_operation == "select_action"
            and unambiguous_single_intent
            and not has_new_action
        ):
            # A single prior action with no real ambiguity to select
            # between - treat exactly like "replace_country".
            final_country_codes = (
                explicit_country_codes
                if explicit_country_codes
                else list(previous_action.country_codes)
            )

            inherited = _inherit_action(
                previous_action,
                country_codes=final_country_codes,
            )

            if inherited.subject_became_empty:
                return _missing_topic_clarification(
                    action_type=previous_action.type,
                    country_codes=final_country_codes,
                )

            return TransitionOutcome(
                final_status="resolved",
                final_actions=[inherited.action],
                final_clarification_reason=None,
                pending_clarification=None,
                semantic_result_overridden=True,
                semantic_override_reason=(
                    "single_active_action_country_continuation"
                ),
                context_inheritance_applied=True,
                inherited_action_type=previous_action.type,
                inherited_country_replaced=bool(explicit_country_codes),
                subject_canonicalization_applied=(
                    inherited.canonicalization_applied
                ),
                subject_scope_removed_country_codes=(
                    inherited.removed_country_codes
                ),
                inherited_subject_canonicalized=(
                    inherited.canonicalization_applied
                ),
            )

        candidate_countries = (
            previous_action.country_codes
            if previous_action.type != "comparison"
            else conversation_state.ordered_country_codes
        )

        # A genuinely ambiguous follow-up must never erase an
        # already-established single legal context. The semantic model
        # may itself incorrectly claim a new action while asking for a
        # clarification; deterministic contact/comparison signals are
        # therefore the authoritative blockers here, not the model's
        # own provisional action list.
        #
        # This never guesses the missing legal meaning: it only keeps
        # the already-known country/action alive and asks for the one
        # point that is actually unclear.
        if (
            result.status == "clarification"
            and result.clarification_reason == "ambiguous_request"
            and result.is_follow_up
            and previous_action.type == "legal_information"
            and len(candidate_countries) == 1
            and not explicit_country_codes
            and not hints.strong_contact_signal
            and not hints.comparison_signal
        ):
            country_name = resolve_country_display_name(
                candidate_countries[0]
            )

            partial_subject = None

            if (
                len(result.actions) == 1
                and result.actions[0].type == "legal_information"
            ):
                partial_subject = (
                    result.actions[0].subject_text
                    or result.actions[0].topic_text
                )

            if partial_subject:
                contextual_answer = (
                    f"I'll keep {country_name} and the current "
                    "employment-law context. Could you clarify "
                    f'exactly what you mean by "{partial_subject}"?'
                )
            else:
                contextual_answer = (
                    f"I'll keep {country_name} and the current "
                    "employment-law context. Could you clarify the "
                    "specific point in your last question?"
                )

            return TransitionOutcome(
                final_status="clarification",
                final_actions=[],
                final_clarification_reason="ambiguous_request",
                pending_clarification=(
                    ConversationPendingClarification(
                        reason="subject_detail",
                        candidate_action_types=[
                            "legal_information"
                        ],
                        candidate_country_codes=list(
                            candidate_countries
                        ),
                    )
                ),
                semantic_result_overridden=True,
                semantic_override_reason=(
                    "single_active_legal_context_clarification"
                ),
                context_inheritance_applied=False,
                inherited_action_type=previous_action.type,
                inherited_country_replaced=False,
                contextual_clarification_answer=contextual_answer,
            )

        if (
            delta.context_operation == "ambiguous"
            and not (has_new_action or has_new_subject)
            and len(candidate_countries) > 1
        ):
            return _country_reference_clarification(
                conversation_state,
                action_type=previous_action.type,
            )

        if (
            has_new_action
            and not explicit_country_codes
            and not has_new_subject
            and len(candidate_countries) > 1
            and previous_action.type
            not in set(delta.explicit_action_types)
        ):
            # A new action type was explicitly named (e.g. "contact"
            # after a two-country comparison), but nothing disambiguates
            # which of those countries it should target - ask, using
            # the newly-named type, never the old action's own type.
            new_action_type = delta.explicit_action_types[0]

            return _country_reference_clarification(
                conversation_state,
                action_type=new_action_type,
            )

        return _passthrough(result)

    # More than one prior action: a bare country/continue signal, or
    # an "ambiguous"/inconsistent operation, must never guess which
    # action it belongs to.
    if has_new_action:
        explicit_types = set(delta.explicit_action_types)
        matching_actions = [
            action
            for action in previous_actions
            if action.type in explicit_types
        ]

        if len(explicit_types) == 1 and len(matching_actions) == 1:
            selected_action = matching_actions[0]

            final_country_codes = (
                explicit_country_codes
                if explicit_country_codes
                else list(selected_action.country_codes)
            )

            inherited = _inherit_action(
                selected_action,
                country_codes=final_country_codes,
            )

            if inherited.subject_became_empty:
                return _missing_topic_clarification(
                    action_type=selected_action.type,
                    country_codes=final_country_codes,
                )

            return TransitionOutcome(
                final_status="resolved",
                final_actions=[inherited.action],
                final_clarification_reason=None,
                pending_clarification=None,
                semantic_result_overridden=True,
                semantic_override_reason=(
                    "multi_action_context_explicit_selection"
                ),
                context_inheritance_applied=True,
                inherited_action_type=selected_action.type,
                inherited_country_replaced=bool(
                    explicit_country_codes
                ),
                subject_canonicalization_applied=(
                    inherited.canonicalization_applied
                ),
                subject_scope_removed_country_codes=(
                    inherited.removed_country_codes
                ),
                inherited_subject_canonicalized=(
                    inherited.canonicalization_applied
                ),
            )

        # A genuinely new action type not among the prior candidates -
        # the classifier's own resolved actions already describe it.
        return _passthrough(result)

    if has_new_subject or strong_new_contact_signal:
        return _passthrough(result)

    return _multi_action_clarification(
        conversation_state,
        reason="select_action",
        new_country_codes=explicit_country_codes,
    )


def build_next_conversation_state(
    *,
    executed: list[tuple[RequestUnderstandingAction, list[str]]],
    pending_clarification: ConversationPendingClarification | None = None,
) -> ConversationState | None:
    """
    Build the ConversationState to return to the client, from the
    actions actually executed this turn (RULE 10) - never from the
    classifier's raw, pre-availability-check output.

    `executed` pairs each resolved action with the country codes it
    was actually run against (after dropping any unavailable
    country), in execution order. Returns None when there is nothing
    meaningful to persist (no action executed and no clarification
    pending), which the caller treats the same as an absent state.
    """

    if pending_clarification is not None:
        return ConversationState(
            actions=[],
            focus_action_index=None,
            ordered_country_codes=[],
            pending_clarification=pending_clarification,
        )

    if not executed:
        return None

    actions: list[ConversationActionState] = []
    comparison_country_codes: list[str] = []

    for action, actual_country_codes in executed:
        if not actual_country_codes:
            continue

        actions.append(
            ConversationActionState(
                type=action.type,
                country_codes=actual_country_codes,
                legal_topics=list(action.legal_topics),
                subject_text=(
                    action.effective_subject_text()
                    if action.type != "contact"
                    else None
                ),
                search_concepts=[
                    {"terms": list(concept.terms)}
                    for concept in action.search_concepts
                ],
                subject_specificity=(
                    action.subject_specificity
                    if action.type != "contact"
                    else None
                ),
                evidence_mode=(
                    action.resolved_evidence_mode()
                    if action.type != "contact"
                    else None
                ),
            )
        )

        if action.type == "comparison":
            comparison_country_codes = actual_country_codes

    if not actions:
        return None

    return ConversationState(
        actions=actions,
        focus_action_index=(0 if len(actions) == 1 else None),
        ordered_country_codes=comparison_country_codes,
        pending_clarification=None,
    )
