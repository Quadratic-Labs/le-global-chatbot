"""Manager max-six-country limit: direct resolver, legacy source budget, boundaries."""
import unittest
from app.models.catalog import LegalCatalogCountry, LegalCatalogResponse
from app.models.chat import LegalChatRequest
from app.routers.chat import resolve_legal_chat_response
from app.services.rag_answer import InvalidLegalChatRequestError
from tests.test_chat import (
    FakeUnderstandingClient, NoCallGenerationClient,
    _build_catalog, _document_topic_provider, _unexpected_search,
    _understanding_action, _understanding_result,
)

EXPECTED = (
    "The chatbot can compare a maximum of 6 countries. "
    "Please limit the scope of your comparison."
)
CODES = ["AU", "BE", "CA", "FR", "DE", "IT", "ES", "JP", "NL", "PL", "RO"]


def full_catalog():
    base = _build_catalog()
    existing = {c.country_code for c in base.countries}
    additional = [LegalCatalogCountry(country_code=code, country=name, chunk_count=25)
                  for code, name in (("FR", "France"), ("DE", "Germany"))
                  if code not in existing]
    return LegalCatalogResponse(
        countries=[*base.countries, *additional],
        legal_topics=[], subsections=[],
    )


class MaxSixCountriesManagerTests(unittest.TestCase):
    def run_case(self, codes, *, max_sources=6, generation=None):
        understanding = FakeUnderstandingClient(payload=_understanding_result(
            actions=[_understanding_action(
                "comparison", country_codes=codes,
                legal_topics=["Working Conditions"],
            )],
        ))
        def generation_must_not_run(*args, **kwargs):
            raise AssertionError("Legal generation should not run for >6 countries")
        return resolve_legal_chat_response(
            request=LegalChatRequest(
                question="Compare statutory working hours across these countries.",
                country_codes=codes, max_sources=max_sources,
            ),
            catalog_provider=full_catalog,
            document_topic_provider=_document_topic_provider,
            search_function=_unexpected_search,
            understanding_client=understanding,
            generation_client=NoCallGenerationClient(),
            legal_answer_generation_fn=generation or generation_must_not_run,
        )

    def assert_limited(self, response):
        self.assertEqual(EXPECTED, response.answer)
        self.assertFalse(response.grounded)
        self.assertIsNone(response.model)
        self.assertEqual([], response.sources)
        self.assertEqual([], response.contacts)
        self.assertEqual(0, response.retrieval_total)

    def test_seven_countries_default_budget(self):
        self.assert_limited(self.run_case(CODES[:7]))

    def test_nine_countries_default_budget(self):
        self.assert_limited(self.run_case(CODES[:9]))

    def test_seven_countries_cannot_bypass_with_ten_sources(self):
        self.assert_limited(self.run_case(CODES[:7], max_sources=10))

    def test_nine_countries_cannot_bypass_with_ten_sources(self):
        self.assert_limited(self.run_case(CODES[:9], max_sources=10))

    def test_six_countries_still_reach_legal_generation(self):
        def probe(*args, **kwargs):
            raise AssertionError("SIX_COUNTRIES_GENERATION_WAS_REACHED")
        with self.assertRaisesRegex(
            AssertionError, "SIX_COUNTRIES_GENERATION_WAS_REACHED"
        ):
            self.run_case(CODES[:6], generation=probe)




if __name__ == "__main__":
    unittest.main()
