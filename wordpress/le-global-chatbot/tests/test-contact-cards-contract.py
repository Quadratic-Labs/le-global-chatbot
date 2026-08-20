import unittest
from pathlib import Path

R = Path("wordpress/le-global-chatbot")
JS = (R / "assets/chatbot.js").read_text()
PHP = (R / "le-global-chatbot.php").read_text()

class TestContactCards(unittest.TestCase):
    def test_answer_stays_safe(self):
        self.assertIn(
            'answerElement.textContent = turn.answer || "";',
            JS,
        )

    def test_contacts_enter_turn(self):
        self.assertIn(
            "turn.contacts = normalizePublicContacts(response.contacts);",
            JS,
        )

    def test_contacts_are_persisted(self):
        self.assertIn("assistantMessage.contacts", JS)
        self.assertIn("message.contacts", JS)

    def test_contact_cards_exist(self):
        self.assertIn("buildContactCardsSection", JS)

    def test_photo_endpoint_is_given_to_js(self):
        self.assertIn("data-contact-photo-endpoint", PHP)

    def test_photo_proxy_exists(self):
        self.assertIn("proxy_contact_photo", PHP)
        self.assertIn(
            "/api/v1/contact-photos/",
            PHP,
        )

if __name__ == "__main__":
    unittest.main()
