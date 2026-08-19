from __future__ import annotations

import unittest
from pathlib import Path


class ContactSemanticRecoveryRegressionTests(
    unittest.TestCase
):

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


if __name__ == "__main__":
    unittest.main()
