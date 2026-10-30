"""Client wording and country-specific referral regression tests."""
import unittest
from unittest import mock
from app.models.chat import LegalChatContact, LegalContactReferral
from app.routers import chat


class ContactReferralTests(unittest.TestCase):
    def test_exact_client_wording(self):
        self.assertEqual(
            chat.CONTACT_REFERRAL_SENTENCE,
            "There are nuances and exceptions that might impact the answer "
            "based on facts not provided so we recommend that you contact "
            "the L&E Global member firm.",
        )

    def test_comparison_is_generic_and_does_not_look_up_people(self):
        with mock.patch.object(chat, "build_legal_chat_contacts") as lookup:
            referral = chat._legal_contact_referral(
                country_codes=["FR", "DE"],
                legal_sources=[], displayed_cards=[],
            )
        self.assertEqual(referral, LegalContactReferral())
        lookup.assert_not_called()

    def test_single_country_uses_validated_contact_person_and_email(self):
        person = LegalChatContact(
            contact_id="known-id", country_code="IT",
            contact_person="Example Lawyer", email="lawyer@example.org",
        )
        with mock.patch.object(chat, "_optional_contact_source_directory", return_value="/tmp/fixture"), mock.patch.object(
            chat, "build_legal_chat_contacts", return_value=[person],
        ) as lookup:
            referral = chat._legal_contact_referral(
                country_codes=["IT"], legal_sources=[], displayed_cards=[],
            )
        self.assertEqual(referral.contact_person, "Example Lawyer")
        self.assertEqual(referral.email, "lawyer@example.org")
        lookup.assert_called_once()

    def test_card_takes_priority_and_does_not_duplicate_personal_details(self):
        with mock.patch.object(chat, "build_legal_chat_contacts") as lookup:
            referral = chat._legal_contact_referral(
                country_codes=["IT"], legal_sources=[], displayed_cards=["card"],
            )
        self.assertEqual(referral, LegalContactReferral())
        lookup.assert_not_called()

    def test_no_sidecar_never_invents_an_email(self):
        with mock.patch.object(chat, "_optional_contact_source_directory", return_value=None):
            referral = chat._legal_contact_referral(
                country_codes=["IT"], legal_sources=[], displayed_cards=[],
            )
        self.assertIsNone(referral.email)
        self.assertIsNone(referral.contact_person)

    def test_missing_email_never_renders_incomplete_identity(self):
        person = LegalChatContact(
            contact_id="known", country_code="FR", contact_person="Example", email=None,
        )
        with mock.patch.object(chat, "_optional_contact_source_directory", return_value="/tmp/fixture"), mock.patch.object(
            chat, "build_legal_chat_contacts", return_value=[person],
        ):
            referral = chat._legal_contact_referral(
                country_codes=["FR"], legal_sources=[], displayed_cards=[],
            )
        self.assertIsNone(referral.contact_person)
        self.assertIsNone(referral.email)

if __name__ == "__main__":
    unittest.main()
