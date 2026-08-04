"""Tests for the semantic request-understanding model and service."""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from pydantic import ValidationError

from app.clients.openai_responses import (
    GeneratedText,
    OpenAIConfigurationError,
    OpenAIResponseError,
)
from app.models.catalog import LegalCatalogCountry, LegalCatalogResponse
from app.services.legal_topic_detection import CANONICAL_LEGAL_TOPICS
from app.services.request_understanding import (
    MAX_RESOLVED_QUESTION_CHARACTERS,
    MAX_TOPIC_TEXT_CHARACTERS,
    UNDERSTANDING_JSON_SCHEMA,
    DeterministicHints,
    HistoryTurn,
    RequestUnderstandingAction,
    RequestUnderstandingResult,
    understand_request,
)


def _iter_schema_nodes(node: object):
    """Recursively yield every dict node in a JSON Schema document."""

    if isinstance(node, dict):
        yield node

        for value in node.values():
            yield from _iter_schema_nodes(value)

    elif isinstance(node, list):
        for item in node:
            yield from _iter_schema_nodes(item)


def _legal_action(**overrides: object) -> dict[str, object]:
    action: dict[str, object] = {
        "type": "legal_information",
        "country_codes": ["PE"],
        "legal_topics": ["Termination of Employment Contracts"],
        "topic_text": None,
        "resolved_question": None,
    }
    action.update(overrides)

    return action


def _contact_action(**overrides: object) -> dict[str, object]:
    action: dict[str, object] = {
        "type": "contact",
        "country_codes": ["PE"],
        "legal_topics": [],
        "topic_text": None,
        "resolved_question": None,
    }
    action.update(overrides)

    return action


def _comparison_action(**overrides: object) -> dict[str, object]:
    action: dict[str, object] = {
        "type": "comparison",
        "country_codes": ["PE", "MX"],
        "legal_topics": ["Termination of Employment Contracts"],
        "topic_text": None,
        "resolved_question": None,
    }
    action.update(overrides)

    return action


def _delta(**overrides: object) -> dict[str, object]:
    """Build one minimal, valid CurrentMessageDelta payload."""

    payload: dict[str, object] = {
        "explicit_action_types": [],
        "explicit_country_codes": [],
        "explicit_legal_topics": [],
        "explicit_subject_text": None,
        "context_operation": "independent",
    }
    payload.update(overrides)

    return payload


def _resolved_result(**overrides: object) -> dict[str, object]:
    """Build one minimal, valid 'resolved' RequestUnderstandingResult payload."""

    payload: dict[str, object] = {
        "status": "resolved",
        "actions": [_legal_action()],
        "is_follow_up": False,
        "confidence": 0.9,
        "clarification_reason": None,
        "current_message_delta": _delta(),
    }
    payload.update(overrides)

    return payload


def _build_fake_catalog(
    countries: list[tuple[str, str]] | None = None,
) -> LegalCatalogResponse:
    """Build a self-contained catalog fixture using real ISO country codes."""

    resolved_countries = countries or [
        ("PE", "Peru"),
        ("ES", "Spain"),
        ("MX", "Mexico"),
        ("GB", "United Kingdom"),
        ("AU", "Australia"),
    ]

    return LegalCatalogResponse(
        countries=[
            LegalCatalogCountry(
                country_code=code,
                country=name,
                chunk_count=25,
            )
            for code, name in resolved_countries
        ],
        legal_topics=[],
        subsections=[],
    )


def _fake_catalog_provider(catalog: LegalCatalogResponse | None = None):
    """Build a catalog_provider callable returning a fixed catalog fixture."""

    resolved_catalog = catalog or _build_fake_catalog()

    def provider() -> LegalCatalogResponse:
        return resolved_catalog

    return provider


class UnderstandingJSONSchemaTests(unittest.TestCase):
    """
    Tests for the hand-written JSON Schema sent to OpenAI's structured
    output.

    Regression coverage for a real-world defect: a nullable field
    (type includes "null") whose schema also restricts values via
    "enum" must include null in that enum list, or OpenAI's strict
    structured-output mode silently forbids the field from ever being
    null - forcing the model to invent a non-null value even when the
    Pydantic model requires null (e.g. clarification_reason when
    status="resolved"). That mismatch made nearly every real,
    correctly-resolved request fail post-hoc validation and silently
    degrade to the conservative fallback, which cannot express a
    mixed multi-action plan - reproducing the exact Contact-loss
    defect this mission fixed, through a new root cause. This test
    would have caught it without needing a real OpenAI call.
    """

    def test_every_nullable_enum_field_permits_null(self) -> None:
        offending_nodes = []

        for node in _iter_schema_nodes(UNDERSTANDING_JSON_SCHEMA):
            node_type = node.get("type")
            is_nullable = (
                isinstance(node_type, list) and "null" in node_type
            )

            if is_nullable and "enum" in node:
                if None not in node["enum"]:
                    offending_nodes.append(node)

        self.assertEqual(
            offending_nodes,
            [],
            msg=(
                "Every nullable field with an enum must include "
                "null in that enum, or strict structured output "
                "forbids the model from ever returning null: "
                f"{offending_nodes!r}"
            ),
        )

    def test_clarification_reason_enum_includes_null(self) -> None:
        clarification_reason_schema = UNDERSTANDING_JSON_SCHEMA[
            "schema"
        ]["properties"]["clarification_reason"]

        self.assertIn(None, clarification_reason_schema["enum"])


class RequestUnderstandingActionModelTests(unittest.TestCase):
    """Tests for the per-action RequestUnderstandingAction model."""

    def test_valid_action_is_accepted(self) -> None:
        action = RequestUnderstandingAction(**_legal_action())

        self.assertEqual(action.type, "legal_information")
        self.assertEqual(action.country_codes, ["PE"])

    def test_unsupported_type_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            RequestUnderstandingAction(
                **_legal_action(type="schedule_a_meeting")
            )

    def test_extra_field_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            RequestUnderstandingAction(
                **_legal_action(),
                extra_field="not allowed",
            )

    def test_country_codes_are_uppercased_and_deduplicated(self) -> None:
        action = RequestUnderstandingAction(
            **_legal_action(country_codes=["pe", "PE", "  br "])
        )

        self.assertEqual(action.country_codes, ["PE", "BR"])

    def test_legal_topics_are_deduplicated(self) -> None:
        action = RequestUnderstandingAction(
            **_legal_action(
                legal_topics=[
                    "Pay Equity Laws",
                    "Pay Equity Laws",
                    "Working Conditions",
                ]
            )
        )

        self.assertEqual(
            action.legal_topics,
            ["Pay Equity Laws", "Working Conditions"],
        )

    def test_blank_topic_text_is_normalized_to_none(self) -> None:
        action = RequestUnderstandingAction(
            **_legal_action(topic_text="   ")
        )

        self.assertIsNone(action.topic_text)

    def test_blank_resolved_question_is_normalized_to_none(self) -> None:
        action = RequestUnderstandingAction(
            **_legal_action(resolved_question="   ")
        )

        self.assertIsNone(action.resolved_question)

    def test_resolved_question_over_max_length_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            RequestUnderstandingAction(
                **_legal_action(
                    resolved_question=(
                        "a" * (MAX_RESOLVED_QUESTION_CHARACTERS + 1)
                    )
                )
            )

    def test_topic_text_over_max_length_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            RequestUnderstandingAction(
                **_legal_action(
                    topic_text="a" * (MAX_TOPIC_TEXT_CHARACTERS + 1)
                )
            )


class ResolvedSubjectPrecisionTests(unittest.TestCase):
    """
    resolved_subject_precision() reconciles subject_specificity/
    evidence_mode against what search_concepts itself proves - never
    trusting a broad/broad_topic label the model attached despite
    real, distinct search_concepts, and never narrowing a genuinely
    general question either (mission "MISSION EXPRESS BLOQUANTE
    0.4.2", Regles A/C/D).
    """

    def test_1_real_remote_work_case_is_forced_to_specific_direct(
        self,
    ) -> None:
        # TEST 1 - the exact real-world defect: the model itself
        # mislabeled a precise remote-work question as broad/
        # broad_topic despite carrying real, distinct search_concepts.
        action = RequestUnderstandingAction(
            **_legal_action(
                legal_topics=["Working Conditions"],
                subject_text="rules on remote work (telework)",
                search_concepts=[
                    {
                        "terms": [
                            "remote work",
                            "telework",
                            "working from home",
                        ]
                    }
                ],
                subject_specificity="broad",
                evidence_mode="broad_topic",
            )
        )

        self.assertEqual(
            action.resolved_subject_precision(),
            ("specific", "direct_topic"),
        )

    def test_5_a_genuinely_general_question_keeps_broad_broad_topic(
        self,
    ) -> None:
        # TEST 5 - "Tell me about working conditions in Peru." - no
        # search_concepts distinct from the topic's own generic label,
        # so broad/broad_topic must survive untouched.
        action = RequestUnderstandingAction(
            **_legal_action(
                legal_topics=["Working Conditions"],
                subject_text="working conditions",
                search_concepts=[],
                subject_specificity="broad",
                evidence_mode="broad_topic",
            )
        )

        self.assertEqual(
            action.resolved_subject_precision(),
            ("broad", "broad_topic"),
        )

    def test_a_concept_repeating_only_the_topic_label_never_forces_specific(
        self,
    ) -> None:
        action = RequestUnderstandingAction(
            **_legal_action(
                legal_topics=["Working Conditions"],
                subject_text="working conditions",
                search_concepts=[{"terms": ["working conditions"]}],
                subject_specificity="broad",
                evidence_mode="broad_topic",
            )
        )

        self.assertEqual(
            action.resolved_subject_precision(),
            ("broad", "broad_topic"),
        )

    def test_real_world_broad_paraphrases_never_force_specific(
        self,
    ) -> None:
        # Real production output for "Tell me about working
        # conditions in Peru." - the model still supplies several
        # search_concepts terms, but every one is a paraphrase of the
        # topic's own label ("workplace conditions" shares "conditions",
        # "working environment" shares "working"), never a narrower
        # legal concept - word overlap, not exact-string equality,
        # is what must keep this broad/broad_topic.
        action = RequestUnderstandingAction(
            **_legal_action(
                legal_topics=["Working Conditions"],
                subject_text="working conditions",
                search_concepts=[
                    {
                        "terms": [
                            "working conditions",
                            "workplace conditions",
                            "working environment",
                        ]
                    }
                ],
                subject_specificity="broad",
                evidence_mode="broad_topic",
            )
        )

        self.assertEqual(
            action.resolved_subject_precision(),
            ("broad", "broad_topic"),
        )

    def test_never_weakens_an_already_specific_direct_topic_action(
        self,
    ) -> None:
        action = RequestUnderstandingAction(
            **_legal_action(
                legal_topics=["Working Conditions"],
                subject_text="overtime rules",
                search_concepts=[],
                subject_specificity="specific",
                evidence_mode="direct_topic",
            )
        )

        self.assertEqual(
            action.resolved_subject_precision(),
            ("specific", "direct_topic"),
        )


class RequestUnderstandingResultModelTests(unittest.TestCase):
    """
    Tests for RequestUnderstandingResult's model_validator - the exact
    business rules for which JSON shape is valid for each status.
    """

    def test_valid_resolved_single_action_result_is_accepted(self) -> None:
        result = RequestUnderstandingResult(**_resolved_result())

        self.assertEqual(result.status, "resolved")
        self.assertEqual(result.action_types, ["legal_information"])

    def test_valid_resolved_two_action_mixed_result_keeps_actions_independent(
        self,
    ) -> None:
        """
        A legal_information action and a contact action for different
        countries must never be merged into one flat scope.
        """

        result = RequestUnderstandingResult(
            **_resolved_result(
                actions=[
                    _legal_action(country_codes=["ES"]),
                    _contact_action(country_codes=["MX"]),
                ]
            )
        )

        legal_action = result.actions_of_type("legal_information")[0]
        contact_action = result.actions_of_type("contact")[0]

        self.assertEqual(legal_action.country_codes, ["ES"])
        self.assertEqual(contact_action.country_codes, ["MX"])

    def test_valid_resolved_three_action_result_is_accepted(self) -> None:
        result = RequestUnderstandingResult(
            **_resolved_result(
                actions=[
                    _contact_action(country_codes=["PE"]),
                    _legal_action(country_codes=["ES"]),
                    _comparison_action(country_codes=["MX", "GB"]),
                ]
            )
        )

        self.assertEqual(
            result.action_types,
            ["contact", "legal_information", "comparison"],
        )

    def test_more_than_three_actions_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            RequestUnderstandingResult(
                **_resolved_result(
                    actions=[
                        _contact_action(country_codes=["PE"]),
                        _legal_action(country_codes=["ES"]),
                        _comparison_action(country_codes=["MX", "GB"]),
                        _contact_action(country_codes=["AU"]),
                    ]
                )
            )

    def test_duplicate_type_and_country_scope_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            RequestUnderstandingResult(
                **_resolved_result(
                    actions=[
                        _legal_action(country_codes=["PE"]),
                        _legal_action(country_codes=["PE"]),
                    ]
                )
            )

    def test_same_type_with_different_country_scope_is_accepted(
        self,
    ) -> None:
        result = RequestUnderstandingResult(
            **_resolved_result(
                actions=[
                    _legal_action(country_codes=["PE"]),
                    _legal_action(country_codes=["MX"]),
                ]
            )
        )

        self.assertEqual(len(result.actions), 2)

    def test_resolved_contact_action_missing_country_codes_is_rejected(
        self,
    ) -> None:
        with self.assertRaises(ValidationError):
            RequestUnderstandingResult(
                **_resolved_result(
                    actions=[_contact_action(country_codes=[])]
                )
            )

    def test_resolved_contact_action_with_legal_topics_is_rejected(
        self,
    ) -> None:
        with self.assertRaises(ValidationError):
            RequestUnderstandingResult(
                **_resolved_result(
                    actions=[
                        _contact_action(
                            legal_topics=["Working Conditions"]
                        )
                    ]
                )
            )

    def test_resolved_contact_action_with_topic_text_is_rejected(
        self,
    ) -> None:
        with self.assertRaises(ValidationError):
            RequestUnderstandingResult(
                **_resolved_result(
                    actions=[
                        _contact_action(topic_text="notice periods")
                    ]
                )
            )

    def test_resolved_legal_information_action_missing_country_codes_is_rejected(
        self,
    ) -> None:
        with self.assertRaises(ValidationError):
            RequestUnderstandingResult(
                **_resolved_result(
                    actions=[_legal_action(country_codes=[])]
                )
            )

    def test_resolved_legal_information_action_without_topic_is_rejected(
        self,
    ) -> None:
        with self.assertRaises(ValidationError):
            RequestUnderstandingResult(
                **_resolved_result(
                    actions=[
                        _legal_action(legal_topics=[], topic_text=None)
                    ]
                )
            )

    def test_resolved_comparison_action_with_one_country_is_rejected(
        self,
    ) -> None:
        with self.assertRaises(ValidationError):
            RequestUnderstandingResult(
                **_resolved_result(
                    actions=[_comparison_action(country_codes=["PE"])]
                )
            )

    def test_resolved_comparison_action_without_topic_is_rejected(
        self,
    ) -> None:
        with self.assertRaises(ValidationError):
            RequestUnderstandingResult(
                **_resolved_result(
                    actions=[
                        _comparison_action(
                            legal_topics=[], topic_text=None
                        )
                    ]
                )
            )

    def test_resolved_with_clarification_reason_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            RequestUnderstandingResult(
                **_resolved_result(
                    clarification_reason="missing_country"
                )
            )

    def test_resolved_with_zero_actions_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            RequestUnderstandingResult(**_resolved_result(actions=[]))

    def test_valid_clarification_with_no_action_is_accepted(self) -> None:
        result = RequestUnderstandingResult(
            status="clarification",
            actions=[],
            is_follow_up=False,
            confidence=0.4,
            clarification_reason="ambiguous_request",
            current_message_delta=_delta(),
        )

        self.assertEqual(result.actions, [])

    def test_valid_clarification_with_one_partial_action_is_accepted(
        self,
    ) -> None:
        """
        A clarification may carry one incomplete action (e.g. a
        contact request with no country yet) purely to hint which
        kind of clarification wording applies.
        """

        result = RequestUnderstandingResult(
            status="clarification",
            actions=[_contact_action(country_codes=[])],
            is_follow_up=False,
            confidence=0.4,
            clarification_reason="missing_country",
            current_message_delta=_delta(),
        )

        self.assertEqual(result.action_hint_type(), "contact")

    def test_clarification_with_null_reason_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            RequestUnderstandingResult(
                status="clarification",
                actions=[],
                is_follow_up=False,
                confidence=0.4,
                clarification_reason=None,
                current_message_delta=_delta(),
            )

    def test_clarification_with_two_or_more_actions_is_rejected(
        self,
    ) -> None:
        with self.assertRaises(ValidationError):
            RequestUnderstandingResult(
                status="clarification",
                actions=[
                    _contact_action(country_codes=["PE"]),
                    _legal_action(country_codes=["MX"]),
                ],
                is_follow_up=False,
                confidence=0.4,
                clarification_reason="ambiguous_request",
                current_message_delta=_delta(),
            )

    def test_clarification_with_unsupported_request_reason_is_rejected(
        self,
    ) -> None:
        """
        clarification_reason='unsupported_request' must always pair
        with status='unsupported', never 'clarification'.
        """

        with self.assertRaises(ValidationError):
            RequestUnderstandingResult(
                status="clarification",
                actions=[],
                is_follow_up=False,
                confidence=0.4,
                clarification_reason="unsupported_request",
                current_message_delta=_delta(),
            )

    def test_valid_unsupported_result_is_accepted(self) -> None:
        result = RequestUnderstandingResult(
            status="unsupported",
            actions=[],
            is_follow_up=False,
            confidence=0.95,
            clarification_reason="unsupported_request",
            current_message_delta=_delta(),
        )

        self.assertEqual(result.status, "unsupported")
        self.assertEqual(result.actions, [])

    def test_unsupported_with_other_reason_is_rejected(self) -> None:
        for reason in ("missing_country", "ambiguous_request", None):
            with self.subTest(reason=reason):
                with self.assertRaises(ValidationError):
                    RequestUnderstandingResult(
                        status="unsupported",
                        actions=[],
                        is_follow_up=False,
                        confidence=0.5,
                        clarification_reason=reason,
                        current_message_delta=_delta(),
                    )

    def test_unsupported_carrying_an_action_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            RequestUnderstandingResult(
                status="unsupported",
                actions=[_contact_action()],
                is_follow_up=False,
                confidence=0.5,
                clarification_reason="unsupported_request",
                current_message_delta=_delta(),
            )

    def test_confidence_below_zero_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            RequestUnderstandingResult(**_resolved_result(confidence=-0.1))

    def test_confidence_above_one_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            RequestUnderstandingResult(**_resolved_result(confidence=1.1))

    def test_unknown_status_string_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            RequestUnderstandingResult(
                **_resolved_result(status="in_progress")
            )

    def test_unknown_top_level_extra_field_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            RequestUnderstandingResult(
                **_resolved_result(),
                extra_field="not allowed",
            )

    def test_action_types_property_preserves_order(self) -> None:
        result = RequestUnderstandingResult(
            **_resolved_result(
                actions=[
                    _contact_action(country_codes=["PE"]),
                    _legal_action(country_codes=["ES"]),
                ]
            )
        )

        self.assertEqual(
            result.action_types,
            ["contact", "legal_information"],
        )

    def test_actions_of_type_filters_and_preserves_order(self) -> None:
        result = RequestUnderstandingResult(
            **_resolved_result(
                actions=[
                    _contact_action(country_codes=["PE"]),
                    _legal_action(country_codes=["ES"]),
                    _legal_action(country_codes=["MX"]),
                ]
            )
        )

        legal_actions = result.actions_of_type("legal_information")

        self.assertEqual(len(legal_actions), 2)
        self.assertEqual(
            [action.country_codes for action in legal_actions],
            [["ES"], ["MX"]],
        )

    def test_action_hint_type_is_none_when_no_actions(self) -> None:
        result = RequestUnderstandingResult(
            status="clarification",
            actions=[],
            is_follow_up=False,
            confidence=0.4,
            clarification_reason="missing_country",
            current_message_delta=_delta(),
        )

        self.assertIsNone(result.action_hint_type())


class FakeUnderstandingClient:
    """
    Test double standing in for OpenAIResponsesClient.generate().

    Configured with a sequence of behaviors, one per expected call -
    either a JSON response string or an OpenAIResponseError to raise -
    so a retry sequence (success-after-failure, failure-after-failure)
    can be exercised without ever making a real network call.
    """

    def __init__(self, behaviors: list[str | OpenAIResponseError]) -> None:
        self._behaviors = list(behaviors)
        self.calls: list[dict[str, object]] = []

    def generate(
        self,
        instructions: str,
        input_text: str,
        text_format: dict | None = None,
    ) -> GeneratedText:
        call_index = len(self.calls)

        self.calls.append(
            {
                "instructions": instructions,
                "input_text": input_text,
                "text_format": text_format,
            }
        )

        behavior = self._behaviors[call_index]

        if isinstance(behavior, OpenAIResponseError):
            raise behavior

        return GeneratedText(text=behavior, model="test-model")


class UnderstandRequestTests(unittest.TestCase):
    """Tests for understand_request()'s resilience, retries, and parsing."""

    def test_valid_json_response_is_parsed_into_matching_result(
        self,
    ) -> None:
        client = FakeUnderstandingClient(
            [json.dumps(_resolved_result(actions=[_contact_action()]))]
        )

        outcome = understand_request(
            current_question="I need someone in Peru",
            history=[],
            hints=DeterministicHints(),
            catalog_provider=_fake_catalog_provider(),
            generation_client=client,
        )

        self.assertIsNotNone(outcome.result)
        self.assertEqual(outcome.result.action_types, ["contact"])
        self.assertIsNone(outcome.error)
        self.assertEqual(outcome.attempts, 1)

    def test_markdown_fenced_json_response_is_parsed(self) -> None:
        fenced_text = "```json\n" + json.dumps(_resolved_result()) + "\n```"
        client = FakeUnderstandingClient([fenced_text])

        outcome = understand_request(
            current_question="Hiring requirements in Peru?",
            history=[],
            hints=DeterministicHints(),
            catalog_provider=_fake_catalog_provider(),
            generation_client=client,
        )

        self.assertIsNotNone(outcome.result)
        self.assertEqual(outcome.result.status, "resolved")

    def test_invalid_json_text_yields_none_result(self) -> None:
        client = FakeUnderstandingClient(["this is not JSON at all"])

        outcome = understand_request(
            current_question="q",
            history=[],
            hints=DeterministicHints(),
            catalog_provider=_fake_catalog_provider(),
            generation_client=client,
        )

        self.assertIsNone(outcome.result)
        self.assertEqual(outcome.error, "invalid_response")

    def test_json_failing_pydantic_validation_yields_none_result(
        self,
    ) -> None:
        payload = _resolved_result(
            actions=[_legal_action(type="not_a_real_type")]
        )
        client = FakeUnderstandingClient([json.dumps(payload)])

        outcome = understand_request(
            current_question="q",
            history=[],
            hints=DeterministicHints(),
            catalog_provider=_fake_catalog_provider(),
            generation_client=client,
        )

        self.assertIsNone(outcome.result)
        self.assertEqual(outcome.error, "invalid_response")

    def test_configuration_error_yields_none_result_with_zero_attempts(
        self,
    ) -> None:
        """
        generation_client=None forces get_openai_understanding_client()
        to run - patched here to simulate a missing API key without
        ever touching real configuration or the network.
        """

        with patch(
            "app.services.request_understanding."
            "get_openai_understanding_client",
            side_effect=OpenAIConfigurationError("no api key"),
        ):
            outcome = understand_request(
                current_question="q",
                history=[],
                hints=DeterministicHints(),
                catalog_provider=_fake_catalog_provider(),
                generation_client=None,
            )

        self.assertIsNone(outcome.result)
        self.assertEqual(outcome.attempts, 0)
        self.assertEqual(outcome.error, "OpenAIConfigurationError")

    def test_transient_failure_then_success_retries_exactly_once(
        self,
    ) -> None:
        client = FakeUnderstandingClient(
            [
                OpenAIResponseError("boom", retryable=True),
                json.dumps(_resolved_result()),
            ]
        )

        outcome = understand_request(
            current_question="q",
            history=[],
            hints=DeterministicHints(),
            catalog_provider=_fake_catalog_provider(),
            generation_client=client,
        )

        self.assertEqual(len(client.calls), 2)
        self.assertEqual(outcome.attempts, 2)
        self.assertTrue(outcome.retry_triggered)
        self.assertIsNotNone(outcome.result)

    def test_two_consecutive_transient_failures_stop_after_two_attempts(
        self,
    ) -> None:
        client = FakeUnderstandingClient(
            [
                OpenAIResponseError("boom-1", retryable=True),
                OpenAIResponseError("boom-2", retryable=True),
            ]
        )

        outcome = understand_request(
            current_question="q",
            history=[],
            hints=DeterministicHints(),
            catalog_provider=_fake_catalog_provider(),
            generation_client=client,
        )

        self.assertEqual(len(client.calls), 2)
        self.assertIsNone(outcome.result)
        self.assertEqual(outcome.error, "OpenAIResponseError")

    def test_non_retryable_failure_is_never_retried(self) -> None:
        client = FakeUnderstandingClient(
            [
                OpenAIResponseError(
                    "bad request", retryable=False, status_code=400
                ),
            ]
        )

        outcome = understand_request(
            current_question="q",
            history=[],
            hints=DeterministicHints(),
            catalog_provider=_fake_catalog_provider(),
            generation_client=client,
        )

        self.assertEqual(len(client.calls), 1)
        self.assertFalse(outcome.retry_triggered)
        self.assertIsNone(outcome.result)

    def test_instructions_include_every_canonical_topic_and_catalog_country(
        self,
    ) -> None:
        """
        The prompt must give the model the full canonical topic list
        and every currently-indexed country, so it can resolve a
        topic or an unambiguous city without guessing.
        """

        catalog = _build_fake_catalog([("PE", "Peru"), ("ES", "Spain")])
        client = FakeUnderstandingClient([json.dumps(_resolved_result())])

        understand_request(
            current_question="q",
            history=[],
            hints=DeterministicHints(),
            catalog_provider=_fake_catalog_provider(catalog),
            generation_client=client,
        )

        instructions = client.calls[0]["instructions"]

        for topic in CANONICAL_LEGAL_TOPICS:
            self.assertIn(topic, instructions)

        self.assertIn("PE: Peru", instructions)
        self.assertIn("ES: Spain", instructions)

    def test_input_text_includes_question_history_and_hints(self) -> None:
        client = FakeUnderstandingClient([json.dumps(_resolved_result())])

        history = [
            HistoryTurn(
                role="user",
                content="What about termination rules in Spain?",
            ),
            HistoryTurn(
                role="assistant",
                content="Spain requires a statutory notice period.",
            ),
        ]
        hints = DeterministicHints(
            strong_contact_signal=True,
            current_country_codes=["PE"],
        )

        understand_request(
            current_question="Now what about hiring rules?",
            history=history,
            hints=hints,
            catalog_provider=_fake_catalog_provider(),
            generation_client=client,
        )

        input_text = client.calls[0]["input_text"]

        self.assertIn("Now what about hiring rules?", input_text)
        self.assertIn("What about termination rules in Spain?", input_text)
        self.assertIn(
            "Spain requires a statutory notice period.", input_text
        )
        self.assertIn("strong_contact_signal", input_text)


if __name__ == "__main__":
    unittest.main()
