import unittest
import subprocess
import os
from types import SimpleNamespace
from unittest.mock import patch

from agents.base import elapsed_status, run_cmd, wait_with_countdown


class RunCommandTests(unittest.TestCase):
    @patch(
        "agents.base.subprocess.run",
        return_value=SimpleNamespace(returncode=2, stdout="", stderr="useful error"),
    )
    def test_failure_includes_command_and_stderr(self, execute):
        with self.assertRaisesRegex(RuntimeError, "useful error"):
            run_cmd(["tool", "arg"])

    @patch("agents.base.wait_with_countdown")
    @patch(
        "agents.base.subprocess.run",
        side_effect=[
            SimpleNamespace(returncode=1, stdout="", stderr="connection reset by peer"),
            SimpleNamespace(returncode=0, stdout="[]", stderr=""),
        ],
    )
    def test_gh_retries_transient_failure(self, execute, countdown):
        result = run_cmd(["gh", "issue", "list"], check=False)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(execute.call_count, 2)
        countdown.assert_called_once()

    @patch("agents.base.wait_with_countdown")
    @patch(
        "agents.base.subprocess.run",
        return_value=SimpleNamespace(returncode=1, stdout="", stderr="permission denied"),
    )
    def test_gh_does_not_retry_non_transient_failure(self, execute, countdown):
        result = run_cmd(["gh", "issue", "list"], check=False)
        self.assertEqual(result.returncode, 1)
        execute.assert_called_once()
        countdown.assert_not_called()

    @patch("agents.base.wait_with_countdown")
    @patch(
        "agents.base.subprocess.run",
        side_effect=[
            subprocess.TimeoutExpired("gh", 60),
            SimpleNamespace(returncode=0, stdout="{}", stderr=""),
        ],
    )
    def test_gh_retries_timeout(self, execute, countdown):
        result = run_cmd(["gh", "issue", "view", "1"], check=False)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(execute.call_count, 2)
        countdown.assert_called_once()

    @patch("agents.base.wait_with_countdown")
    @patch(
        "agents.base.subprocess.run",
        return_value=SimpleNamespace(returncode=1, stdout="", stderr="HTTP 503"),
    )
    def test_retry_can_be_disabled(self, execute, countdown):
        run_cmd(["gh", "pr", "checks", "--watch"], check=False, retry=False)
        execute.assert_called_once()
        countdown.assert_not_called()

    @patch("agents.base.GITHUB_COMMAND_TIMEOUT", 17)
    @patch(
        "agents.base.subprocess.run",
        return_value=SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    def test_retry_enabled_command_gets_default_timeout(self, execute):
        run_cmd(["git", "fetch", "origin"], retry=True)
        self.assertEqual(execute.call_args.kwargs["timeout"], 17)
        self.assertEqual(execute.call_args.kwargs["errors"], "replace")

    @patch.dict(os.environ, {"AGENT_Z_QUIET_LIVE": "1"})
    @patch("agents.base.time.sleep")
    def test_quiet_countdown_only_sleeps(self, sleep):
        wait_with_countdown("Retrying", 3)
        sleep.assert_called_once_with(3)

    @patch.dict(os.environ, {"AGENT_Z_QUIET_LIVE": "1"})
    @patch("agents.base.console.status")
    def test_quiet_elapsed_status_does_not_render(self, status):
        with elapsed_status("Working"):
            pass
        status.assert_not_called()


if __name__ == "__main__":
    unittest.main()
