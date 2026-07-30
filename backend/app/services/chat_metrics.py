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

    generation_attempts: int = 0
    repair_triggered: bool = False
    repair_success: bool | None = None
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
            "generation_attempts": (
                self.generation_attempts
            ),
            "repair_triggered": self.repair_triggered,
            "repair_success": self.repair_success,
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
