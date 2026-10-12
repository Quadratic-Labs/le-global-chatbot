"""Manager rule 7: wrong understanding topics must not pollute counters."""
import unittest

from app.models.conversation_state import ConversationState
from app.routers.chat import _advance_legal_question_counts
from app.services.request_understanding import RequestUnderstandingAction


def action(topic, country="FR", kind="legal_information", resolved=None):
    return RequestUnderstandingAction(
        type=kind,
        country_codes=[country],
        legal_topics=[topic] if kind != "contact" else [],
        subject_text="employment law" if kind != "contact" else None,
        resolved_question=resolved,
    )


def advance(state, question, topic="Working Conditions", *,
            country="FR", explicit=None, kind="legal_information"):
    entries, contacts = _advance_legal_question_counts(
        state,
        [(action(topic, country, kind), [country])],
        question=question,
        explicit_legal_topics=explicit,
    )
    return ConversationState(legal_question_counts=entries), contacts


def get_counts(state):
    return {(e.country_code, e.legal_topic): e.count
            for e in state.legal_question_counts}


class TopicCounterAlignmentTests(unittest.TestCase):
    def test_paid_leave_overrides_wrong_working_conditions_classification(self):
        state, contacts = advance(
            None, "What is the statutory paid annual leave entitlement in France?")
        self.assertEqual(get_counts(state), {("FR", "Employee Benefits"): 1})
        self.assertEqual(contacts, set())

    def test_working_week_is_working_conditions(self):
        state, contacts = advance(None, "What is the normal statutory working week in France?")
        self.assertEqual(get_counts(state), {("FR", "Working Conditions"): 1})
        self.assertEqual(contacts, set())

    def test_wrong_topic_does_not_trigger_early_contact(self):
        state, _ = advance(None, "What is the normal statutory working week in France?")
        state, contacts = advance(
            state, "What is the statutory paid annual leave entitlement in France?")
        self.assertEqual(contacts, set())
        self.assertEqual(get_counts(state), {
            ("FR", "Working Conditions"): 1,
            ("FR", "Employee Benefits"): 1,
        })

    def test_second_paid_leave_triggers_only_then(self):
        state, _ = advance(None, "What is the statutory paid annual leave in France?")
        state, contacts = advance(state, "How much paid leave is required in France?")
        self.assertEqual(contacts, {"FR"})
        self.assertEqual(get_counts(state)[("FR", "Employee Benefits")], 2)
        self.assertNotIn(("FR", "Working Conditions"), get_counts(state))

    def test_multitopic_question_counts_both_once(self):
        question = ("Explain both the normal statutory working week and "
                    "paid annual leave entitlement in France.")
        state, contacts = advance(None, question)
        self.assertEqual(contacts, set())
        self.assertEqual(get_counts(state), {
            ("FR", "Working Conditions"): 1,
            ("FR", "Employee Benefits"): 1,
        })

    def test_multitopic_second_turn_triggers_existing_pair(self):
        state, _ = advance(None, "What is the normal statutory working week in France?")
        state, contacts = advance(state, "Compare working week and paid annual leave in France")
        self.assertEqual(contacts, {"FR"})
        self.assertEqual(get_counts(state)[("FR", "Employee Benefits")], 1)

    def test_unknown_phrase_preserves_understood_topic(self):
        state, _ = advance(None, "Please explain this point in more detail.")
        self.assertEqual(get_counts(state), {("FR", "Working Conditions"): 1})

    def test_ambiguous_notice_keeps_validated_topic(self):
        state, _ = advance(None,
            "What is the statutory notice period for termination in Spain?",
            topic="Termination of Employment Contracts", country="ES")
        self.assertEqual(get_counts(state), {
            ("ES", "Termination of Employment Contracts"): 1})

    def test_explicit_topics_override_detected_wording(self):
        state, _ = advance(None, "What is paid annual leave in France?",
                           explicit=["Working Conditions"])
        self.assertEqual(get_counts(state), {("FR", "Working Conditions"): 1})

    def test_interleaved_country_is_independent(self):
        state, _ = advance(None, "What is paid annual leave in France?")
        state, contacts = advance(state, "What is paid annual leave in Spain?", country="ES")
        self.assertEqual(contacts, set())
        self.assertEqual(get_counts(state), {
            ("FR", "Employee Benefits"): 1, ("ES", "Employee Benefits"): 1,
        })

    def test_contact_intent_does_not_increment(self):
        state, _ = advance(None, "What is paid annual leave in France?")
        state, contacts = advance(state, "Who do I contact in France?", kind="contact")
        self.assertEqual(contacts, set())
        self.assertEqual(get_counts(state)[("FR", "Employee Benefits")], 1)

    def test_new_conversation_resets_count(self):
        _, contacts = advance(None, "What is paid annual leave in France?")
        self.assertFalse(contacts)


if __name__ == "__main__":
    unittest.main()
