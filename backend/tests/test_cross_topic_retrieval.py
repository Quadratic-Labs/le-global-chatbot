"""Cross-topic retrieval regression tests. No OpenAI or live OpenSearch."""

import unittest

from app.models.chat import LegalChatRequest
from app.models.conversation_state import ConversationSearchConcept
from app.models.search import LegalSearchHit, LegalSearchResponse
from app.services.rag_answer import (
    LegalActionEvidenceSpec,
    _prepare_grounded_generation,
)


def make_hit(chunk_id, topic, subsection, content, country="ES"):
    return LegalSearchHit(
        score=10.0,
        document_id="document-" + country,
        chunk_id=chunk_id,
        country="Spain" if country == "ES" else "France",
        country_code=country,
        legal_topic=topic,
        document_type="comparator",
        language="en",
        section=topic,
        subsection=subsection,
        content=content,
        source_filename="Test 2026.docx",
        source_format="docx",
        reference_year=2026,
    )


class FakeClient:
    model = "test-model"

    def generate(self, **kwargs):
        raise AssertionError("Generation must not run in retrieval tests")


class CrossTopicRetrievalTests(unittest.TestCase):
    def run_case(self, original, broader, *, enabled=True):
        calls = []

        def search(request):
            calls.append(request)
            hits = original if request.legal_topics else broader
            return LegalSearchResponse(
                query=request.query,
                total=len(hits),
                limit=request.limit,
                offset=0,
                took_ms=1,
                hits=list(hits),
            )

        request = LegalChatRequest(
            question="What is the statutory notice period for termination in Spain?",
            country_codes=["ES"],
        )

        spec = LegalActionEvidenceSpec(
            country_codes=["ES"],
            legal_topics=["Termination of Employment Contracts"],
            subject_text="statutory notice period for termination",
            search_concepts=[
                ConversationSearchConcept(terms=["notice period"])
            ],
            evidence_mode="direct_topic",
            allow_cross_topic_fallback=enabled,
        )

        result = _prepare_grounded_generation(
            request=request,
            search_function=search,
            generation_client=FakeClient(),
            rerank_enabled=False,
            rerank_pool_multiplier=1,
            max_context_characters=16000,
            max_source_characters=4000,
            metrics=None,
            subject_text=None,
            search_concepts=None,
            evidence_mode=None,
            action_specs=[spec],
            known_excluded_country_codes=None,
            current_user_question=None,
        )

        return result, calls

    def test_recovers_notice_rule_from_adjacent_section(self):
        scoped = make_hit(
            "scoped-1",
            "Termination of Employment Contracts",
            "Grounds for Termination",
            "An employer may terminate a contract for misconduct.",
        )
        notice = make_hit(
            "notice-1",
            "Employment Contracts",
            "Notice Period",
            "Spanish Labour Law requires a party seeking to terminate "
            "an employment agreement to provide a minimum of fifteen "
            "(15) days' notice prior to termination. This does not "
            "apply to interim contracts.",
        )

        result, calls = self.run_case([scoped], [notice])

        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0].legal_topics, [
            "Termination of Employment Contracts",
            "Employment Contracts",
        ])
        self.assertEqual(calls[1].legal_topics, [])
        self.assertEqual(result.selected_hits[0].chunk_id, "notice-1")
        self.assertIn("fifteen", result.model_input.lower())

    def test_direct_scoped_evidence_needs_no_fallback(self):
        direct = make_hit(
            "direct-1",
            "Termination of Employment Contracts",
            "Notice Period",
            "The statutory notice period for termination is two weeks.",
        )

        result, calls = self.run_case([direct], [])

        self.assertEqual(len(calls), 1)
        self.assertEqual(result.selected_hits[0].chunk_id, "direct-1")

    def test_unrelated_cross_topic_notice_is_rejected(self):
        scoped = make_hit(
            "scoped-1",
            "Termination of Employment Contracts",
            "Grounds",
            "Dismissal for misconduct has procedural requirements.",
        )
        unrelated = make_hit(
            "unrelated-1",
            "Restrictive Covenants",
            "Garden Leave",
            "Garden leave arrangements can involve a notice period.",
        )

        result, calls = self.run_case([scoped], [unrelated])

        self.assertEqual(len(calls), 2)
        self.assertFalse(hasattr(result, "selected_hits"))

    def test_disabled_fallback_preserves_strict_topic(self):
        scoped = make_hit(
            "scoped-1",
            "Termination of Employment Contracts",
            "Grounds",
            "Dismissal for misconduct has procedural requirements.",
        )
        notice = make_hit(
            "notice-1",
            "Employment Contracts",
            "Notice Period",
            "The statutory notice period for termination is "
            "fifteen days.",
        )

        result, calls = self.run_case(
            [scoped], [notice], enabled=False
        )

        self.assertEqual(len(calls), 1)
        self.assertFalse(hasattr(result, "selected_hits"))

    def test_other_country_cannot_enter_fallback(self):
        foreign = make_hit(
            "foreign-1",
            "Employment Contracts",
            "Notice Period",
            "The statutory notice period for termination is "
            "fifteen days.",
            country="FR",
        )

        result, calls = self.run_case([], [foreign])

        self.assertEqual(len(calls), 2)
        self.assertFalse(hasattr(result, "selected_hits"))


if __name__ == "__main__":
    unittest.main()
