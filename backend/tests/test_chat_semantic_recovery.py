"""
Tests for the chat router's deterministic semantic-result recovery
paths: narrow, code-structure-verified overrides that let a specific
semantic-understanding outcome (an unsupported/clarification result
naming exactly one unavailable country, or a pure contact request
misclassified as unsupported) resolve deterministically instead of
falling through to a full RAG generation call.

test_mixed_legal_contact_scope.py's single behavioral test (calling
_legal_generation_user_question directly) and the two source-structure
checks (test_contact_semantic_recovery.py, test_unsupported_legal_
country_recovery.py - both read routers/chat.py's own text to confirm
the exact override conditions are present, since these recovery
branches are deliberately narrow "if" blocks with no separately
exported function to call directly) are consolidated here as one
domain.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from app.routers.chat import (
    _legal_generation_user_question,
)


class MixedLegalContactGenerationScopeTests(unittest.TestCase):
    """The text sent to legal-answer generation must be the resolved
    legal question alone - never the raw user message when a contact
    request was mixed into the same turn, since the raw message may
    itself carry no legal content for the model to answer."""

    def test_mixed_request_exposes_only_resolved_legal_question(
        self,
    ) -> None:
        original = (
            "What is the notice period for dismissal in Italy, "
            "and can I also have the L&E Global contact?"
        )

        resolved = (
            "What is the notice period for dismissal in Italy?"
        )

        self.assertEqual(
            _legal_generation_user_question(
                original_question=original,
                resolved_legal_question=resolved,
                has_contact_actions=True,
            ),
            resolved,
        )

    def test_pure_legal_request_preserves_literal_user_message(
        self,
    ) -> None:
        self.assertEqual(
            _legal_generation_user_question(
                original_question="Are you sure?",
                resolved_legal_question=(
                    "Can an employer dismiss immediately "
                    "for serious misconduct in Australia?"
                ),
                has_contact_actions=False,
            ),
            "Are you sure?",
        )


class ContactSemanticRecoveryTests(unittest.TestCase):
    """A semantic-understanding result of 'clarification' or
    'unsupported' must still resolve deterministically to a pure
    contact answer when the deterministic hints show a strong contact
    signal with no comparison signal and no supported legal scope -
    never widened to also cover an ordinary 'resolved' result."""

    def setUp(self) -> None:
        self.source = (
            Path("/app/app/routers/chat.py")
            .read_text(encoding="utf-8")
        )

    def _contact_recovery_block(self) -> str:
        marker = (
            '"semantic_contact_'
            'clarification_recovered"'
        )

        position = self.source.index(marker)

        start = self.source.rfind(
            "        if (",
            0,
            position,
        )

        end = self.source.index(
            "            return response",
            position,
        )

        return self.source[
            start:
            end + len("            return response")
        ]

    def test_contact_recovery_accepts_semantic_unsupported(
        self,
    ) -> None:
        block = self._contact_recovery_block()

        self.assertIn(
            'result.status in '
            '{"clarification", "unsupported"}',
            block,
        )

    def test_contact_recovery_stays_pure_contact_only(
        self,
    ) -> None:
        block = self._contact_recovery_block()

        self.assertIn(
            "not current_legal_scope.is_supported",
            block,
        )

        self.assertIn(
            "hints.strong_contact_signal",
            block,
        )

        self.assertIn(
            "not hints.comparison_signal",
            block,
        )

    def test_normal_resolved_result_is_not_in_override_set(
        self,
    ) -> None:
        block = self._contact_recovery_block()

        self.assertNotIn(
            '{"clarification", "unsupported", "resolved"}',
            block,
        )


class UnsupportedLegalCountryRecoveryTests(unittest.TestCase):
    """A semantic-understanding result naming exactly one unavailable
    country (and no supported country) must resolve directly to the
    deterministic unavailable-country answer - no search, no RAG
    generation fallback - and only when there is real legal scope, not
    a contact-only or comparison request."""

    def setUp(self) -> None:
        self.source = (
            Path("/app/app/routers/chat.py")
            .read_text(encoding="utf-8")
        )

    def _block(self) -> str:
        marker = (
            '"semantic_unavailable_legal_country_recovered"'
        )

        position = self.source.index(marker)

        start = self.source.rfind(
            "        if (",
            0,
            position,
        )

        # This recovery now returns LegalChatResponse directly.
        # Stop at the next sibling router branch instead of looking
        # for "return response", which belongs to the following
        # clarification-recovery block.
        next_branch = self.source.index(
            "\n        if (",
            position,
        )

        return self.source[
            start:
            next_branch
        ]

    def test_recovers_clarification_and_unsupported(self) -> None:
        block = self._block()

        self.assertIn(
            'result.status in {"clarification", "unsupported"}',
            block,
        )

    def test_requires_exactly_one_unavailable_country(self) -> None:
        block = self._block()

        self.assertIn(
            "not current_country_scope.available_codes",
            block,
        )
        self.assertIn(
            "len(current_country_scope.unavailable_codes) == 1",
            block,
        )

    def test_recovery_is_direct_no_search_fallback(
        self,
    ) -> None:
        block = self._block()

        self.assertIn(
            'metrics.outcome = "fallback_unavailable_country"',
            block,
        )
        self.assertIn(
            "_unavailable_countries_answer(",
            block,
        )
        self.assertIn(
            "retrieval_total=0",
            block,
        )
        self.assertNotIn(
            "_resolve_conservative_fallback(",
            block,
        )
        self.assertNotIn(
            "answer_legal_question(",
            block,
        )

    def test_requires_real_legal_scope(self) -> None:
        block = self._block()

        self.assertIn(
            "current_legal_scope.is_supported",
            block,
        )
        self.assertIn(
            "not hints.strong_contact_signal",
            block,
        )
        self.assertIn(
            "not hints.comparison_signal",
            block,
        )


if __name__ == "__main__":
    unittest.main()
