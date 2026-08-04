"""
Tests for the deterministic assistant-help/meta-intent detection layer
(mission "PATCH PRODUIT 0.4.3").

Exercises every positive example from the mission's sections 3-11 and
18, every false positive from sections 14 and 19, and the imperfect-
English forms from section 12 - each against detect_assistant_help_
intent directly (never resolve_legal_chat_response here; that is
covered separately in test_chat.py's route/continuity tests).
"""

from __future__ import annotations

import unittest

from app.services.assistant_help import (
    ASSISTANT_IDENTITY_ANSWER,
    build_assistant_help_answer,
    detect_assistant_help_intent,
)

_SUPPORTED_CODES = (
    "AR",
    "AU",
    "BE",
    "BR",
    "CZ",
    "GR",
    "IT",
    "JP",
    "MX",
    "PE",
    "PL",
    "RO",
    "SG",
    "ES",
    "SE",
    "CH",
    "GB",
)


def _detect(question: str):
    return detect_assistant_help_intent(question, _SUPPORTED_CODES)


class AssistantIdentityDetectionTests(unittest.TestCase):
    POSITIVE_QUESTIONS = (
        "Who are you?",
        "What are you?",
        "What is this chatbot?",
        "What assistant is this?",
        "What is your role?",
        "What's your role?",
        "Whats your role?",
        "What is your purpose?",
        "Why are you here?",
        "What do you do?",
    )

    def test_all_positive_phrasings_are_detected(self) -> None:
        for question in self.POSITIVE_QUESTIONS:
            with self.subTest(question=question):
                intent = _detect(question)
                self.assertIsNotNone(intent)
                self.assertEqual(intent.intent_type, "assistant_identity")

    def test_answer_mentions_required_elements(self) -> None:
        answer = build_assistant_help_answer(
            _detect("What is your role?"),
            original_question="What is your role?",
        )
        for required in (
            "L&E Global",
            "employment",
            "validated",
            "compare",
            "member",
            "legal information, not legal advice",
        ):
            with self.subTest(required=required):
                self.assertIn(required.casefold(), answer.casefold())

    def test_answer_is_the_exact_target_text(self) -> None:
        self.assertEqual(
            build_assistant_help_answer(
                _detect("Who are you?"), original_question="Who are you?"
            ),
            ASSISTANT_IDENTITY_ANSWER,
        )


class AssistantCapabilitiesDetectionTests(unittest.TestCase):
    POSITIVE_QUESTIONS = (
        "What can you do?",
        "What can you answer?",
        "What questions can you answer?",
        "What question can you answer?",
        "Whats question you can answer?",
        "What can I ask?",
        "What can I ask you?",
        "What can you help me with?",
        "How can you help?",
        "Show me what you can do.",
        "Help.",
        "Help me use this chatbot.",
        "What do you know about?",
    )

    def test_all_positive_phrasings_are_detected(self) -> None:
        for question in self.POSITIVE_QUESTIONS:
            with self.subTest(question=question):
                intent = _detect(question)
                self.assertIsNotNone(intent)
                self.assertEqual(
                    intent.intent_type, "assistant_capabilities"
                )

    def test_answer_never_shows_an_immense_list(self) -> None:
        answer = build_assistant_help_answer(
            _detect("What can you do?"),
            original_question="What can you do?",
        )
        self.assertLess(len(answer), 800)


class SupportedLegalTopicsDetectionTests(unittest.TestCase):
    POSITIVE_QUESTIONS = (
        "What topics do you cover?",
        "Which legal topics do you cover?",
        "What employment law themes can you answer?",
        "What themes are available?",
        "Which subjects can I ask about?",
        "What laws can you explain?",
        "List the available topics.",
    )

    def test_all_positive_phrasings_are_detected(self) -> None:
        for question in self.POSITIVE_QUESTIONS:
            with self.subTest(question=question):
                intent = _detect(question)
                self.assertIsNotNone(intent)
                self.assertEqual(
                    intent.intent_type, "supported_legal_topics"
                )

    def test_answer_lists_the_real_configured_topics(self) -> None:
        answer = build_assistant_help_answer(
            _detect("What topics do you cover?"),
            original_question="What topics do you cover?",
        )
        for topic in (
            "Hiring Practices",
            "Employment Contracts",
            "Working Conditions",
            "Anti-Discrimination Laws",
            "Pay Equity Laws",
            "Social Media and Data Privacy",
            "Termination of Employment Contracts",
            "Restrictive Covenants",
            "Transfer of Undertakings",
            "Trade Unions and Employers Associations",
            "Employee Benefits",
        ):
            with self.subTest(topic=topic):
                self.assertIn(topic, answer)

    def test_answer_never_promises_a_complete_answer(self) -> None:
        answer = build_assistant_help_answer(
            _detect("What topics do you cover?"),
            original_question="What topics do you cover?",
        )
        self.assertIn("do not contain enough direct information", answer)


class SupportedCountriesDetectionTests(unittest.TestCase):
    GENERAL_QUESTIONS = (
        "Which countries do you cover?",
        "What countries are supported?",
        "Which jurisdictions are available?",
        "Where can you answer employment law questions?",
        "List the countries.",
    )

    def test_all_general_phrasings_are_detected(self) -> None:
        for question in self.GENERAL_QUESTIONS:
            with self.subTest(question=question):
                intent = _detect(question)
                self.assertIsNotNone(intent)
                self.assertEqual(intent.intent_type, "supported_countries")
                self.assertEqual(intent.referenced_country_codes, ())

    def test_do_you_cover_spain_is_targeted_and_supported(self) -> None:
        intent = _detect("Do you cover Spain?")
        self.assertIsNotNone(intent)
        self.assertEqual(intent.intent_type, "supported_countries")
        self.assertEqual(intent.referenced_country_codes, ("ES",))
        answer = build_assistant_help_answer(
            intent, original_question="Do you cover Spain?"
        )
        self.assertIn("Yes", answer)
        self.assertIn("Spain", answer)

    def test_can_you_answer_about_peru_is_targeted(self) -> None:
        intent = _detect("Can you answer questions about Peru?")
        self.assertIsNotNone(intent)
        self.assertEqual(intent.referenced_country_codes, ("PE",))

    def test_is_australia_supported_is_targeted(self) -> None:
        intent = _detect("Is Australia supported?")
        self.assertIsNotNone(intent)
        self.assertEqual(intent.referenced_country_codes, ("AU",))

    def test_unsupported_country_gets_an_honest_negative_answer(
        self,
    ) -> None:
        intent = _detect("Do you cover France?")
        self.assertIsNotNone(intent)
        answer = build_assistant_help_answer(
            intent, original_question="Do you cover France?"
        )
        self.assertIn("do not currently have", answer)
        self.assertIn("France", answer)

    def test_never_claims_a_country_without_checking_real_config(
        self,
    ) -> None:
        # France is not in the 17-country supported set used by these
        # tests - the answer must say so, never "Yes".
        answer = build_assistant_help_answer(
            _detect("Do you cover France?"),
            original_question="Do you cover France?",
        )
        self.assertNotIn("Yes.", answer)


class ComparisonCapabilitiesDetectionTests(unittest.TestCase):
    GENERAL_QUESTIONS = (
        "Can you compare countries?",
        "What countries can you compare?",
        "What comparisons can you make?",
        "How does comparison work?",
        "How do comparisons work?",
        "How can I compare countries?",
        "What topics can you compare?",
        "Compare which countries?",
        "What do I need to provide for a comparison?",
        "What is required for a comparison?",
        "Can you make a multi-country comparison?",
    )

    def test_all_general_phrasings_are_detected(self) -> None:
        for question in self.GENERAL_QUESTIONS:
            with self.subTest(question=question):
                intent = _detect(question)
                self.assertIsNotNone(intent)
                self.assertEqual(
                    intent.intent_type, "comparison_capabilities"
                )

    def test_compare_spain_and_peru_with_no_topic_asks_for_one(
        self,
    ) -> None:
        for question in (
            "Can you compare Spain and Peru?",
            "Compare Spain and Peru.",
        ):
            with self.subTest(question=question):
                intent = _detect(question)
                self.assertIsNotNone(intent)
                self.assertEqual(intent.intent_type, "comparison_guidance")
                self.assertEqual(
                    set(intent.referenced_country_codes), {"ES", "PE"}
                )
                answer = build_assistant_help_answer(
                    intent, original_question=question
                )
                self.assertIn("Yes", answer)
                self.assertIn("Spain", answer)
                self.assertIn("Peru", answer)
                self.assertIn("topic", answer.casefold())

    def test_compare_australia_and_uk_with_no_topic_asks_for_one(
        self,
    ) -> None:
        intent = _detect(
            "Can you compare Australia with the United Kingdom?"
        )
        self.assertIsNotNone(intent)
        self.assertEqual(intent.intent_type, "comparison_guidance")
        self.assertEqual(
            set(intent.referenced_country_codes), {"AU", "GB"}
        )

    def test_compare_overtime_rules_is_a_real_comparison_not_meta(
        self,
    ) -> None:
        self.assertIsNone(
            _detect("Compare overtime rules in Spain and Peru.")
        )

    def test_compare_dismissal_notice_is_a_real_comparison_not_meta(
        self,
    ) -> None:
        self.assertIsNone(
            _detect(
                "Can you compare dismissal notice in Australia and Peru?"
            )
        )


class ComparisonLimitsDetectionTests(unittest.TestCase):
    QUESTIONS = (
        "What happens if one country has no information?",
        "Can you compare countries if one document is incomplete?",
        "Do comparisons use the same sources?",
        "How reliable are the comparisons?",
        "Can you compare different topics?",
        "Can you compare more than two countries?",
    )

    def test_all_phrasings_are_detected_as_comparison_capabilities(
        self,
    ) -> None:
        for question in self.QUESTIONS:
            with self.subTest(question=question):
                intent = _detect(question)
                self.assertIsNotNone(intent)
                self.assertEqual(
                    intent.intent_type, "comparison_capabilities"
                )

    def test_answer_explains_independent_sources_never_invents(
        self,
    ) -> None:
        answer = build_assistant_help_answer(
            _detect("What happens if one country has no information?"),
            original_question=(
                "What happens if one country has no information?"
            ),
        )
        self.assertIn("independently", answer)
        self.assertIn("infer or invent", answer)

    def test_never_states_a_maximum_country_count(self) -> None:
        answer = build_assistant_help_answer(
            _detect("Can you compare more than two countries?"),
            original_question="Can you compare more than two countries?",
        )
        self.assertNotRegex(answer, r"\bat most \d+ countries\b")


class ContactCapabilitiesDetectionTests(unittest.TestCase):
    GENERAL_QUESTIONS = (
        "Can you provide contacts?",
        "What contact information can you give?",
        "Can you give me a law firm contact?",
        "Can I ask for member firm contacts?",
        "Which contacts do you have?",
        "How do I get the contact for a country?",
    )

    def test_all_general_phrasings_are_detected(self) -> None:
        for question in self.GENERAL_QUESTIONS:
            with self.subTest(question=question):
                intent = _detect(question)
                self.assertIsNotNone(intent)
                self.assertEqual(
                    intent.intent_type, "contact_capabilities"
                )

    def test_targeted_contact_request_is_not_meta(self) -> None:
        self.assertIsNone(_detect("Can you give me the contact in Spain?"))


class QuestionExamplesDetectionTests(unittest.TestCase):
    POSITIVE_QUESTIONS = (
        "Give me examples.",
        "Show example questions.",
        "How should I ask a question?",
        "How do I use this chatbot?",
        "Give me a comparison example.",
        "Give me a contact example.",
        "What is a good question?",
        "How should I formulate my request?",
    )

    def test_all_positive_phrasings_are_detected(self) -> None:
        for question in self.POSITIVE_QUESTIONS:
            with self.subTest(question=question):
                intent = _detect(question)
                self.assertIsNotNone(intent)
                self.assertEqual(intent.intent_type, "question_examples")


class SourcesAndLimitationsDetectionTests(unittest.TestCase):
    SOURCE_QUESTIONS = (
        "What sources do you use?",
        "Where does your information come from?",
        "Do you use the internet?",
        "Are your answers legal advice?",
        "Can you answer from your own knowledge?",
    )
    LIMITATION_QUESTIONS = (
        "What are your limitations?",
        "What can't you answer?",
        "Can you answer questions outside employment law?",
        "Can you invent an answer if information is missing?",
    )

    def test_all_source_phrasings_are_detected(self) -> None:
        for question in self.SOURCE_QUESTIONS:
            with self.subTest(question=question):
                intent = _detect(question)
                self.assertIsNotNone(intent)
                self.assertEqual(intent.intent_type, "source_policy")

    def test_all_limitation_phrasings_are_detected(self) -> None:
        for question in self.LIMITATION_QUESTIONS:
            with self.subTest(question=question):
                intent = _detect(question)
                self.assertIsNotNone(intent)
                self.assertEqual(
                    intent.intent_type, "assistant_limitations"
                )

    def test_no_documentary_disclaimer_is_added(self) -> None:
        answer = build_assistant_help_answer(
            _detect("What sources do you use?"),
            original_question="What sources do you use?",
        )
        self.assertNotIn(
            "does not constitute legal advice", answer.casefold()
        )


class ImperfectEnglishDetectionTests(unittest.TestCase):
    CASES = (
        ("whats your role", "assistant_identity"),
        ("whats question you can answer", "assistant_capabilities"),
        ("what theme you cover", "supported_legal_topics"),
        ("which country you support", "supported_countries"),
        ("how comparison work", "comparison_capabilities"),
        ("give example", "question_examples"),
        ("what source you use", "source_policy"),
    )

    def test_all_imperfect_english_phrasings_are_detected(self) -> None:
        for question, expected_type in self.CASES:
            with self.subTest(question=question):
                intent = _detect(question)
                self.assertIsNotNone(intent, f"{question!r} -> None")
                self.assertEqual(intent.intent_type, expected_type)

    def test_can_compare_spain_peru_without_apostrophes(self) -> None:
        intent = _detect("can compare Spain Peru")
        self.assertIsNotNone(intent)
        self.assertIn(
            intent.intent_type,
            ("comparison_guidance", "comparison_capabilities"),
        )


class FalsePositiveDetectionTests(unittest.TestCase):
    """Section 14/19 - these must all stay legal (return None)."""

    LEGAL_QUESTIONS = (
        "What is the role of trade unions in Spain?",
        "What is the employer's role in workplace safety?",
        "Explain the role of employee representatives.",
        "What questions can an employer ask during an interview "
        "in Spain?",
        "What can an employer do during probation?",
        "What topics must be discussed with a works council?",
        "Can an employer compare employee salaries?",
        "What are the limits of a non-compete clause?",
        "What sources of law govern employment in Spain?",
        "Compare overtime rules in Spain and Peru.",
        "Can you compare dismissal notice in Australia and Peru?",
        "Give me the contact details in Spain.",
    )

    def test_all_legal_questions_return_none(self) -> None:
        for question in self.LEGAL_QUESTIONS:
            with self.subTest(question=question):
                self.assertIsNone(_detect(question))

    def test_can_you_compare_spain_and_peru_is_comparison_help(
        self,
    ) -> None:
        intent = _detect("Can you compare Spain and Peru?")
        self.assertIsNotNone(intent)
        self.assertEqual(intent.intent_type, "comparison_guidance")

    def test_can_you_compare_overtime_in_spain_and_peru_is_real(
        self,
    ) -> None:
        self.assertIsNone(
            _detect("Can you compare overtime rules in Spain and Peru?")
        )

    def test_can_you_give_me_contacts_is_contact_help(self) -> None:
        intent = _detect("Can you give me contacts?")
        self.assertIsNotNone(intent)
        self.assertEqual(intent.intent_type, "contact_capabilities")

    def test_can_you_give_me_the_contact_in_spain_is_real(self) -> None:
        self.assertIsNone(_detect("Can you give me the contact in Spain?"))


if __name__ == "__main__":
    unittest.main()
