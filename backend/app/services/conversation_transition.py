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

from dataclasses import dataclass, field

from app.models.conversation_state import (
    MAX_COUNTRY_CODES_PER_ACTION,
    ConversationActionState,
    ConversationPendingClarification,
    ConversationState,
)
from app.services.country_detection import resolve_country_display_name
from app.services.request_understanding import (
    DeterministicHints,
    RequestUnderstandingAction,
    RequestUnderstandingResult,
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


def _inherit_action(
    previous_action: ConversationActionState,
    *,
    country_codes: list[str],
) -> RequestUnderstandingAction:
    """
    Rebuild a full RequestUnderstandingAction from one prior
    conversation_state action, keeping its type/subject/topics intact
    and using only the given (possibly new) country_codes.
    """

    subject_text = previous_action.subject_text

    resolved_question = (
        _build_resolved_question(
            action_type=previous_action.type,
            country_codes=country_codes,
            subject_text=subject_text,
        )
        if subject_text and previous_action.type != "contact"
        else None
    )

    return RequestUnderstandingAction(
        type=previous_action.type,
        country_codes=country_codes,
        legal_topics=list(previous_action.legal_topics),
        topic_text=None,
        resolved_question=resolved_question,
        subject_text=subject_text,
        search_concepts=[
            {"terms": list(concept.terms)}
            for concept in previous_action.search_concepts
        ],
        subject_specificity=previous_action.subject_specificity,
        evidence_mode=previous_action.evidence_mode,
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

    return TransitionOutcome(
        final_status=result.status,
        final_actions=list(result.actions),
        final_clarification_reason=result.clarification_reason,
        pending_clarification=None,
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


def apply_conversation_transition(
    *,
    result: RequestUnderstandingResult,
    conversation_state: ConversationState | None,
    hints: DeterministicHints,
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
    """

    if conversation_state is None:
        return _passthrough(result)

    try:
        return _apply_transition(
            result=result,
            conversation_state=conversation_state,
            hints=hints,
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

            return TransitionOutcome(
                final_status="resolved",
                final_actions=[inherited],
                final_clarification_reason=None,
                pending_clarification=None,
                semantic_result_overridden=True,
                semantic_override_reason=(
                    "single_active_action_country_continuation"
                ),
                context_inheritance_applied=True,
                inherited_action_type=previous_action.type,
                inherited_country_replaced=country_replaced,
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

            return TransitionOutcome(
                final_status="resolved",
                final_actions=[inherited],
                final_clarification_reason=None,
                pending_clarification=None,
                semantic_result_overridden=True,
                semantic_override_reason=(
                    "single_active_action_country_continuation"
                ),
                context_inheritance_applied=True,
                inherited_action_type=previous_action.type,
                inherited_country_replaced=bool(explicit_country_codes),
            )

        candidate_countries = (
            previous_action.country_codes
            if previous_action.type != "comparison"
            else conversation_state.ordered_country_codes
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

            return TransitionOutcome(
                final_status="resolved",
                final_actions=[inherited],
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
