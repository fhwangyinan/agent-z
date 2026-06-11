import unittest
from types import SimpleNamespace
from unittest.mock import patch

from agents.base import run_cmd


class RunCommandTests(unittest.TestCase):
    @patch(
        "agents.base.subprocess.run",
        return_value=SimpleNamespace(returncode=2, stdout="", stderr="useful error"),
    )
    def test_failure_includes_command_and_stderr(self, execute):
        with self.assertRaisesRegex(RuntimeError, "useful error"):
            run_cmd(["tool", "arg"])


if __name__ == "__main__":
    unittest.main()
