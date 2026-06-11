import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import run


def result(returncode=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


class QuietRunTest(unittest.TestCase):
    def setUp(self):
        super().setUp()
        console_patcher = patch("run.console")
        console_patcher.start()
        self.addCleanup(console_patcher.stop)
        for name in ("step", "log", "warn", "done"):
            patcher = patch(f"run.{name}")
            patcher.start()
            self.addCleanup(patcher.stop)


class PrChecksTests(QuietRunTest):
    @patch("run.run_cmd", return_value=result(1, stderr="no checks reported on the 'main' branch"))
    def test_no_checks_reported_is_retryable(self, run_cmd):
        self.assertEqual(run._get_pr_checks("https://example/pr/1"), [])

    @patch("run.run_cmd")
    @patch("run._get_pr_checks")
    def test_failed_checks_are_complete_and_actionable(self, get_checks, run_cmd):
        get_checks.side_effect = [
            [{"name": "CI", "bucket": "pending"}],
            [{"name": "CI", "bucket": "fail"}],
        ]
        run_cmd.return_value = result(1)
        self.assertTrue(run.wait_for_pr_checks("https://example/pr/1"))
        self.assertIn("--watch", run_cmd.call_args.args[0])

    @patch("run.run_cmd")
    @patch("run._get_pr_checks")
    def test_pending_checks_do_not_report_complete(self, get_checks, run_cmd):
        get_checks.side_effect = [
            [{"name": "CI", "bucket": "pending"}],
            [{"name": "CI", "bucket": "pending"}],
        ]
        run_cmd.return_value = result()
        self.assertFalse(run.wait_for_pr_checks("https://example/pr/1"))

    @patch("run.time.sleep")
    @patch("run.run_cmd")
    @patch("run._get_pr_checks")
    def test_waits_for_checks_to_register(self, get_checks, run_cmd, sleep):
        get_checks.side_effect = [
            [],
            [{"name": "CI", "bucket": "pending"}],
            [{"name": "CI", "bucket": "pass"}],
        ]
        run_cmd.return_value = result()
        self.assertTrue(run.wait_for_pr_checks("https://example/pr/1"))
        sleep.assert_called_once()

    @patch("run.run_cmd", side_effect=subprocess.TimeoutExpired("gh", 1))
    @patch("run._get_pr_checks")
    def test_watch_timeout_stops_processing(self, get_checks, run_cmd):
        get_checks.return_value = [{"name": "CI", "bucket": "pending"}]
        self.assertFalse(run.wait_for_pr_checks("https://example/pr/1"))

    @patch("run._get_pr_checks", return_value=None)
    def test_query_error_stops_processing(self, get_checks):
        self.assertFalse(run.wait_for_pr_checks("https://example/pr/1"))


class LocalReviewTests(QuietRunTest):
    def test_review_passes_without_applying_changes(self):
        reviewer = Mock()
        reviewer.review.return_value = []
        developer = Mock()
        self.assertTrue(run.run_local_review(1, reviewer, developer))
        developer.apply_review.assert_not_called()

    def test_review_limit_stops_flow(self):
        reviewer = Mock()
        reviewer.review.return_value = ["still broken"]
        developer = Mock()
        with patch("run.MAX_LOCAL_REVIEW_ROUNDS", 2):
            self.assertFalse(run.run_local_review(1, reviewer, developer))
        self.assertEqual(developer.apply_review.call_count, 2)


class RoundFlowTests(QuietRunTest):
    @patch("run.managed_environment")
    @patch("run.run_local_review", return_value=False)
    @patch("run.confirm_issue", return_value=1)
    @patch("run.choose_issue", return_value=1)
    @patch("run.show_analysis")
    def test_failed_pre_review_does_not_submit(
        self,
        show_analysis,
        choose_issue,
        confirm_issue,
        local_review,
        environment,
    ):
        analyst = Mock()
        analyst.analyze.return_value = (1, "analysis")
        analyst.assess_impact.return_value = ("impact", "low")
        developer = Mock()
        reviewer = Mock()
        submitter = Mock()
        with patch("run.AUTO_MODE", True):
            self.assertFalse(run.run_round(analyst, developer, reviewer, submitter))
        submitter.submit.assert_not_called()


class ValidationTests(QuietRunTest):
    @patch("run.os.path.isdir", return_value=False)
    def test_missing_project_directory_fails_fast(self, isdir):
        with self.assertRaisesRegex(RuntimeError, "PROJECT_DIR does not exist"):
            run.validate_environment()


class ManagedEnvironmentTests(QuietRunTest):
    @patch("run._find_stash_ref", side_effect=["stash@{0}", "stash@{0}"])
    @patch("run.run_cmd")
    def test_stash_is_restored_and_dropped(self, run_cmd, find_stash):
        def execute(args, **kwargs):
            if args[:4] == ["git", "symbolic-ref", "--short", "-q"]:
                return result(stdout="feature\n")
            if args == ["git", "status", "--short"]:
                return result(stdout=" M work.py\n")
            return result()

        run_cmd.side_effect = execute
        with run.managed_environment():
            pass

        commands = [entry.args[0] for entry in run_cmd.call_args_list]
        self.assertTrue(any(command[:4] == ["git", "stash", "push", "-u"] for command in commands))
        self.assertIn(["git", "checkout", "feature"], commands)
        self.assertIn(["git", "stash", "apply", "stash@{0}"], commands)
        self.assertIn(["git", "stash", "drop", "stash@{0}"], commands)

    @patch("run._find_stash_ref", side_effect=["stash@{0}", "stash@{0}"])
    @patch("run.run_cmd")
    def test_restore_conflict_keeps_stash(self, run_cmd, find_stash):
        def execute(args, **kwargs):
            if args[:4] == ["git", "symbolic-ref", "--short", "-q"]:
                return result(stdout="feature\n")
            if args == ["git", "status", "--short"]:
                return result(stdout=" M work.py\n")
            if args == ["git", "stash", "apply", "stash@{0}"]:
                return result(1, stderr="conflict")
            return result()

        run_cmd.side_effect = execute
        with run.managed_environment():
            pass

        commands = [entry.args[0] for entry in run_cmd.call_args_list]
        self.assertNotIn(["git", "stash", "drop", "stash@{0}"], commands)


if __name__ == "__main__":
    unittest.main()
