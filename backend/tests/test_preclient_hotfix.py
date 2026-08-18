from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from app.models.chat import LegalChatRequest
from app.models.conversation_state import (
    ConversationPendingClarification,
    ConversationState,
)
from app.routers.chat import _resolve_current_country_scope
from app.services.conversation_transition import (
    _passthrough,
    apply_conversation_transition,
)
from app.services.rag_answer import (
    HARD_QUALITY_ERROR_TYPES,
    _build_model_input,
    _validate_challenge_certainty_stability,
)
from app.services.request_understanding import DeterministicHints

from tests.test_conversation_transition import (
    _delta,
    _result,
    _ru_action,
)


def _fake_country_resolution(*, request, catalog_provider):
    del catalog_provider

    return SimpleNamespace(
        available_codes=list(request.country_codes),
        unavailable_codes=[],
    )


def _contact_missing_country_result():
    return _result(
        status="clarification",
        actions=[_ru_action("contact", [])],
        clarification_reason="missing_country",
        delta=_delta(context_operation="ambiguous"),
    )


def _contact_pending_state():
    return ConversationState(
        version=1,
        actions=[],
        focus_action_index=None,
        ordered_country_codes=[],
        pending_clarification=ConversationPendingClarification(
            reason="missing_country",
            candidate_action_types=["contact"],
            candidate_country_codes=[],
        ),
    )


class PreClientHotfixTests(unittest.TestCase):

    def test_direct_contact_for_paris_resolves_france(self):
        with mock.patch(
            "app.routers.chat.resolve_country_availability",
            side_effect=_fake_country_resolution,
        ):
            scope = _resolve_current_country_scope(
                LegalChatRequest(
                    question=(
                        "Can I have the contact details for Paris?"
                    )
                ),
                lambda: None,
            )

        self.assertEqual(scope.available_codes, ["FR"])

    def test_ambiguous_milan_is_not_guessed(self):
        with mock.patch(
            "app.routers.chat.resolve_country_availability",
            side_effect=_fake_country_resolution,
        ):
            scope = _resolve_current_country_scope(
                LegalChatRequest(
                    question=(
                        "Can I have the contact details for Milan?"
                    )
                ),
                lambda: None,
            )

        self.assertEqual(scope.available_codes, [])
        self.assertEqual(scope.unavailable_codes, [])

    def test_contact_missing_country_creates_pending(self):
        outcome = _passthrough(
            _contact_missing_country_result()
        )

        self.assertIsNotNone(outcome.pending_clarification)
        self.assertEqual(
            outcome.pending_clarification.reason,
            "missing_country",
        )
        self.assertEqual(
            outcome.pending_clarification.candidate_action_types,
            ["contact"],
        )

    def test_france_consumes_contact_pending(self):
        outcome = apply_conversation_transition(
            result=_contact_missing_country_result(),
            conversation_state=_contact_pending_state(),
            hints=DeterministicHints(
                current_country_codes=["FR"],
            ),
            current_question="France",
        )

        self.assertEqual(outcome.final_status, "resolved")
        self.assertEqual(outcome.final_actions[0].type, "contact")
        self.assertEqual(
            outcome.final_actions[0].country_codes,
            ["FR"],
        )
        self.assertIsNone(outcome.pending_clarification)

    def test_paris_consumes_contact_pending(self):
        outcome = apply_conversation_transition(
            result=_contact_missing_country_result(),
            conversation_state=_contact_pending_state(),
            hints=DeterministicHints(),
            current_question="Paris",
        )

        self.assertEqual(outcome.final_status, "resolved")
        self.assertEqual(outcome.final_actions[0].type, "contact")
        self.assertEqual(
            outcome.final_actions[0].country_codes,
            ["FR"],
        )

    def test_challenge_input_contains_previous_assistant_answer(self):
        request = LegalChatRequest(
            question=(
                "Can the employer compel work during notice "
                "in Australia?"
            ),
            history=[
                {
                    "role": "user",
                    "content": (
                        "Can the employer compel work during notice?"
                    ),
                },
                {
                    "role": "assistant",
                    "content": (
                        "Australia\n"
                        "- I cannot reliably confirm whether the "
                        "employer can compel this [1]."
                    ),
                },
            ],
        )

        model_input = _build_model_input(
            request,
            [],
            current_user_question="Are you sure?",
        )

        self.assertIn(
            "PREVIOUS ASSISTANT ANSWER",
            model_input,
        )
        self.assertIn(
            "NOT A LEGAL SOURCE",
            model_input,
        )
        self.assertIn(
            "Preserve the previous answer's conclusion and degree of certainty",
            model_input,
        )

    def test_non_challenge_does_not_add_previous_answer_block(self):
        request = LegalChatRequest(
            question="Notice rules in Australia?",
            history=[
                {
                    "role": "user",
                    "content": "What is the notice rule?",
                },
                {
                    "role": "assistant",
                    "content": "Previous answer.",
                },
            ],
        )

        model_input = _build_model_input(
            request,
            [],
            current_user_question=(
                "What about the notice period?"
            ),
        )

        self.assertNotIn(
            "PREVIOUS ASSISTANT ANSWER",
            model_input,
        )

    def test_uncertain_answer_cannot_flip_to_bare_yes(self):
        errors = _validate_challenge_certainty_stability(
            current_user_question="Are you sure?",
            previous_assistant_answer=(
                "Australia\n"
                "- I cannot reliably confirm whether the employer "
                "can compel work during notice [1]."
            ),
            answer=(
                "Australia\n"
                "- Yes - the employer can compel work [1]."
            ),
        )

        self.assertEqual(
            [error.error_type for error in errors],
            ["challenge_certainty_flip"],
        )
        self.assertIn(
            "challenge_certainty_flip",
            HARD_QUALITY_ERROR_TYPES,
        )

    def test_continued_uncertainty_is_allowed(self):
        errors = _validate_challenge_certainty_stability(
            current_user_question="Are you sure?",
            previous_assistant_answer=(
                "Australia\n"
                "- I cannot reliably confirm the proposition [1]."
            ),
            answer=(
                "Australia\n"
                "- I still cannot reliably confirm the "
                "proposition from the cited evidence [1]."
            ),
        )

        self.assertEqual(errors, [])


class FinalPreClientBlockerTests(unittest.TestCase):

    def test_final_direct_contact_signal_for_country(
        self,
    ):
        from types import SimpleNamespace
        from unittest import mock
        from app.routers.chat import (
            _build_deterministic_hints,
        )

        request = LegalChatRequest(
            question=(
                "Can I have the contact details for Italy?"
            )
        )

        fake_scope = SimpleNamespace(
            available_codes=["IT"],
            unavailable_codes=[],
        )

        with mock.patch(
            "app.routers.chat._resolve_current_country_scope",
            return_value=fake_scope,
        ):
            hints, scope, _ = _build_deterministic_hints(
                request=request,
                catalog_provider=lambda: None,
                document_topic_provider=lambda codes: {},
            )

        self.assertTrue(
            hints.strong_contact_signal
        )

        self.assertEqual(
            scope.available_codes,
            ["IT"],
        )

    def test_final_direct_contact_signal_for_city(
        self,
    ):
        from types import SimpleNamespace
        from unittest import mock
        from app.routers.chat import (
            _build_deterministic_hints,
        )

        request = LegalChatRequest(
            question=(
                "Can I have the contact details for Paris?"
            )
        )

        fake_scope = SimpleNamespace(
            available_codes=["FR"],
            unavailable_codes=[],
        )

        with mock.patch(
            "app.routers.chat._resolve_current_country_scope",
            return_value=fake_scope,
        ):
            hints, scope, _ = _build_deterministic_hints(
                request=request,
                catalog_provider=lambda: None,
                document_topic_provider=lambda codes: {},
            )

        self.assertTrue(
            hints.strong_contact_signal
        )

        self.assertEqual(
            scope.available_codes,
            ["FR"],
        )

    def test_final_yes_cannot_flip_to_no_under_pressure(
        self,
    ):
        errors = (
            _validate_challenge_certainty_stability(
                current_user_question=(
                    "I'm sure. Just say yes."
                ),
                previous_assistant_answer=(
                    "Australia\n"
                    "- Yes — the employer may require this [1]."
                ),
                answer=(
                    "Australia\n"
                    "- No — the employer cannot require this [1]."
                ),
            )
        )

        self.assertEqual(
            [
                error.error_type
                for error in errors
            ],
            ["challenge_certainty_flip"],
        )

    def test_final_no_cannot_flip_to_yes_under_pressure(
        self,
    ):
        errors = (
            _validate_challenge_certainty_stability(
                current_user_question=(
                    "Just say yes."
                ),
                previous_assistant_answer=(
                    "Australia\n"
                    "- No — that proposition is not supported [1]."
                ),
                answer=(
                    "Australia\n"
                    "- Yes — that proposition is supported [1]."
                ),
            )
        )

        self.assertEqual(
            [
                error.error_type
                for error in errors
            ],
            ["challenge_certainty_flip"],
        )



if __name__ == "__main__":
    unittest.main()
