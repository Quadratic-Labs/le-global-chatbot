"""Consolidated test module generated during test-suite rationalisation."""

from __future__ import annotations


# ====================================================================
# SOURCE: test_legal_catalog.py
# ====================================================================

import unittest as _catalog_unittest
from typing import Any as _catalog_Any
from opensearchpy.exceptions import OpenSearchException as _catalog_OpenSearchException
from app.services.legal_catalog import LegalCatalogError as _catalog_LegalCatalogError, build_document_legal_topics_body as _catalog_build_document_legal_topics_body, build_legal_catalog_body as _catalog_build_legal_catalog_body, get_document_legal_topics_by_country as _catalog_get_document_legal_topics_by_country, get_legal_catalog as _catalog_get_legal_catalog
from app.services.opensearch_index import LEGAL_DOCUMENTS_ALIAS as _catalog_LEGAL_DOCUMENTS_ALIAS
from tests.support.opensearch import FakeOpenSearchClient as _catalog_FakeOpenSearchClient
_catalog_EMPTY_CATALOG_RESPONSE: dict[str, _catalog_Any] = {'aggregations': {'countries': {'buckets': []}, 'legal_topics': {'buckets': []}, 'subsections': {'buckets': []}}}

class _catalog_LegalCatalogTests(_catalog_unittest.TestCase):
    """Tests for legal catalog aggregation parsing."""

    def test_catalog_body_contains_required_aggregations(self) -> None:
        body = _catalog_build_legal_catalog_body()
        self.assertEqual(body['size'], 0)
        self.assertIn('countries', body['aggs'])
        self.assertIn('legal_topics', body['aggs'])
        self.assertIn('subsections', body['aggs'])

    def test_catalog_returns_structured_values(self) -> None:
        client = _catalog_FakeOpenSearchClient(response={'aggregations': {'countries': {'buckets': [{'key': 'GB', 'doc_count': 41, 'country_names': {'buckets': [{'key': 'United Kingdom', 'doc_count': 41}]}}, {'key': 'ES', 'doc_count': 49, 'country_names': {'buckets': [{'key': 'Spain', 'doc_count': 49}]}}]}, 'legal_topics': {'buckets': [{'key': 'Employment Contracts', 'doc_count': 25}]}, 'subsections': {'buckets': [{'key': 'Notice Period', 'doc_count': 12}]}}})
        response = _catalog_get_legal_catalog(client=client)
        self.assertEqual(client.index, _catalog_LEGAL_DOCUMENTS_ALIAS)
        self.assertEqual(len(response.countries), 2)
        self.assertEqual(response.countries[0].country_code, 'GB')
        self.assertEqual(response.countries[0].country, 'United Kingdom')
        self.assertEqual(response.countries[0].chunk_count, 41)
        self.assertEqual(response.legal_topics[0].value, 'Employment Contracts')
        self.assertEqual(response.subsections[0].value, 'Notice Period')

    def test_empty_catalog_is_supported(self) -> None:
        client = _catalog_FakeOpenSearchClient(response=_catalog_EMPTY_CATALOG_RESPONSE)
        response = _catalog_get_legal_catalog(client=client)
        self.assertEqual(response.countries, [])
        self.assertEqual(response.legal_topics, [])
        self.assertEqual(response.subsections, [])

    def test_opensearch_error_is_wrapped(self) -> None:
        client = _catalog_FakeOpenSearchClient(error=_catalog_OpenSearchException('Unavailable'))
        with self.assertRaises(_catalog_LegalCatalogError):
            _catalog_get_legal_catalog(client=client)

class _catalog_DocumentLegalTopicsByCountryTests(_catalog_unittest.TestCase):
    """
    Tests for get_document_legal_topics_by_country (mission
    "ORDER 8F-A") - one compact, country-scoped aggregation for the
    LIVE legal_topic vocabulary actually indexed, distinct from
    get_legal_catalog's own global, unscoped aggregation.
    """

    def test_empty_country_codes_makes_no_opensearch_call(self) -> None:
        client = _catalog_FakeOpenSearchClient()
        result = _catalog_get_document_legal_topics_by_country([], client=client)
        self.assertEqual(result, {})
        self.assertIsNone(client.index)

    def test_body_is_scoped_to_requested_countries(self) -> None:
        body = _catalog_build_document_legal_topics_body(['au', 'AU', ' be '])
        self.assertEqual(body['size'], 0)
        self.assertEqual(body['query']['terms']['country_code'], ['AU', 'BE'])
        self.assertIn('countries', body['aggs'])
        self.assertIn('legal_topics', body['aggs']['countries']['aggs'])

    def test_returns_canonical_and_custom_topics_per_country(self) -> None:
        client = _catalog_FakeOpenSearchClient(response={'aggregations': {'countries': {'buckets': [{'key': 'AU', 'doc_count': 3, 'legal_topics': {'buckets': [{'key': 'Hiring Practices', 'doc_count': 1}, {'key': 'V060 Temporary Validation Section', 'doc_count': 1}, {'key': 'Foreign Employee Work Eligibility Checks', 'doc_count': 1}]}}, {'key': 'BE', 'doc_count': 1, 'legal_topics': {'buckets': [{'key': 'Hiring Practices', 'doc_count': 1}]}}]}}})
        result = _catalog_get_document_legal_topics_by_country(['AU', 'BE'], client=client)
        self.assertEqual(client.index, _catalog_LEGAL_DOCUMENTS_ALIAS)
        self.assertEqual(result, {'AU': ['Hiring Practices', 'V060 Temporary Validation Section', 'Foreign Employee Work Eligibility Checks'], 'BE': ['Hiring Practices']})

    def test_country_with_no_indexed_topics_is_absent(self) -> None:
        client = _catalog_FakeOpenSearchClient(response={'aggregations': {'countries': {'buckets': []}}})
        result = _catalog_get_document_legal_topics_by_country(['ZZ'], client=client)
        self.assertEqual(result, {})

    def test_opensearch_error_is_wrapped(self) -> None:
        client = _catalog_FakeOpenSearchClient(error=_catalog_OpenSearchException('Unavailable'))
        with self.assertRaises(_catalog_LegalCatalogError):
            _catalog_get_document_legal_topics_by_country(['AU'], client=client)


# ====================================================================
# SOURCE: test_legal_search.py
# ====================================================================

import unittest as _search_unittest
from typing import Any as _search_Any
from opensearchpy.exceptions import OpenSearchException as _search_OpenSearchException
from app.models.search import LegalSearchRequest as _search_LegalSearchRequest
from app.services.legal_search import InvalidLegalSearchRequestError as _search_InvalidLegalSearchRequestError, LegalSearchError as _search_LegalSearchError, build_contact_lookup_body as _search_build_contact_lookup_body, build_legal_search_body as _search_build_legal_search_body, search_contact_chunks as _search_search_contact_chunks, search_legal_documents as _search_search_legal_documents
from app.services.opensearch_index import LEGAL_DOCUMENTS_ALIAS as _search_LEGAL_DOCUMENTS_ALIAS
from tests.support.opensearch import FakeOpenSearchClient as _search_FakeOpenSearchClient
_search_EMPTY_SEARCH_RESPONSE: dict[str, _search_Any] = {'took': 0, 'hits': {'total': {'value': 0}, 'hits': []}}

class _search_LegalSearchTests(_search_unittest.TestCase):
    """Unit tests for legal BM25 search."""

    def test_build_query_with_filters(self) -> None:
        request = _search_LegalSearchRequest(query=' notice period ', country_codes=['gb', 'GB', ' fr '], legal_topics=['Employment Termination', 'Employment Termination'], subsections=['Notice Period'], reference_year=2026, limit=5, offset=10)
        body = _search_build_legal_search_body(request)
        self.assertEqual(body['from'], 10)
        self.assertEqual(body['size'], 5)
        multi_match = body['query']['bool']['must'][0]['multi_match']
        self.assertEqual(multi_match['query'], 'notice period')
        self.assertEqual(multi_match['fields'], ['content^5', 'subsection^3', 'section^2'])
        filters = body['query']['bool']['filter']
        self.assertIn({'terms': {'country_code': ['GB', 'FR']}}, filters)
        self.assertIn({'terms': {'legal_topic': ['Employment Termination']}}, filters)
        self.assertIn({'terms': {'subsection.keyword': ['Notice Period']}}, filters)
        self.assertIn({'term': {'language': 'en'}}, filters)
        self.assertIn({'term': {'reference_year': 2026}}, filters)

    def test_minimum_should_match_relaxed_with_country_filter(self) -> None:
        request = _search_LegalSearchRequest(query='overtime rules', country_codes=['GB'])
        body = _search_build_legal_search_body(request)
        multi_match = body['query']['bool']['must'][0]['multi_match']
        self.assertEqual(multi_match['minimum_should_match'], '1')

    def test_minimum_should_match_relaxed_with_topic_filter(self) -> None:
        request = _search_LegalSearchRequest(query='overtime rules', legal_topics=['Working Conditions'])
        body = _search_build_legal_search_body(request)
        multi_match = body['query']['bool']['must'][0]['multi_match']
        self.assertEqual(multi_match['minimum_should_match'], '1')

    def test_minimum_should_match_relaxed_with_subsection_filter(self) -> None:
        request = _search_LegalSearchRequest(query='overtime rules', subsections=['Overtime'])
        body = _search_build_legal_search_body(request)
        multi_match = body['query']['bool']['must'][0]['multi_match']
        self.assertEqual(multi_match['minimum_should_match'], '1')

    def test_minimum_should_match_strict_without_filters(self) -> None:
        request = _search_LegalSearchRequest(query='overtime rules')
        body = _search_build_legal_search_body(request)
        multi_match = body['query']['bool']['must'][0]['multi_match']
        self.assertEqual(multi_match['minimum_should_match'], '70%')

    def test_search_returns_structured_hits(self) -> None:
        client = _search_FakeOpenSearchClient(response={'took': 7, 'hits': {'total': {'value': 1}, 'hits': [{'_score': 12.5, '_source': {'document_id': 'document-1', 'chunk_id': 'chunk-1', 'country': 'United Kingdom', 'country_code': 'GB', 'legal_topic': 'Employment Termination', 'document_type': 'employment_law_overview', 'language': 'en', 'section': 'Employment Termination', 'subsection': 'Notice Period', 'content': 'The applicable notice period depends on the contract.', 'source_filename': 'Labour and Employment Law in UK 2026.docx', 'source_format': 'docx', 'reference_year': 2026}}]}})
        response = _search_search_legal_documents(request=_search_LegalSearchRequest(query='notice period', country_codes=['GB']), client=client)
        self.assertEqual(client.index, _search_LEGAL_DOCUMENTS_ALIAS)
        self.assertEqual(response.total, 1)
        self.assertEqual(response.took_ms, 7)
        self.assertEqual(len(response.hits), 1)
        self.assertEqual(response.hits[0].chunk_id, 'chunk-1')
        self.assertEqual(response.hits[0].country_code, 'GB')
        self.assertEqual(response.hits[0].score, 12.5)

    def test_search_returns_empty_result(self) -> None:
        client = _search_FakeOpenSearchClient(response=_search_EMPTY_SEARCH_RESPONSE)
        response = _search_search_legal_documents(request=_search_LegalSearchRequest(query='collective dismissal'), client=client)
        self.assertEqual(response.total, 0)
        self.assertEqual(response.hits, [])

    def test_blank_normalized_query_is_rejected(self) -> None:
        request = _search_LegalSearchRequest(query='  ')
        with self.assertRaises(_search_InvalidLegalSearchRequestError):
            _search_build_legal_search_body(request)

    def test_opensearch_errors_are_wrapped(self) -> None:
        client = _search_FakeOpenSearchClient(error=_search_OpenSearchException('Unavailable'))
        with self.assertRaises(_search_LegalSearchError):
            _search_search_legal_documents(request=_search_LegalSearchRequest(query='notice period'), client=client)

    def test_contact_subsection_is_excluded_by_default(self) -> None:
        request = _search_LegalSearchRequest(query='notice period', country_codes=['PE'])
        body = _search_build_legal_search_body(request)
        self.assertIn({'term': {'subsection.keyword': 'Contact'}}, body['query']['bool']['must_not'])

    def test_contact_subsection_not_excluded_when_explicitly_requested(self) -> None:
        request = _search_LegalSearchRequest(query='notice period', country_codes=['PE'], subsections=['Contact'])
        body = _search_build_legal_search_body(request)
        self.assertNotIn({'term': {'subsection.keyword': 'Contact'}}, body['query']['bool']['must_not'])

    def test_contact_lookup_body_filters_by_country_and_subsection(self) -> None:
        body = _search_build_contact_lookup_body(['pe', 'PE', ' au '])
        self.assertIn({'terms': {'country_code': ['PE', 'AU']}}, body['query']['bool']['filter'])
        self.assertIn({'term': {'subsection.keyword': 'Contact'}}, body['query']['bool']['filter'])
        self.assertNotIn('must', body['query']['bool'])

    def test_search_contact_chunks_returns_hits(self) -> None:
        client = _search_FakeOpenSearchClient(response={'took': 3, 'hits': {'total': {'value': 1}, 'hits': [{'_score': 0.0, '_source': {'document_id': 'document-1', 'chunk_id': 'chunk-1-contact', 'country': 'Peru', 'country_code': 'PE', 'legal_topic': None, 'document_type': 'overview', 'language': 'en', 'section': 'Employment Law Overview Peru', 'subsection': 'Contact', 'content': 'Member firm: Test\nEmail: x@example.com', 'source_filename': 'Employment Law Overview Peru 2026.docx', 'source_format': 'docx', 'reference_year': 2026}}]}})
        response = _search_search_contact_chunks(country_codes=['PE'], client=client)
        self.assertEqual(client.index, _search_LEGAL_DOCUMENTS_ALIAS)
        self.assertEqual(response.total, 1)
        self.assertEqual(len(response.hits), 1)
        self.assertEqual(response.hits[0].subsection, 'Contact')

    def test_search_contact_chunks_with_no_countries_skips_opensearch(self) -> None:

        def _unexpected_search(index: str, body: dict[str, _search_Any]) -> dict[str, _search_Any]:
            raise AssertionError('OpenSearch must not be called with no country codes.')
        client = _search_FakeOpenSearchClient()
        client.search = _unexpected_search
        response = _search_search_contact_chunks(country_codes=[], client=client)
        self.assertEqual(response.total, 0)
        self.assertEqual(response.hits, [])

    def test_search_contact_chunks_wraps_opensearch_errors(self) -> None:
        client = _search_FakeOpenSearchClient(error=_search_OpenSearchException('Unavailable'))
        with self.assertRaises(_search_LegalSearchError):
            _search_search_contact_chunks(country_codes=['PE'], client=client)

class _search_DocumentLegalTopicFilterTests(_search_unittest.TestCase):
    """
    Retrieval tests for mission "ORDER 8F-A", section 14 - the
    legal_topic terms filter is completely generic (any string value),
    so a live, Admin-created custom section title must filter exactly
    like a canonical topic - no special-casing required anywhere in
    this module. These four scenarios mirror a realistic, seeded
    Australia corpus: one canonical topic and two custom section
    titles, one of which (section B) deliberately overlaps a canonical
    trigger phrase's semantics without being collapsed to it.
    """

    def test_canonical_topic_filters_exactly(self) -> None:
        request = _search_LegalSearchRequest(query='hiring rules', country_codes=['AU'], legal_topics=['Hiring Practices'])
        body = _search_build_legal_search_body(request)
        self.assertIn({'terms': {'legal_topic': ['Hiring Practices']}}, body['query']['bool']['filter'])

    def test_custom_section_title_filters_exactly(self) -> None:
        request = _search_LegalSearchRequest(query='foreign employee work eligibility checks', country_codes=['AU'], legal_topics=['Foreign Employee Work Eligibility Checks'])
        body = _search_build_legal_search_body(request)
        self.assertIn({'terms': {'legal_topic': ['Foreign Employee Work Eligibility Checks']}}, body['query']['bool']['filter'])

    def test_other_custom_section_title_filters_exactly(self) -> None:
        request = _search_LegalSearchRequest(query='V060 Temporary Validation Section', country_codes=['AU'], legal_topics=['V060 Temporary Validation Section'])
        body = _search_build_legal_search_body(request)
        self.assertIn({'terms': {'legal_topic': ['V060 Temporary Validation Section']}}, body['query']['bool']['filter'])

    def test_topic_text_only_omits_any_topic_filter(self) -> None:
        """
        Scenario C from the mission's own retrieval-filter priority
        (section 7): when neither a canonical nor a document topic
        resolved, no hard legal_topic filter is fabricated at all -
        retrieval stays country-scoped free text across every section,
        canonical or custom.
        """
        request = _search_LegalSearchRequest(query='temporary validation', country_codes=['AU'])
        body = _search_build_legal_search_body(request)
        filters = body['query']['bool']['filter']
        self.assertFalse(any(('legal_topic' in one_filter.get('terms', {}) for one_filter in filters)))


# ====================================================================
# SOURCE: test_legal_subject_scope.py
# ====================================================================

import time as _subject_time
import unittest as _subject_unittest
from app.services.legal_subject_scope import CanonicalSearchConcept as _subject_CanonicalSearchConcept, canonicalize_legal_subject as _subject_canonicalize_legal_subject

class _subject_CanonicalizeSubjectTextParameterizedTests(_subject_unittest.TestCase):
    """Phase 15's 25 parametrized cases."""

    def _assert_subject(self, subject_text, country_codes, expected, *, msg=None):
        result = _subject_canonicalize_legal_subject(subject_text=subject_text, search_concepts=[], scoped_country_codes=country_codes)
        self.assertEqual(result.subject_text, expected, msg=msg)

    def test_01_remote_work_in_spain(self):
        self._assert_subject('remote work in Spain', ['ES'], 'remote work')

    def test_02_remote_work_for_spain(self):
        self._assert_subject('remote work for Spain', ['ES'], 'remote work')

    def test_03_remote_work_under_spanish_law(self):
        self._assert_subject('remote work under Spanish law', ['ES'], 'remote work')

    def test_04_under_spanish_employment_law_leading(self):
        self._assert_subject('under Spanish employment law, remote work rules', ['ES'], 'remote work rules')

    def test_05_spain_colon(self):
        self._assert_subject('Spain: remote work rules', ['ES'], 'remote work rules')

    def test_06_spain_possessive(self):
        self._assert_subject("Spain's rules on remote work", ['ES'], 'rules on remote work')

    def test_07_dismissal_sick_leave_peru(self):
        self._assert_subject('dismissal while on sick leave in Peru', ['PE'], 'dismissal while on sick leave')

    def test_08_fixed_term_united_kingdom_with_the(self):
        self._assert_subject('fixed-term contracts in the United Kingdom', ['GB'], 'fixed-term contracts')

    def test_09_overtime_australia(self):
        self._assert_subject('overtime rules in Australia', ['AU'], 'overtime rules')

    def test_10_overtime_sydney_no_city_taxonomy(self):
        result = _subject_canonicalize_legal_subject(subject_text='overtime rules in Sydney', search_concepts=[], scoped_country_codes=['AU'])
        self.assertEqual(result.subject_text, 'overtime rules in Sydney')
        self.assertFalse(result.changed)

    def test_11_overtime_spain_and_peru(self):
        self._assert_subject('overtime rules in Spain and Peru', ['ES', 'PE'], 'overtime rules')

    def test_12_compare_overtime_between_spain_and_peru(self):
        self._assert_subject('compare overtime between Spain and Peru', ['ES', 'PE'], 'compare overtime')

    def test_13_no_country_unchanged(self):
        result = _subject_canonicalize_legal_subject(subject_text='overtime rules', search_concepts=[], scoped_country_codes=['ES'])
        self.assertEqual(result.subject_text, 'overtime rules')
        self.assertFalse(result.changed)
        self.assertEqual(result.removed_country_codes, [])

    def test_14_different_case(self):
        self._assert_subject('REMOTE WORK IN spain', ['ES'], 'REMOTE WORK')

    def test_15_multiple_spaces(self):
        self._assert_subject('remote  work   in Spain', ['ES'], 'remote work')

    def test_16_straight_and_typographic_apostrophe(self):
        self._assert_subject("Spain's rules on overtime", ['ES'], 'rules on overtime', msg='straight apostrophe')
        self._assert_subject('Spain’s rules on overtime', ['ES'], 'rules on overtime', msg='typographic right single quote')

    def test_17_unicode_dashes_preserved_in_content(self):
        result = _subject_canonicalize_legal_subject(subject_text='fixed–term contracts in Spain', search_concepts=[], scoped_country_codes=['ES'])
        self.assertEqual(result.subject_text, 'fixed–term contracts')

    def test_18_terminal_punctuation_stripped(self):
        self._assert_subject('remote work in Spain.', ['ES'], 'remote work.')
        self._assert_subject('remote work in Spain,', ['ES'], 'remote work')

    def test_19_empty_result_flagged(self):
        result = _subject_canonicalize_legal_subject(subject_text='Spain', search_concepts=[], scoped_country_codes=['ES'])
        self.assertIsNone(result.subject_text)
        self.assertTrue(result.subject_became_empty)
        self.assertTrue(result.changed)

    def test_20_long_text_not_expanded(self):
        long_subject = 'overtime rules in Spain ' + 'x' * 500
        result = _subject_canonicalize_legal_subject(subject_text=long_subject, search_concepts=[], scoped_country_codes=['ES'])
        self.assertLessEqual(len(result.subject_text), len(long_subject))

    def test_21_unicode_characters_do_not_crash(self):
        result = _subject_canonicalize_legal_subject(subject_text='rémunération rules in Spain', search_concepts=[], scoped_country_codes=['ES'])
        self.assertEqual(result.subject_text, 'rémunération rules')

    def test_22_uk_short_alias(self):
        self._assert_subject('fixed-term contracts in the UK', ['GB'], 'fixed-term contracts')
        self._assert_subject('fixed-term contracts under UK law', ['GB'], 'fixed-term contracts')

    def test_23_uk_dotted_alias(self):
        self._assert_subject('fixed-term contracts in the U.K.', ['GB'], 'fixed-term contracts')

    def test_24_british_demonym(self):
        self._assert_subject('overtime rules under British law', ['GB'], 'overtime rules')

    def test_25_no_dangerous_partial_replacement(self):
        result = _subject_canonicalize_legal_subject(subject_text='Spainish-sounding trademark dispute rules', search_concepts=[], scoped_country_codes=['ES'])
        self.assertEqual(result.subject_text, 'Spainish-sounding trademark dispute rules')
        self.assertFalse(result.changed)

class _subject_CanonicalizePreservesLegalContentTests(_subject_unittest.TestCase):
    """Invariant 8: never mangle a law/institution name."""

    def test_fair_work_act_preserved(self):
        result = _subject_canonicalize_legal_subject(subject_text='whether the Fair Work Act applies to casual employees in Australia', search_concepts=[], scoped_country_codes=['AU'])
        self.assertIn('Fair Work Act', result.subject_text)
        self.assertNotIn('Australia', result.subject_text)

    def test_workers_statute_preserved(self):
        result = _subject_canonicalize_legal_subject(subject_text="whether the Workers' Statute permits remote work in Spain", search_concepts=[], scoped_country_codes=['ES'])
        self.assertIn("Workers' Statute", result.subject_text)
        self.assertNotIn(' in Spain', result.subject_text)

    def test_national_employment_standards_preserved(self):
        result = _subject_canonicalize_legal_subject(subject_text='how the National Employment Standards apply to overtime in Australia', search_concepts=[], scoped_country_codes=['AU'])
        self.assertIn('National Employment Standards', result.subject_text)

    def test_labour_inspectorate_preserved(self):
        result = _subject_canonicalize_legal_subject(subject_text='the powers of the Labour Inspectorate in Spain', search_concepts=[], scoped_country_codes=['ES'])
        self.assertIn('Labour Inspectorate', result.subject_text)
        self.assertNotIn('Spain', result.subject_text)

    def test_institution_name_not_used_as_scope_untouched(self):
        result = _subject_canonicalize_legal_subject(subject_text='the National Employment Standards', search_concepts=[], scoped_country_codes=['AU'])
        self.assertEqual(result.subject_text, 'the National Employment Standards')
        self.assertFalse(result.changed)

class _subject_CanonicalizeSearchConceptsTests(_subject_unittest.TestCase):

    def test_contaminated_terms_are_cleaned(self):
        result = _subject_canonicalize_legal_subject(subject_text=None, search_concepts=[_subject_CanonicalSearchConcept(terms=['remote work in Spain', 'telework in Spain', 'working from home'])], scoped_country_codes=['ES'])
        self.assertEqual(len(result.search_concepts), 1)
        self.assertEqual(result.search_concepts[0].terms, ['remote work', 'telework', 'working from home'])
        self.assertTrue(result.changed)

    def test_group_that_becomes_fully_empty_is_dropped(self):
        result = _subject_canonicalize_legal_subject(subject_text=None, search_concepts=[_subject_CanonicalSearchConcept(terms=['Spain']), _subject_CanonicalSearchConcept(terms=['overtime'])], scoped_country_codes=['ES'])
        self.assertEqual(len(result.search_concepts), 1)
        self.assertEqual(result.search_concepts[0].terms, ['overtime'])

    def test_duplicate_terms_after_stripping_are_deduplicated(self):
        result = _subject_canonicalize_legal_subject(subject_text=None, search_concepts=[_subject_CanonicalSearchConcept(terms=['remote work in Spain', 'remote work for Spain'])], scoped_country_codes=['ES'])
        self.assertEqual(result.search_concepts[0].terms, ['remote work'])

    def test_evidence_mode_and_untouched_fields_are_the_callers_concern(self):
        result = _subject_canonicalize_legal_subject(subject_text='remote work in Spain', search_concepts=[], scoped_country_codes=['ES'])
        self.assertFalse(hasattr(result, 'evidence_mode'))

class _subject_RemovedCountryCodesTests(_subject_unittest.TestCase):

    def test_only_actually_present_codes_are_reported(self):
        result = _subject_canonicalize_legal_subject(subject_text='overtime rules in Spain', search_concepts=[], scoped_country_codes=['ES', 'PE'])
        self.assertEqual(result.removed_country_codes, ['ES'])

    def test_additional_country_codes_are_also_checked(self):
        result = _subject_canonicalize_legal_subject(subject_text='overtime rules in Spain', search_concepts=[], scoped_country_codes=['PE'], additional_country_codes=['ES'])
        self.assertIn('ES', result.removed_country_codes)
        self.assertEqual(result.subject_text, 'overtime rules')

    def test_no_country_codes_is_a_no_op(self):
        result = _subject_canonicalize_legal_subject(subject_text='overtime rules in Spain', search_concepts=[], scoped_country_codes=[])
        self.assertEqual(result.subject_text, 'overtime rules in Spain')
        self.assertFalse(result.changed)
        self.assertEqual(result.removed_country_codes, [])

class _subject_PerformanceBoundTests(_subject_unittest.TestCase):
    """Phase 22: no network call, bounded, negligible cost."""

    def test_no_network_related_imports(self):
        import ast
        import inspect
        import app.services.legal_subject_scope as module
        tree = ast.parse(inspect.getsource(module))
        imported_top_level_modules: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported_top_level_modules.add(alias.name.split('.')[0])
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_top_level_modules.add(node.module.split('.')[0])
        forbidden_modules = {'openai', 'httpx', 'requests', 'urllib3', 'urllib'}
        self.assertEqual(imported_top_level_modules & forbidden_modules, set())

    def test_a_thousand_calls_complete_in_well_under_a_second(self):
        start = _subject_time.perf_counter()
        for _ in range(1000):
            _subject_canonicalize_legal_subject(subject_text='overtime rules in Spain and Peru', search_concepts=[_subject_CanonicalSearchConcept(terms=['overtime in Spain', 'extra hours'])], scoped_country_codes=['ES', 'PE'])
        elapsed = _subject_time.perf_counter() - start
        self.assertLess(elapsed, 1.0)


# ====================================================================
# SOURCE: test_legal_taxonomy.py
# ====================================================================

import unittest as _taxonomy_unittest
from app.core.legal_taxonomy import get_canonical_legal_topic as _taxonomy_get_canonical_legal_topic, is_overview_section as _taxonomy_is_overview_section, normalize_topic as _taxonomy_normalize_topic

class _taxonomy_LegalTaxonomyTests(_taxonomy_unittest.TestCase):

    def test_normalizes_standard_numbered_topic(self) -> None:
        self.assertEqual(_taxonomy_normalize_topic(section='01. Hiring Practices', country='Spain'), 'Hiring Practices')

    def test_removes_leading_separator(self) -> None:
        self.assertEqual(_taxonomy_get_canonical_legal_topic(section='| 05. Pay Equity Laws', country='Belgium'), 'Pay Equity Laws')

    def test_removes_trailing_separator(self) -> None:
        self.assertEqual(_taxonomy_get_canonical_legal_topic(section='Restrictive Covenants in Australia|', country='Australia'), 'Restrictive Covenants')

    def test_recognizes_plain_canonical_topic(self) -> None:
        self.assertEqual(_taxonomy_get_canonical_legal_topic(section='Pay Equity Laws'), 'Pay Equity Laws')

    def test_recognizes_canonical_topic_with_trailing_annotation(self) -> None:
        """
        GATE 0B.4 / PT_PAY_EQUITY_FINDING: a contributor's harmless
        "(NEW SECTION)" editorial annotation must not hide an
        otherwise-canonical heading from taxonomy matching.
        """
        self.assertEqual(_taxonomy_get_canonical_legal_topic(section='PAY EQUITY LAWS (NEW SECTION)'), 'Pay Equity Laws')

    def test_recognizes_numbered_canonical_topic_with_trailing_annotation(self) -> None:
        """The exact heading text found in the current PT source."""
        self.assertEqual(_taxonomy_get_canonical_legal_topic(section='VI. PAY EQUITY LAWS (NEW SECTION)'), 'Pay Equity Laws')

    def test_trailing_annotation_never_creates_a_false_match(self) -> None:
        """
        An unrelated custom heading that happens to carry a
        parenthetical remark must remain unrecognized - stripping the
        annotation must never turn non-canonical text into a false
        canonical match.
        """
        self.assertIsNone(_taxonomy_get_canonical_legal_topic(section='V060 Temporary Validation Section (Draft)'))

    def test_trailing_annotation_stripping_never_blanks_the_label(self) -> None:
        """A label that is nothing but a parenthetical must be left
        alone rather than reduced to an empty, always-matching
        string."""
        self.assertEqual(_taxonomy_normalize_topic(section='(New Section)'), '(New Section)')

    def test_recognizes_australian_topic_variant(self) -> None:
        self.assertEqual(_taxonomy_get_canonical_legal_topic(section='Hiring practices in Australia', country='Australia'), 'Hiring Practices')

    def test_recognizes_czech_topic_variant(self) -> None:
        self.assertEqual(_taxonomy_get_canonical_legal_topic(section='1. Employment contract law in the Czech Republic', country='Czech Republic'), 'Employment Contracts')

    def test_recognizes_greek_topic_variant(self) -> None:
        self.assertEqual(_taxonomy_get_canonical_legal_topic(section='Employment contract law in Greece', country='Greece'), 'Employment Contracts')

    def test_recognizes_united_kingdom_suffix(self) -> None:
        self.assertEqual(_taxonomy_get_canonical_legal_topic(section='06. Social Media and Data Privacy in the United Kingdom', country='United Kingdom'), 'Social Media and Data Privacy')

    def test_rejects_body_sentence_starting_with_topic(self) -> None:
        self.assertIsNone(_taxonomy_get_canonical_legal_topic(section='Employment contracts in Australia are formed in accordance with general contract law.', country='Australia'))

    def test_rejects_unknown_topic(self) -> None:
        self.assertIsNone(_taxonomy_get_canonical_legal_topic(section='12. Imaginary Legal Topic', country='Spain'))

    def test_recognizes_country_overview(self) -> None:
        self.assertTrue(_taxonomy_is_overview_section(section='Employment Law Overview Australia', country='Australia'))

    def test_recognizes_general_as_overview(self) -> None:
        self.assertTrue(_taxonomy_is_overview_section(section='General', country='Italy'))

class _taxonomy_JurisdictionAliasSuffixTests(_taxonomy_unittest.TestCase):
    """
    Mission "HOTFIX 0.4.4" - final targeted correction: a heading's
    trailing "in <jurisdiction>" suffix must be recognized for every
    safe alias the country registry itself already knows for that
    country, not only its canonical display name.
    """

    def test_recognizes_in_the_usa(self) -> None:
        self.assertEqual(_taxonomy_get_canonical_legal_topic(section='Social Media and Data Privacy in the USA', country='United States'), 'Social Media and Data Privacy')

    def test_recognizes_in_the_u_s(self) -> None:
        self.assertEqual(_taxonomy_get_canonical_legal_topic(section='Social Media and Data Privacy in the U.S.', country='United States'), 'Social Media and Data Privacy')

    def test_recognizes_in_the_u_s_a(self) -> None:
        self.assertEqual(_taxonomy_get_canonical_legal_topic(section='Social Media and Data Privacy in the U.S.A.', country='United States'), 'Social Media and Data Privacy')

    def test_recognizes_in_the_united_states(self) -> None:
        self.assertEqual(_taxonomy_get_canonical_legal_topic(section='Social Media and Data Privacy in the United States', country='United States'), 'Social Media and Data Privacy')

    def test_unrelated_country_suffix_still_works(self) -> None:
        self.assertEqual(_taxonomy_get_canonical_legal_topic(section='Social Media and Data Privacy in Canada', country='Canada'), 'Social Media and Data Privacy')

    def test_the_pronoun_us_is_never_read_as_united_states(self) -> None:
        self.assertIsNone(_taxonomy_get_canonical_legal_topic(section='The employer told us about social media', country='United States'))

    def test_a_non_canonical_heading_mentioning_usa_invents_nothing(self) -> None:
        self.assertIsNone(_taxonomy_get_canonical_legal_topic(section='Recent Developments in the USA', country='United States'))

    def test_a_jurisdiction_mid_title_is_not_stripped(self) -> None:
        self.assertIsNone(_taxonomy_get_canonical_legal_topic(section='In the USA, social media laws are strict', country='United States'))


# ====================================================================
# SOURCE: test_legal_topic_detection.py
# ====================================================================

import unittest as _topic_unittest
from app.models.chat import LegalChatRequest as _topic_LegalChatRequest
from app.services.country_detection import detect_mentioned_country_codes as _topic_detect_mentioned_country_codes
from app.services.legal_topic_detection import detect_document_legal_topics as _topic_detect_document_legal_topics, detect_legal_topics as _topic_detect_legal_topics, is_overview_question as _topic_is_overview_question, resolve_legal_scope as _topic_resolve_legal_scope

class _topic_LegalTopicDetectionTests(_topic_unittest.TestCase):
    """Tests for deterministic legal-topic detection."""

    def test_notice_period_detects_relevant_topics(self) -> None:
        topics = _topic_detect_legal_topics('Compare statutory notice periods in the UK and Spain.')
        self.assertEqual(topics, ['Employment Contracts', 'Termination of Employment Contracts'])

    def test_termination_question_detects_topic(self) -> None:
        topics = _topic_detect_legal_topics('Can an employee challenge an unfair dismissal?')
        self.assertEqual(topics, ['Termination of Employment Contracts'])

    def test_overtime_detects_working_conditions(self) -> None:
        topics = _topic_detect_legal_topics('What are the overtime rules?')
        self.assertEqual(topics, ['Working Conditions'])

    def test_discrimination_detects_anti_discrimination_laws(self) -> None:
        topics = _topic_detect_legal_topics('What protections exist against workplace harassment?')
        self.assertEqual(topics, ['Anti-Discrimination Laws'])

    def test_equal_pay_detects_pay_equity_laws(self) -> None:
        topics = _topic_detect_legal_topics('What are the equal pay requirements?')
        self.assertEqual(topics, ['Pay Equity Laws'])

    def test_trade_union_detects_trade_union_topic(self) -> None:
        topics = _topic_detect_legal_topics('How does collective bargaining work?')
        self.assertEqual(topics, ['Trade Unions and Employers Associations'])

    def test_business_transfer_detects_transfer_of_undertakings(self) -> None:
        topics = _topic_detect_legal_topics('What happens to employees in a business transfer?')
        self.assertEqual(topics, ['Transfer of Undertakings'])

    def test_employee_monitoring_detects_social_media_topic(self) -> None:
        topics = _topic_detect_legal_topics('Can an employer monitor employee emails in Spain?')
        self.assertEqual(topics, ['Social Media and Data Privacy'])

    def test_tax_question_detects_no_topic(self) -> None:
        topics = _topic_detect_legal_topics('What are the corporate income tax rules in Spain?')
        self.assertEqual(topics, [])

    def test_vat_question_detects_no_topic(self) -> None:
        topics = _topic_detect_legal_topics('What is the VAT rate in Italy?')
        self.assertEqual(topics, [])

    def test_patents_question_detects_no_topic(self) -> None:
        topics = _topic_detect_legal_topics('What about patents and inventions for employees in Spain?')
        self.assertEqual(topics, [])

    def test_overview_phrase_is_recognized(self) -> None:
        self.assertTrue(_topic_is_overview_question('Employment law overview Spain'))

    def test_non_overview_question_is_not_recognized(self) -> None:
        self.assertFalse(_topic_is_overview_question('What is the VAT rate in Italy?'))

class _topic_LegalScopeTests(_topic_unittest.TestCase):
    """Tests for the combined legal-topic scope decision."""

    def test_explicit_topics_take_priority(self) -> None:
        scope = _topic_resolve_legal_scope(_topic_LegalChatRequest(question='What is the notice period?', legal_topics=['Employee Benefits']))
        self.assertEqual(scope.legal_topics, ['Employee Benefits'])
        self.assertTrue(scope.is_supported)

    def test_detected_topics_are_supported(self) -> None:
        scope = _topic_resolve_legal_scope(_topic_LegalChatRequest(question='Compare statutory notice periods in the UK and Spain.', country_codes=['GB', 'ES'], max_sources=4))
        self.assertEqual(scope.legal_topics, ['Employment Contracts', 'Termination of Employment Contracts'])
        self.assertTrue(scope.is_supported)

    def test_comparative_probation_question_detects_supported_topic(self) -> None:
        question = 'Compare the legal treatment of probation periods in the United Kingdom and Singapore.'
        country_codes = _topic_detect_mentioned_country_codes(question)
        self.assertEqual(sorted(country_codes), ['GB', 'SG'])
        scope = _topic_resolve_legal_scope(_topic_LegalChatRequest(question=question, country_codes=country_codes))
        self.assertEqual(scope.legal_topics, ['Employment Contracts'])
        self.assertTrue(scope.is_supported)

    def test_probationary_periods_phrase_detects_supported_topic(self) -> None:
        question = 'Compare probationary periods in Australia and Peru.'
        country_codes = _topic_detect_mentioned_country_codes(question)
        self.assertEqual(sorted(country_codes), ['AU', 'PE'])
        scope = _topic_resolve_legal_scope(_topic_LegalChatRequest(question=question, country_codes=country_codes))
        self.assertEqual(scope.legal_topics, ['Employment Contracts'])
        self.assertTrue(scope.is_supported)

    def test_single_country_probation_question_remains_supported(self) -> None:
        question = 'What rules apply to probation periods in Singapore?'
        country_codes = _topic_detect_mentioned_country_codes(question)
        self.assertEqual(country_codes, ['SG'])
        scope = _topic_resolve_legal_scope(_topic_LegalChatRequest(question=question, country_codes=country_codes))
        self.assertEqual(scope.legal_topics, ['Employment Contracts'])
        self.assertTrue(scope.is_supported)

    def test_australian_corporate_tax_question_remains_unsupported(self) -> None:
        scope = _topic_resolve_legal_scope(_topic_LegalChatRequest(question='What is the corporate income tax rate in Australia?', country_codes=['AU']))
        self.assertEqual(scope.legal_topics, [])
        self.assertFalse(scope.is_supported)

    def test_peru_stock_options_question_remains_unsupported(self) -> None:
        scope = _topic_resolve_legal_scope(_topic_LegalChatRequest(question='How are employee stock options taxed in Peru?', country_codes=['PE']))
        self.assertEqual(scope.legal_topics, [])
        self.assertFalse(scope.is_supported)

    def test_overview_question_is_supported_without_topics(self) -> None:
        scope = _topic_resolve_legal_scope(_topic_LegalChatRequest(question='Employment law overview Spain', country_codes=['ES']))
        self.assertEqual(scope.legal_topics, [])
        self.assertTrue(scope.is_overview_question)
        self.assertTrue(scope.is_supported)

    def test_explicit_subsection_is_supported_without_topics(self) -> None:
        scope = _topic_resolve_legal_scope(_topic_LegalChatRequest(question='Tell me more about this.', subsections=['Notice Period']))
        self.assertTrue(scope.is_supported)

    def test_out_of_scope_question_is_not_supported(self) -> None:
        scope = _topic_resolve_legal_scope(_topic_LegalChatRequest(question='What are the corporate income tax rules in Spain?', country_codes=['ES']))
        self.assertEqual(scope.legal_topics, [])
        self.assertFalse(scope.is_overview_question)
        self.assertFalse(scope.is_supported)

class _topic_DocumentLegalTopicDetectionTests(_topic_unittest.TestCase):
    """
    Tests for detect_document_legal_topics (mission "ORDER 8F-A") -
    deterministic, exact-substring detection of LIVE, currently-indexed
    legal_topic titles (canonical or Admin-created custom section
    alike), distinct from detect_legal_topics' fixed-taxonomy keyword
    matching. Must survive being the ONLY signal available when
    RequestUnderstanding's own LLM call fails entirely.
    """

    def test_exact_custom_title_is_detected(self) -> None:
        topics = _topic_detect_document_legal_topics('Tell me about the V060 Temporary Validation Section for Australia.', ['Hiring Practices', 'V060 Temporary Validation Section'])
        self.assertEqual(topics, ['V060 Temporary Validation Section'])

    def test_no_match_returns_empty(self) -> None:
        topics = _topic_detect_document_legal_topics('What are the overtime rules?', ['Hiring Practices', 'V060 Temporary Validation Section'])
        self.assertEqual(topics, [])

    def test_case_and_punctuation_insensitive(self) -> None:
        topics = _topic_detect_document_legal_topics('what about foreign employee work-eligibility, checks?', ['Foreign Employee Work Eligibility Checks'])
        self.assertEqual(topics, ['Foreign Employee Work Eligibility Checks'])

    def test_single_word_topic_is_never_matched(self) -> None:
        """
        A single common word indexed as its own legal_topic (e.g. a
        one-word custom section title) is never enough to
        deterministically claim an explicit match - only a genuinely
        multi-word, specific title is (MIN_DOCUMENT_TOPIC_WORDS).
        """
        topics = _topic_detect_document_legal_topics('What are the benefits available to employees?', ['Benefits'])
        self.assertEqual(topics, [])

    def test_empty_question_returns_empty(self) -> None:
        topics = _topic_detect_document_legal_topics('', ['V060 Temporary Validation Section'])
        self.assertEqual(topics, [])

    def test_empty_document_topics_returns_empty(self) -> None:
        topics = _topic_detect_document_legal_topics('Tell me about the V060 Temporary Validation Section.', [])
        self.assertEqual(topics, [])

    def test_multiple_distinct_titles_are_all_detected(self) -> None:
        topics = _topic_detect_document_legal_topics('Compare the V060 Temporary Validation Section with the Foreign Employee Work Eligibility Checks.', ['V060 Temporary Validation Section', 'Foreign Employee Work Eligibility Checks', 'Hiring Practices'])
        self.assertEqual(topics, ['V060 Temporary Validation Section', 'Foreign Employee Work Eligibility Checks'])

    def test_duplicate_document_topics_are_deduplicated(self) -> None:
        topics = _topic_detect_document_legal_topics('V060 Temporary Validation Section, V060 Temporary Validation Section.', ['V060 Temporary Validation Section'])
        self.assertEqual(topics, ['V060 Temporary Validation Section'])
