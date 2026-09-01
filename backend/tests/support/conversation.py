"""
Shared builders for RequestUnderstanding/ConversationState test fixtures.

Extracted from test_conversation_transition.py, which previously defined
these privately while four other test files imported them from it directly
(test_contextual_unavailable_country_switch.py, test_jurisdiction_role_
regressions.py, test_preclient_hotfix.py, test_legal_pressure_followup.py).
"""

from __future__ import annotations

from app.models.conversation_state import (
    ConversationActionState,
    ConversationState,
)
from app.services.request_understanding import (
    CurrentMessageDelta,
    DeterministicHints,
    RequestUnderstandingAction,
    RequestUnderstandingResult,
)


def _delta(
    *,
    context_operation: str = "independent",
    explicit_action_types: list[str] | None = None,
    explicit_country_codes: list[str] | None = None,
    explicit_legal_topics: list[str] | None = None,
    explicit_subject_text: str | None = None,
) -> CurrentMessageDelta:
    return CurrentMessageDelta(
        explicit_action_types=explicit_action_types or [],
        explicit_country_codes=explicit_country_codes or [],
        explicit_legal_topics=explicit_legal_topics or [],
        explicit_subject_text=explicit_subject_text,
        context_operation=context_operation,
    )


def _hints(
    *,
    strong_contact_signal: bool = False,
    comparison_signal: bool = False,
) -> DeterministicHints:
    return DeterministicHints(
        strong_contact_signal=strong_contact_signal,
        comparison_signal=comparison_signal,
    )


def _ru_action(
    action_type: str,
    country_codes: list[str],
    **kwargs,
) -> RequestUnderstandingAction:
    return RequestUnderstandingAction(
        type=action_type,
        country_codes=country_codes,
        **kwargs,
    )


def _result(
    *,
    status: str = "clarification",
    actions: list[RequestUnderstandingAction] | None = None,
    clarification_reason: str | None = "ambiguous_request",
    delta: CurrentMessageDelta | None = None,
    is_follow_up: bool = True,
) -> RequestUnderstandingResult:
    """
    A stand-in "classifier's own result" - defaults to a generic,
    contentless clarification so that any test asserting an override
    can tell the engine's own inheritance/clarification apart from
    whatever the classifier alone produced.
    """

    return RequestUnderstandingResult(
        status=status,
        actions=actions or [],
        is_follow_up=is_follow_up,
        confidence=0.5,
        clarification_reason=clarification_reason,
        current_message_delta=(
            delta or _delta(context_operation="ambiguous")
        ),
    )


def _action_state(
    action_type: str,
    country_codes: list[str],
    **kwargs,
) -> ConversationActionState:
    return ConversationActionState(
        type=action_type,
        country_codes=country_codes,
        **kwargs,
    )


def _state(
    actions: list[ConversationActionState],
    *,
    focus_action_index: int | None = None,
    ordered_country_codes: list[str] | None = None,
) -> ConversationState:
    return ConversationState(
        actions=actions,
        focus_action_index=focus_action_index,
        ordered_country_codes=ordered_country_codes or [],
    )
