import unittest

from app.core.subsection_taxonomy import get_subsection_topic_override


class FranceStatutorySeveranceOverrideTests(unittest.TestCase):
    def test_statutory_severance_maps_to_termination_topic(self) -> None:
        self.assertEqual(
            get_subsection_topic_override("STATUTORY SEVERANCE"),
            "Termination of Employment Contracts",
        )


if __name__ == "__main__":
    unittest.main()
