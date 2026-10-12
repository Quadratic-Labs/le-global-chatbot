"""Manager rule 7: country/topic-scoped second-question contact."""
import unittest
from unittest import mock

from app.models.conversation_state import (
    ConversationState, LegalQuestionCount,
)
from app.routers.chat import _advance_legal_question_counts
from app.services.request_understanding import RequestUnderstandingAction


def action(country, topic, type="legal_information"):
    return RequestUnderstandingAction(
        type=type,
        country_codes=[country],
        legal_topics=[topic] if type != "contact" else [],
        subject_text="working hours" if type != "contact" else None,
    )


def advance(state, country, topic, type="legal_information"):
    entries, repeat = _advance_legal_question_counts(
        state, [(action(country, topic, type), [country])]
    )
    return ConversationState(
        legal_question_counts=entries,
    ), repeat


class ManagerSecondQuestionContactTests(unittest.TestCase):
    def test_second_question_same_pair(self):
        state, codes = advance(None, "FR", "Working Conditions")
        self.assertEqual(codes, set())
        self.assertEqual(state.legal_question_counts[0].count, 1)
        state, codes = advance(state, "FR", "Working Conditions")
        self.assertEqual(codes, {"FR"})
        self.assertEqual(state.legal_question_counts[0].count, 2)
        state, codes = advance(state, "FR", "Working Conditions")
        self.assertEqual(codes, {"FR"})

    def test_interleaved_country_and_topic(self):
        state = None
        for country, topic in (
            ("FR", "Working Conditions"),
            ("ES", "Working Conditions"),
            ("FR", "Employment Contracts"),
        ):
            state, repeated = advance(state, country, topic)
            self.assertFalse(repeated)
        state, repeated = advance(state, "FR", "Working Conditions")
        self.assertEqual(repeated, {"FR"})
        state, repeated = advance(state, "ES", "Working Conditions")
        self.assertEqual(repeated, {"ES"})
        state, repeated = advance(state, "FR", "Employment Contracts")
        self.assertEqual(repeated, {"FR"})
        self.assertEqual(len(state.legal_question_counts), 3)

    def test_new_conversation_resets(self):
        state, _ = advance(None, "ES", "Working Conditions")
        state, codes = advance(None, "ES", "Working Conditions")
        self.assertFalse(codes)

    def test_contact_action_does_not_increment(self):
        state, _ = advance(None, "FR", "Working Conditions")
        values, repeat = _advance_legal_question_counts(
            state,
            [(action("FR", "Working Conditions", "contact"), ["FR"])],
        )
        self.assertEqual(repeat, set())
        self.assertEqual(values[0].count, 1)

    def test_multicountry_does_not_increment_but_multitopic_counts_each_pair(self):
        state, _ = advance(None, "FR", "Working Conditions")
        multi = action("FR", "Working Conditions")
        entries, repeated = _advance_legal_question_counts(
            state,
            [(multi, ["FR", "ES"])],
        )
        self.assertEqual(repeated, set())
        self.assertEqual(entries[0].count, 1)
        multi_topics = multi.model_copy(update={
            "legal_topics": ["Working Conditions", "Employment Contracts"],
        })
        entries, repeated = _advance_legal_question_counts(
            state,
            [(multi_topics, ["FR"])],
        )
        self.assertEqual(repeated, {"FR"})
        self.assertEqual(
            {(item.legal_topic, item.count) for item in entries},
            {("Working Conditions", 2), ("Employment Contracts", 1)},
        )

    def test_same_pair_not_counted_twice_in_one_turn(self):
        one = action("FR", "Working Conditions")
        entries, repeated = _advance_legal_question_counts(
            None, [(one, ["FR"]), (one, ["FR"])],
        )
        self.assertEqual(repeated, set())
        self.assertEqual(entries[0].count, 1)

    def test_invalid_duplicate_and_unknown_pairs_rejected(self):
        with self.assertRaises(ValueError):
            ConversationState(legal_question_counts=[
                LegalQuestionCount(
                    country_code="FR", legal_topic="Working Conditions", count=1
                ),
                LegalQuestionCount(
                    country_code="FR", legal_topic="Working Conditions", count=2
                ),
            ])
        with self.assertRaises(ValueError):
            LegalQuestionCount(
                country_code="ZZ", legal_topic="Working Conditions", count=1,
            )

    def test_existing_state_without_counts_still_valid(self):
        state = ConversationState.model_validate({"actions": []})
        self.assertEqual(state.legal_question_counts, [])

    def test_bounded_and_saturating(self):
        from app.services.legal_topic_taxonomy import CANONICAL_LEGAL_TOPICS
        topics = sorted(CANONICAL_LEGAL_TOPICS)
        state = None
        for i in range(30):
            country = ["FR", "ES", "DE", "NL", "IT", "BE", "CA", "US", "AU", "JP"][i//len(topics)]
            topic = topics[i % len(topics)]
            state, _ = advance(state, country, topic)
        self.assertLessEqual(len(state.legal_question_counts), 24)

if __name__ == "__main__":
    unittest.main()
