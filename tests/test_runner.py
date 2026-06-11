import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from agents.runners.claude import ClaudeRunner


def result(returncode=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


class ClaudeRunnerTests(unittest.TestCase):
    def test_success_does_not_retry(self):
        runner = ClaudeRunner(flags=["--flag"])
        with patch.object(runner, "_run", return_value=result(stdout="ok")) as execute:
            self.assertEqual(runner.execute("prompt"), "ok")
        self.assertEqual(execute.call_count, 1)

    def test_timeout_retries_with_continue(self):
        runner = ClaudeRunner(flags=["--flag"])
        with patch.object(
            runner,
            "_run",
            side_effect=[subprocess.TimeoutExpired("claude", 1), result(stdout="retried")],
        ) as execute:
            self.assertEqual(runner.execute("prompt"), "retried")
        self.assertIn("--continue", execute.call_args_list[1].args[0])

    def test_nonzero_result_retries_once_and_reports_details(self):
        runner = ClaudeRunner()
        with patch.object(
            runner,
            "_run",
            side_effect=[result(1, stderr="first"), result(2, stderr="final failure")],
        ):
            with self.assertRaisesRegex(RuntimeError, "final failure"):
                runner.execute("prompt")


if __name__ == "__main__":
    unittest.main()
