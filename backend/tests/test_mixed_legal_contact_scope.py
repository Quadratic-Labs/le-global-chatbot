from __future__ import annotations

import unittest

from app.routers.chat import (
    _legal_generation_user_question,
)


class MixedLegalContactGenerationScopeTests(unittest.TestCase):

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


if __name__ == "__main__":
    unittest.main()
