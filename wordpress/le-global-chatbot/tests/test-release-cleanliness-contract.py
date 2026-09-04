import re
import unittest
from pathlib import Path


PLUGIN_ROOT = Path("wordpress/le-global-chatbot")
SOURCE_ROOTS = (PLUGIN_ROOT, Path("backend"))


class ReleaseCleanlinessContractTests(unittest.TestCase):
    def test_runtime_versions_match_0814(self):
        plugin_source = (
            PLUGIN_ROOT / "le-global-chatbot.php"
        ).read_text(encoding="utf-8")
        admin_source = (
            PLUGIN_ROOT
            / "includes"
            / "class-le-global-chatbot-admin.php"
        ).read_text(encoding="utf-8")

        header_match = re.search(
            r"^ \* Version: ([0-9.]+)$",
            plugin_source,
            re.MULTILINE,
        )
        plugin_constant_match = re.search(
            r"private const VERSION = '([0-9.]+)';",
            plugin_source,
        )
        admin_constant_match = re.search(
            r"private const VERSION = '([0-9.]+)';",
            admin_source,
        )

        self.assertIsNotNone(header_match)
        self.assertIsNotNone(plugin_constant_match)
        self.assertIsNotNone(admin_constant_match)
        self.assertEqual(
            {
                header_match.group(1),
                plugin_constant_match.group(1),
                admin_constant_match.group(1),
            },
            {"1.0.0"},
        )

    def test_temporary_streaming_diagnostics_are_absent(self):
        markers = (
            "GATE " + "DIAG-TEMP",
            "X-LE-" + "Debug",
            "diag" + "2_",
        )
        source_suffixes = {".php", ".js", ".css", ".py"}

        for source_root in SOURCE_ROOTS:
            for path in source_root.rglob("*"):
                if (
                    not path.is_file()
                    or path.suffix not in source_suffixes
                ):
                    continue

                contents = path.read_text(encoding="utf-8")

                for marker in markers:
                    self.assertNotIn(marker, contents, str(path))

        removed_diagnostic_tests = (
            "chat-stream-content-type.test.php",
            "chat-stream-" + "diag2.test.php",
        )

        for filename in removed_diagnostic_tests:
            self.assertFalse(
                (PLUGIN_ROOT / "tests" / filename).exists(),
                filename,
            )


if __name__ == "__main__":
    unittest.main()
