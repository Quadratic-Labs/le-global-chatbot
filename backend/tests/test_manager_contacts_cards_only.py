from __future__ import annotations

import inspect
import unittest

from app.routers import chat


class ManagerContactCardsOnlyTests(unittest.TestCase):
    def test_validated_card_has_intro_without_contact_data(self):
        answer = chat._contact_card_user_message(
            cards=[object()], country_codes=["IT"]
        )
        self.assertEqual(
            "For case-specific guidance, you can use "
            "the L&E Global contacts below:",
            answer,
        )
        self.assertNotIn("Member firm:", answer)
        self.assertNotIn("Email:", answer)

    def test_contact_source_without_card_does_not_promise_cards(self):
        answer = chat._contact_card_user_message(
            cards=[], country_codes=["GB"]
        )
        self.assertIn("United Kingdom", answer)
        self.assertIn("not currently available", answer)
        self.assertNotIn("below", answer)

    def test_multiple_countries_without_cards_never_invent_contacts(self):
        answer = chat._contact_card_user_message(
            cards=[], country_codes=["GB", "PT"]
        )
        self.assertNotIn("below", answer)
        self.assertNotIn("Member firm:", answer)
        self.assertIn("United Kingdom", answer)

    def test_no_country_has_neutral_fallback(self):
        answer = chat._contact_card_user_message(
            cards=[], country_codes=[]
        )
        self.assertIn("not currently available", answer)

    def test_full_pipeline_does_not_append_raw_contact_answer(self):
        implementation = inspect.getsource(chat._execute_resolved_plan)
        self.assertNotIn("answer_parts.append(contact_answer)", implementation)
        self.assertNotIn("answer_parts.append(fallback_contact_answer)", implementation)
        self.assertIn("contacts.extend(explicit_contact_cards)", implementation)
        self.assertIn("contacts.extend(fallback_contact_cards)", implementation)

    def test_direct_shortcut_never_serializes_raw_contact_text(self):
        implementation = inspect.getsource(chat._resolve_conservative_fallback)
        self.assertNotIn("answer=contact_answer", implementation)
        self.assertIn("_contact_card_user_message(", implementation)

    def test_unavailable_country_message_preserved(self):
        implementation = inspect.getsource(chat._execute_resolved_plan)
        self.assertIn("action_scope.unavailable_codes", implementation)
        self.assertIn("_unavailable_countries_answer(", implementation)

    def test_direct_shortcut_includes_unavailable_country_in_fallback(self):
        # Regression for test_chat.test_unavailable_contact_country_is_controlled.
        implementation = inspect.getsource(chat._resolve_conservative_fallback)
        self.assertIn("*current_country_scope.unavailable_codes", implementation)


if __name__ == "__main__":
    unittest.main()
