import os
import importlib
import unittest
from unittest.mock import patch

import config


class ConfigTests(unittest.TestCase):
    def test_claude_flags_preserve_quoted_arguments(self):
        try:
            with patch.dict(
                os.environ,
                {"CLAUDE_FLAGS": '--model "claude sonnet" --verbose'},
                clear=False,
            ):
                importlib.reload(config)
                self.assertEqual(
                    config.CLAUDE_FLAGS,
                    ["--model", "claude sonnet", "--verbose"],
                )
        finally:
            importlib.reload(config)

    def test_positive_int_uses_legacy_fallback(self):
        with patch.dict(os.environ, {"LEGACY_INTERVAL": "17"}, clear=False):
            os.environ.pop("NEW_INTERVAL", None)
            self.assertEqual(
                config._get_positive_int("NEW_INTERVAL", 10, "LEGACY_INTERVAL"),
                17,
            )

    def test_positive_int_rejects_invalid_values(self):
        with patch.dict(os.environ, {"BAD_INTERVAL": "0"}, clear=False):
            with self.assertRaisesRegex(ValueError, "BAD_INTERVAL"):
                config._get_positive_int("BAD_INTERVAL", 10)


if __name__ == "__main__":
    unittest.main()
