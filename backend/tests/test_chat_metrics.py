"""Tests for the LegalChatMetrics logging payload."""

from __future__ import annotations

import json
import unittest

from app.services.chat_metrics import LegalChatMetrics


def _build_metrics(**overrides: object) -> LegalChatMetrics:
    defaults: dict[str, object] = {
        "request_id": "request-1",
        "question_characters": 42,
        "max_sources": 6,
        "rerank_enabled": False,
    }
    defaults.update(overrides)

    return LegalChatMetrics(**defaults)


class LegalChatMetricsTests(unittest.TestCase):
    """Tests for LegalChatMetrics accumulation and serialization."""

    def test_default_outcome_is_unknown(
        self,
    ) -> None:
        metrics = _build_metrics()

        self.assertEqual(
            metrics.outcome,
            "unknown",
        )

    def test_add_opensearch_seconds_accumulates(
        self,
    ) -> None:
        metrics = _build_metrics()

        metrics.add_opensearch_seconds(0.010)
        metrics.add_opensearch_seconds(0.020)

        self.assertAlmostEqual(
            metrics.opensearch_ms,
            30.0,
            places=3,
        )

    def test_add_rerank_seconds_accumulates(
        self,
    ) -> None:
        metrics = _build_metrics()

        metrics.add_rerank_seconds(0.005)
        metrics.add_rerank_seconds(0.005)

        self.assertAlmostEqual(
            metrics.rerank_ms,
            10.0,
            places=3,
        )

    def test_as_log_payload_contains_expected_event(
        self,
    ) -> None:
        metrics = _build_metrics(
            request_id="request-42",
        )

        payload = metrics.as_log_payload()

        self.assertEqual(
            payload["event"],
            "legal_chat_performance",
        )

        self.assertEqual(
            payload["request_id"],
            "request-42",
        )

        self.assertEqual(
            payload["question_characters"],
            42,
        )

        self.assertEqual(
            payload["max_sources"],
            6,
        )

    def test_as_log_payload_never_includes_question_or_answer_fields(
        self,
    ) -> None:
        metrics = _build_metrics()

        payload = metrics.as_log_payload()

        self.assertNotIn(
            "question",
            payload,
        )

        self.assertNotIn(
            "answer",
            payload,
        )

        self.assertNotIn(
            "content",
            payload,
        )

        self.assertNotIn(
            "api_key",
            payload,
        )

    def test_repair_metrics_serialize_boolean_defaults(
        self,
    ) -> None:
        metrics = _build_metrics()

        payload = metrics.as_log_payload()

        self.assertIs(
            payload["repair_triggered"],
            False,
        )

        self.assertIs(
            payload["repair_answer_returned"],
            False,
        )

        self.assertIs(
            payload["repair_success"],
            False,
        )

        self.assertIsInstance(
            payload["repair_success"],
            bool,
        )

    def test_request_understanding_fields_default_safely(
        self,
    ) -> None:
        metrics = _build_metrics()

        payload = metrics.as_log_payload()

        self.assertEqual(
            payload["request_actions"],
            [],
        )

        self.assertEqual(
            payload["request_understanding_method"],
            "fallback",
        )

        self.assertIsNone(
            payload["request_understanding_confidence"],
        )

        self.assertEqual(
            payload["request_understanding_ms"],
            0,
        )

        self.assertIsNone(
            payload["request_understanding_error"],
        )

        self.assertIsNone(
            payload["clarification_reason"],
        )

        self.assertEqual(
            payload["resolved_country_codes"],
            [],
        )

        self.assertEqual(
            payload["resolved_legal_topics"],
            [],
        )

        self.assertIsNone(
            payload["request_status"],
        )

        self.assertEqual(
            payload["request_understanding_openai_ms"],
            0,
        )

        self.assertEqual(
            payload["request_understanding_attempts"],
            0,
        )

        self.assertIs(
            payload["request_understanding_retry_triggered"],
            False,
        )

        self.assertIsNone(
            payload["request_understanding_retry_reason"],
        )

        self.assertEqual(
            payload["resolved_action_topics"],
            [],
        )

    def test_request_understanding_fields_are_recorded(
        self,
    ) -> None:
        metrics = _build_metrics()

        metrics.request_actions = ["contact"]
        metrics.request_status = "resolved"
        metrics.request_understanding_method = "semantic"
        metrics.request_understanding_confidence = 0.87
        metrics.request_understanding_ms = 42.5
        metrics.request_understanding_openai_ms = 40.1
        metrics.request_understanding_attempts = 2
        metrics.request_understanding_retry_triggered = True
        metrics.request_understanding_retry_reason = "http_503"
        metrics.request_understanding_error = None
        metrics.clarification_reason = "missing_country"
        metrics.resolved_country_codes = ["PE"]
        metrics.resolved_legal_topics = ["Employee Benefits"]
        metrics.resolved_action_topics = [
            {
                "type": "legal_information",
                "legal_topics": ["Employee Benefits"],
                "topic_text": None,
            }
        ]

        payload = metrics.as_log_payload()

        self.assertEqual(
            payload["request_status"],
            "resolved",
        )

        self.assertEqual(
            payload["request_understanding_openai_ms"],
            40.1,
        )

        self.assertEqual(
            payload["request_understanding_attempts"],
            2,
        )

        self.assertIs(
            payload["request_understanding_retry_triggered"],
            True,
        )

        self.assertEqual(
            payload["request_understanding_retry_reason"],
            "http_503",
        )

        self.assertEqual(
            payload["resolved_action_topics"],
            [
                {
                    "type": "legal_information",
                    "legal_topics": ["Employee Benefits"],
                    "topic_text": None,
                }
            ],
        )

        self.assertEqual(
            payload["request_actions"],
            ["contact"],
        )

        self.assertEqual(
            payload["request_understanding_method"],
            "semantic",
        )

        self.assertEqual(
            payload["request_understanding_confidence"],
            0.87,
        )

        self.assertEqual(
            payload["request_understanding_ms"],
            42.5,
        )

        self.assertEqual(
            payload["clarification_reason"],
            "missing_country",
        )

        self.assertEqual(
            payload["resolved_country_codes"],
            ["PE"],
        )

        self.assertEqual(
            payload["resolved_legal_topics"],
            ["Employee Benefits"],
        )

    def test_log_emits_exactly_one_json_record(
        self,
    ) -> None:
        metrics = _build_metrics()
        metrics.outcome = "generated"

        with self.assertLogs(
            "app.services.chat_metrics",
            level="INFO",
        ) as log_context:
            metrics.log()

        self.assertEqual(
            len(log_context.records),
            1,
        )

        payload = json.loads(
            log_context.records[0].getMessage()
        )

        self.assertEqual(
            payload["outcome"],
            "generated",
        )


if __name__ == "__main__":
    unittest.main()
