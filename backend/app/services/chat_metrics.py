"""Structured, privacy-safe performance metrics for legal chat requests."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any


logger = logging.getLogger(__name__)


@dataclass(slots=True)
class LegalChatMetrics:
    """
    Per-request performance metrics for one /api/v1/chat call.

    Never carries the question text, the generated answer, source
    content, or API keys - only counts, durations, and classification
    labels safe to log.
    """

    request_id: str
    question_characters: int
    max_sources: int
    rerank_enabled: bool

    outcome: str = "unknown"
    country_detection_ms: float = 0.0
    topic_detection_ms: float = 0.0
    opensearch_ms: float = 0.0
    rerank_ms: float = 0.0
    openai_ms: float = 0.0
    total_ms: float = 0.0

    country_codes: list[str] = field(
        default_factory=list
    )
    unavailable_country_codes: list[str] = field(
        default_factory=list
    )
    legal_topics: list[str] = field(
        default_factory=list
    )

    retrieval_total: int = 0
    selected_sources: int = 0
    model: str | None = None
    error_type: str | None = None
    transition_error: bool = False

    generation_attempts: int = 0
    repair_triggered: bool = False
    repair_success: bool = False
    repair_answer_returned: bool = False
    initial_hard_error_types: list[str] = field(
        default_factory=list
    )
    initial_soft_error_types: list[str] = field(
        default_factory=list
    )
    final_hard_error_types: list[str] = field(
        default_factory=list
    )
    final_soft_error_types: list[str] = field(
        default_factory=list
    )

    # Counts only - never the history content itself.
    history_messages: int = 0
    history_characters: int = 0
    contextual_question_used: bool = False

    # Request-understanding observability - labels and durations only,
    # never the question/answer text. RequestUnderstanding is the
    # primary router for every free-text request now, so
    # request_understanding_method only ever takes two values:
    # "semantic" (the understanding call succeeded and was used) or
    # "fallback" (every attempt failed/was unparsable, and a
    # conservative deterministic fallback route or safe clarification
    # was used instead - see request_understanding_error).
    request_actions: list[str] = field(
        default_factory=list
    )
    request_status: str | None = None
    request_understanding_method: str = "fallback"
    request_understanding_confidence: float | None = None
    request_understanding_ms: float = 0.0
    # OpenAI call time only, across every attempt - distinct from the
    # legal-generation call's own openai_ms, though both add into the
    # shared, backward-compatible openai_ms total below.
    request_understanding_openai_ms: float = 0.0
    request_understanding_attempts: int = 0
    request_understanding_retry_triggered: bool = False
    request_understanding_retry_reason: str | None = None
    request_understanding_error: str | None = None
    clarification_reason: str | None = None
    # Kept for backward compatibility with earlier log consumers - an
    # aggregate view (union across every action) of the per-action
    # fields below, which are the source of truth for a mixed request.
    resolved_country_codes: list[str] = field(
        default_factory=list
    )
    resolved_legal_topics: list[str] = field(
        default_factory=list
    )
    # Per-action country codes for a mixed request, e.g.
    # [{"type": "comparison", "country_codes": ["ES", "AU"]},
    #  {"type": "contact", "country_codes": ["ES"]}] - makes each
    # action's own country scope observable without ambiguity, since
    # a mixed request's actions never share one flat country list.
    resolved_action_countries: list[dict[str, object]] = field(
        default_factory=list
    )
    resolved_action_topics: list[dict[str, object]] = field(
        default_factory=list
    )

    # Conversation-continuity observability (0.4.2) - labels and
    # counts only, never the conversation_state content itself.
    conversation_state_received: bool = False
    conversation_state_version: int | None = None
    previous_action_types: list[str] = field(default_factory=list)
    previous_focus_action: str | None = None
    current_message_operation: str | None = None
    context_inheritance_applied: bool = False
    inherited_action_type: str | None = None
    inherited_country_replaced: bool = False
    inherited_legal_topics: list[str] = field(default_factory=list)
    inherited_subject_text: bool = False
    semantic_result_overridden: bool = False
    semantic_override_reason: str | None = None
    final_subject_text: bool = False
    search_concept_groups: int = 0
    conversation_state_emitted: bool = False

    # Evidence-fidelity observability (0.4.2).
    evidence_status_by_country: dict[str, str] = field(
        default_factory=dict
    )
    concept_coverage_by_country: dict[str, str] = field(
        default_factory=dict
    )
    direct_source_count: int = 0
    partial_source_count: int = 0
    rejected_source_count: int = 0
    subject_drift_detected: bool = False
    clarification_options: list[str] = field(default_factory=list)

    # Additional latencies (0.4.2) - distinct phases of the same
    # request, on top of the existing timers above.
    conversation_transition_ms: float = 0.0
    evidence_gate_ms: float = 0.0
    answer_generation_openai_ms: float = 0.0

    def add_opensearch_seconds(
        self,
        elapsed_seconds: float,
    ) -> None:
        self.opensearch_ms += (
            elapsed_seconds * 1000
        )

    def add_rerank_seconds(
        self,
        elapsed_seconds: float,
    ) -> None:
        self.rerank_ms += (
            elapsed_seconds * 1000
        )

    def as_log_payload(self) -> dict[str, Any]:
        return {
            "event": "legal_chat_performance",
            "request_id": self.request_id,
            "outcome": self.outcome,
            "total_ms": round(self.total_ms, 2),
            "country_detection_ms": round(
                self.country_detection_ms,
                2,
            ),
            "topic_detection_ms": round(
                self.topic_detection_ms,
                2,
            ),
            "opensearch_ms": round(
                self.opensearch_ms,
                2,
            ),
            "rerank_ms": round(
                self.rerank_ms,
                2,
            ),
            "openai_ms": round(
                self.openai_ms,
                2,
            ),
            "country_codes": self.country_codes,
            "unavailable_country_codes": (
                self.unavailable_country_codes
            ),
            "legal_topics": self.legal_topics,
            "retrieval_total": self.retrieval_total,
            "selected_sources": self.selected_sources,
            "model": self.model,
            "question_characters": (
                self.question_characters
            ),
            "max_sources": self.max_sources,
            "rerank_enabled": self.rerank_enabled,
            "error_type": self.error_type,
            "transition_error": self.transition_error,
            "generation_attempts": (
                self.generation_attempts
            ),
            "repair_triggered": self.repair_triggered,
            "repair_success": self.repair_success,
            "repair_answer_returned": (
                self.repair_answer_returned
            ),
            "initial_hard_error_types": (
                self.initial_hard_error_types
            ),
            "initial_soft_error_types": (
                self.initial_soft_error_types
            ),
            "final_hard_error_types": (
                self.final_hard_error_types
            ),
            "final_soft_error_types": (
                self.final_soft_error_types
            ),
            "history_messages": self.history_messages,
            "history_characters": self.history_characters,
            "contextual_question_used": (
                self.contextual_question_used
            ),
            "request_actions": self.request_actions,
            "request_status": self.request_status,
            "request_understanding_method": (
                self.request_understanding_method
            ),
            "request_understanding_confidence": (
                self.request_understanding_confidence
            ),
            "request_understanding_ms": round(
                self.request_understanding_ms,
                2,
            ),
            "request_understanding_openai_ms": round(
                self.request_understanding_openai_ms,
                2,
            ),
            "request_understanding_attempts": (
                self.request_understanding_attempts
            ),
            "request_understanding_retry_triggered": (
                self.request_understanding_retry_triggered
            ),
            "request_understanding_retry_reason": (
                self.request_understanding_retry_reason
            ),
            "request_understanding_error": (
                self.request_understanding_error
            ),
            "clarification_reason": self.clarification_reason,
            "resolved_country_codes": (
                self.resolved_country_codes
            ),
            "resolved_legal_topics": (
                self.resolved_legal_topics
            ),
            "resolved_action_countries": (
                self.resolved_action_countries
            ),
            "resolved_action_topics": (
                self.resolved_action_topics
            ),
            "conversation_state_received": (
                self.conversation_state_received
            ),
            "conversation_state_version": (
                self.conversation_state_version
            ),
            "previous_action_types": self.previous_action_types,
            "previous_focus_action": self.previous_focus_action,
            "current_message_operation": (
                self.current_message_operation
            ),
            "context_inheritance_applied": (
                self.context_inheritance_applied
            ),
            "inherited_action_type": self.inherited_action_type,
            "inherited_country_replaced": (
                self.inherited_country_replaced
            ),
            "inherited_legal_topics": self.inherited_legal_topics,
            "inherited_subject_text": self.inherited_subject_text,
            "semantic_result_overridden": (
                self.semantic_result_overridden
            ),
            "semantic_override_reason": (
                self.semantic_override_reason
            ),
            "final_subject_text": self.final_subject_text,
            "search_concept_groups": self.search_concept_groups,
            "conversation_state_emitted": (
                self.conversation_state_emitted
            ),
            "evidence_status_by_country": (
                self.evidence_status_by_country
            ),
            "concept_coverage_by_country": (
                self.concept_coverage_by_country
            ),
            "direct_source_count": self.direct_source_count,
            "partial_source_count": self.partial_source_count,
            "rejected_source_count": self.rejected_source_count,
            "subject_drift_detected": self.subject_drift_detected,
            "clarification_options": self.clarification_options,
            "conversation_transition_ms": round(
                self.conversation_transition_ms, 2
            ),
            "evidence_gate_ms": round(self.evidence_gate_ms, 2),
            "answer_generation_openai_ms": round(
                self.answer_generation_openai_ms, 2
            ),
        }

    def log(self) -> None:
        logger.info(
            "%s",
            json.dumps(
                self.as_log_payload(),
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        )
