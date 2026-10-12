"""Regression tests: one action's question must not pollute another's retrieval."""
from __future__ import annotations

import unittest

from app.models.chat import LegalChatRequest
from app.models.search import LegalSearchResponse
from app.services.rag_answer import (
    LegalActionEvidenceSpec,
    answer_legal_question,
)


class ActionScopedRetrievalTests(unittest.TestCase):
    def _collect_queries(self, specs):
        queries = []

        def fake_search(request):
            queries.append(request.query)
            return LegalSearchResponse(
                query=request.query,
                total=0,
                limit=request.limit,
                offset=request.offset,
                took_ms=0,
                hits=[],
            )

        response = answer_legal_question(
            request=LegalChatRequest(
                question=(
                    "In France, what are the normal working hours, "
                    "and what is the statutory notice period for termination?"
                ),
                country_codes=["FR"],
            ),
            search_function=fake_search,
            action_specs=specs,
        )
        self.assertFalse(response.grounded)
        return queries

    def test_each_action_uses_its_own_resolved_question(self):
        queries = self._collect_queries([
            LegalActionEvidenceSpec(
                country_codes=["FR"],
                retrieval_question="What are the normal working hours in France?",
            ),
            LegalActionEvidenceSpec(
                country_codes=["FR"],
                retrieval_question="What is the statutory notice period for termination in France?",
            ),
        ])
        self.assertEqual(len(queries), 2)
        self.assertIn("hours", queries[0].lower())
        self.assertNotIn("termination", queries[0].lower())
        self.assertIn("termination", queries[1].lower())
        self.assertNotIn("hours", queries[1].lower())

    def test_missing_resolved_question_preserves_old_behavior(self):
        queries = self._collect_queries([
            LegalActionEvidenceSpec(country_codes=["FR"]),
            LegalActionEvidenceSpec(country_codes=["FR"]),
        ])
        self.assertEqual(len(queries), 2)
        self.assertEqual(queries[0], queries[1])
        self.assertIn("hours", queries[0].lower())
        self.assertIn("termination", queries[0].lower())

    def test_blank_action_question_falls_back_to_combined_question(self):
        queries = self._collect_queries([
            LegalActionEvidenceSpec(
                country_codes=["FR"],
                retrieval_question="   ",
            ),
        ])
        self.assertEqual(len(queries), 1)
        self.assertIn("hours", queries[0].lower())
        self.assertIn("termination", queries[0].lower())


if __name__ == "__main__":
    unittest.main()
