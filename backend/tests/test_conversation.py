"""
Consolidated tests for conversation state, conversation metadata,
conversation transitions, contextual follow-ups and historical
conversation regressions.

Generated during test-suite rationalisation from the previously
independent conversation-domain test modules. Top-level bindings from
each former module are namespaced so each original test keeps its own
fixtures and semantics without test-to-test imports.
"""

from __future__ import annotations



# ====================================================================
# SOURCE DOMAIN: test_contextual_multi_country_contact.py
# ====================================================================


import unittest as _ctx_unittest
from types import SimpleNamespace as _ctx_SimpleNamespace
from unittest import mock as _ctx_mock
from app.models.catalog import LegalCatalogCountry as _ctx_LegalCatalogCountry, LegalCatalogResponse as _ctx_LegalCatalogResponse
from app.models.chat import LegalChatRequest as _ctx_LegalChatRequest
from app.models.conversation_state import ConversationActionState as _ctx_ConversationActionState, ConversationSearchConcept as _ctx_ConversationSearchConcept, ConversationState as _ctx_ConversationState
from app.routers.chat import resolve_legal_chat_response as _ctx_resolve_legal_chat_response
from app.services.conversation_transition import apply_conversation_transition as _ctx_apply_conversation_transition, resolve_contextual_multi_country_contact_codes as _ctx_resolve_contextual_multi_country_contact_codes
from app.services.request_understanding import CurrentMessageDelta as _ctx_CurrentMessageDelta, DeterministicHints as _ctx_DeterministicHints, RequestUnderstandingResult as _ctx_RequestUnderstandingResult

def _ctx_comparison_state(country_codes: list[str]) -> _ctx_ConversationState:
    return _ctx_ConversationState(version=1, actions=[_ctx_ConversationActionState(type='comparison', country_codes=country_codes, legal_topics=['Anti-Discrimination Laws'], subject_text='anti-discrimination laws', search_concepts=[_ctx_ConversationSearchConcept(terms=['anti-discrimination'])], subject_specificity='specific', evidence_mode='direct_topic')], focus_action_index=0, ordered_country_codes=country_codes, pending_clarification=None)

def _ctx_semantic_clarification() -> _ctx_RequestUnderstandingResult:
    return _ctx_RequestUnderstandingResult(status='clarification', actions=[], is_follow_up=True, confidence=0.5, clarification_reason='ambiguous_request', current_message_delta=_ctx_CurrentMessageDelta(explicit_action_types=[], explicit_country_codes=[], explicit_legal_topics=[], explicit_subject_text=None, context_operation='ambiguous'))

class _ctx_ContextualCountryResolverTests(_ctx_unittest.TestCase):

    def test_exact_real_user_phrase_resolves_both(self) -> None:
        state = _ctx_comparison_state(['FR', 'JP'])
        self.assertEqual(_ctx_resolve_contextual_multi_country_contact_codes('Can you give me contact from both country to go further?', state), ['FR', 'JP'])

    def test_common_both_variants(self) -> None:
        state = _ctx_comparison_state(['FR', 'JP'])
        for question in ('Can I have the contacts for both?', 'Give me both contacts.', 'Give me the contacts for these two countries.', 'Who can I contact in both countries?'):
            with self.subTest(question=question):
                self.assertEqual(_ctx_resolve_contextual_multi_country_contact_codes(question, state), ['FR', 'JP'])

    def test_all_three_uses_all_three(self) -> None:
        state = _ctx_comparison_state(['FR', 'JP', 'IT'])
        self.assertEqual(_ctx_resolve_contextual_multi_country_contact_codes('Give me the contacts for all three.', state), ['FR', 'JP', 'IT'])

    def test_both_never_guesses_inside_three_country_state(self) -> None:
        state = _ctx_comparison_state(['FR', 'JP', 'IT'])
        self.assertEqual(_ctx_resolve_contextual_multi_country_contact_codes('Give me the contacts for both countries.', state), [])

    def test_no_state_never_fabricates_countries(self) -> None:
        self.assertIsNone(_ctx_resolve_contextual_multi_country_contact_codes('Give me both contacts.', None))

    def test_non_contact_followup_is_not_captured(self) -> None:
        state = _ctx_comparison_state(['FR', 'JP'])
        self.assertIsNone(_ctx_resolve_contextual_multi_country_contact_codes('Are the penalties the same in both countries?', state))

class _ctx_TransitionTests(_ctx_unittest.TestCase):

    def test_semantic_clarification_is_corrected_to_contact(self) -> None:
        state = _ctx_comparison_state(['FR', 'JP'])
        outcome = _ctx_apply_conversation_transition(result=_ctx_semantic_clarification(), conversation_state=state, hints=_ctx_DeterministicHints(), current_question='Can you give me contact from both country to go further?')
        self.assertEqual(outcome.final_status, 'resolved')
        self.assertEqual(len(outcome.final_actions), 1)
        self.assertEqual(outcome.final_actions[0].type, 'contact')
        self.assertEqual(outcome.final_actions[0].country_codes, ['FR', 'JP'])

class _ctx_RouterIntegrationTests(_ctx_unittest.TestCase):

    def test_help_does_not_swallow_contextual_contact_request(self) -> None:
        state = _ctx_comparison_state(['FR', 'JP'])

        def catalog() -> _ctx_LegalCatalogResponse:
            return _ctx_LegalCatalogResponse(countries=[_ctx_LegalCatalogCountry(country_code='FR', country='France', chunk_count=10), _ctx_LegalCatalogCountry(country_code='JP', country='Japan', chunk_count=10)], legal_topics=[], subsections=[])
        semantic_outcome = _ctx_SimpleNamespace(result=_ctx_semantic_clarification(), elapsed_ms=0.0, openai_ms=0.0, attempts=1, retry_triggered=False, retry_reason=None, error=None)
        captured: list[list[str]] = []

        def fake_contact_section(*, country_codes, unavailable_country_codes, citation_offset):
            captured.append(list(country_codes))
            return ('France\nI could not find a validated L&E Global contact for France in the available documents.\n\nJapan\nMember firm: Atsumi & Sakai', [], 0, 0.0)
        with _ctx_mock.patch('app.routers.chat.understand_request', return_value=semantic_outcome) as semantic_mock, _ctx_mock.patch('app.routers.chat._build_contact_section', side_effect=fake_contact_section):
            response = _ctx_resolve_legal_chat_response(_ctx_LegalChatRequest(question='Can you give me contact from both country to go further?', conversation_state=state), catalog_provider=catalog, document_topic_provider=lambda codes: {code: [] for code in codes})
        semantic_mock.assert_called_once()
        self.assertEqual(captured, [['FR', 'JP']])
        self.assertIn('France', response.answer)
        self.assertIn('Japan', response.answer)
        self.assertNotIn('Please specify the country', response.answer)
        self.assertIsNotNone(response.conversation_state)
        self.assertEqual(response.conversation_state.actions[0].type, 'contact')
        self.assertEqual(response.conversation_state.actions[0].country_codes, ['FR', 'JP'])



# ====================================================================
# SOURCE DOMAIN: test_conversation_meta.py
# ====================================================================


import json as _meta_json
import unittest as _meta_unittest
from types import SimpleNamespace as _meta_SimpleNamespace
from typing import Any as _meta_Any
from unittest import mock as _meta_mock
from app.clients.openai_responses import GeneratedText as _meta_GeneratedText
from app.models.catalog import LegalCatalogCountry as _meta_LegalCatalogCountry, LegalCatalogResponse as _meta_LegalCatalogResponse
from app.models.chat import LegalChatRequest as _meta_LegalChatRequest
from app.models.search import LegalSearchResponse as _meta_LegalSearchResponse
from app.routers.chat import resolve_legal_chat_response as _meta_resolve_legal_chat_response
from app.services.conversation_meta import append_personalised_legal_caution as _meta_append_personalised_legal_caution, build_comparison_country_limit_answer as _meta_build_comparison_country_limit_answer, requires_personalised_legal_caution as _meta_requires_personalised_legal_caution, resolve_ambiguous_city_followup_question as _meta_resolve_ambiguous_city_followup_question, resolve_conversation_meta as _meta_resolve_conversation_meta

def _meta_document_topic_provider(country_codes: list[str]) -> dict[str, list[str]]:
    """
    Fake DocumentLegalTopicsProvider - mission "ORDER 8F-A" - no live
    document legal topics for any country, matching every test in this
    file written before that mission (none of them concern the new
    document_legal_topics concept).
    """
    return {}

class _meta_FakeUnderstandingClient:
    """Minimal test double for the semantic-understanding client -
    only what AmbiguousCityFollowupResumeTests' own end-to-end test
    needs (see test_chat.py's own, fuller FakeUnderstandingClient for
    the canonical version)."""

    def __init__(self, payload: dict[str, _meta_Any]) -> None:
        self.payload = payload
        self.call_count = 0

    def generate(self, instructions: str, input_text: str, text_format: dict[str, _meta_Any] | None=None) -> _meta_GeneratedText:
        self.call_count += 1
        return _meta_GeneratedText(text=_meta_json.dumps(self.payload), model='test-model')

def _meta_catalog() -> _meta_LegalCatalogResponse:
    return _meta_LegalCatalogResponse(countries=[_meta_LegalCatalogCountry(country_code='ES', country='Spain', chunk_count=50), _meta_LegalCatalogCountry(country_code='IT', country='Italy', chunk_count=40), _meta_LegalCatalogCountry(country_code='GB', country='United Kingdom', chunk_count=45)], legal_topics=[], subsections=[])

def _meta_resolution(question: str, *, history=None, conversation_state=None):
    return _meta_resolve_conversation_meta(question=question, history=history or [], conversation_state=conversation_state, catalog_provider=_meta_catalog)

class _meta_SmallTalkTests(_meta_unittest.TestCase):

    def test_greeting_is_natural(self) -> None:
        result = _meta_resolution('Hello')
        self.assertIsNotNone(result)
        self.assertEqual(result.intent_type, 'greeting')
        self.assertIn('Hello', result.answer)
        self.assertNotIn('specify the country', result.answer.casefold())

    def test_wellbeing_is_natural(self) -> None:
        result = _meta_resolution('How are u?')
        self.assertEqual(result.intent_type, 'wellbeing')
        self.assertIn('doing well', result.answer.casefold())

    def test_gratitude_is_natural(self) -> None:
        result = _meta_resolution('Thank you')
        self.assertEqual(result.intent_type, 'gratitude')
        self.assertIn('welcome', result.answer.casefold())

    def test_extended_gratitude_is_natural(self) -> None:
        for question in ('Thank you, that was helpful.', 'Thank you im fine'):
            with self.subTest(question=question):
                result = _meta_resolution(question)
                self.assertEqual(result.intent_type, 'gratitude')
                self.assertIn('welcome', result.answer.casefold())

    def test_acknowledgements_are_natural(self) -> None:
        for question in ('aaa ok', 'no problem'):
            with self.subTest(question=question):
                result = _meta_resolution(question)
                self.assertEqual(result.intent_type, 'acknowledgement')
                self.assertNotIn('clarify', result.answer.casefold())

    def test_gratitude_and_farewell_is_natural(self) -> None:
        result = _meta_resolution("Thanks, that's all for now.")
        self.assertEqual(result.intent_type, 'farewell')
        self.assertIn('nice day', result.answer.casefold())

    def test_smalltalk_does_not_capture_a_legal_question(self) -> None:
        self.assertIsNone(_meta_resolution('How are employees paid for overtime in Spain?'))

class _meta_CapabilityTests(_meta_unittest.TestCase):

    def test_explain_role_is_recognised(self) -> None:
        result = _meta_resolution('Explain me your role')
        self.assertEqual(result.intent_type, 'assistant_identity')
        self.assertIn('L&E Global', result.answer)

    def test_can_you_help_me_is_recognised(self) -> None:
        result = _meta_resolution('Can you help me?')
        self.assertEqual(result.intent_type, 'assistant_capabilities')
        self.assertIn('compare', result.answer.casefold())

    def test_what_else_uses_capability_history(self) -> None:
        result = _meta_resolution('What else?', history=[{'role': 'user', 'content': 'How can u help me?'}, {'role': 'assistant', 'content': 'I provide employment-law information, compare countries and give member-firm contact details.'}])
        self.assertEqual(result.intent_type, 'capability_followup')
        self.assertIn('available countries', result.answer.casefold())

    def test_what_else_without_history_is_not_guessed(self) -> None:
        self.assertIsNone(_meta_resolution('What else?'))

    def test_what_else_can_you_help_me_with_is_recognised(self) -> None:
        result = _meta_resolution('What else can you help me with?')
        self.assertEqual(result.intent_type, 'assistant_capabilities')
        self.assertIn('compare', result.answer.casefold())

    def test_comparison_capability_variant(self) -> None:
        result = _meta_resolution('Can you do comparisons too?')
        self.assertEqual(result.intent_type, 'comparison_capabilities')

class _meta_CatalogueTests(_meta_unittest.TestCase):

    def test_country_catalogue_is_dynamic(self) -> None:
        result = _meta_resolution('Which countries do you support?')
        self.assertEqual(result.intent_type, 'supported_countries')
        self.assertIn('3 countries', result.answer)
        self.assertIn('Spain', result.answer)
        self.assertNotIn('Australia', result.answer)

    def test_imperfect_country_count_question(self) -> None:
        result = _meta_resolution('How many country do u have?')
        self.assertEqual(result.intent_type, 'supported_countries')
        self.assertIn('3 countries', result.answer)

    def test_imperfect_country_list_question(self) -> None:
        result = _meta_resolution('whitch country u can help for')
        self.assertEqual(result.intent_type, 'supported_countries')

    def test_concise_topic_catalogue_question(self) -> None:
        result = _meta_resolution('whitch topics')
        self.assertEqual(result.intent_type, 'supported_legal_topics')

    def test_concise_country_catalogue_followup(self) -> None:
        result = _meta_resolution('And countries?', history=[{'role': 'assistant', 'content': 'You can ask about the following canonical employment-law topics.'}])
        self.assertEqual(result.intent_type, 'supported_countries')
        self.assertIn('3 countries', result.answer)

    def test_targeted_unavailable_country(self) -> None:
        result = _meta_resolution('Do you support Tunisia?')
        self.assertEqual(result.intent_type, 'targeted_country_availability')
        self.assertIn('Tunisia', result.answer)
        self.assertIn('do not currently have', result.answer)
        self.assertIn('Tunisia is a valid country', result.answer)
        self.assertIn('Would you like to see the countries currently covered?', result.answer)

    def test_targeted_availability_matches_country_between_verb_and_adjective(self) -> None:
        for question in ('Is Tunisia supported?', 'Is Tunisia available?', 'Are Tunisia and Algeria supported?'):
            with self.subTest(question=question):
                result = _meta_resolution(question)
                self.assertEqual(result.intent_type, 'targeted_country_availability')
                self.assertIn('do not currently have', result.answer)
        available = _meta_resolution('Is Spain supported?')
        self.assertEqual(available.intent_type, 'targeted_country_availability')
        self.assertIn('Spain is currently available', available.answer)

    def test_yes_after_unavailable_country_shows_dynamic_coverage_list(self) -> None:
        offer = _meta_resolution('Do you support Algeria?')
        result = _meta_resolution('Yes', history=[{'role': 'user', 'content': 'Do you support Algeria?'}, {'role': 'assistant', 'content': offer.answer}])
        self.assertEqual(result.intent_type, 'coverage_list_followup')
        self.assertIn('3 countries', result.answer)
        self.assertIn('Spain', result.answer)
        self.assertIn('Italy', result.answer)
        self.assertIn('United Kingdom', result.answer)

    def test_coverage_list_followup_state_is_not_stuck(self) -> None:
        offer = _meta_resolution('Do you support Algeria?')
        shown = _meta_resolution('Yes', history=[{'role': 'user', 'content': 'Do you support Algeria?'}, {'role': 'assistant', 'content': offer.answer}])
        result = _meta_resolution('Yes', history=[{'role': 'user', 'content': 'Do you support Algeria?'}, {'role': 'assistant', 'content': offer.answer}, {'role': 'user', 'content': 'Yes'}, {'role': 'assistant', 'content': shown.answer}, {'role': 'user', 'content': 'Do you have topics too?'}, {'role': 'assistant', 'content': 'You can ask about the following canonical employment-law topics.'}])
        self.assertNotEqual(result.intent_type if result else None, 'coverage_list_followup')

    def test_coverage_list_reflects_catalog_changes(self) -> None:

        def catalog_without_spain() -> _meta_LegalCatalogResponse:
            return _meta_LegalCatalogResponse(countries=[_meta_LegalCatalogCountry(country_code='IT', country='Italy', chunk_count=40), _meta_LegalCatalogCountry(country_code='GB', country='United Kingdom', chunk_count=45)], legal_topics=[], subsections=[])
        offer = _meta_resolve_conversation_meta(question='Do you support Algeria?', history=[], conversation_state=None, catalog_provider=catalog_without_spain)
        result = _meta_resolve_conversation_meta(question='Yes', history=[{'role': 'user', 'content': 'Do you support Algeria?'}, {'role': 'assistant', 'content': offer.answer}], conversation_state=None, catalog_provider=catalog_without_spain)
        self.assertIn('2 countries', result.answer)
        self.assertNotIn('Spain', result.answer)

    def test_embedded_country_typo_is_suggested(self) -> None:
        result = _meta_resolution('Do you have data for Egypte?', history=[{'role': 'user', 'content': 'What about France?'}, {'role': 'assistant', 'content': 'France is not currently available.'}, {'role': 'user', 'content': 'What about Tunisia?'}], conversation_state=_meta_SimpleNamespace(pending_clarification=None))
        self.assertEqual(result.intent_type, 'country_suggestion')
        self.assertIn('Did you mean Egypt', result.answer)
        self.assertNotIn('France', result.answer)
        self.assertNotIn('Tunisia', result.answer)
        self.assertFalse(result.preserve_conversation_state)

    def test_country_correction_inside_natural_phrase(self) -> None:
        result = _meta_resolution('No, I ask about tunsie')
        self.assertEqual(result.intent_type, 'country_suggestion')
        self.assertIn('Did you mean Tunisia', result.answer)

    def test_yes_confirms_the_latest_country_suggestion(self) -> None:
        result = _meta_resolution('yes', history=[{'role': 'assistant', 'content': 'Did you mean Tunisia? Tunisia is not currently available in the validated corpus.'}])
        self.assertEqual(result.intent_type, 'targeted_country_availability')
        self.assertIn('Tunisia is a valid country', result.answer)
        self.assertFalse(result.preserve_conversation_state)

    def test_mixed_supported_and_unsupported_comparison(self) -> None:
        result = _meta_resolution('Can you compare Italy and Tunisia?')
        self.assertEqual(result.intent_type, 'unsupported_comparison')
        self.assertIn('Italy is currently available', result.answer)
        self.assertIn('Tunisia', result.answer)
        self.assertIn('cannot produce a reliable comparison', result.answer)

    def test_country_pair_after_compare_history(self) -> None:
        result = _meta_resolution('Italy and Tunisia', history=[{'role': 'user', 'content': 'Compare'}, {'role': 'assistant', 'content': 'Which countries would you like to compare?'}])
        self.assertEqual(result.intent_type, 'unsupported_comparison')

    def test_topic_catalogue(self) -> None:
        result = _meta_resolution('Give me all the topics that you can compare')
        self.assertEqual(result.intent_type, 'supported_legal_topics')
        self.assertIn('Hiring Practices', result.answer)
        self.assertIn('Employee Benefits', result.answer)

    def test_country_topic_catalogue(self) -> None:
        result = _meta_resolution('Which topics are available for Spain?')
        self.assertEqual(result.intent_type, 'country_legal_topics')
        self.assertIn('For Spain', result.answer)

    def test_contact_catalogue(self) -> None:
        result = _meta_resolution('Show all available contacts.')
        self.assertEqual(result.intent_type, 'contact_catalogue')
        self.assertIn('Spain', result.answer)

class _meta_ConversationRegressionTests(_meta_unittest.TestCase):

    def test_topic_catalogue_preserves_existing_state(self) -> None:
        state = _meta_SimpleNamespace(pending_clarification=None)
        result = _meta_resolution('What topics can you compare?', conversation_state=state)
        self.assertIsNotNone(result)
        self.assertEqual(result.intent_type, 'supported_legal_topics')
        self.assertTrue(result.preserve_conversation_state)

    def test_legal_comparison_with_unavailable_country(self) -> None:
        result = _meta_resolution('Compare overtime rules in Spain and France.')
        self.assertIsNone(result)

    def test_supported_country_answer_starts_with_yes(self) -> None:
        result = _meta_resolution('Do you support Spain?')
        self.assertIsNotNone(result)
        self.assertIn('Yes', result.answer)

    def test_unsupported_country_uses_product_wording(self) -> None:
        result = _meta_resolution('Do you support France?')
        self.assertIsNotNone(result)
        self.assertIn('do not currently have', result.answer)

class _meta_CorrectionAndGuardTests(_meta_unittest.TestCase):

    def test_country_typo_suggests_spain(self) -> None:
        result = _meta_resolution('sapin')
        self.assertEqual(result.intent_type, 'country_suggestion')
        self.assertIn('Did you mean Spain', result.answer)

    def test_reset_clears_context(self) -> None:
        result = _meta_resolution('Forget my previous question.')
        self.assertEqual(result.intent_type, 'reset')
        self.assertFalse(result.preserve_conversation_state)

    def test_personalised_request_needs_caution(self) -> None:
        self.assertTrue(_meta_requires_personalised_legal_caution('Should I fire this employee in Spain?'))
        answer = _meta_append_personalised_legal_caution('General dismissal information.')
        self.assertIn('not advice for a specific case', answer)

    def test_comparison_limit_is_user_friendly(self) -> None:
        action = _meta_SimpleNamespace(type='comparison', country_codes=['ES', 'IT', 'GB'])
        answer = _meta_build_comparison_country_limit_answer([action], 2)
        self.assertIsNotNone(answer)
        self.assertIn('up to 2 countries', answer)
        self.assertNotIn('max_sources', answer)

class _meta_RouterIntegrationTests(_meta_unittest.TestCase):

    def test_smalltalk_short_circuits_before_catalogue(self) -> None:

        def forbidden_catalog():
            raise AssertionError('The catalogue must not be called.')
        response = _meta_resolve_legal_chat_response(_meta_LegalChatRequest(question='Hello', language='en'), catalog_provider=forbidden_catalog, document_topic_provider=_meta_document_topic_provider)
        self.assertEqual(response.grounded, False)
        self.assertEqual(response.retrieval_total, 0)
        self.assertEqual(response.sources, [])
        self.assertIn('Hello', response.answer)

    def test_country_catalogue_uses_active_catalogue(self) -> None:
        response = _meta_resolve_legal_chat_response(_meta_LegalChatRequest(question='Which countries do you support?', language='en'), catalog_provider=_meta_catalog, document_topic_provider=_meta_document_topic_provider)
        self.assertEqual(response.grounded, False)
        self.assertIn('3 countries', response.answer)
        self.assertIn('Italy', response.answer)

class _meta_AmbiguousCityClarificationTests(_meta_unittest.TestCase):
    """
    Corrective gate, section 9 - a genuinely ambiguous city name (real
    geonamescache data: Barcelona, Spain / Barcelona, Venezuela at a
    ~2x population ratio) asks a specific clarifying question rather
    than falling through to the generic "specify a country" prompt,
    and never hijacks a real multi-country comparison (which is also
    AMBIGUOUS from resolve_jurisdiction's own point of view, but for
    an entirely different, explicit-countries reason).
    """

    def test_ambiguous_city_alone_asks_which_country(self) -> None:
        result = _meta_resolution('social media rules at work in Barcelona')
        self.assertEqual(result.intent_type, 'ambiguous_city_clarification')
        self.assertIn('Barcelona', result.answer)
        self.assertIn('Spain', result.answer)
        self.assertIn('Venezuela', result.answer)

    def test_explicit_country_is_never_hijacked_as_ambiguous(self) -> None:
        result = _meta_resolution('Barcelona, Spain')
        self.assertIsNone(result)

    def test_two_country_comparison_is_never_hijacked(self) -> None:
        result = _meta_resolution('Compare Spain and Italy')
        self.assertIsNone(result)

class _meta_NoRedundantCountryDetectionCallTests(_meta_unittest.TestCase):
    """
    Adversarial-review finding, corrective gate: the ambiguous-city/
    unknown-locality check used to call the full resolve_jurisdiction,
    which re-derives explicit countries from scratch even though
    resolve_conversation_meta's own country_codes (already confirmed
    empty at that point) came from the exact same ~400-precompiled-
    regex scan moments earlier - a real, measured ~2x-per-call
    overhead. Going straight to the city-only primitives instead must
    call the explicit-country scan exactly once per request.
    """

    def test_detect_mentioned_country_codes_called_once(self) -> None:
        import app.services.conversation_meta as conversation_meta_module
        import app.services.country_detection as country_detection_module
        call_count = 0
        original = country_detection_module.detect_mentioned_country_codes

        def counting(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return original(*args, **kwargs)
        with _meta_mock.patch.object(country_detection_module, 'detect_mentioned_country_codes', side_effect=counting), _meta_mock.patch.object(conversation_meta_module, 'detect_mentioned_country_codes', side_effect=counting):
            _meta_resolve_conversation_meta(question='What are the general employment law considerations for probation, notice, and severance, without naming any specific place?', history=[], conversation_state=None, catalog_provider=_meta_catalog)
        self.assertEqual(call_count, 1)

class _meta_AmbiguousCityFollowupResumeTests(_meta_unittest.TestCase):
    """
    Corrective gate, section 9 - "User: Spain" after the clarification
    above resumes the ORIGINAL question with Spain substituted for the
    ambiguous city, entirely as plain text rewriting (this module has
    no RAG access of its own).
    """

    def test_naming_an_offered_country_rebuilds_the_question(self) -> None:
        offer = _meta_resolution('social media rules at work in Barcelona')
        resumed = _meta_resolve_ambiguous_city_followup_question(question='Spain', history=[{'role': 'user', 'content': 'social media rules at work in Barcelona'}, {'role': 'assistant', 'content': offer.answer}])
        self.assertIsNotNone(resumed)
        self.assertIn('Spain', resumed)
        self.assertNotIn('Barcelona', resumed)
        self.assertIn('social media rules at work', resumed)

    def test_naming_an_unoffered_country_does_not_resume(self) -> None:
        offer = _meta_resolution('social media rules at work in Barcelona')
        resumed = _meta_resolve_ambiguous_city_followup_question(question='Italy', history=[{'role': 'user', 'content': 'social media rules at work in Barcelona'}, {'role': 'assistant', 'content': offer.answer}])
        self.assertIsNone(resumed)

    def test_unrelated_later_turn_never_resumes(self) -> None:
        offer = _meta_resolution('social media rules at work in Barcelona')
        resumed = _meta_resolve_ambiguous_city_followup_question(question='Spain', history=[{'role': 'user', 'content': 'social media rules at work in Barcelona'}, {'role': 'assistant', 'content': offer.answer}, {'role': 'user', 'content': 'Thanks, one more thing'}, {'role': 'assistant', 'content': 'Sure, go ahead.'}])
        self.assertIsNone(resumed)

    def test_no_prior_offer_never_resumes(self) -> None:
        resumed = _meta_resolve_ambiguous_city_followup_question(question='Spain', history=[])
        self.assertIsNone(resumed)

    def test_end_to_end_through_the_router_resolves_spain(self) -> None:
        offer_response = _meta_resolve_legal_chat_response(_meta_LegalChatRequest(question='Who can help me in Barcelona?'), catalog_provider=_meta_catalog, document_topic_provider=_meta_document_topic_provider)
        self.assertIn('Barcelona', offer_response.answer)
        understanding_client = _meta_FakeUnderstandingClient(payload={'status': 'resolved', 'actions': [{'type': 'contact', 'country_codes': ['ES'], 'legal_topics': [], 'topic_text': None, 'resolved_question': None}], 'is_follow_up': False, 'confidence': 0.9, 'clarification_reason': None, 'current_message_delta': {'explicit_action_types': ['contact'], 'explicit_country_codes': ['ES'], 'explicit_legal_topics': [], 'explicit_subject_text': None, 'context_operation': 'independent'}})
        with _meta_mock.patch('app.routers.chat.search_contact_chunks', return_value=_meta_LegalSearchResponse(query='', total=0, limit=20, offset=0, took_ms=1, hits=[])):
            resumed_response = _meta_resolve_legal_chat_response(_meta_LegalChatRequest(question='Spain', history=[{'role': 'user', 'content': 'Who can help me in Barcelona?'}, {'role': 'assistant', 'content': offer_response.answer}]), catalog_provider=_meta_catalog, document_topic_provider=_meta_document_topic_provider, understanding_client=understanding_client)
        self.assertIn('Spain', resumed_response.question)
        self.assertNotIn('Barcelona', resumed_response.question)
        self.assertEqual(understanding_client.call_count, 1)

class _meta_UnknownLocalityClarificationTests(_meta_unittest.TestCase):
    """
    Corrective gate, section 11 - a question that clearly names a
    place the dataset does not recognize must ask which country it is
    in, never fabricate a country, and never call the internet or an
    LLM to resolve it.
    """

    def test_unrecognized_place_asks_which_country(self) -> None:
        result = _meta_resolution('employment law in Ruritania')
        self.assertEqual(result.intent_type, 'unknown_locality_clarification')
        self.assertIn('Ruritania', result.answer)
        self.assertIn('Which country', result.answer)

    def test_no_location_at_all_is_not_hijacked(self) -> None:
        result = _meta_resolution('What are the rules on termination?')
        self.assertIsNone(result)



# ====================================================================
# SOURCE DOMAIN: test_conversation_regressions.py
# ====================================================================


import ast as _reg_ast
import inspect as _reg_inspect
import unittest as _reg_unittest
from typing import Any as _reg_Any
from unittest import mock as _reg_mock
from app.clients.openai_responses import GeneratedText as _reg_GeneratedText
from app.core.country_registry import COUNTRIES as _reg_COUNTRIES
from app.models.catalog import LegalCatalogCountry as _reg_LegalCatalogCountry, LegalCatalogResponse as _reg_LegalCatalogResponse
from app.models.chat import LegalChatRequest as _reg_LegalChatRequest
from app.models.search import LegalSearchHit as _reg_LegalSearchHit, LegalSearchResponse as _reg_LegalSearchResponse
from app.routers.chat import CONTACT_CLARIFICATION_ANSWER as _reg_CONTACT_CLARIFICATION_ANSWER, CLARIFICATION_LEGAL_MISSING_COUNTRY_ANSWER as _reg_CLARIFICATION_LEGAL_MISSING_COUNTRY_ANSWER, CLARIFICATION_UNSUPPORTED_REQUEST_ANSWER as _reg_CLARIFICATION_UNSUPPORTED_REQUEST_ANSWER, resolve_legal_chat_response as _reg_resolve_legal_chat_response
from app.models.conversation_state import ConversationActionState as _reg_ConversationActionState, ConversationSearchConcept as _reg_ConversationSearchConcept, ConversationState as _reg_ConversationState
from app.services.country_detection import resolve_country_display_name as _reg_resolve_country_display_name
from app.services.rag_answer import answer_legal_question as _reg_answer_legal_question

def _reg_document_topic_provider(country_codes: list[str]) -> dict[str, list[str]]:
    """
    Fake DocumentLegalTopicsProvider - mission "ORDER 8F-A" - no live
    document legal topics for any country, matching every test in this
    file written before that mission (none of them concern the new
    document_legal_topics concept).
    """
    return {}

def _reg_catalog_provider() -> _reg_LegalCatalogResponse:
    return _reg_LegalCatalogResponse(countries=[_reg_LegalCatalogCountry(country_code=country.code, country=country.display_name, chunk_count=42) for country in _reg_COUNTRIES], legal_topics=[], subsections=[])

def _reg_understanding_action(action_type: str, *, country_codes: list[str] | None=None, legal_topics: list[str] | None=None, topic_text: str | None=None, resolved_question: str | None=None, subject_text: str | None=None, search_concepts: list[dict[str, _reg_Any]] | None=None, subject_specificity: str | None=None, evidence_mode: str | None=None) -> dict[str, _reg_Any]:
    return {'type': action_type, 'country_codes': country_codes or [], 'legal_topics': legal_topics or [], 'topic_text': topic_text, 'resolved_question': resolved_question, 'subject_text': subject_text, 'search_concepts': search_concepts or [], 'subject_specificity': subject_specificity, 'evidence_mode': evidence_mode}

def _reg_delta(*, context_operation: str='independent', explicit_action_types: list[str] | None=None, explicit_country_codes: list[str] | None=None, explicit_legal_topics: list[str] | None=None, explicit_subject_text: str | None=None) -> dict[str, _reg_Any]:
    return {'explicit_action_types': explicit_action_types or [], 'explicit_country_codes': explicit_country_codes or [], 'explicit_legal_topics': explicit_legal_topics or [], 'explicit_subject_text': explicit_subject_text, 'context_operation': context_operation}

def _reg_understanding_result(*, status: str='resolved', actions: list[dict[str, _reg_Any]] | None=None, is_follow_up: bool=False, confidence: float=0.9, clarification_reason: str | None=None, delta: dict[str, _reg_Any] | None=None) -> dict[str, _reg_Any]:
    return {'status': status, 'actions': actions or [], 'is_follow_up': is_follow_up, 'confidence': confidence, 'current_message_delta': delta or _reg_delta(context_operation='independent'), 'clarification_reason': clarification_reason}

class _reg_FakeUnderstandingClient:

    def __init__(self, payload: dict[str, _reg_Any]) -> None:
        import json
        self._text = json.dumps(payload)
        self.call_count = 0

    def generate(self, instructions: str, input_text: str, text_format: dict[str, _reg_Any] | None=None) -> _reg_GeneratedText:
        self.call_count += 1
        return _reg_GeneratedText(text=self._text, model='test-model')

class _reg_CapturingGenerationClient:
    """Records every (instructions, input_text) pair it was called with."""
    model = 'test-model'

    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.calls: list[tuple[str, str]] = []

    def generate(self, instructions: str, input_text: str) -> _reg_GeneratedText:
        self.calls.append((instructions, input_text))
        return _reg_GeneratedText(text=self.answer, model=self.model)

    @property
    def called(self) -> bool:
        return bool(self.calls)

class _reg_NoCallGenerationClient:
    """Fails the test if generate() is ever called."""
    model = 'test-model'

    def generate(self, instructions: str, input_text: str) -> _reg_GeneratedText:
        raise AssertionError('OpenAI must not be called for a deterministic contact response.')

def _reg_dismissal_sick_leave_search_function():
    """
    Returns one on-subject hit for whichever single country is
    requested - realistic enough to classify as "direct" evidence
    under evidence_mode="relation_required" for the dismissal/sick-
    leave concepts used throughout sequences A/B/D/E.
    """

    def fake_search(request: _reg_Any) -> _reg_LegalSearchResponse:
        country_code = request.country_codes[0] if request.country_codes else 'PE'
        country_name = _reg_resolve_country_display_name(country_code)
        hit = _reg_LegalSearchHit(score=10.0, document_id=f'document-{country_code.lower()}', chunk_id=f'chunk-{country_code.lower()}', country=country_name, country_code=country_code, legal_topic='Termination of Employment Contracts', document_type='comparator', language='en', section='Termination of Employment Contracts', subsection='Dismissal During Sick Leave', content='An employee dismissed while on sick leave retains additional termination protections and continues to receive sick leave benefits during the notice period.', source_filename=f'Labour and Employment Law in {country_name} 2026.docx', source_format='docx', reference_year=2026)
        return _reg_LegalSearchResponse(query=request.query, total=1, limit=request.limit, offset=0, took_ms=1, hits=[hit])
    return fake_search

def _reg_build_contact_hit(*, country_code: str, country: str) -> _reg_LegalSearchHit:
    return _reg_LegalSearchHit(score=10.0, document_id=f'document-{country_code.lower()}', chunk_id=f'chunk-{country_code.lower()}-contact', country=country, country_code=country_code, legal_topic=None, document_type='overview', language='en', section=f'Employment Law Overview {country}', subsection='Contact', content=f'Member firm: Test Firm {country}\nEmail: contact@test-firm.example', source_filename=f'Labour and Employment Law in {country} 2026.docx', source_format='docx', reference_year=2026)

def _reg_fake_contact_search(country_codes: list[str], client: _reg_Any=None):
    return _reg_LegalSearchResponse(query='', total=len(country_codes), limit=20, offset=0, took_ms=1, hits=[_reg_build_contact_hit(country_code=code, country=_reg_resolve_country_display_name(code)) for code in country_codes])
_reg_DISMISSAL_SEARCH_CONCEPTS = [{'terms': ['dismissal', 'termination']}, {'terms': ['sick leave', 'medical leave']}]

def _reg_turn_one_dismissal_understanding_payload() -> dict[str, _reg_Any]:
    return _reg_understanding_result(status='resolved', actions=[_reg_understanding_action('legal_information', country_codes=['PE'], legal_topics=['Termination of Employment Contracts'], subject_text='dismissal while on sick leave', resolved_question='For Peru, answer this employment law question: dismissal while on sick leave.', search_concepts=_reg_DISMISSAL_SEARCH_CONCEPTS, subject_specificity='specific', evidence_mode='relation_required')], is_follow_up=False, delta=_reg_delta(context_operation='independent', explicit_action_types=['legal_information'], explicit_country_codes=['PE'], explicit_legal_topics=['Termination of Employment Contracts'], explicit_subject_text='dismissal while on sick leave'))

class _reg_SequenceABLastStateAndPreciseSubjectTests(_reg_unittest.TestCase):
    """
    Sequence A+B: the last active conversational state (here, a
    precise sub-topic - "dismissal while on sick leave" - not just the
    broad "termination" topic) must survive a bare country follow-up,
    even when the classifier's own re-derivation for turn 2 only gets
    the broad topic right.
    """

    def test_precise_subject_and_country_both_survive_the_follow_up(self) -> None:
        turn_one_client = _reg_FakeUnderstandingClient(payload=_reg_turn_one_dismissal_understanding_payload())
        turn_one_generation = _reg_CapturingGenerationClient(answer='Peru\n- Dismissal while on sick leave triggers additional termination protections. [1]')
        turn_one_response = _reg_resolve_legal_chat_response(request=_reg_LegalChatRequest(question='Can an employee be dismissed while on sick leave in Peru?'), catalog_provider=_reg_catalog_provider, document_topic_provider=_reg_document_topic_provider, search_function=_reg_dismissal_sick_leave_search_function(), generation_client=turn_one_generation, understanding_client=turn_one_client)
        self.assertTrue(turn_one_response.grounded)
        self.assertIsNotNone(turn_one_response.conversation_state)
        state = turn_one_response.conversation_state
        self.assertEqual(len(state.actions), 1)
        self.assertEqual(state.actions[0].type, 'legal_information')
        self.assertEqual(state.actions[0].country_codes, ['PE'])
        self.assertEqual(state.actions[0].subject_text, 'dismissal while on sick leave')
        self.assertEqual(state.focus_action_index, 0)
        turn_two_client = _reg_FakeUnderstandingClient(payload=_reg_understanding_result(status='resolved', actions=[_reg_understanding_action('legal_information', country_codes=['ES'], legal_topics=['Termination of Employment Contracts'], resolved_question='For Spain, answer this employment law question about termination.')], is_follow_up=True, delta=_reg_delta(context_operation='replace_country', explicit_country_codes=['ES'])))
        turn_two_generation = _reg_CapturingGenerationClient(answer='Spain\n- Dismissal while on sick leave triggers additional termination protections. [1]')
        turn_two_response = _reg_resolve_legal_chat_response(request=_reg_LegalChatRequest(question='What about in Spain?', conversation_state=turn_one_response.conversation_state), catalog_provider=_reg_catalog_provider, document_topic_provider=_reg_document_topic_provider, search_function=_reg_dismissal_sick_leave_search_function(), generation_client=turn_two_generation, understanding_client=turn_two_client)
        self.assertTrue(turn_two_response.grounded)
        self.assertEqual(len(turn_two_generation.calls), 1)
        generation_input = turn_two_generation.calls[0][1]
        self.assertIn('Spain', generation_input)
        self.assertIn('dismissal', generation_input)
        self.assertIn('sick leave', generation_input)
        next_state = turn_two_response.conversation_state
        self.assertIsNotNone(next_state)
        self.assertEqual(next_state.actions[0].country_codes, ['ES'])
        self.assertEqual(next_state.actions[0].subject_text, 'dismissal while on sick leave')

class _reg_SameInformationCountryFollowUpTests(_reg_unittest.TestCase):
    """A natural one-country "same information" follow-up reuses the subject."""

    def test_same_information_for_new_country_reuses_previous_subject(self) -> None:
        state = _reg_ConversationState(actions=[_reg_ConversationActionState(type='legal_information', country_codes=['PE'], legal_topics=['Termination of Employment Contracts'], subject_text='dismissal while on sick leave', search_concepts=[{'terms': ['dismissal', 'termination']}, {'terms': ['sick leave', 'medical leave']}], subject_specificity='specific', evidence_mode='relation_required')], focus_action_index=0)
        understanding_client = _reg_FakeUnderstandingClient(payload=_reg_understanding_result(status='clarification', actions=[_reg_understanding_action('legal_information', country_codes=['ES'])], is_follow_up=True, clarification_reason='ambiguous_request', delta=_reg_delta(context_operation='ambiguous')))
        generation_client = _reg_CapturingGenerationClient(answer='Spain\n- Dismissal while on sick leave triggers additional termination protections. [1]')
        response = _reg_resolve_legal_chat_response(request=_reg_LegalChatRequest(question='Now give me the same information for Spain.', conversation_state=state), catalog_provider=_reg_catalog_provider, document_topic_provider=_reg_document_topic_provider, search_function=_reg_dismissal_sick_leave_search_function(), generation_client=generation_client, understanding_client=understanding_client)
        self.assertTrue(response.grounded)
        self.assertEqual(understanding_client.call_count, 1)
        self.assertEqual(len(generation_client.calls), 1)
        generation_input = generation_client.calls[0][1]
        self.assertIn('Spain', generation_input)
        self.assertIn('dismissal', generation_input)
        self.assertIn('sick leave', generation_input)
        next_state = response.conversation_state
        self.assertIsNotNone(next_state)
        self.assertEqual(next_state.actions[0].country_codes, ['ES'])
        self.assertEqual(next_state.actions[0].subject_text, 'dismissal while on sick leave')

class _reg_SequenceDContactFollowUpStabilityTests(_reg_unittest.TestCase):
    """
    Sequence D: a Contact follow-up must resolve the same way every
    time, even when the classifier's own per-call result is unstable
    (here: it falls back to a generic "missing_country" clarification
    despite the message plainly naming a new country).
    """

    def test_an_unstable_clarification_is_overridden_to_the_new_country(self) -> None:
        turn_one_client = _reg_FakeUnderstandingClient(payload=_reg_understanding_result(status='resolved', actions=[_reg_understanding_action('contact', country_codes=['PE'])], delta=_reg_delta(context_operation='independent', explicit_action_types=['contact'], explicit_country_codes=['PE'])))
        with _reg_mock.patch('app.routers.chat.search_contact_chunks', side_effect=_reg_fake_contact_search):
            turn_one_response = _reg_resolve_legal_chat_response(request=_reg_LegalChatRequest(question='Who is the L&E Global contact in Peru?'), catalog_provider=_reg_catalog_provider, document_topic_provider=_reg_document_topic_provider, search_function=lambda request: (_ for _ in ()).throw(AssertionError('Legal search must not be called for a pure contact request.')), generation_client=_reg_NoCallGenerationClient(), understanding_client=turn_one_client)
        self.assertTrue(turn_one_response.grounded)
        self.assertEqual(turn_one_response.conversation_state.actions[0].type, 'contact')
        turn_two_client = _reg_FakeUnderstandingClient(payload=_reg_understanding_result(status='clarification', clarification_reason='missing_country', actions=[], delta=_reg_delta(context_operation='replace_country', explicit_country_codes=['ES'])))
        with _reg_mock.patch('app.routers.chat.search_contact_chunks', side_effect=_reg_fake_contact_search):
            turn_two_response = _reg_resolve_legal_chat_response(request=_reg_LegalChatRequest(question='And in Spain?', conversation_state=turn_one_response.conversation_state), catalog_provider=_reg_catalog_provider, document_topic_provider=_reg_document_topic_provider, search_function=lambda request: (_ for _ in ()).throw(AssertionError('Legal search must not be called for a pure contact request.')), generation_client=_reg_NoCallGenerationClient(), understanding_client=turn_two_client)
        self.assertTrue(turn_two_response.grounded)
        self.assertNotEqual(turn_two_response.answer, _reg_CONTACT_CLARIFICATION_ANSWER)
        self.assertNotEqual(turn_two_response.answer, _reg_CLARIFICATION_LEGAL_MISSING_COUNTRY_ANSWER)
        self.assertIn('Test Firm Spain', turn_two_response.answer)
        self.assertEqual(turn_two_response.conversation_state.actions[0].country_codes, ['ES'])

class _reg_SequenceEStateReplacementNotAccumulationTests(_reg_unittest.TestCase):
    """
    Sequence E: a genuinely new action must never be contaminated by
    the previous turn's topic, and the persisted state must be
    replaced outright, never accumulated (rectificatif A).
    """

    def test_a_new_contact_request_drops_the_old_legal_topic_entirely(self) -> None:
        turn_one_client = _reg_FakeUnderstandingClient(payload=_reg_turn_one_dismissal_understanding_payload())
        turn_one_generation = _reg_CapturingGenerationClient(answer='Peru\n- Dismissal while on sick leave triggers additional termination protections. [1]')
        turn_one_response = _reg_resolve_legal_chat_response(request=_reg_LegalChatRequest(question='Can an employee be dismissed while on sick leave in Peru?'), catalog_provider=_reg_catalog_provider, document_topic_provider=_reg_document_topic_provider, search_function=_reg_dismissal_sick_leave_search_function(), generation_client=turn_one_generation, understanding_client=turn_one_client)
        self.assertTrue(turn_one_response.grounded)
        turn_two_client = _reg_FakeUnderstandingClient(payload=_reg_understanding_result(status='resolved', actions=[_reg_understanding_action('contact', country_codes=['PE'])], is_follow_up=True, delta=_reg_delta(context_operation='select_action', explicit_action_types=['contact'], explicit_country_codes=['PE'])))
        with _reg_mock.patch('app.routers.chat.search_contact_chunks', side_effect=_reg_fake_contact_search):
            turn_two_response = _reg_resolve_legal_chat_response(request=_reg_LegalChatRequest(question='Who is the contact in Peru?', conversation_state=turn_one_response.conversation_state), catalog_provider=_reg_catalog_provider, document_topic_provider=_reg_document_topic_provider, search_function=lambda request: (_ for _ in ()).throw(AssertionError('Legal search must not run for a pure contact follow-up.')), generation_client=_reg_NoCallGenerationClient(), understanding_client=turn_two_client)
        self.assertTrue(turn_two_response.grounded)
        lowered_answer = turn_two_response.answer.lower()
        self.assertNotIn('dismissal', lowered_answer)
        self.assertNotIn('sick leave', lowered_answer)
        self.assertNotIn('termination', lowered_answer)
        next_state = turn_two_response.conversation_state
        self.assertEqual(len(next_state.actions), 1)
        self.assertEqual(next_state.actions[0].type, 'contact')

class _reg_SequenceFContextualClarificationTests(_reg_unittest.TestCase):
    """
    Sequence F: after a multi-country comparison, naming a new action
    with no country must ask specifically about those countries -
    never a generic "please specify a country" clarification.
    """

    def test_contact_after_a_comparison_asks_about_its_two_countries(self) -> None:
        turn_one_client = _reg_FakeUnderstandingClient(payload=_reg_understanding_result(status='resolved', actions=[_reg_understanding_action('comparison', country_codes=['PE', 'ES'], legal_topics=['Termination of Employment Contracts'], resolved_question='Compare Peru and Spain regarding this employment law issue: termination.')], delta=_reg_delta(context_operation='independent', explicit_action_types=['comparison'], explicit_country_codes=['PE', 'ES'], explicit_legal_topics=['Termination of Employment Contracts'])))
        turn_one_generation = _reg_CapturingGenerationClient(answer='Peru\n- Termination content. [1]\n\nSpain\n- Termination content. [2]')

        def two_country_search(request: _reg_Any) -> _reg_LegalSearchResponse:
            country_code = request.country_codes[0]
            country_name = _reg_resolve_country_display_name(country_code)
            return _reg_LegalSearchResponse(query=request.query, total=1, limit=request.limit, offset=0, took_ms=1, hits=[_reg_LegalSearchHit(score=10.0, document_id=f'document-{country_code.lower()}', chunk_id=f'chunk-{country_code.lower()}', country=country_name, country_code=country_code, legal_topic='Termination of Employment Contracts', document_type='comparator', language='en', section='Termination of Employment Contracts', subsection='Notice', content='Termination content.', source_filename=f'Labour and Employment Law in {country_name} 2026.docx', source_format='docx', reference_year=2026)])
        turn_one_response = _reg_resolve_legal_chat_response(request=_reg_LegalChatRequest(question='Compare termination rules in Peru and Spain.'), catalog_provider=_reg_catalog_provider, document_topic_provider=_reg_document_topic_provider, search_function=two_country_search, generation_client=turn_one_generation, understanding_client=turn_one_client)
        self.assertTrue(turn_one_response.grounded)
        state = turn_one_response.conversation_state
        self.assertEqual(state.actions[0].type, 'comparison')
        self.assertEqual(state.ordered_country_codes, ['PE', 'ES'])
        turn_two_client = _reg_FakeUnderstandingClient(payload=_reg_understanding_result(status='clarification', clarification_reason='missing_country', actions=[], delta=_reg_delta(context_operation='select_action', explicit_action_types=['contact'])))
        turn_two_response = _reg_resolve_legal_chat_response(request=_reg_LegalChatRequest(question='give me the local contact', conversation_state=state), catalog_provider=_reg_catalog_provider, document_topic_provider=_reg_document_topic_provider, search_function=lambda request: (_ for _ in ()).throw(AssertionError('Legal search must not run for a clarification.')), generation_client=_reg_NoCallGenerationClient(), understanding_client=turn_two_client)
        self.assertFalse(turn_two_response.grounded)
        self.assertEqual(turn_two_response.answer, 'Do you mean the contact in Peru or in Spain?')
        clarification_state = turn_two_response.conversation_state
        self.assertIsNotNone(clarification_state)
        self.assertEqual(clarification_state.actions, [])
        self.assertIsNotNone(clarification_state.pending_clarification)
        self.assertEqual(clarification_state.pending_clarification.candidate_country_codes, ['PE', 'ES'])

class _reg_SequenceGDisclaimerSignalAfterOutOfScopeTests(_reg_unittest.TestCase):
    """
    Sequence G, out-of-scope half: conversation_state must be None
    entirely (not merely empty-actions) after an out-of-scope answer,
    matching the other three non-resolved response paths.
    """

    def test_an_unsupported_request_carries_no_conversation_state(self) -> None:
        understanding_client = _reg_FakeUnderstandingClient(payload=_reg_understanding_result(status='unsupported', clarification_reason='unsupported_request', actions=[], delta=_reg_delta(context_operation='independent')))
        response = _reg_resolve_legal_chat_response(request=_reg_LegalChatRequest(question='What is the weather like today?'), catalog_provider=_reg_catalog_provider, document_topic_provider=_reg_document_topic_provider, search_function=lambda request: (_ for _ in ()).throw(AssertionError('Legal search must not run for an out-of-scope request.')), generation_client=_reg_NoCallGenerationClient(), understanding_client=understanding_client)
        self.assertFalse(response.grounded)
        self.assertEqual(response.answer, _reg_CLARIFICATION_UNSUPPORTED_REQUEST_ANSWER)
        self.assertIsNone(response.conversation_state)

class _reg_RequestUnderstandingCallBudgetTests(_reg_unittest.TestCase):
    """
    Phase 27: conversation_state reconciliation is entirely local
    (conversation_transition.py never calls OpenAI) - a turn carrying
    conversation_state must cost exactly the same one
    RequestUnderstanding call as a turn without it, never a second
    "contextualization" call.
    """

    def test_a_turn_with_conversation_state_still_makes_one_call(self) -> None:
        turn_one_client = _reg_FakeUnderstandingClient(payload=_reg_turn_one_dismissal_understanding_payload())
        turn_one_response = _reg_resolve_legal_chat_response(request=_reg_LegalChatRequest(question='Can an employee be dismissed while on sick leave in Peru?'), catalog_provider=_reg_catalog_provider, document_topic_provider=_reg_document_topic_provider, search_function=_reg_dismissal_sick_leave_search_function(), generation_client=_reg_CapturingGenerationClient(answer='Peru\n- Dismissal while on sick leave triggers additional termination protections. [1]'), understanding_client=turn_one_client)
        self.assertEqual(turn_one_client.call_count, 1)
        turn_two_client = _reg_FakeUnderstandingClient(payload=_reg_understanding_result(status='resolved', actions=[_reg_understanding_action('legal_information', country_codes=['ES'], legal_topics=['Termination of Employment Contracts'])], is_follow_up=True, delta=_reg_delta(context_operation='replace_country', explicit_country_codes=['ES'])))
        _reg_resolve_legal_chat_response(request=_reg_LegalChatRequest(question='What about in Spain?', conversation_state=turn_one_response.conversation_state), catalog_provider=_reg_catalog_provider, document_topic_provider=_reg_document_topic_provider, search_function=_reg_dismissal_sick_leave_search_function(), generation_client=_reg_CapturingGenerationClient(answer='Spain\n- Dismissal while on sick leave triggers additional termination protections. [1]'), understanding_client=turn_two_client)
        self.assertEqual(turn_two_client.call_count, 1)

class _reg_EvidenceGatingGenerationCallBudgetTests(_reg_unittest.TestCase):
    """
    Phase 27: evidence-gating must never add a third generation call -
    the existing one-generation-plus-at-most-one-repair budget is
    unchanged, even when a partial-evidence instruction is injected
    and the first attempt triggers a repair (here, via subject_drift).
    """

    def test_partial_evidence_plus_a_triggered_repair_stays_at_two_calls(self) -> None:
        hits = [_reg_LegalSearchHit(score=10.0, document_id='document-gb-1', chunk_id='chunk-gb-1', country='United Kingdom', country_code='GB', legal_topic='Working Conditions', document_type='comparator', language='en', section='Working Conditions', subsection='Remote Work', content='Teleworking is permitted for eligible roles.', source_filename='Labour and Employment Law in United Kingdom 2026.docx', source_format='docx', reference_year=2026), _reg_LegalSearchHit(score=9.0, document_id='document-gb-2', chunk_id='chunk-gb-2', country='United Kingdom', country_code='GB', legal_topic='Working Conditions', document_type='comparator', language='en', section='Working Conditions', subsection='Equipment', content='Equipment costs are reimbursed by the employer.', source_filename='Labour and Employment Law in United Kingdom 2026.docx', source_format='docx', reference_year=2026)]

        class _RepairCountingGenerationClient:
            model = 'test-model'

            def __init__(self) -> None:
                self.calls: list[tuple[str, str]] = []

            def generate(self, instructions: str, input_text: str) -> _reg_GeneratedText:
                self.calls.append((instructions, input_text))
                if len(self.calls) == 1:
                    return _reg_GeneratedText(text='United Kingdom\n- General workplace policies apply. [1]', model=self.model)
                if len(self.calls) == 2:
                    return _reg_GeneratedText(text='United Kingdom\n- Teleworking arrangements affect the equipment allowance provided. [1]', model=self.model)
                raise AssertionError('Generation must never be called a third time - one generation plus at most one repair is the whole budget.')
        client = _RepairCountingGenerationClient()

        def fake_search(request: _reg_Any) -> _reg_LegalSearchResponse:
            return _reg_LegalSearchResponse(query=request.query, total=len(hits), limit=request.limit, offset=0, took_ms=1, hits=hits)
        from app.services.chat_metrics import LegalChatMetrics
        metrics = LegalChatMetrics(request_id='performance-budget', question_characters=10, max_sources=6, rerank_enabled=False)
        response = _reg_answer_legal_question(request=_reg_LegalChatRequest(question='What are the remote work equipment rules?', country_codes=['GB']), search_function=fake_search, generation_client=client, metrics=metrics, subject_text='remote work equipment allowance', search_concepts=[_reg_ConversationSearchConcept(terms=['teleworking']), _reg_ConversationSearchConcept(terms=['equipment allowance'])], evidence_mode='relation_required')
        self.assertEqual(len(client.calls), 2)
        self.assertTrue(response.grounded)
        self.assertEqual(metrics.generation_attempts, 2)
        self.assertTrue(metrics.repair_triggered)

class _reg_EvidenceCoverageHasNoNetworkDependencyTests(_reg_unittest.TestCase):
    """
    Phase 27: the local concept-coverage engine must never make its
    own network/OpenAI call - it is pure, deterministic text matching,
    always available even when reranking is disabled (the production
    default).
    """

    def test_evidence_coverage_module_imports_nothing_network_related(self) -> None:
        import app.services.evidence_coverage as module
        tree = _reg_ast.parse(_reg_inspect.getsource(module))
        imported_top_level_modules: set[str] = set()
        for node in _reg_ast.walk(tree):
            if isinstance(node, _reg_ast.Import):
                for alias in node.names:
                    imported_top_level_modules.add(alias.name.split('.')[0])
            elif isinstance(node, _reg_ast.ImportFrom) and node.module:
                imported_top_level_modules.add(node.module.split('.')[0])
        forbidden_modules = {'openai', 'httpx', 'requests', 'urllib3', 'urllib'}
        self.assertEqual(imported_top_level_modules & forbidden_modules, set())

class _reg_JurisdictionNeutralSubjectRegressionTests(_reg_unittest.TestCase):
    """
    Mission "DÉCOUPLAGE COMPLET DU SUJET JURIDIQUE ET DE LA JURIDICTION",
    Phase 2: reproduces, end to end through resolve_legal_chat_response,
    the exact reported defect - RequestUnderstanding sometimes bakes the
    jurisdiction into subject_text itself (e.g. "rules on remote work
    (telework) in Spain" instead of "rules on remote work (telework)"),
    and a bare country follow-up ("Peru?") only ever replaces
    country_codes (see conversation_transition._inherit_action) - so
    the OLD country silently survives inside the inherited subject_text,
    the retrieval query built from it, and the insufficient-evidence
    message shown for the NEW country.

    Zero hits for every country throughout, so both turns land on the
    insufficient-evidence path (matching the exact bug report) without
    needing a generation client at all.
    """

    def _off_topic_search_function(self):
        """
        One real hit per requested country, on an unrelated
        subsection - forces a genuine content-mismatch "insufficient"
        verdict (the per-subject/per-country message template) rather
        than the unrelated all-countries-zero-hits NO_INFORMATION_
        ANSWER short-circuit.
        """
        captured: list[_reg_Any] = []

        def fake_search(request: _reg_Any) -> _reg_LegalSearchResponse:
            captured.append(request)
            code = request.country_codes[0] if request.country_codes else 'XX'
            hit = _reg_LegalSearchHit(score=5.0, document_id=f'document-{code.lower()}', chunk_id=f'chunk-{code.lower()}', country=code, country_code=code, legal_topic='Working Conditions', document_type='comparator', language='en', section='Working Conditions', subsection='Meal Breaks', content='Employees are entitled to a 30-minute meal break after six hours of work.', source_filename=f'Labour Law {code} 2026.docx', source_format='docx', reference_year=2026)
            return _reg_LegalSearchResponse(query=request.query, total=1, limit=request.limit, offset=0, took_ms=1, hits=[hit])
        return (fake_search, captured)

    @_reg_mock.patch('app.routers.chat._build_contact_section', new=lambda *args, **kwargs: ('', [], 0, 0.0))
    def test_old_jurisdiction_never_survives_a_bare_country_follow_up(self) -> None:
        turn_one_search, turn_one_requests = self._off_topic_search_function()
        turn_one_client = _reg_FakeUnderstandingClient(payload=_reg_understanding_result(status='resolved', actions=[_reg_understanding_action('legal_information', country_codes=['ES'], legal_topics=['Working Conditions'], subject_text='rules on remote work (telework) in Spain', search_concepts=[{'terms': ['remote work', 'telework', 'telecommuting', 'working from home']}], subject_specificity='specific', evidence_mode='direct_topic')], is_follow_up=False, delta=_reg_delta(context_operation='independent', explicit_action_types=['legal_information'], explicit_country_codes=['ES'], explicit_legal_topics=['Working Conditions'], explicit_subject_text='rules on remote work (telework) in Spain')))
        turn_one_response = _reg_resolve_legal_chat_response(request=_reg_LegalChatRequest(question='What are the rules on remote work in Spain?'), catalog_provider=_reg_catalog_provider, document_topic_provider=_reg_document_topic_provider, search_function=turn_one_search, understanding_client=turn_one_client)
        self.assertFalse(turn_one_response.grounded)
        state = turn_one_response.conversation_state
        self.assertIsNotNone(state)
        self.assertEqual(state.actions[0].country_codes, ['ES'])
        turn_two_search, turn_two_requests = self._off_topic_search_function()
        turn_two_client = _reg_FakeUnderstandingClient(payload=_reg_understanding_result(status='resolved', actions=[_reg_understanding_action('legal_information', country_codes=['PE'], legal_topics=['Working Conditions'], resolved_question='For Peru, answer this employment law question.')], is_follow_up=True, delta=_reg_delta(context_operation='replace_country', explicit_country_codes=['PE'])))
        turn_two_response = _reg_resolve_legal_chat_response(request=_reg_LegalChatRequest(question='Peru?', conversation_state=turn_one_response.conversation_state), catalog_provider=_reg_catalog_provider, document_topic_provider=_reg_document_topic_provider, search_function=turn_two_search, understanding_client=turn_two_client)
        self.assertFalse(turn_two_response.grounded)
        next_state = turn_two_response.conversation_state
        self.assertIsNotNone(next_state)
        self.assertEqual(next_state.actions[0].country_codes, ['PE'])
        self.assertNotIn('Spain', next_state.actions[0].subject_text or '')
        self.assertEqual(len(turn_two_requests), 1)
        retrieval_query = turn_two_requests[0].query
        self.assertNotIn('Spain', retrieval_query)
        self.assertEqual(turn_two_requests[0].country_codes, ['PE'])
        self.assertTrue('remote work' in retrieval_query or 'telework' in retrieval_query)
        self.assertNotIn('Spain', turn_two_response.answer)
        self.assertEqual(turn_two_response.answer.count('Peru'), 1)



# ====================================================================
# SOURCE DOMAIN: test_conversation_state.py
# ====================================================================


import json as _state_json
import unittest as _state_unittest
from pydantic import ValidationError as _state_ValidationError
from app.core.country_registry import COUNTRIES as _state_COUNTRIES
from app.models.conversation_state import MAX_ACTIONS as _state_MAX_ACTIONS, MAX_CONCEPT_TERM_CHARACTERS as _state_MAX_CONCEPT_TERM_CHARACTERS, MAX_CONCEPT_TERMS as _state_MAX_CONCEPT_TERMS, MAX_CONVERSATION_STATE_JSON_CHARACTERS as _state_MAX_CONVERSATION_STATE_JSON_CHARACTERS, MAX_COUNTRY_CODES_PER_ACTION as _state_MAX_COUNTRY_CODES_PER_ACTION, MAX_RESOLVED_QUESTION_CHARACTERS as _state_MAX_RESOLVED_QUESTION_CHARACTERS, MAX_SEARCH_CONCEPT_GROUPS as _state_MAX_SEARCH_CONCEPT_GROUPS, MAX_SUBJECT_TEXT_CHARACTERS as _state_MAX_SUBJECT_TEXT_CHARACTERS, MIN_CONCEPT_TERM_CHARACTERS as _state_MIN_CONCEPT_TERM_CHARACTERS, ConversationActionState as _state_ConversationActionState, ConversationPendingClarification as _state_ConversationPendingClarification, ConversationSearchConcept as _state_ConversationSearchConcept, ConversationState as _state_ConversationState

class _state_ConversationSearchConceptTests(_state_unittest.TestCase):

    def test_accepts_a_normal_synonym_group(self) -> None:
        concept = _state_ConversationSearchConcept(terms=['remote work', 'telework', 'teleworking'])
        self.assertEqual(concept.terms, ['remote work', 'telework', 'teleworking'])

    def test_strips_surrounding_whitespace_from_each_term(self) -> None:
        concept = _state_ConversationSearchConcept(terms=['  remote work  ', 'telework'])
        self.assertEqual(concept.terms, ['remote work', 'telework'])

    def test_rejects_an_empty_terms_list(self) -> None:
        with self.assertRaises(_state_ValidationError):
            _state_ConversationSearchConcept(terms=[])

    def test_rejects_more_than_the_maximum_number_of_terms(self) -> None:
        with self.assertRaises(_state_ValidationError):
            _state_ConversationSearchConcept(terms=[f'term-{index}' for index in range(_state_MAX_CONCEPT_TERMS + 1)])

    def test_rejects_a_term_shorter_than_the_minimum_length(self) -> None:
        with self.assertRaises(_state_ValidationError):
            _state_ConversationSearchConcept(terms=['a' * (_state_MIN_CONCEPT_TERM_CHARACTERS - 1)])

    def test_rejects_a_term_longer_than_the_maximum_length(self) -> None:
        with self.assertRaises(_state_ValidationError):
            _state_ConversationSearchConcept(terms=['a' * (_state_MAX_CONCEPT_TERM_CHARACTERS + 1)])

    def test_rejects_case_insensitive_duplicate_terms(self) -> None:
        with self.assertRaises(_state_ValidationError):
            _state_ConversationSearchConcept(terms=['Remote Work', 'remote work'])

    def test_rejects_unknown_fields(self) -> None:
        with self.assertRaises(_state_ValidationError):
            _state_ConversationSearchConcept(terms=['remote work'], extra_field='not allowed')

class _state_ConversationActionStateTests(_state_unittest.TestCase):

    def test_accepts_a_minimal_contact_action(self) -> None:
        action = _state_ConversationActionState(type='contact', country_codes=['ES'])
        self.assertEqual(action.country_codes, ['ES'])
        self.assertEqual(action.legal_topics, [])

    def test_contact_action_rejects_legal_topics(self) -> None:
        with self.assertRaises(_state_ValidationError):
            _state_ConversationActionState(type='contact', country_codes=['ES'], legal_topics=['Employment Contracts'])

    def test_contact_action_rejects_subject_text(self) -> None:
        with self.assertRaises(_state_ValidationError):
            _state_ConversationActionState(type='contact', country_codes=['ES'], subject_text='dismissal while on sick leave')

    def test_contact_action_rejects_search_concepts(self) -> None:
        with self.assertRaises(_state_ValidationError):
            _state_ConversationActionState(type='contact', country_codes=['ES'], search_concepts=[_state_ConversationSearchConcept(terms=['dismissal'])])

    def test_contact_action_rejects_subject_specificity(self) -> None:
        with self.assertRaises(_state_ValidationError):
            _state_ConversationActionState(type='contact', country_codes=['ES'], subject_specificity='specific')

    def test_contact_action_rejects_evidence_mode(self) -> None:
        with self.assertRaises(_state_ValidationError):
            _state_ConversationActionState(type='contact', country_codes=['ES'], evidence_mode='broad_topic')

    def test_legal_information_action_accepts_legal_topics_alone(self) -> None:
        action = _state_ConversationActionState(type='legal_information', country_codes=['ES'], legal_topics=['Termination of Employment Contracts'])
        self.assertEqual(action.legal_topics, ['Termination of Employment Contracts'])

    def test_legal_information_action_accepts_subject_text_alone(self) -> None:
        action = _state_ConversationActionState(type='legal_information', country_codes=['ES'], subject_text='dismissal while on sick leave')
        self.assertEqual(action.subject_text, 'dismissal while on sick leave')

    def test_legal_information_action_requires_topics_or_subject(self) -> None:
        with self.assertRaises(_state_ValidationError):
            _state_ConversationActionState(type='legal_information', country_codes=['ES'])

    def test_comparison_action_accepts_two_countries(self) -> None:
        action = _state_ConversationActionState(type='comparison', country_codes=['ES', 'IT'], legal_topics=['Termination of Employment Contracts'])
        self.assertEqual(action.country_codes, ['ES', 'IT'])

    def test_comparison_action_rejects_a_single_country(self) -> None:
        with self.assertRaises(_state_ValidationError):
            _state_ConversationActionState(type='comparison', country_codes=['ES'], legal_topics=['Termination of Employment Contracts'])

    def test_country_codes_are_uppercased_and_deduplicated(self) -> None:
        action = _state_ConversationActionState(type='contact', country_codes=['es', 'ES', 'it'])
        self.assertEqual(action.country_codes, ['ES', 'IT'])

    def test_rejects_an_unsupported_country_code(self) -> None:
        with self.assertRaises(_state_ValidationError):
            _state_ConversationActionState(type='contact', country_codes=['ZZ'])

    def test_rejects_more_country_codes_than_exist(self) -> None:
        with self.assertRaises(_state_ValidationError):
            _state_ConversationActionState(type='contact', country_codes=[_state_COUNTRIES[0].code] * (_state_MAX_COUNTRY_CODES_PER_ACTION + 1))

    def test_rejects_a_non_canonical_legal_topic(self) -> None:
        with self.assertRaises(_state_ValidationError):
            _state_ConversationActionState(type='legal_information', country_codes=['ES'], legal_topics=['Termination'])

    def test_rejects_subject_text_longer_than_the_maximum(self) -> None:
        with self.assertRaises(_state_ValidationError):
            _state_ConversationActionState(type='legal_information', country_codes=['ES'], subject_text='a' * (_state_MAX_SUBJECT_TEXT_CHARACTERS + 1))

    def test_rejects_resolved_question_longer_than_the_maximum(self) -> None:
        with self.assertRaises(_state_ValidationError):
            _state_ConversationActionState(type='legal_information', country_codes=['ES'], subject_text='dismissal', resolved_question='a' * (_state_MAX_RESOLVED_QUESTION_CHARACTERS + 1))

    def test_rejects_more_search_concept_groups_than_the_maximum(self) -> None:
        with self.assertRaises(_state_ValidationError):
            _state_ConversationActionState(type='legal_information', country_codes=['ES'], subject_text='dismissal', search_concepts=[_state_ConversationSearchConcept(terms=[f'term-{index}']) for index in range(_state_MAX_SEARCH_CONCEPT_GROUPS + 1)])

    def test_accepts_each_evidence_mode(self) -> None:
        for evidence_mode in ('broad_topic', 'direct_topic', 'relation_required', None):
            action = _state_ConversationActionState(type='legal_information', country_codes=['ES'], subject_text='dismissal', evidence_mode=evidence_mode)
            self.assertEqual(action.evidence_mode, evidence_mode)

    def test_rejects_an_unsupported_evidence_mode(self) -> None:
        with self.assertRaises(_state_ValidationError):
            _state_ConversationActionState(type='legal_information', country_codes=['ES'], subject_text='dismissal', evidence_mode='vector_search')

    def test_rejects_an_unsupported_action_type(self) -> None:
        with self.assertRaises(_state_ValidationError):
            _state_ConversationActionState(type='unknown_action', country_codes=['ES'])

    def test_rejects_unknown_fields(self) -> None:
        with self.assertRaises(_state_ValidationError):
            _state_ConversationActionState(type='contact', country_codes=['ES'], extra_field='not allowed')

class _state_ConversationPendingClarificationTests(_state_unittest.TestCase):

    def test_accepts_a_minimal_clarification(self) -> None:
        clarification = _state_ConversationPendingClarification(reason='select_country', candidate_country_codes=['ES', 'IT'])
        self.assertEqual(clarification.candidate_country_codes, ['ES', 'IT'])

    def test_accepts_a_clarification_with_no_candidates_yet(self) -> None:
        clarification = _state_ConversationPendingClarification(reason='ambiguous_reference')
        self.assertEqual(clarification.candidate_action_types, [])
        self.assertEqual(clarification.candidate_country_codes, [])

    def test_rejects_an_unsupported_reason(self) -> None:
        with self.assertRaises(_state_ValidationError):
            _state_ConversationPendingClarification(reason='not_a_real_reason')

    def test_candidate_country_codes_are_uppercased_and_deduplicated(self) -> None:
        clarification = _state_ConversationPendingClarification(reason='select_country', candidate_country_codes=['es', 'ES', 'it'])
        self.assertEqual(clarification.candidate_country_codes, ['ES', 'IT'])

    def test_rejects_an_unsupported_candidate_country_code(self) -> None:
        with self.assertRaises(_state_ValidationError):
            _state_ConversationPendingClarification(reason='select_country', candidate_country_codes=['ZZ'])

    def test_rejects_unknown_fields(self) -> None:
        with self.assertRaises(_state_ValidationError):
            _state_ConversationPendingClarification(reason='select_country', extra_field='not allowed')

def _state_contact(country_codes: list[str]) -> _state_ConversationActionState:
    return _state_ConversationActionState(type='contact', country_codes=country_codes)

def _state_legal(country_codes: list[str], topics: list[str] | None=None) -> _state_ConversationActionState:
    return _state_ConversationActionState(type='legal_information', country_codes=country_codes, legal_topics=topics if topics is not None else ['Termination of Employment Contracts'])

def _state_comparison(country_codes: list[str], topics: list[str] | None=None) -> _state_ConversationActionState:
    return _state_ConversationActionState(type='comparison', country_codes=country_codes, legal_topics=topics if topics is not None else ['Termination of Employment Contracts'])

class _state_ConversationStateTests(_state_unittest.TestCase):

    def test_accepts_a_fully_empty_state(self) -> None:
        state = _state_ConversationState()
        self.assertEqual(state.actions, [])
        self.assertIsNone(state.focus_action_index)
        self.assertIsNone(state.pending_clarification)

    def test_accepts_two_different_action_types_for_the_same_country(self) -> None:
        state = _state_ConversationState(actions=[_state_contact(['ES']), _state_legal(['ES'])], focus_action_index=None)
        self.assertEqual(len(state.actions), 2)

    def test_accepts_the_same_action_type_for_different_countries(self) -> None:
        state = _state_ConversationState(actions=[_state_legal(['ES']), _state_legal(['IT'])], focus_action_index=None)
        self.assertEqual(len(state.actions), 2)

    def test_rejects_duplicate_action_scope_even_with_different_topics(self) -> None:
        with self.assertRaises(_state_ValidationError):
            _state_ConversationState(actions=[_state_legal(['ES'], ['Termination of Employment Contracts']), _state_legal(['ES'], ['Employee Benefits'])], focus_action_index=0)

    def test_focus_action_index_must_be_null_with_no_actions(self) -> None:
        with self.assertRaises(_state_ValidationError):
            _state_ConversationState(actions=[], focus_action_index=0)

    def test_focus_action_index_must_be_zero_with_one_action(self) -> None:
        state = _state_ConversationState(actions=[_state_contact(['ES'])], focus_action_index=0)
        self.assertEqual(state.focus_action_index, 0)

    def test_focus_action_index_cannot_be_null_with_one_action(self) -> None:
        with self.assertRaises(_state_ValidationError):
            _state_ConversationState(actions=[_state_contact(['ES'])], focus_action_index=None)

    def test_focus_action_index_cannot_be_nonzero_with_one_action(self) -> None:
        with self.assertRaises(_state_ValidationError):
            _state_ConversationState(actions=[_state_contact(['ES'])], focus_action_index=1)

    def test_focus_action_index_may_be_null_with_multiple_actions(self) -> None:
        state = _state_ConversationState(actions=[_state_contact(['ES']), _state_legal(['IT'])], focus_action_index=None)
        self.assertIsNone(state.focus_action_index)

    def test_focus_action_index_may_select_among_multiple_actions(self) -> None:
        state = _state_ConversationState(actions=[_state_contact(['ES']), _state_legal(['IT'])], focus_action_index=1)
        self.assertEqual(state.focus_action_index, 1)

    def test_focus_action_index_out_of_range_is_rejected(self) -> None:
        with self.assertRaises(_state_ValidationError):
            _state_ConversationState(actions=[_state_contact(['ES']), _state_legal(['IT'])], focus_action_index=5)

    def test_focus_action_index_negative_is_rejected(self) -> None:
        with self.assertRaises(_state_ValidationError):
            _state_ConversationState(actions=[_state_contact(['ES']), _state_legal(['IT'])], focus_action_index=-1)

    def test_comparison_action_requires_ordered_country_codes(self) -> None:
        with self.assertRaises(_state_ValidationError):
            _state_ConversationState(actions=[_state_comparison(['ES', 'IT'])], focus_action_index=0, ordered_country_codes=[])

    def test_ordered_country_codes_without_a_comparison_is_rejected(self) -> None:
        with self.assertRaises(_state_ValidationError):
            _state_ConversationState(actions=[_state_contact(['ES'])], focus_action_index=0, ordered_country_codes=['ES'])

    def test_ordered_country_codes_must_match_the_comparison_countries(self) -> None:
        with self.assertRaises(_state_ValidationError):
            _state_ConversationState(actions=[_state_comparison(['ES', 'IT'])], focus_action_index=0, ordered_country_codes=['ES', 'BE'])

    def test_ordered_country_codes_may_differ_in_order_from_the_action(self) -> None:
        state = _state_ConversationState(actions=[_state_comparison(['ES', 'IT'])], focus_action_index=0, ordered_country_codes=['IT', 'ES'])
        self.assertEqual(state.ordered_country_codes, ['IT', 'ES'])

    def test_ordered_country_codes_rejects_two_active_comparisons(self) -> None:
        with self.assertRaises(_state_ValidationError):
            _state_ConversationState(actions=[_state_comparison(['ES', 'IT']), _state_comparison(['BE', 'IT'])], focus_action_index=None, ordered_country_codes=['ES', 'IT'])

    def test_rejects_more_actions_than_the_maximum(self) -> None:
        with self.assertRaises(_state_ValidationError):
            _state_ConversationState(actions=[_state_legal([country.code]) for country in _state_COUNTRIES[:_state_MAX_ACTIONS + 1]], focus_action_index=None)

    def test_version_must_be_exactly_one(self) -> None:
        with self.assertRaises(_state_ValidationError):
            _state_ConversationState(version=2)

    def test_rejects_unknown_fields(self) -> None:
        with self.assertRaises(_state_ValidationError):
            _state_ConversationState(extra_field='not allowed')

    def test_rejects_a_state_serializing_past_the_size_ceiling(self) -> None:
        oversized_subject = 'a' * _state_MAX_SUBJECT_TEXT_CHARACTERS
        oversized_resolved_question = 'b' * _state_MAX_RESOLVED_QUESTION_CHARACTERS
        unique_near_max_terms = ['c' * (_state_MAX_CONCEPT_TERM_CHARACTERS - 3) + f'{index:03d}' for index in range(_state_MAX_CONCEPT_TERMS)]
        padding_concepts = [_state_ConversationSearchConcept(terms=unique_near_max_terms) for _ in range(_state_MAX_SEARCH_CONCEPT_GROUPS)]
        with self.assertRaises(_state_ValidationError):
            _state_ConversationState(actions=[_state_ConversationActionState(type='legal_information', country_codes=[country.code], subject_text=oversized_subject, resolved_question=oversized_resolved_question, search_concepts=padding_concepts) for country in _state_COUNTRIES[:_state_MAX_ACTIONS]], focus_action_index=None)

    def test_a_moderately_sized_state_stays_within_the_ceiling(self) -> None:
        state = _state_ConversationState(actions=[_state_legal(['ES']), _state_contact(['IT'])], focus_action_index=None)
        serialized_length = len(_state_json.dumps(state.model_dump(mode='json'), separators=(',', ':')))
        self.assertLess(serialized_length, _state_MAX_CONVERSATION_STATE_JSON_CHARACTERS)



# ====================================================================
# SOURCE DOMAIN: test_conversation_transition.py
# ====================================================================


import unittest as _trans_unittest
from dataclasses import replace as _trans_replace
from unittest import mock as _trans_mock
from app.models.conversation_state import ConversationActionState as _trans_ConversationActionState, ConversationPendingClarification as _trans_ConversationPendingClarification, ConversationSearchConcept as _trans_ConversationSearchConcept, ConversationState as _trans_ConversationState
from app.services.conversation_transition import ConversationTransitionError as _trans_ConversationTransitionError, apply_conversation_transition as _trans_apply_conversation_transition, build_next_conversation_state as _trans_build_next_conversation_state
from app.services.request_understanding import CurrentMessageDelta as _trans_CurrentMessageDelta, DeterministicHints as _trans_DeterministicHints, RequestUnderstandingResult as _trans_RequestUnderstandingResult
from tests.support.chat import _action_state as _trans_action_state, _delta as _trans_delta, _hints as _trans_hints, _result as _trans_result, _ru_action as _trans_ru_action, _state as _trans_state

class _trans_NoConversationStateTests(_trans_unittest.TestCase):
    """No conversation_state at all - always a pure passthrough."""

    def test_passes_the_classifier_result_through_unchanged(self) -> None:
        result = _trans_result(status='unsupported', clarification_reason='unsupported_request', is_follow_up=False, delta=_trans_delta(context_operation='independent'))
        outcome = _trans_apply_conversation_transition(result=result, conversation_state=None, hints=_trans_hints())
        self.assertEqual(outcome.final_status, 'unsupported')
        self.assertEqual(outcome.final_clarification_reason, 'unsupported_request')
        self.assertFalse(outcome.semantic_result_overridden)
        self.assertFalse(outcome.context_inheritance_applied)
        self.assertIsNone(outcome.pending_clarification)

class _trans_EmptyPriorActionsTests(_trans_unittest.TestCase):
    """conversation_state with zero actions - nothing to reconcile."""

    def test_passes_through_when_conversation_state_has_no_actions(self) -> None:
        result = _trans_result(status='resolved', actions=[_trans_ru_action('contact', ['ES'])], clarification_reason=None, delta=_trans_delta(context_operation='independent'))
        outcome = _trans_apply_conversation_transition(result=result, conversation_state=_trans_state([]), hints=_trans_hints())
        self.assertFalse(outcome.semantic_result_overridden)
        self.assertEqual(outcome.final_status, 'resolved')
        self.assertEqual(outcome.final_actions[0].type, 'contact')

class _trans_SingleActionContinuationTests(_trans_unittest.TestCase):
    """
    Single prior action, "continue"/"replace_country"/"add_country" -
    the trivial single-active-action country change (defect A).
    """

    def test_continue_keeps_the_same_country_unreplaced(self) -> None:
        previous = _trans_action_state('legal_information', ['PE'], subject_text='dismissal while on sick leave')
        outcome = _trans_apply_conversation_transition(result=_trans_result(delta=_trans_delta(context_operation='continue')), conversation_state=_trans_state([previous], focus_action_index=0), hints=_trans_hints())
        self.assertTrue(outcome.semantic_result_overridden)
        self.assertEqual(outcome.semantic_override_reason, 'single_active_action_country_continuation')
        self.assertTrue(outcome.context_inheritance_applied)
        self.assertFalse(outcome.inherited_country_replaced)
        self.assertEqual(outcome.final_status, 'resolved')
        inherited = outcome.final_actions[0]
        self.assertEqual(inherited.country_codes, ['PE'])
        self.assertEqual(inherited.subject_text, 'dismissal while on sick leave')
        self.assertIn('Peru', inherited.resolved_question)
        self.assertIn('dismissal while on sick leave', inherited.resolved_question)

    def test_replace_country_swaps_to_the_new_country(self) -> None:
        previous = _trans_action_state('legal_information', ['PE'], subject_text='dismissal while on sick leave')
        outcome = _trans_apply_conversation_transition(result=_trans_result(delta=_trans_delta(context_operation='replace_country', explicit_country_codes=['ES'])), conversation_state=_trans_state([previous], focus_action_index=0), hints=_trans_hints())
        self.assertTrue(outcome.inherited_country_replaced)
        inherited = outcome.final_actions[0]
        self.assertEqual(inherited.country_codes, ['ES'])
        self.assertIn('Spain', inherited.resolved_question)

    def test_replace_country_with_no_explicit_code_keeps_previous(self) -> None:
        previous = _trans_action_state('legal_information', ['PE'], subject_text='dismissal while on sick leave')
        outcome = _trans_apply_conversation_transition(result=_trans_result(delta=_trans_delta(context_operation='replace_country')), conversation_state=_trans_state([previous], focus_action_index=0), hints=_trans_hints())
        self.assertFalse(outcome.inherited_country_replaced)
        self.assertEqual(outcome.final_actions[0].country_codes, ['PE'])

    def test_add_country_merges_onto_the_previous_country(self) -> None:
        previous = _trans_action_state('legal_information', ['PE'], subject_text='dismissal while on sick leave')
        outcome = _trans_apply_conversation_transition(result=_trans_result(delta=_trans_delta(context_operation='add_country', explicit_country_codes=['ES'])), conversation_state=_trans_state([previous], focus_action_index=0), hints=_trans_hints())
        self.assertTrue(outcome.inherited_country_replaced)
        self.assertEqual(outcome.final_actions[0].country_codes, ['PE', 'ES'])

    def test_add_country_is_idempotent_for_an_already_present_country(self) -> None:
        previous = _trans_action_state('legal_information', ['PE'], subject_text='dismissal while on sick leave')
        outcome = _trans_apply_conversation_transition(result=_trans_result(delta=_trans_delta(context_operation='add_country', explicit_country_codes=['PE'])), conversation_state=_trans_state([previous], focus_action_index=0), hints=_trans_hints())
        self.assertEqual(outcome.final_actions[0].country_codes, ['PE'])

    def test_comparison_continue_keeps_both_countries(self) -> None:
        previous = _trans_action_state('comparison', ['PE', 'ES'], legal_topics=['Termination of Employment Contracts'])
        outcome = _trans_apply_conversation_transition(result=_trans_result(delta=_trans_delta(context_operation='continue')), conversation_state=_trans_state([previous], focus_action_index=0, ordered_country_codes=['PE', 'ES']), hints=_trans_hints())
        self.assertEqual(outcome.final_actions[0].country_codes, ['PE', 'ES'])
        self.assertFalse(outcome.inherited_country_replaced)

    def test_comparison_add_country_grows_to_three_countries(self) -> None:
        previous = _trans_action_state('comparison', ['PE', 'ES'], legal_topics=['Termination of Employment Contracts'])
        outcome = _trans_apply_conversation_transition(result=_trans_result(delta=_trans_delta(context_operation='add_country', explicit_country_codes=['IT'])), conversation_state=_trans_state([previous], focus_action_index=0, ordered_country_codes=['PE', 'ES']), hints=_trans_hints())
        self.assertEqual(outcome.final_actions[0].country_codes, ['PE', 'ES', 'IT'])

    def test_comparison_cannot_be_replaced_down_to_one_country(self) -> None:
        previous = _trans_action_state('comparison', ['PE', 'ES'], legal_topics=['Termination of Employment Contracts'])
        classifier_result = _trans_result(status='clarification', clarification_reason='missing_comparison_countries', delta=_trans_delta(context_operation='replace_country', explicit_country_codes=['IT']))
        outcome = _trans_apply_conversation_transition(result=classifier_result, conversation_state=_trans_state([previous], focus_action_index=0, ordered_country_codes=['PE', 'ES']), hints=_trans_hints())
        self.assertFalse(outcome.semantic_result_overridden)
        self.assertEqual(outcome.final_clarification_reason, 'missing_comparison_countries')

    def test_a_genuinely_new_action_never_inherits(self) -> None:
        previous = _trans_action_state('legal_information', ['PE'], subject_text='dismissal while on sick leave')
        classifier_result = _trans_result(status='resolved', actions=[_trans_ru_action('contact', ['ES'])], clarification_reason=None, delta=_trans_delta(context_operation='change_action', explicit_action_types=['contact'], explicit_country_codes=['ES']))
        outcome = _trans_apply_conversation_transition(result=classifier_result, conversation_state=_trans_state([previous], focus_action_index=0), hints=_trans_hints())
        self.assertFalse(outcome.semantic_result_overridden)
        self.assertEqual(outcome.final_actions[0].type, 'contact')

    def test_a_genuinely_new_subject_never_inherits(self) -> None:
        previous = _trans_action_state('legal_information', ['PE'], subject_text='dismissal while on sick leave')
        classifier_result = _trans_result(status='resolved', actions=[_trans_ru_action('legal_information', ['PE'], legal_topics=['Employee Benefits'])], clarification_reason=None, delta=_trans_delta(context_operation='change_subject', explicit_legal_topics=['Employee Benefits'], explicit_subject_text='parental leave entitlement'))
        outcome = _trans_apply_conversation_transition(result=classifier_result, conversation_state=_trans_state([previous], focus_action_index=0), hints=_trans_hints())
        self.assertFalse(outcome.semantic_result_overridden)

    def test_a_strong_contact_signal_never_inherits(self) -> None:
        previous = _trans_action_state('legal_information', ['PE'], subject_text='dismissal while on sick leave')
        classifier_result = _trans_result(status='resolved', actions=[_trans_ru_action('contact', ['PE'])], clarification_reason=None, delta=_trans_delta(context_operation='continue'))
        outcome = _trans_apply_conversation_transition(result=classifier_result, conversation_state=_trans_state([previous], focus_action_index=0), hints=_trans_hints(strong_contact_signal=True))
        self.assertFalse(outcome.semantic_result_overridden)

    def test_a_comparison_signal_never_inherits(self) -> None:
        previous = _trans_action_state('legal_information', ['PE'], subject_text='dismissal while on sick leave')
        classifier_result = _trans_result(status='resolved', actions=[_trans_ru_action('comparison', ['PE', 'ES'], legal_topics=['Termination of Employment Contracts'])], clarification_reason=None, delta=_trans_delta(context_operation='continue'))
        outcome = _trans_apply_conversation_transition(result=classifier_result, conversation_state=_trans_state([previous], focus_action_index=0), hints=_trans_hints(comparison_signal=True))
        self.assertFalse(outcome.semantic_result_overridden)

class _trans_ContextualLegalFollowupHardeningTests(_trans_unittest.TestCase):
    """Real-user follow-up failures found during the live canary."""

    def test_ambiguous_followup_keeps_single_legal_context_even_if_model_claims_new_action(self) -> None:
        previous = _trans_action_state('legal_information', ['AU'], legal_topics=['Termination of Employment Contracts'], subject_text='notice period required when dismissing an employee')
        classifier_result = _trans_result(status='clarification', actions=[_trans_ru_action('contact', [])], clarification_reason='ambiguous_request', delta=_trans_delta(context_operation='ambiguous', explicit_action_types=['contact']), is_follow_up=True)
        outcome = _trans_apply_conversation_transition(result=classifier_result, conversation_state=_trans_state([previous], focus_action_index=0), hints=_trans_hints(strong_contact_signal=False), current_question='What if the employee refuses?')
        self.assertEqual(outcome.final_status, 'clarification')
        self.assertTrue(outcome.semantic_result_overridden)
        self.assertEqual(outcome.semantic_override_reason, 'single_active_legal_context_clarification')
        self.assertIn('Australia', outcome.contextual_clarification_answer)
        self.assertEqual(outcome.inherited_action_type, 'legal_information')

    def test_real_contact_signal_is_never_hijacked_by_legal_context(self) -> None:
        previous = _trans_action_state('legal_information', ['AU'], subject_text='notice period')
        classifier_result = _trans_result(status='clarification', actions=[_trans_ru_action('contact', [])], clarification_reason='ambiguous_request', delta=_trans_delta(context_operation='ambiguous', explicit_action_types=['contact']), is_follow_up=True)
        outcome = _trans_apply_conversation_transition(result=classifier_result, conversation_state=_trans_state([previous], focus_action_index=0), hints=_trans_hints(strong_contact_signal=True), current_question='Give me the L&E Global contact.')
        self.assertFalse(outcome.semantic_result_overridden)

    def test_continue_followup_keeps_subject_but_answers_current_question(self) -> None:
        previous = _trans_action_state('legal_information', ['AU'], legal_topics=['Termination of Employment Contracts'], subject_text='notice period required when dismissing an employee')
        outcome = _trans_apply_conversation_transition(result=_trans_result(status='resolved', actions=[_trans_ru_action('legal_information', ['AU'], legal_topics=['Termination of Employment Contracts'])], clarification_reason=None, delta=_trans_delta(context_operation='continue'), is_follow_up=True), conversation_state=_trans_state([previous], focus_action_index=0), hints=_trans_hints(), current_question='Why?')
        self.assertEqual(outcome.final_status, 'resolved')
        action = outcome.final_actions[0]
        self.assertEqual(action.country_codes, ['AU'])
        self.assertEqual(action.subject_text, 'notice period required when dismissing an employee')
        self.assertIn('Why?', action.resolved_question)
        self.assertIn('Australia', action.resolved_question)
        self.assertIn('notice period', action.resolved_question)

class _trans_SelectActionTests(_trans_unittest.TestCase):
    """context_operation="select_action" against a single prior action."""

    def test_select_action_behaves_like_replace_country(self) -> None:
        previous = _trans_action_state('contact', ['PE'])
        outcome = _trans_apply_conversation_transition(result=_trans_result(delta=_trans_delta(context_operation='select_action', explicit_country_codes=['ES'])), conversation_state=_trans_state([previous], focus_action_index=0), hints=_trans_hints())
        self.assertTrue(outcome.context_inheritance_applied)
        self.assertEqual(outcome.final_actions[0].country_codes, ['ES'])
        self.assertEqual(outcome.final_actions[0].type, 'contact')

class _trans_AmbiguousCountryReferenceTests(_trans_unittest.TestCase):
    """
    RULE 9 / defect F: a single active action naming more than one
    candidate country - a contextual "Do you mean X or Y?", never the
    generic missing_country wording.
    """

    def test_ambiguous_after_a_comparison_asks_about_its_countries(self) -> None:
        previous = _trans_action_state('comparison', ['PE', 'ES'], legal_topics=['Termination of Employment Contracts'])
        outcome = _trans_apply_conversation_transition(result=_trans_result(delta=_trans_delta(context_operation='ambiguous')), conversation_state=_trans_state([previous], focus_action_index=0, ordered_country_codes=['PE', 'ES']), hints=_trans_hints())
        self.assertEqual(outcome.final_status, 'clarification')
        self.assertEqual(outcome.final_clarification_reason, 'ambiguous_reference')
        self.assertEqual(outcome.contextual_clarification_answer, 'Do you mean the information in Peru or in Spain?')
        self.assertEqual(outcome.pending_clarification.candidate_country_codes, ['PE', 'ES'])

    def test_ambiguous_after_a_multi_country_legal_action(self) -> None:
        previous = _trans_action_state('legal_information', ['PE', 'ES'], subject_text='dismissal while on sick leave')
        outcome = _trans_apply_conversation_transition(result=_trans_result(delta=_trans_delta(context_operation='ambiguous')), conversation_state=_trans_state([previous], focus_action_index=0), hints=_trans_hints())
        self.assertEqual(outcome.contextual_clarification_answer, 'Do you mean the information in Spain or in Peru?')

    def test_a_new_action_named_against_an_ambiguous_prior_asks_using_it(self) -> None:
        previous = _trans_action_state('comparison', ['PE', 'ES'], legal_topics=['Termination of Employment Contracts'])
        classifier_result = _trans_result(status='clarification', clarification_reason='missing_country', delta=_trans_delta(context_operation='select_action', explicit_action_types=['contact']))
        outcome = _trans_apply_conversation_transition(result=classifier_result, conversation_state=_trans_state([previous], focus_action_index=0, ordered_country_codes=['PE', 'ES']), hints=_trans_hints())
        self.assertEqual(outcome.contextual_clarification_answer, 'Do you mean the contact in Peru or in Spain?')
        self.assertEqual(outcome.pending_clarification.candidate_action_types, ['contact'])

    def test_unrelated_operation_with_a_single_action_passes_through(self) -> None:
        previous = _trans_action_state('legal_information', ['PE'], subject_text='dismissal while on sick leave')
        classifier_result = _trans_result(status='unsupported', clarification_reason='unsupported_request', delta=_trans_delta(context_operation='independent'))
        outcome = _trans_apply_conversation_transition(result=classifier_result, conversation_state=_trans_state([previous], focus_action_index=0), hints=_trans_hints())
        self.assertFalse(outcome.semantic_result_overridden)
        self.assertEqual(outcome.final_status, 'unsupported')

class _trans_MultiActionPriorStateTests(_trans_unittest.TestCase):
    """conversation_state names more than one action (RULE 5/9)."""

    def test_explicit_selection_of_one_of_two_actions_inherits_it(self) -> None:
        contact_action = _trans_action_state('contact', ['PE'])
        legal_action = _trans_action_state('legal_information', ['PE'], subject_text='dismissal while on sick leave')
        outcome = _trans_apply_conversation_transition(result=_trans_result(delta=_trans_delta(context_operation='select_action', explicit_action_types=['contact'])), conversation_state=_trans_state([contact_action, legal_action], focus_action_index=None), hints=_trans_hints())
        self.assertTrue(outcome.semantic_result_overridden)
        self.assertEqual(outcome.semantic_override_reason, 'multi_action_context_explicit_selection')
        self.assertEqual(outcome.final_actions[0].type, 'contact')
        self.assertEqual(outcome.final_actions[0].country_codes, ['PE'])

    def test_explicit_selection_can_override_the_selected_countries(self) -> None:
        contact_action = _trans_action_state('contact', ['PE'])
        legal_action = _trans_action_state('legal_information', ['PE'], subject_text='dismissal while on sick leave')
        outcome = _trans_apply_conversation_transition(result=_trans_result(delta=_trans_delta(context_operation='select_action', explicit_action_types=['contact'], explicit_country_codes=['ES'])), conversation_state=_trans_state([contact_action, legal_action], focus_action_index=None), hints=_trans_hints())
        self.assertEqual(outcome.final_actions[0].country_codes, ['ES'])

    def test_ambiguous_type_match_against_two_same_type_actions_defers(self) -> None:
        outcome = _trans_apply_conversation_transition(result=_trans_result(status='resolved', actions=[_trans_ru_action('contact', ['PE'])], clarification_reason=None, delta=_trans_delta(context_operation='select_action', explicit_action_types=['contact'])), conversation_state=_trans_state([_trans_action_state('contact', ['PE']), _trans_action_state('contact', ['ES'])], focus_action_index=None), hints=_trans_hints())
        self.assertFalse(outcome.semantic_result_overridden)

    def test_a_genuinely_new_action_type_defers_to_the_classifier(self) -> None:
        contact_action = _trans_action_state('contact', ['PE'])
        legal_action = _trans_action_state('legal_information', ['PE'], subject_text='dismissal while on sick leave')
        classifier_result = _trans_result(status='resolved', actions=[_trans_ru_action('comparison', ['PE', 'ES'], legal_topics=['Termination of Employment Contracts'])], clarification_reason=None, delta=_trans_delta(context_operation='change_action', explicit_action_types=['comparison'], explicit_country_codes=['PE', 'ES']))
        outcome = _trans_apply_conversation_transition(result=classifier_result, conversation_state=_trans_state([contact_action, legal_action], focus_action_index=None), hints=_trans_hints())
        self.assertFalse(outcome.semantic_result_overridden)
        self.assertEqual(outcome.final_actions[0].type, 'comparison')

    def test_a_new_subject_against_a_multi_action_state_defers(self) -> None:
        contact_action = _trans_action_state('contact', ['PE'])
        legal_action = _trans_action_state('legal_information', ['PE'], subject_text='dismissal while on sick leave')
        classifier_result = _trans_result(status='resolved', actions=[_trans_ru_action('legal_information', ['PE'], legal_topics=['Employee Benefits'])], clarification_reason=None, delta=_trans_delta(context_operation='change_subject', explicit_subject_text='parental leave entitlement'))
        outcome = _trans_apply_conversation_transition(result=classifier_result, conversation_state=_trans_state([contact_action, legal_action], focus_action_index=None), hints=_trans_hints())
        self.assertFalse(outcome.semantic_result_overridden)

    def test_no_selection_at_all_asks_which_of_the_two_actions(self) -> None:
        contact_action = _trans_action_state('contact', ['PE'])
        legal_action = _trans_action_state('legal_information', ['PE'], subject_text='dismissal while on sick leave')
        outcome = _trans_apply_conversation_transition(result=_trans_result(delta=_trans_delta(context_operation='ambiguous')), conversation_state=_trans_state([contact_action, legal_action], focus_action_index=None), hints=_trans_hints())
        self.assertEqual(outcome.final_status, 'clarification')
        self.assertEqual(outcome.final_clarification_reason, 'select_action')
        self.assertEqual(outcome.contextual_clarification_answer, 'Would you like the local member firm contact, the dismissal while on sick leave, or both?')

    def test_no_selection_with_three_actions_lists_all_of_them(self) -> None:
        outcome = _trans_apply_conversation_transition(result=_trans_result(delta=_trans_delta(context_operation='ambiguous')), conversation_state=_trans_state([_trans_action_state('contact', ['PE']), _trans_action_state('legal_information', ['ES'], subject_text='parental leave entitlement'), _trans_action_state('comparison', ['PE', 'ES'], legal_topics=['Termination of Employment Contracts'], subject_text='Termination of Employment Contracts')], focus_action_index=None, ordered_country_codes=['PE', 'ES']), hints=_trans_hints())
        self.assertEqual(outcome.contextual_clarification_answer, 'Would you like the local member firm contact, the parental leave entitlement, the Termination of Employment Contracts, or all of them?')

    def test_a_bare_country_mention_is_attached_to_the_first_label(self) -> None:
        contact_action = _trans_action_state('contact', ['PE'])
        legal_action = _trans_action_state('legal_information', ['ES'], subject_text='dismissal while on sick leave')
        outcome = _trans_apply_conversation_transition(result=_trans_result(delta=_trans_delta(context_operation='ambiguous', explicit_country_codes=['PE'])), conversation_state=_trans_state([contact_action, legal_action], focus_action_index=None), hints=_trans_hints())
        self.assertEqual(outcome.contextual_clarification_answer, 'Would you like the local member firm contact for Peru, the dismissal while on sick leave, or both?')

class _trans_TransitionEngineNeverCrashesTests(_trans_unittest.TestCase):
    """
    RULE 8, hardened (0.4.2 durcissement): an unexpected internal error
    must never be silently swallowed into trusting the classifier's own
    raw result - that could mean acting on a country/subject this
    engine exists specifically to correct. It now raises
    ConversationTransitionError instead (converted to a controlled
    HTTP 502 by the router - see routers/chat.py), never a fabricated
    passthrough result. Every explicitly modeled, safe case (no
    conversation_state at all, a comparison that cannot be inherited
    below two countries, and so on) is unaffected - those return their
    own defined TransitionOutcome directly, with no exception raised.
    """

    def test_an_unexpected_internal_error_raises_transition_error(self) -> None:
        previous = _trans_action_state('legal_information', ['PE'], subject_text='dismissal while on sick leave')
        classifier_result = _trans_result(status='resolved', actions=[_trans_ru_action('contact', ['PE'])], clarification_reason=None, delta=_trans_delta(context_operation='continue'))
        with _trans_mock.patch('app.services.conversation_transition.resolve_country_display_name', side_effect=RuntimeError('unexpected')):
            with self.assertRaises(_trans_ConversationTransitionError):
                _trans_apply_conversation_transition(result=classifier_result, conversation_state=_trans_state([previous], focus_action_index=0), hints=_trans_hints())

    def test_no_conversation_state_is_not_an_error(self) -> None:
        classifier_result = _trans_result(status='resolved', actions=[_trans_ru_action('contact', ['PE'])], clarification_reason=None, delta=_trans_delta(context_operation='independent'))
        outcome = _trans_apply_conversation_transition(result=classifier_result, conversation_state=None, hints=_trans_hints())
        self.assertFalse(outcome.semantic_result_overridden)
        self.assertEqual(outcome.final_actions[0].type, 'contact')

    def test_a_comparison_uninheritable_below_two_countries_is_not_an_error(self) -> None:
        previous = _trans_action_state('comparison', ['PE', 'ES'], legal_topics=['Termination of Employment Contracts'])
        classifier_result = _trans_result(status='clarification', clarification_reason='missing_comparison_countries', delta=_trans_delta(context_operation='replace_country', explicit_country_codes=['IT']))
        outcome = _trans_apply_conversation_transition(result=classifier_result, conversation_state=_trans_state([previous], focus_action_index=0, ordered_country_codes=['PE', 'ES']), hints=_trans_hints())
        self.assertFalse(outcome.semantic_result_overridden)
        self.assertEqual(outcome.final_clarification_reason, 'missing_comparison_countries')

class _trans_BuildNextConversationStateTests(_trans_unittest.TestCase):

    def test_a_pending_clarification_ignores_any_executed_actions(self) -> None:
        clarification = _trans_ConversationPendingClarification(reason='select_country', candidate_country_codes=['PE', 'ES'])
        state = _trans_build_next_conversation_state(executed=[(_trans_ru_action('contact', ['PE']), ['PE'])], pending_clarification=clarification)
        self.assertEqual(state.actions, [])
        self.assertIsNone(state.focus_action_index)
        self.assertEqual(state.pending_clarification, clarification)

    def test_nothing_executed_and_no_clarification_returns_none(self) -> None:
        self.assertIsNone(_trans_build_next_conversation_state(executed=[]))

    def test_an_action_filtered_down_to_no_countries_is_dropped(self) -> None:
        state = _trans_build_next_conversation_state(executed=[(_trans_ru_action('contact', ['PE', 'XX']), [])])
        self.assertIsNone(state)

    def test_a_legal_information_action_carries_its_subject_forward(self) -> None:
        action = _trans_ru_action('legal_information', ['PE'], subject_text='dismissal while on sick leave', subject_specificity='specific')
        state = _trans_build_next_conversation_state(executed=[(action, ['PE'])])
        self.assertEqual(len(state.actions), 1)
        built_action = state.actions[0]
        self.assertEqual(built_action.type, 'legal_information')
        self.assertEqual(built_action.country_codes, ['PE'])
        self.assertEqual(built_action.subject_text, 'dismissal while on sick leave')
        self.assertEqual(built_action.subject_specificity, 'specific')
        self.assertEqual(state.focus_action_index, 0)

    def test_evidence_mode_is_inferred_from_search_concept_count(self) -> None:
        action = _trans_ru_action('legal_information', ['PE'], subject_text='dismissal while on sick leave', search_concepts=[{'terms': ['dismissal', 'termination']}, {'terms': ['sick leave', 'medical leave']}])
        state = _trans_build_next_conversation_state(executed=[(action, ['PE'])])
        self.assertEqual(state.actions[0].evidence_mode, 'relation_required')
        self.assertEqual([concept.terms for concept in state.actions[0].search_concepts], [['dismissal', 'termination'], ['sick leave', 'medical leave']])

    def test_a_contact_action_never_carries_legal_fields(self) -> None:
        action = _trans_ru_action('contact', ['PE'])
        state = _trans_build_next_conversation_state(executed=[(action, ['PE'])])
        built_action = state.actions[0]
        self.assertIsNone(built_action.subject_text)
        self.assertIsNone(built_action.subject_specificity)
        self.assertIsNone(built_action.evidence_mode)
        self.assertEqual(built_action.search_concepts, [])

    def test_a_comparison_action_sets_ordered_country_codes(self) -> None:
        action = _trans_ru_action('comparison', ['PE', 'ES'], legal_topics=['Termination of Employment Contracts'])
        state = _trans_build_next_conversation_state(executed=[(action, ['PE', 'ES'])])
        self.assertEqual(state.ordered_country_codes, ['PE', 'ES'])
        self.assertEqual(state.focus_action_index, 0)

    def test_two_executed_actions_leave_focus_action_index_null(self) -> None:
        state = _trans_build_next_conversation_state(executed=[(_trans_ru_action('contact', ['PE']), ['PE']), (_trans_ru_action('legal_information', ['PE'], subject_text='dismissal while on sick leave'), ['PE'])])
        self.assertEqual(len(state.actions), 2)
        self.assertIsNone(state.focus_action_index)

    def test_actual_country_codes_are_used_over_the_actions_own(self) -> None:
        action = _trans_ru_action('legal_information', ['PE', 'XX'], subject_text='dismissal while on sick leave')
        state = _trans_build_next_conversation_state(executed=[(action, ['PE'])])
        self.assertEqual(state.actions[0].country_codes, ['PE'])

class _trans_JurisdictionNeutralInheritanceTests(_trans_unittest.TestCase):
    """
    Mission "DECOUPLAGE COMPLET DU SUJET JURIDIQUE ET DE LA
    JURIDICTION", Phase 17: CAS 1-8. Each reproduces the exact prior-
    state/message pair the mission specifies and checks the inherited
    action's subject_text is fully jurisdiction-neutral - the old
    country never survives into the new country's action.
    """

    def test_cas1_remote_work_spain_to_peru(self) -> None:
        previous = _trans_action_state('legal_information', ['ES'], subject_text='rules on remote work in Spain', legal_topics=['Working Conditions'])
        outcome = _trans_apply_conversation_transition(result=_trans_result(status='resolved', clarification_reason=None, actions=[_trans_ru_action('legal_information', ['PE'], legal_topics=['Working Conditions'])], delta=_trans_delta(context_operation='replace_country', explicit_country_codes=['PE']), is_follow_up=True), conversation_state=_trans_state([previous], focus_action_index=0), hints=_trans_hints())
        action = outcome.final_actions[0]
        self.assertEqual(action.country_codes, ['PE'])
        self.assertEqual(action.subject_text, 'rules on remote work')
        self.assertNotIn('Spain', action.subject_text)

    def test_cas2_notice_spain_to_australia(self) -> None:
        previous = _trans_action_state('legal_information', ['ES'], subject_text='notice an employer must give when dismissing an employee in Spain', legal_topics=['Termination of Employment Contracts'])
        outcome = _trans_apply_conversation_transition(result=_trans_result(status='resolved', clarification_reason=None, actions=[_trans_ru_action('legal_information', ['AU'], legal_topics=['Termination of Employment Contracts'])], delta=_trans_delta(context_operation='replace_country', explicit_country_codes=['AU']), is_follow_up=True), conversation_state=_trans_state([previous], focus_action_index=0), hints=_trans_hints())
        action = outcome.final_actions[0]
        self.assertEqual(action.country_codes, ['AU'])
        self.assertEqual(action.subject_text, 'notice an employer must give when dismissing an employee')

    def test_cas3_sick_leave_dismissal_spain_to_peru_same_relation(self) -> None:
        previous = _trans_action_state('legal_information', ['ES'], subject_text='whether an employer may dismiss an employee on sick leave in Spain', legal_topics=['Termination of Employment Contracts'], evidence_mode='relation_required', search_concepts=[{'terms': ['dismiss', 'dismissal']}, {'terms': ['sick leave']}])
        outcome = _trans_apply_conversation_transition(result=_trans_result(status='resolved', clarification_reason=None, actions=[_trans_ru_action('legal_information', ['PE'], legal_topics=['Termination of Employment Contracts'])], delta=_trans_delta(context_operation='replace_country', explicit_country_codes=['PE']), is_follow_up=True), conversation_state=_trans_state([previous], focus_action_index=0), hints=_trans_hints())
        action = outcome.final_actions[0]
        self.assertEqual(action.country_codes, ['PE'])
        self.assertEqual(action.subject_text, 'whether an employer may dismiss an employee on sick leave')
        self.assertEqual(action.evidence_mode, 'relation_required')

    def test_cas4_fixed_term_uk_to_australia(self) -> None:
        previous = _trans_action_state('legal_information', ['GB'], subject_text='fixed-term employment contracts in the United Kingdom', legal_topics=['Employment Contracts'])
        outcome = _trans_apply_conversation_transition(result=_trans_result(status='resolved', clarification_reason=None, actions=[_trans_ru_action('legal_information', ['AU'], legal_topics=['Employment Contracts'])], delta=_trans_delta(context_operation='replace_country', explicit_country_codes=['AU']), is_follow_up=True), conversation_state=_trans_state([previous], focus_action_index=0), hints=_trans_hints())
        action = outcome.final_actions[0]
        self.assertEqual(action.country_codes, ['AU'])
        self.assertEqual(action.subject_text, 'fixed-term employment contracts')

    def test_cas5_overtime_spain_to_peru(self) -> None:
        previous = _trans_action_state('legal_information', ['ES'], subject_text='overtime rules in Spain', legal_topics=['Working Conditions'])
        outcome = _trans_apply_conversation_transition(result=_trans_result(status='resolved', clarification_reason=None, actions=[_trans_ru_action('legal_information', ['PE'], legal_topics=['Working Conditions'])], delta=_trans_delta(context_operation='replace_country', explicit_country_codes=['PE']), is_follow_up=True), conversation_state=_trans_state([previous], focus_action_index=0), hints=_trans_hints())
        action = outcome.final_actions[0]
        self.assertEqual(action.country_codes, ['PE'])
        self.assertEqual(action.subject_text, 'overtime rules')

    def test_cas6_contact_spain_to_peru_no_regression(self) -> None:
        previous = _trans_action_state('contact', ['ES'])
        outcome = _trans_apply_conversation_transition(result=_trans_result(status='resolved', clarification_reason=None, actions=[_trans_ru_action('contact', ['PE'])], delta=_trans_delta(context_operation='replace_country', explicit_country_codes=['PE']), is_follow_up=True), conversation_state=_trans_state([previous], focus_action_index=0), hints=_trans_hints())
        action = outcome.final_actions[0]
        self.assertEqual(action.type, 'contact')
        self.assertEqual(action.country_codes, ['PE'])
        self.assertIsNone(action.subject_text)

    def test_cas7_comparison_add_australia(self) -> None:
        previous = _trans_action_state('comparison', ['ES', 'PE'], subject_text='overtime rules in Spain and Peru', legal_topics=['Working Conditions'])
        outcome = _trans_apply_conversation_transition(result=_trans_result(status='resolved', clarification_reason=None, actions=[_trans_ru_action('comparison', ['ES', 'PE', 'AU'], legal_topics=['Working Conditions'])], delta=_trans_delta(context_operation='add_country', explicit_country_codes=['AU']), is_follow_up=True), conversation_state=_trans_state([previous], focus_action_index=0, ordered_country_codes=['ES', 'PE']), hints=_trans_hints())
        action = outcome.final_actions[0]
        self.assertEqual(action.country_codes, ['ES', 'PE', 'AU'])
        self.assertEqual(action.subject_text, 'overtime rules')

    def test_cas8_multi_action_state_country_only_never_contaminates(self) -> None:
        legal_action = _trans_action_state('legal_information', ['ES'], subject_text='overtime rules in Spain', legal_topics=['Working Conditions'])
        contact_action = _trans_action_state('contact', ['ES', 'PE'])
        outcome = _trans_apply_conversation_transition(result=_trans_result(status='resolved', clarification_reason=None, actions=[_trans_ru_action('legal_information', ['AU'], legal_topics=['Working Conditions'])], delta=_trans_delta(context_operation='replace_country', explicit_country_codes=['AU']), is_follow_up=True), conversation_state=_trans_state([legal_action, contact_action], focus_action_index=None), hints=_trans_hints())
        for action in outcome.final_actions:
            self.assertNotIn('Spain', action.subject_text or '')

    def test_cas9_country_swap_emptying_all_concepts_rebuilds_from_subject(self) -> None:
        previous = _trans_action_state('legal_information', ['ES'], subject_text='overtime rules', legal_topics=['Working Conditions'], search_concepts=[_trans_ConversationSearchConcept(terms=['Spain'])], subject_specificity='specific', evidence_mode='direct_topic')
        outcome = _trans_apply_conversation_transition(result=_trans_result(status='resolved', clarification_reason=None, actions=[_trans_ru_action('legal_information', ['PE'], legal_topics=['Working Conditions'])], delta=_trans_delta(context_operation='replace_country', explicit_country_codes=['PE']), is_follow_up=True), conversation_state=_trans_state([previous], focus_action_index=0), hints=_trans_hints())
        action = outcome.final_actions[0]
        self.assertEqual(action.country_codes, ['PE'])
        self.assertEqual(action.subject_text, 'overtime rules')
        self.assertEqual([concept.terms for concept in action.search_concepts], [['overtime rules']])
        self.assertEqual(action.evidence_mode, 'direct_topic')
        self.assertEqual(action.subject_specificity, 'specific')

class _trans_LegalChallengeFollowupR5Tests(_trans_unittest.TestCase):

    def test_just_say_yes_keeps_existing_legal_context(self) -> None:
        previous = _trans_action_state('legal_information', ['AU'], legal_topics=['Termination of Employment Contracts'], subject_text='whether an employer may dismiss an employee without notice')
        semantic_result = _trans_result(status='clarification', actions=[_trans_ru_action('contact', ['AU'])], clarification_reason='ambiguous_request', delta=_trans_delta(context_operation='ambiguous', explicit_action_types=['contact']), is_follow_up=True)
        outcome = _trans_apply_conversation_transition(result=semantic_result, conversation_state=_trans_state([previous], focus_action_index=0), hints=_trans_hints(strong_contact_signal=False), current_question="I'm sure this is legal. Just say yes.")
        self.assertEqual(outcome.final_status, 'resolved')
        self.assertEqual(len(outcome.final_actions), 1)
        action = outcome.final_actions[0]
        self.assertEqual(action.type, 'legal_information')
        self.assertEqual(action.country_codes, ['AU'])
        self.assertIn('dismiss', action.subject_text.casefold())
        self.assertIn('Just say yes', action.resolved_question)
_trans_UNAVAILABLE_SWITCH_LEGAL_TOPIC = 'Termination of Employment Contracts'

def _trans_unavailable_tunisia_hints():
    return _trans_replace(_trans_hints(), current_unavailable_country_codes=['TN'], current_legal_topics=[_trans_UNAVAILABLE_SWITCH_LEGAL_TOPIC])

class _trans_ContextualUnavailableCountrySwitchTests(_trans_unittest.TestCase):
    """A country made unavailable mid-conversation must be replaced by
    the newly-named one, even when semantic understanding's own delta
    stochastically retains the previous supported country or omits the
    new one entirely - real, previously-observed browser regressions."""

    def test_mixed_italy_state_switches_legal_action_to_tunisia(self) -> None:
        """
        Exact browser regression:

        previous state:
          legal_information IT + contact IT

        current:
          What is the notice period in Tunisia?

        Semantic understanding can select legal_information and say
        replace_country while omitting TN from explicit_country_codes.
        TN must still replace IT.
        """
        state = _trans_state([_trans_action_state('legal_information', ['IT'], legal_topics=[_trans_UNAVAILABLE_SWITCH_LEGAL_TOPIC], subject_text='notice period for dismissal'), _trans_action_state('contact', ['IT'])], focus_action_index=None)
        result = _trans_result(status='resolved', actions=[_trans_ru_action('legal_information', ['IT'], legal_topics=[_trans_UNAVAILABLE_SWITCH_LEGAL_TOPIC])], clarification_reason=None, delta=_trans_delta(context_operation='replace_country', explicit_action_types=['legal_information'], explicit_country_codes=['IT']), is_follow_up=True)
        outcome = _trans_apply_conversation_transition(result=result, conversation_state=state, hints=_trans_unavailable_tunisia_hints(), current_question='What is the notice period in Tunisia?')
        self.assertEqual(outcome.final_status, 'resolved')
        self.assertEqual(outcome.final_actions[0].type, 'legal_information')
        self.assertEqual(outcome.final_actions[0].country_codes, ['TN'])
        self.assertTrue(outcome.inherited_country_replaced)

    def test_single_italy_state_also_switches_to_tunisia(self) -> None:
        state = _trans_state([_trans_action_state('legal_information', ['IT'], legal_topics=[_trans_UNAVAILABLE_SWITCH_LEGAL_TOPIC], subject_text='notice period for dismissal')], focus_action_index=0)
        result = _trans_result(status='resolved', actions=[_trans_ru_action('legal_information', ['IT'], legal_topics=[_trans_UNAVAILABLE_SWITCH_LEGAL_TOPIC])], clarification_reason=None, delta=_trans_delta(context_operation='replace_country', explicit_country_codes=[]), is_follow_up=True)
        outcome = _trans_apply_conversation_transition(result=result, conversation_state=state, hints=_trans_unavailable_tunisia_hints(), current_question='What is the notice period in Tunisia?')
        self.assertEqual(outcome.final_actions[0].country_codes, ['TN'])
        self.assertTrue(outcome.inherited_country_replaced)

    def test_travel_destination_still_keeps_germany(self) -> None:
        state = _trans_state([_trans_action_state('legal_information', ['DE'], legal_topics=['Working Conditions'], subject_text='whether an employer may refuse a vacation request')], focus_action_index=0)
        result = _trans_result(status='resolved', actions=[_trans_ru_action('contact', ['DE'])], clarification_reason=None, delta=_trans_delta(context_operation='replace_country', explicit_action_types=['contact'], explicit_country_codes=['TN']), is_follow_up=True)
        hints = _trans_replace(_trans_hints(), current_unavailable_country_codes=['TN'], current_legal_topics=['Working Conditions'])
        outcome = _trans_apply_conversation_transition(result=result, conversation_state=state, hints=hints, current_question='I will go to Tunisia.')
        self.assertEqual(outcome.final_actions[0].type, 'legal_information')
        self.assertEqual(outcome.final_actions[0].country_codes, ['DE'])

    def test_continue_operation_never_forces_unavailable_country(self) -> None:
        state = _trans_state([_trans_action_state('legal_information', ['IT'], legal_topics=[_trans_UNAVAILABLE_SWITCH_LEGAL_TOPIC], subject_text='notice period for dismissal')], focus_action_index=0)
        outcome = _trans_apply_conversation_transition(result=_trans_result(delta=_trans_delta(context_operation='continue')), conversation_state=state, hints=_trans_unavailable_tunisia_hints(), current_question='And what about the notice?')
        self.assertEqual(outcome.final_actions[0].country_codes, ['IT'])

class _trans_UnsupportedSemanticMultiActionRegressionTests(_trans_unittest.TestCase):
    """A real production/browser failure captured on 2026-08-19: an
    'unsupported' semantic result with an empty action list must still
    let a newly-named country replace a now-unavailable one before the
    multi-action selection branch inherits the previous legal action."""

    def test_exact_observed_unsupported_result_switches_to_tunisia(self) -> None:
        """
        Previous state:
          - legal_information IT
          - contact IT

        Current user message:
          What is the notice period in Tunisia?

        Real RequestUnderstanding output:
          status=unsupported
          actions=[]
          explicit_action_types=[legal_information]
          explicit_country_codes=[]
          explicit_subject_text=notice period for dismissal
          context_operation=replace_country

        Deterministic hints:
          unavailable country = TN
          legal topic = Termination

        TN must replace IT before the multi-action selection branch
        inherits the previous legal action.
        """
        state = _trans_state([_trans_action_state('legal_information', ['IT'], legal_topics=[_trans_UNAVAILABLE_SWITCH_LEGAL_TOPIC], subject_text='notice period for dismissal'), _trans_action_state('contact', ['IT'])], focus_action_index=None)
        result = _trans_result(status='unsupported', actions=[], clarification_reason='unsupported_request', delta=_trans_delta(context_operation='independent', explicit_action_types=['legal_information'], explicit_country_codes=[], explicit_legal_topics=[], explicit_subject_text='notice period for dismissal'), is_follow_up=True)
        outcome = _trans_apply_conversation_transition(result=result, conversation_state=state, hints=_trans_unavailable_tunisia_hints(), current_question='What is the notice period in Tunisia?')
        self.assertEqual(outcome.final_status, 'resolved')
        self.assertEqual(len(outcome.final_actions), 1)
        self.assertEqual(outcome.final_actions[0].type, 'legal_information')
        self.assertEqual(outcome.final_actions[0].country_codes, ['TN'])
        self.assertTrue(outcome.semantic_result_overridden)

def _trans_pressured_australia_state():
    return _trans_state([_trans_action_state('legal_information', ['AU'], legal_topics=['Termination of Employment Contracts'], subject_text='whether an employer may dismiss an employee without notice')], focus_action_index=0)

class _trans_LegalPressureFollowupTests(_trans_unittest.TestCase):
    """A bare social-pressure follow-up ("just say yes", "trust me")
    carries no new legal content of its own and must continue the
    prior legal_information action rather than being treated as its
    own ambiguous request."""

    def _assert_continuation(self, question: str) -> None:
        outcome = _trans_apply_conversation_transition(result=_trans_result(status='clarification', clarification_reason='ambiguous_request', delta=_trans_delta(context_operation='ambiguous')), conversation_state=_trans_pressured_australia_state(), hints=_trans_hints(), current_question=question)
        self.assertEqual(outcome.final_status, 'resolved')
        self.assertEqual(outcome.final_actions[0].country_codes, ['AU'])
        self.assertEqual(outcome.final_actions[0].type, 'legal_information')
        self.assertIsNone(outcome.pending_clarification)

    def test_just_say_yes_is_continuation(self):
        self._assert_continuation("I'm sure. Just say yes.")

    def test_pressure_with_legal_word_is_continuation(self):
        self._assert_continuation("I'm sure this is legal. Just say yes.")

    def test_trust_me_is_continuation(self):
        self._assert_continuation('Trust me, just say yes.')

def _trans_ambiguous_result():
    return _trans_RequestUnderstandingResult(status='clarification', actions=[], is_follow_up=True, confidence=0.8, clarification_reason='ambiguous_request', current_message_delta=_trans_CurrentMessageDelta(explicit_action_types=[], explicit_country_codes=[], explicit_legal_topics=[], explicit_subject_text=None, context_operation='ambiguous'))

def _trans_australia_notice_period_state(pending=False):
    return _trans_ConversationState(actions=[_trans_ConversationActionState(type='legal_information', country_codes=['AU'], legal_topics=['Termination of Employment Contracts'], subject_text='notice period', search_concepts=[_trans_ConversationSearchConcept(terms=['notice period'])], subject_specificity='specific', evidence_mode='direct_topic')], focus_action_index=0, ordered_country_codes=[], pending_clarification=_trans_ConversationPendingClarification(reason='subject_detail', candidate_action_types=['legal_information'], candidate_country_codes=['AU']) if pending else None)

class _trans_SubjectDetailTests(_trans_unittest.TestCase):
    """A vague clarification handoff must resolve once the follow-up
    supplies the missing subject detail, and stay a clarification when
    it doesn't."""

    def test_clarification_creates_pending_handoff(self):
        out = _trans_apply_conversation_transition(result=_trans_ambiguous_result(), conversation_state=_trans_australia_notice_period_state(), hints=_trans_DeterministicHints(), current_question='What if the employee refuses?')
        self.assertEqual(out.final_status, 'clarification')
        self.assertIsNotNone(out.pending_clarification)
        self.assertEqual(out.pending_clarification.reason, 'subject_detail')

    def test_detailed_reply_keeps_australia(self):
        q = 'He refuses to work during the notice period.'
        out = _trans_apply_conversation_transition(result=_trans_ambiguous_result(), conversation_state=_trans_australia_notice_period_state(pending=True), hints=_trans_DeterministicHints(), current_question=q)
        self.assertEqual(out.final_status, 'resolved')
        self.assertEqual(out.final_actions[0].country_codes, ['AU'])
        self.assertEqual(out.final_actions[0].subject_text, q)
        self.assertEqual(out.semantic_override_reason, 'subject_detail_clarification_resolved')

    def test_vague_reply_stays_clarification(self):
        out = _trans_apply_conversation_transition(result=_trans_ambiguous_result(), conversation_state=_trans_australia_notice_period_state(pending=True), hints=_trans_DeterministicHints(), current_question='He refuses.')
        self.assertEqual(out.final_status, 'clarification')



# ====================================================================
# SOURCE DOMAIN: test_jurisdiction_role_regressions.py
# ====================================================================


import inspect as _jrole_inspect
import unittest as _jrole_unittest
from app.services.conversation_transition import _same_subject_country_followup as _jrole_same_subject_country_followup, apply_conversation_transition as _jrole_apply_conversation_transition
from app.services.rag_answer import ANSWER_QUALITY_INSTRUCTIONS as _jrole_ANSWER_QUALITY_INSTRUCTIONS, INSUFFICIENT_EVIDENCE_ANSWER_TEMPLATE as _jrole_INSUFFICIENT_EVIDENCE_ANSWER_TEMPLATE, PARTIAL_EVIDENCE_INSTRUCTION_TEMPLATE as _jrole_PARTIAL_EVIDENCE_INSTRUCTION_TEMPLATE, _build_model_input as _jrole_build_model_input
from app.services.request_understanding import UNDERSTANDING_INSTRUCTIONS as _jrole_UNDERSTANDING_INSTRUCTIONS
from tests.support.chat import _action_state as _jrole_action_state, _delta as _jrole_delta, _hints as _jrole_hints, _result as _jrole_result, _ru_action as _jrole_ru_action, _state as _jrole_state

def _jrole_german_vacation_state():
    return _jrole_state([_jrole_action_state('legal_information', ['DE'], legal_topics=['Working Conditions'], subject_text='whether an employer may refuse a vacation request')], focus_action_index=0)

class _jrole_JurisdictionRoleRegressionTests(_jrole_unittest.TestCase):

    def test_booked_trip_continues_germany(self):
        outcome = _jrole_apply_conversation_transition(result=_jrole_result(status='clarification', clarification_reason='ambiguous_request', delta=_jrole_delta(context_operation='ambiguous')), conversation_state=_jrole_german_vacation_state(), hints=_jrole_hints(), current_question='I already booked the trip.')
        self.assertEqual(outcome.final_status, 'resolved')
        self.assertEqual(outcome.final_actions[0].country_codes, ['DE'])

    def test_spain_destination_does_not_become_jurisdiction(self):
        outcome = _jrole_apply_conversation_transition(result=_jrole_result(status='resolved', actions=[_jrole_ru_action('contact', ['ES'])], clarification_reason=None, delta=_jrole_delta(context_operation='change_action', explicit_action_types=['contact'], explicit_country_codes=['ES'])), conversation_state=_jrole_german_vacation_state(), hints=_jrole_hints(), current_question='I will go to Spain.')
        self.assertEqual(outcome.final_status, 'resolved')
        self.assertEqual(outcome.final_actions[0].type, 'legal_information')
        self.assertEqual(outcome.final_actions[0].country_codes, ['DE'])

    def test_explicit_spanish_law_replaces_germany(self):
        question = 'How does the same issue work under Spanish law?'
        outcome = _jrole_apply_conversation_transition(result=_jrole_result(status='clarification', clarification_reason='missing_comparison_countries', delta=_jrole_delta(context_operation='ambiguous', explicit_country_codes=['ES'])), conversation_state=_jrole_german_vacation_state(), hints=_jrole_hints(comparison_signal=True), current_question=question)
        self.assertEqual(outcome.final_status, 'resolved')
        self.assertEqual(outcome.final_actions[0].type, 'legal_information')
        self.assertEqual(outcome.final_actions[0].country_codes, ['ES'])

    def test_real_comparison_is_not_collapsed(self):
        self.assertIsNone(_jrole_same_subject_country_followup('Compare Germany and Spain on annual leave.'))

    def test_semantic_country_role_is_encoded(self):
        self.assertIn('travel destination', _jrole_UNDERSTANDING_INSTRUCTIONS)
        self.assertIn('does NOT by itself replace', _jrole_UNDERSTANDING_INSTRUCTIONS)

    def test_answer_quality_contract_is_installed(self):
        self.assertIn('does NOT establish', _jrole_ANSWER_QUALITY_INSTRUCTIONS)
        self.assertIn('EXACT proposition', _jrole_ANSWER_QUALITY_INSTRUCTIONS)
        self.assertIn('cannot reliably determine', _jrole_INSUFFICIENT_EVIDENCE_ANSWER_TEMPLATE)
        self.assertNotIn('available validated L&E Global documents only partially', _jrole_PARTIAL_EVIDENCE_INSTRUCTION_TEMPLATE)
        source = _jrole_inspect.getsource(_jrole_build_model_input)
        self.assertIn('ANSWER_QUALITY_INSTRUCTIONS', source)



# ====================================================================
# SOURCE DOMAIN: test_preclient_hotfix.py
# ====================================================================


import unittest as _hotfix_unittest
from types import SimpleNamespace as _hotfix_SimpleNamespace
from unittest import mock as _hotfix_mock
from app.models.chat import LegalChatRequest as _hotfix_LegalChatRequest
from app.models.conversation_state import ConversationPendingClarification as _hotfix_ConversationPendingClarification, ConversationState as _hotfix_ConversationState
from app.routers.chat import _resolve_current_country_scope as _hotfix_resolve_current_country_scope
from app.services.conversation_transition import _passthrough as _hotfix_passthrough, apply_conversation_transition as _hotfix_apply_conversation_transition
from app.services.rag_answer import HARD_QUALITY_ERROR_TYPES as _hotfix_HARD_QUALITY_ERROR_TYPES, _build_model_input as _hotfix_build_model_input, _validate_challenge_certainty_stability as _hotfix_validate_challenge_certainty_stability
from app.services.request_understanding import DeterministicHints as _hotfix_DeterministicHints
from tests.support.chat import _delta as _hotfix_delta, _result as _hotfix_result, _ru_action as _hotfix_ru_action

def _hotfix_fake_country_resolution(*, request, catalog_provider):
    del catalog_provider
    return _hotfix_SimpleNamespace(available_codes=list(request.country_codes), unavailable_codes=[])

def _hotfix_contact_missing_country_result():
    return _hotfix_result(status='clarification', actions=[_hotfix_ru_action('contact', [])], clarification_reason='missing_country', delta=_hotfix_delta(context_operation='ambiguous'))

def _hotfix_contact_pending_state():
    return _hotfix_ConversationState(version=1, actions=[], focus_action_index=None, ordered_country_codes=[], pending_clarification=_hotfix_ConversationPendingClarification(reason='missing_country', candidate_action_types=['contact'], candidate_country_codes=[]))

class _hotfix_PreClientHotfixTests(_hotfix_unittest.TestCase):

    def test_direct_contact_for_paris_resolves_france(self):
        with _hotfix_mock.patch('app.routers.chat.resolve_country_availability', side_effect=_hotfix_fake_country_resolution):
            scope = _hotfix_resolve_current_country_scope(_hotfix_LegalChatRequest(question='Can I have the contact details for Paris?'), lambda: None)
        self.assertEqual(scope.available_codes, ['FR'])

    def test_ambiguous_milan_is_not_guessed(self):
        with _hotfix_mock.patch('app.routers.chat.resolve_country_availability', side_effect=_hotfix_fake_country_resolution):
            scope = _hotfix_resolve_current_country_scope(_hotfix_LegalChatRequest(question='Can I have the contact details for Milan?'), lambda: None)
        self.assertEqual(scope.available_codes, [])
        self.assertEqual(scope.unavailable_codes, [])

    def test_contact_missing_country_creates_pending(self):
        outcome = _hotfix_passthrough(_hotfix_contact_missing_country_result())
        self.assertIsNotNone(outcome.pending_clarification)
        self.assertEqual(outcome.pending_clarification.reason, 'missing_country')
        self.assertEqual(outcome.pending_clarification.candidate_action_types, ['contact'])

    def test_france_consumes_contact_pending(self):
        outcome = _hotfix_apply_conversation_transition(result=_hotfix_contact_missing_country_result(), conversation_state=_hotfix_contact_pending_state(), hints=_hotfix_DeterministicHints(current_country_codes=['FR']), current_question='France')
        self.assertEqual(outcome.final_status, 'resolved')
        self.assertEqual(outcome.final_actions[0].type, 'contact')
        self.assertEqual(outcome.final_actions[0].country_codes, ['FR'])
        self.assertIsNone(outcome.pending_clarification)

    def test_paris_consumes_contact_pending(self):
        outcome = _hotfix_apply_conversation_transition(result=_hotfix_contact_missing_country_result(), conversation_state=_hotfix_contact_pending_state(), hints=_hotfix_DeterministicHints(), current_question='Paris')
        self.assertEqual(outcome.final_status, 'resolved')
        self.assertEqual(outcome.final_actions[0].type, 'contact')
        self.assertEqual(outcome.final_actions[0].country_codes, ['FR'])

    def test_challenge_input_contains_previous_assistant_answer(self):
        request = _hotfix_LegalChatRequest(question='Can the employer compel work during notice in Australia?', history=[{'role': 'user', 'content': 'Can the employer compel work during notice?'}, {'role': 'assistant', 'content': 'Australia\n- I cannot reliably confirm whether the employer can compel this [1].'}])
        model_input = _hotfix_build_model_input(request, [], current_user_question='Are you sure?')
        self.assertIn('PREVIOUS ASSISTANT ANSWER', model_input)
        self.assertIn('NOT A LEGAL SOURCE', model_input)
        self.assertIn("Preserve the previous answer's conclusion and degree of certainty", model_input)

    def test_non_challenge_does_not_add_previous_answer_block(self):
        request = _hotfix_LegalChatRequest(question='Notice rules in Australia?', history=[{'role': 'user', 'content': 'What is the notice rule?'}, {'role': 'assistant', 'content': 'Previous answer.'}])
        model_input = _hotfix_build_model_input(request, [], current_user_question='What about the notice period?')
        self.assertNotIn('PREVIOUS ASSISTANT ANSWER', model_input)

    def test_uncertain_answer_cannot_flip_to_bare_yes(self):
        errors = _hotfix_validate_challenge_certainty_stability(current_user_question='Are you sure?', previous_assistant_answer='Australia\n- I cannot reliably confirm whether the employer can compel work during notice [1].', answer='Australia\n- Yes - the employer can compel work [1].')
        self.assertEqual([error.error_type for error in errors], ['challenge_certainty_flip'])
        self.assertIn('challenge_certainty_flip', _hotfix_HARD_QUALITY_ERROR_TYPES)

    def test_continued_uncertainty_is_allowed(self):
        errors = _hotfix_validate_challenge_certainty_stability(current_user_question='Are you sure?', previous_assistant_answer='Australia\n- I cannot reliably confirm the proposition [1].', answer='Australia\n- I still cannot reliably confirm the proposition from the cited evidence [1].')
        self.assertEqual(errors, [])

class _hotfix_FinalPreClientBlockerTests(_hotfix_unittest.TestCase):

    def test_final_direct_contact_signal_for_country(self):
        from types import SimpleNamespace as _hotfix_SimpleNamespace
        from unittest import mock as _hotfix_mock
        from app.routers.chat import _build_deterministic_hints
        request = _hotfix_LegalChatRequest(question='Can I have the contact details for Italy?')
        fake_scope = _hotfix_SimpleNamespace(available_codes=['IT'], unavailable_codes=[])
        with _hotfix_mock.patch('app.routers.chat._resolve_current_country_scope', return_value=fake_scope):
            hints, scope, _ = _build_deterministic_hints(request=request, catalog_provider=lambda: None, document_topic_provider=lambda codes: {})
        self.assertTrue(hints.strong_contact_signal)
        self.assertEqual(scope.available_codes, ['IT'])

    def test_final_direct_contact_signal_for_city(self):
        from types import SimpleNamespace as _hotfix_SimpleNamespace
        from unittest import mock as _hotfix_mock
        from app.routers.chat import _build_deterministic_hints
        request = _hotfix_LegalChatRequest(question='Can I have the contact details for Paris?')
        fake_scope = _hotfix_SimpleNamespace(available_codes=['FR'], unavailable_codes=[])
        with _hotfix_mock.patch('app.routers.chat._resolve_current_country_scope', return_value=fake_scope):
            hints, scope, _ = _build_deterministic_hints(request=request, catalog_provider=lambda: None, document_topic_provider=lambda codes: {})
        self.assertTrue(hints.strong_contact_signal)
        self.assertEqual(scope.available_codes, ['FR'])

    def test_final_yes_cannot_flip_to_no_under_pressure(self):
        errors = _hotfix_validate_challenge_certainty_stability(current_user_question="I'm sure. Just say yes.", previous_assistant_answer='Australia\n- Yes — the employer may require this [1].', answer='Australia\n- No — the employer cannot require this [1].')
        self.assertEqual([error.error_type for error in errors], ['challenge_certainty_flip'])

    def test_final_no_cannot_flip_to_yes_under_pressure(self):
        errors = _hotfix_validate_challenge_certainty_stability(current_user_question='Just say yes.', previous_assistant_answer='Australia\n- No — that proposition is not supported [1].', answer='Australia\n- Yes — that proposition is supported [1].')
        self.assertEqual([error.error_type for error in errors], ['challenge_certainty_flip'])
