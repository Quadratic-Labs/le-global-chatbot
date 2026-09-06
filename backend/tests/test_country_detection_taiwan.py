import unittest

from app.services.country_detection import (
    detect_mentioned_country_codes,
    get_country_demonyms,
    resolve_country_display_name,
)


class TaiwanCountryDetectionTests(unittest.TestCase):

    def test_short_name_resolves_to_tw(self) -> None:
        self.assertEqual(
            detect_mentioned_country_codes(
                "Taiwan legal information"
            ),
            ["TW"],
        )

    def test_taiwanese_resolves_to_tw(self) -> None:
        self.assertEqual(
            detect_mentioned_country_codes(
                "Taiwanese employment law"
            ),
            ["TW"],
        )

        self.assertIn(
            "Taiwanese",
            get_country_demonyms("TW"),
        )

    def test_official_iso_name_does_not_add_china(self) -> None:
        self.assertEqual(
            detect_mentioned_country_codes(
                "Taiwan, Province of China employment law"
            ),
            ["TW"],
        )

    def test_real_taiwan_china_comparison_keeps_both(self) -> None:
        self.assertEqual(
            detect_mentioned_country_codes(
                "Compare Taiwan and China"
            ),
            ["TW", "CN"],
        )

    def test_china_alone_stays_china(self) -> None:
        self.assertEqual(
            detect_mentioned_country_codes(
                "Employment law in China"
            ),
            ["CN"],
        )

    def test_product_display_name_is_taiwan(self) -> None:
        self.assertEqual(
            resolve_country_display_name("TW"),
            "Taiwan",
        )

    def test_generic_nested_country_name_is_not_double_counted(
        self,
    ) -> None:
        self.assertEqual(
            detect_mentioned_country_codes(
                "United States Minor Outlying Islands"
            ),
            ["UM"],
        )


if __name__ == "__main__":
    unittest.main()
