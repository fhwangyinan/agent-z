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


class InteractiveImpactTests(QuietRunTest):
    @patch("run.Prompt.ask", side_effect=["What changes?", "done"])
    def test_questions_continue_analyst_session(self, ask):
        analyst = Mock()
        analyst.chat.return_value = "Only the API changes."
        self.assertTrue(run._interactive_impact_qa(analyst, "medium"))
        analyst.chat.assert_called_once_with("What changes?")

    @patch("run.Prompt.ask", return_value="skip")
    def test_skip_stops_before_development(self, ask):
        analyst = Mock()
        self.assertFalse(run._interactive_impact_qa(analyst, "high"))
        analyst.chat.assert_not_called()


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
        self.assertEqual(
            developer.apply_review.call_args.kwargs["review_comments"],
            ["still broken"],
        )


class RoundFlowTests(QuietRunTest):
    @patch("run.execute_task", return_value=False)
    @patch("run._session_snapshot", return_value={"analyst": "session-1"})
    @patch("run.confirm_issue", return_value=1)
    @patch("run.show_analysis")
    def test_round_creates_persisted_task(
        self,
        show_analysis,
        confirm_issue,
        session_snapshot,
        execute_task,
    ):
        analyst = Mock()
        analyst.analyze.return_value = (1, "analysis")
        developer = Mock()
        reviewer = Mock()
        submitter = Mock()
        record = SimpleNamespace(run_id="run-1")
        store = Mock()
        store.create.return_value = record
        store.update.return_value = record
        worktrees = Mock()
        with patch("run.AUTO_MODE", True):
            self.assertFalse(run.run_round(
                analyst, developer, reviewer, submitter,
                store, worktrees, target_issue=1,
            ))
        analyst.reset_session.assert_called_once()
        developer.reset_session.assert_called_once()
        reviewer.reset_session.assert_called_once()
        submitter.reset_session.assert_called_once()
        store.create.assert_called_once()
        execute_task.assert_called_once()


class ValidationTests(QuietRunTest):
    @patch("run.os.path.isdir", return_value=False)
    def test_missing_project_directory_fails_fast(self, isdir):
        with self.assertRaisesRegex(RuntimeError, "PROJECT_DIR does not exist"):
            run.validate_environment()

    @patch("run.run_cmd", return_value=result(stdout="true\n"))
    @patch("run.shutil.which")
    @patch("run.os.path.isdir", return_value=True)
    def test_only_selected_backends_are_required(self, isdir, which, run_cmd):
        which.side_effect = lambda command: None if command == "opencode" else command
        with patch("run.ANALYST_BACKEND", "claude"), \
             patch("run.DEVELOPER_BACKEND", "claude"), \
             patch("run.REVIEWER_BACKEND", "codex"), \
             patch("run.SUBMITTER_BACKEND", "claude"):
            run.validate_environment()


class CancelRunTests(QuietRunTest):
    def test_cancel_rejects_live_owner(self):
        store = Mock()
        store.cancel.side_effect = RuntimeError("active process")
        worktrees = Mock()
        with self.assertRaisesRegex(RuntimeError, "active process"):
            run.cancel_run(store, worktrees, "run-1")
        worktrees.remove.assert_not_called()

    def test_cancel_cleans_stale_run(self):
        store = Mock()
        store.cancel.return_value = SimpleNamespace(worktree_path="worktree")
        worktrees = Mock()
        run.cancel_run(store, worktrees, "run-1")
        worktrees.remove.assert_called_once_with("worktree")
        store.update.assert_called_once_with("run-1", worktree_path=None)


if __name__ == "__main__":
    unittest.main()
