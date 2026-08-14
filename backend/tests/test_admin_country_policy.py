"""
Tests for the ADMIN document-upload country allowlist (mission
"ORDER 5C").

Three independent, non-overlapping concepts are exercised across this
file and test_admin_document_replacement.py's allowlist tests:

1. country_registry.COUNTRIES - can this country be detected at all
   (mission "ORDER 5C" registered several countries, France and
   Germany among them, purely so admin-upload detection works for
   them - see test_chat.py's own _NOT_YET_INDEXED_CODES for why that
   does NOT make them available to the chatbot yet).
2. admin_country_policy.ADMIN_ALLOWED_COUNTRY_CODES - is this country
   currently accepted for a NEW admin upload.
3. The real indexed catalog (app.services.legal_catalog) - does the
   chatbot actually have content for this country right now.

This file only ever tests concept 2, and concept 2's relationship to
concept 1 - never concept 3.
"""

from __future__ import annotations

import unittest

from app.core.admin_country_policy import (
    ADMIN_ALLOWED_COUNTRY_CODES,
    is_admin_country_allowed,
)
from app.core.country_registry import COUNTRIES


_EXPECTED_ALLOWED_CODES = frozenset(
    {
        "AR", "AU", "BE", "BR", "CA", "CL", "CN", "CO", "CZ", "FR",
        "DE", "GR", "ID", "IE", "IT", "IN", "JP", "MX", "NL", "NO",
        "PE", "PH", "PL", "PT", "RO", "SG", "SK", "ES", "SE", "CH",
        "TW", "TR", "GB", "US",
    }
)


class AllowlistContentTests(unittest.TestCase):
    def test_exactly_the_34_client_mandated_codes(self) -> None:
        self.assertEqual(
            ADMIN_ALLOWED_COUNTRY_CODES,
            _EXPECTED_ALLOWED_CODES,
        )
        self.assertEqual(len(ADMIN_ALLOWED_COUNTRY_CODES), 34)

    def test_every_allowed_code_is_a_real_registry_entry(self) -> None:
        # Concept 2 must never name a code concept 1 cannot detect -
        # an admin-allowed country that country_registry.py cannot
        # even resolve would make a successful upload impossible.
        registry_codes = {country.code for country in COUNTRIES}

        for code in ADMIN_ALLOWED_COUNTRY_CODES:
            with self.subTest(code=code):
                self.assertIn(code, registry_codes)


class IsAdminCountryAllowedTests(unittest.TestCase):
    def test_every_allowed_code_returns_true(self) -> None:
        for code in ADMIN_ALLOWED_COUNTRY_CODES:
            with self.subTest(code=code):
                self.assertTrue(is_admin_country_allowed(code))

    def test_registered_but_not_allowed_country_returns_false(
        self,
    ) -> None:
        # Tunisia: registered (mission "ORDER 5C", section 7 - the
        # registry may know about a country without it being admin-
        # allowed) but deliberately outside the allowlist.
        self.assertFalse(is_admin_country_allowed("TN"))

    def test_entirely_unregistered_country_returns_false(self) -> None:
        self.assertFalse(is_admin_country_allowed("ZZ"))

    def test_is_case_and_whitespace_insensitive(self) -> None:
        self.assertTrue(is_admin_country_allowed(" fr "))
        self.assertTrue(is_admin_country_allowed("Fr"))
        self.assertFalse(is_admin_country_allowed(" tn "))

    def test_slovakia_identity_and_policy(self) -> None:
        # Mission "ORDER 5C-GEO", section 19 - Slovakia is SK, admin-
        # upload-allowed, and this policy layer has no notion of
        # "indexed" at all (that is the real catalog's job alone -
        # see test_country_detection.py's own availability tests).
        self.assertTrue(is_admin_country_allowed("SK"))
        self.assertIn("SK", ADMIN_ALLOWED_COUNTRY_CODES)


if __name__ == "__main__":
    unittest.main()
