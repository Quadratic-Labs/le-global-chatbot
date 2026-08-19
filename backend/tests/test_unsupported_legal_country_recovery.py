from __future__ import annotations

import unittest
from pathlib import Path


class UnsupportedLegalCountryRecoveryTests(unittest.TestCase):

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
