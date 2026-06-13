import subprocess
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import run
import orchestration.github_ops
import orchestration.errors
import orchestration.pools
import orchestration.submission
import orchestration.tui


def result(returncode=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


class QuietRunTest(unittest.TestCase):
    def setUp(self):
        super().setUp()
        for target in (
            "orchestration.tui.console",
            "orchestration.github_ops.console",
            "orchestration.workflow.console",
        ):
            patcher = patch(target)
            patcher.start()
            self.addCleanup(patcher.stop)
        for module in (
            "orchestration.github_ops",
            "orchestration.submission",
            "orchestration.workflow",
            "orchestration.pools",
        ):
            if module == "orchestration.github_ops":
                names = ("log", "warn", "done", "step")
            elif module == "orchestration.submission":
                names = ("log", "warn")
            else:
                names = ("log", "warn", "done")
            for name in names:
                patcher = patch(f"{module}.{name}")
                patcher.start()
                self.addCleanup(patcher.stop)


class LeaseTests(unittest.TestCase):
    def test_maintain_lease_propagates_lost_ownership(self):
        attempted = threading.Event()
        store = Mock()

        def lose_ownership(*args):
            attempted.set()
            raise RuntimeError("run is no longer owned by worker")

        store.heartbeat.side_effect = lose_ownership
        with self.assertRaisesRegex(RuntimeError, "no longer owned"):
            with orchestration.pools.maintain_lease(
                store, "run-1", "worker", 30, interval=0.001
            ):
                self.assertTrue(attempted.wait(1))


class TuiFormattingTests(QuietRunTest):
    def test_run_context_includes_identity_state_and_elapsed(self):
        record = SimpleNamespace(
            run_id="abc123",
            issue_number=42,
            status="running",
            stage="developing",
            lease_role="worker",
        )
        context = run._run_context_line(record, elapsed=65)
        self.assertIn("abc123", context)
        self.assertIn("Issue #42", context)
        self.assertIn("RUNNING", context)
        self.assertIn("Development", context)
        self.assertIn("1m 05s", context)

    def test_record_age_formats_created_timestamp(self):
        record = SimpleNamespace(
            created_at="2026-01-01T00:00:00+00:00",
            status="running",
        )
        created = orchestration.tui.datetime.fromisoformat("2026-01-01T00:00:00+00:00")
        now = orchestration.tui.datetime.fromisoformat("2026-01-01T01:02:03+00:00")
        with patch("orchestration.tui._parse_iso", return_value=created), patch("orchestration.tui.datetime") as dt:
            dt.now.return_value = now
            self.assertEqual(run._record_age(record), "1h 02m 03s")

    def test_record_age_freezes_when_run_is_final(self):
        record = SimpleNamespace(
            created_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-01-01T00:03:04+00:00",
            status="failed",
        )
        self.assertEqual(run._record_age(record), "3m 04s")


class InteractiveImpactTests(QuietRunTest):
    @patch("orchestration.workflow.Prompt.ask", side_effect=["What changes?", "done"])
    def test_questions_continue_analyst_session(self, ask):
        analyst = Mock()
        analyst.chat.return_value = "Only the API changes."
        self.assertTrue(run._interactive_impact_qa(analyst, "medium"))
        analyst.chat.assert_called_once_with("What changes?")

    @patch("orchestration.workflow.Prompt.ask", return_value="skip")
    def test_skip_stops_before_development(self, ask):
        analyst = Mock()
        self.assertFalse(run._interactive_impact_qa(analyst, "high"))
        analyst.chat.assert_not_called()


class PrChecksTests(QuietRunTest):
    @patch("orchestration.github_ops.run_cmd", return_value=result(1, stderr="no checks reported on the 'main' branch"))
    def test_no_checks_reported_is_retryable(self, run_cmd):
        self.assertEqual(orchestration.github_ops._get_pr_checks("https://example/pr/1"), [])

    @patch("orchestration.github_ops.run_cmd")
    @patch("orchestration.github_ops._get_pr_checks")
    def test_failed_checks_are_complete_and_actionable(self, get_checks, run_cmd):
        get_checks.side_effect = [
            [{"name": "CI", "bucket": "pending"}],
            [{"name": "CI", "bucket": "fail"}],
        ]
        run_cmd.return_value = result(1)
        self.assertTrue(run.wait_for_pr_checks("https://example/pr/1"))
        self.assertIn("--watch", run_cmd.call_args.args[0])

    @patch("orchestration.github_ops.run_cmd")
    @patch("orchestration.github_ops._get_pr_checks")
    def test_pending_checks_do_not_report_complete(self, get_checks, run_cmd):
        get_checks.side_effect = [
            [{"name": "CI", "bucket": "pending"}],
            [{"name": "CI", "bucket": "pending"}],
        ]
        run_cmd.return_value = result()
        self.assertFalse(run.wait_for_pr_checks("https://example/pr/1"))

    @patch("orchestration.github_ops.wait_with_status")
    @patch("orchestration.github_ops.run_cmd")
    @patch("orchestration.github_ops._get_pr_checks")
    def test_waits_for_checks_to_register(self, get_checks, run_cmd, wait_status):
        get_checks.side_effect = [
            [],
            [{"name": "CI", "bucket": "pending"}],
            [{"name": "CI", "bucket": "pass"}],
        ]
        run_cmd.return_value = result()
        self.assertTrue(run.wait_for_pr_checks("https://example/pr/1"))
        wait_status.assert_called_once()

    @patch("orchestration.github_ops.run_cmd", side_effect=subprocess.TimeoutExpired("gh", 1))
    @patch("orchestration.github_ops._get_pr_checks")
    def test_watch_timeout_stops_processing(self, get_checks, run_cmd):
        get_checks.return_value = [{"name": "CI", "bucket": "pending"}]
        self.assertFalse(run.wait_for_pr_checks("https://example/pr/1"))

    @patch("orchestration.github_ops._get_pr_checks", return_value=None)
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
        with patch("orchestration.workflow.MAX_LOCAL_REVIEW_ROUNDS", 2):
            self.assertFalse(run.run_local_review(1, reviewer, developer))
        self.assertEqual(developer.apply_review.call_count, 2)
        self.assertEqual(
            developer.apply_review.call_args.kwargs["review_comments"],
            ["still broken"],
        )


class SkipLabelTests(QuietRunTest):
    @patch("orchestration.github_ops.run_cmd")
    def test_mark_issue_with_skip_label_adds_first_label_and_event(self, run_cmd):
        run_cmd.side_effect = [
            result(stdout='{"labels": []}'),
            result(),
            result(),
            result(),
            result(stdout='{"labels": [{"name": "ongoing"}]}'),
        ]
        record = SimpleNamespace(
            run_id="run-1",
            issue_number=123,
            stage="assessed",
            status="running",
        )
        store = Mock()
        store.list_events.return_value = []
        store.get.return_value = record
        returned = run.mark_issue_with_skip_label(record, store)
        self.assertIs(returned, record)
        self.assertEqual(run_cmd.call_args_list[3].args[0][:3], ["gh", "issue", "edit"])
        self.assertIn("ongoing", run_cmd.call_args_list[3].args[0])
        store.add_event.assert_called_once()
        self.assertEqual(store.add_event.call_args.args[1], "issue_labeled_skip")

    @patch("orchestration.github_ops.run_cmd")
    def test_mark_issue_stops_when_required_label_cannot_be_created(self, run_cmd):
        run_cmd.side_effect = [
            result(stdout='{"labels": []}'),
            result(stdout="[]"),
            result(returncode=1, stderr="permission denied"),
            result(stdout="[]"),
        ]
        record = SimpleNamespace(
            run_id="run-1",
            issue_number=123,
            stage="ready",
            status="running",
        )
        store = Mock()
        store.list_events.return_value = []
        with self.assertRaisesRegex(run.NeedsHumanError, "could not create or verify"):
            run.mark_issue_with_skip_label(record, store)
        self.assertEqual(run_cmd.call_count, 4)
        store.add_event.assert_not_called()

    @patch("orchestration.github_ops.run_cmd")
    def test_mark_issue_with_skip_label_rejects_any_existing_skip_label(self, run_cmd):
        run_cmd.return_value = result(stdout='{"labels": [{"name": "blocked"}]}')
        record = SimpleNamespace(
            run_id="run-1",
            issue_number=123,
            stage="assessed",
            status="running",
        )
        store = Mock()
        store.list_events.return_value = []
        with patch("orchestration.github_ops.SKIP_LABELS", ["ongoing", "blocked"]):
            with self.assertRaisesRegex(RuntimeError, "already has skip label"):
                run.mark_issue_with_skip_label(record, store)
        store.add_event.assert_not_called()
        self.assertEqual(run_cmd.call_count, 1)

    @patch("orchestration.github_ops.run_cmd", return_value=result(returncode=1, stderr="boom"))
    def test_mark_issue_with_skip_label_stops_when_labels_cannot_be_read(self, run_cmd):
        record = SimpleNamespace(
            run_id="run-1",
            issue_number=123,
            stage="assessed",
            status="running",
        )
        store = Mock()
        store.list_events.return_value = []
        with self.assertRaisesRegex(RuntimeError, "could not read labels"):
            run.mark_issue_with_skip_label(record, store)
        self.assertEqual(run_cmd.call_count, 1)

    @patch("orchestration.github_ops.run_cmd")
    def test_mark_issue_with_skip_label_is_idempotent_for_same_run(self, run_cmd):
        record = SimpleNamespace(run_id="run-1", issue_number=123)
        store = Mock()
        store.list_events.return_value = [
            SimpleNamespace(event_type="issue_labeled_skip")
        ]
        self.assertIs(run.mark_issue_with_skip_label(record, store), record)
        run_cmd.assert_not_called()


class WorkerPreflightTests(QuietRunTest):
    @patch("orchestration.github_ops.run_cmd")
    def test_related_pr_query_is_open_only(self, run_cmd):
        run_cmd.return_value = result(stdout="[]")
        self.assertEqual(orchestration.github_ops._get_related_open_prs(1), [])
        command = run_cmd.call_args.args[0]
        self.assertEqual(command[command.index("--state") + 1], "open")

    @patch("orchestration.github_ops.run_cmd")
    def test_related_pr_query_filters_partial_issue_number_matches(self, run_cmd):
        run_cmd.return_value = result(stdout="""[
            {"number": 1, "title": "Fix #123", "body": "", "url": "partial", "state": "OPEN"},
            {"number": 2, "title": "Fix parser", "body": "Closes #12", "url": "exact", "state": "OPEN"}
        ]""")
        prs = orchestration.github_ops._get_related_open_prs(12)
        self.assertEqual([pr["url"] for pr in prs], ["exact"])

    @patch("orchestration.github_ops._get_related_open_prs", return_value=[])
    @patch("orchestration.github_ops._get_issue_snapshot")
    def test_stale_plan_returns_issue_to_planner_queue(self, issue_snapshot, related_prs):
        issue_snapshot.return_value = {
            "state": "OPEN",
            "labels": [],
            "updatedAt": "new",
        }
        record = SimpleNamespace(
            run_id="run-1",
            repo="owner/repo",
            issue_number=1,
            plan={"issue_updated_at": "old"},
        )
        queued = SimpleNamespace(status="queued")
        store = Mock()
        store.find_completed_issue.return_value = None
        store.update.return_value = queued
        self.assertIs(run.preflight_worker(record, store), queued)
        self.assertEqual(store.update.call_args.kwargs["status"], "queued")

    @patch("orchestration.github_ops._get_related_open_prs")
    @patch("orchestration.github_ops._get_issue_snapshot")
    def test_related_open_pr_skips_duplicate_work(self, issue_snapshot, related_prs):
        issue_snapshot.return_value = {
            "state": "OPEN",
            "labels": [],
            "updatedAt": "same",
        }
        related_prs.return_value = [{"url": "https://example/pr/1", "state": "OPEN"}]
        record = SimpleNamespace(
            run_id="run-1",
            repo="owner/repo",
            issue_number=1,
            plan={"issue_updated_at": "same"},
        )
        skipped = SimpleNamespace(status="skipped")
        store = Mock()
        store.find_completed_issue.return_value = None
        store.update.return_value = skipped
        self.assertIs(run.preflight_worker(record, store), skipped)
        self.assertEqual(store.update.call_args.kwargs["status"], "skipped")

    @patch("orchestration.github_ops._get_related_open_prs")
    @patch("orchestration.github_ops._get_issue_snapshot")
    def test_assigned_issue_skips_external_work(self, issue_snapshot, related_prs):
        issue_snapshot.return_value = {
            "state": "OPEN",
            "labels": [],
            "assignees": [{"login": "someone-else"}],
            "updatedAt": "same",
        }
        record = SimpleNamespace(
            run_id="run-1",
            repo="owner/repo",
            issue_number=1,
            plan={"issue_updated_at": "same"},
        )
        skipped = SimpleNamespace(status="skipped")
        store = Mock()
        store.find_completed_issue.return_value = None
        store.update.return_value = skipped
        self.assertIs(run.preflight_worker(record, store), skipped)
        self.assertIn("assigned to", store.update.call_args.kwargs["error"])
        related_prs.assert_not_called()


class SubmissionRecoveryTests(QuietRunTest):
    @patch("orchestration.submission.run_cmd")
    def test_branch_pr_query_is_open_only(self, run_cmd):
        run_cmd.return_value = result(stdout="[]")
        self.assertEqual(run._find_open_pr_for_branch("agent-z/1-run"), "")
        command = run_cmd.call_args.args[0]
        self.assertEqual(command[command.index("--state") + 1], "open")

    @patch("orchestration.submission._find_open_pr_for_branch", return_value="https://example/pr/1")
    def test_coordinator_adopts_existing_branch_pr(self, find):
        record = SimpleNamespace(issue_number=1, branch="agent-z/1-run")
        self.assertEqual(
            run.resolve_submission(record),
            ("https://example/pr/1", "external_existing"),
        )

    @patch("orchestration.submission._create_pr_deterministically", return_value="https://example/pr/2")
    @patch("orchestration.submission._prepare_submission_metadata", return_value={"pr_title": "Fix it"})
    @patch("orchestration.submission._find_open_pr_for_branch", return_value="")
    def test_coordinator_creates_pr_when_none_exists(self, find, metadata, create):
        record = SimpleNamespace(issue_number=1, branch="agent-z/1-run")
        self.assertEqual(
            run.resolve_submission(record),
            ("https://example/pr/2", "coordinator_created"),
        )
        create.assert_called_once_with(record, {"pr_title": "Fix it"})

    @patch("orchestration.submission._create_pr_deterministically", return_value="")
    @patch("orchestration.submission._prepare_submission_metadata", return_value={})
    @patch("orchestration.submission._find_open_pr_for_branch", return_value="")
    def test_submission_returns_empty_when_coordinator_cannot_create(
        self, find, metadata, create
    ):
        record = SimpleNamespace(issue_number=1, branch="agent-z/1-run")
        self.assertEqual(run.resolve_submission(record), ("", ""))

    @patch("orchestration.submission._find_open_pr_for_branch", side_effect=["", ""])
    @patch("orchestration.submission.branch_has_commits", return_value=True)
    @patch("orchestration.submission._verify_pr_url", return_value="https://example/pr/3")
    @patch("orchestration.submission._issue_title_for_pr", return_value="Fix issue")
    @patch("orchestration.submission.run_cmd")
    def test_deterministic_submission_pushes_and_creates_pr(
        self, run_cmd, issue_title, verify, has_commits, find
    ):
        run_cmd.side_effect = [
            result(stdout=""),
            result(),
            result(stdout="https://example/pr/3\n"),
        ]
        record = SimpleNamespace(
            run_id="run-1",
            issue_number=1,
            branch="agent-z/1-run",
            worktree_path="worktree",
        )
        self.assertEqual(
            orchestration.submission._create_pr_deterministically(record),
            "https://example/pr/3",
        )
        commands = [call.args[0][:3] for call in run_cmd.call_args_list]
        self.assertIn(["git", "push", "--set-upstream"], commands)
        self.assertIn(["gh", "pr", "create"], commands)

    @patch(
        "orchestration.submission._find_open_pr_for_branch",
        side_effect=["", "https://example/pr/recovered"],
    )
    @patch("orchestration.submission.branch_has_commits", return_value=True)
    @patch("orchestration.submission._issue_title_for_pr", return_value="Fix issue")
    @patch("orchestration.submission.run_cmd")
    def test_pr_create_failure_adopts_pr_created_before_network_failure(
        self, run_cmd, issue_title, has_commits, find
    ):
        run_cmd.side_effect = [
            result(stdout=""),
            result(),
            result(returncode=1, stderr="connection reset"),
        ]
        record = SimpleNamespace(
            run_id="run-1",
            issue_number=1,
            branch="agent-z/1-run",
            worktree_path="worktree",
        )
        self.assertEqual(
            orchestration.submission._create_pr_deterministically(record),
            "https://example/pr/recovered",
        )

    @patch("orchestration.submission.branch_has_commits", return_value=False)
    @patch("orchestration.submission._issue_title_for_pr", return_value="Fix issue")
    @patch("orchestration.submission.run_cmd", return_value=result(stdout=""))
    def test_deterministic_submission_rejects_branch_without_commits(
        self, run_cmd, issue_title, has_commits
    ):
        record = SimpleNamespace(
            run_id="run-1",
            issue_number=1,
            branch="agent-z/1-run",
            worktree_path="worktree",
        )

        with self.assertRaisesRegex(
            orchestration.errors.NoChangesError,
            "no commits between main",
        ):
            orchestration.submission._create_pr_deterministically(record)

        commands = [call.args[0][:2] for call in run_cmd.call_args_list]
        self.assertNotIn(["git", "push"], commands)

    @patch("orchestration.submission._issue_title_for_pr", return_value="Fallback title")
    def test_submission_metadata_is_sanitized_and_closes_issue(self, issue_title):
        record = SimpleNamespace(issue_number=7, run_id="run-1")
        metadata = run._normalize_submission_metadata(
            record,
            {
                "commit_message": "fix: useful change\nignored",
                "pr_title": "Useful PR\nignored",
                "pr_body": "Summary and tests.",
            },
        )
        self.assertEqual(metadata["commit_message"], "fix: useful change")
        self.assertEqual(metadata["pr_title"], "Useful PR")
        self.assertIn("Closes #7", metadata["pr_body"])
        self.assertIn("Agent-Z run: `run-1`", metadata["pr_body"])

    def test_persisted_submission_metadata_is_reused_without_agent_call(self):
        record = SimpleNamespace(
            issue_number=7,
            run_id="run-1",
            stage="submitting",
            status="running",
        )
        event = SimpleNamespace(
            event_type="submission_metadata_prepared",
            data={
                "commit_message": "fix: persisted",
                "pr_title": "Persisted title",
                "pr_body": "Persisted body\n\nCloses #7\n\nAgent-Z run: `run-1`",
            },
        )
        store = Mock()
        store.list_events.return_value = [event]
        developer = Mock()
        metadata = orchestration.submission._prepare_submission_metadata(record, developer, store)
        self.assertEqual(metadata["commit_message"], "fix: persisted")
        developer.prepare_submission.assert_not_called()
        self.assertEqual(store.add_event.call_args.args[1], "submission_metadata_reused")

    @patch("orchestration.workflow.SUBMISSION_NO_CHANGES_MAX_RETRIES", 1)
    @patch(
        "orchestration.workflow.resolve_submission",
        side_effect=orchestration.errors.NoChangesError("no commits"),
    )
    def test_execute_task_requeues_first_submission_without_changes(self, resolve):
        record = SimpleNamespace(
            run_id="run-1",
            issue_number=1,
            status="running",
            stage="submitting",
            worktree_path="worktree",
            branch="agent-z/1-run",
            sessions={},
            plan={},
            risk="low",
        )
        requeued = SimpleNamespace(
            **{
                **record.__dict__,
                "status": "ready",
                "stage": "developing",
                "error": "no commits",
                "lease_role": None,
            }
        )
        store = Mock()
        store.count_events.return_value = 0
        store.update.return_value = requeued
        worktrees = Mock()
        worktrees.validate.return_value = "worktree"
        agents = [Mock(session_id=None) for _ in range(4)]
        for agent in agents:
            agent.reset_session.side_effect = lambda: None

        self.assertFalse(
            run.execute_task(record, store, worktrees, *agents)
        )

        self.assertEqual(store.update.call_args.kwargs["status"], "ready")
        self.assertEqual(store.update.call_args.kwargs["stage"], "developing")
        event_types = [call.args[1] for call in store.add_event.call_args_list]
        self.assertIn("submission_no_changes_retry", event_types)


class CleanupTests(QuietRunTest):
    @patch("orchestration.github_ops._get_issue_labels", side_effect=[["ongoing"], []])
    @patch("orchestration.github_ops.run_cmd", return_value=result())
    def test_cleanup_removes_owned_label_and_worktree(self, run_cmd, labels):
        record = SimpleNamespace(
            run_id="run-1",
            issue_number=1,
            stage="completed",
            status="completed",
            worktree_path="worktree",
        )
        event = SimpleNamespace(
            event_type="issue_labeled_skip",
            data={"label": "ongoing"},
        )
        updated = SimpleNamespace(
            run_id="run-1",
            stage="completed",
            status="completed",
            worktree_path=None,
        )
        store = Mock()
        store.list_events.return_value = [event]
        store.update.return_value = updated
        worktrees = Mock()
        self.assertTrue(run.cleanup_run_artifacts(
            record,
            store,
            worktrees,
            remove_worktree=True,
            remove_label=True,
        ))
        self.assertIn("--remove-label", run_cmd.call_args.args[0])
        worktrees.remove.assert_called_once_with("worktree")
        event_types = [call.args[1] for call in store.add_event.call_args_list]
        self.assertIn("issue_claim_label_removed", event_types)
        self.assertIn("worktree_removed", event_types)

    @patch("orchestration.github_ops._get_issue_labels", side_effect=[["ongoing"], []])
    @patch(
        "orchestration.github_ops.run_cmd",
        return_value=result(returncode=1, stderr="connection reset"),
    )
    def test_cleanup_accepts_verified_label_removal_after_network_failure(
        self, run_cmd, labels
    ):
        record = SimpleNamespace(
            run_id="run-1",
            issue_number=1,
            stage="completed",
            status="completed",
            worktree_path=None,
        )
        event = SimpleNamespace(
            event_type="issue_labeled_skip",
            data={"label": "ongoing"},
        )
        store = Mock()
        store.list_events.return_value = [event]
        self.assertTrue(run.cleanup_run_artifacts(
            record,
            store,
            Mock(),
            remove_worktree=False,
            remove_label=True,
        ))
        self.assertEqual(store.add_event.call_args.args[1], "issue_claim_label_removed")

    @patch("orchestration.github_ops.run_cmd")
    def test_cleanup_does_not_remove_label_not_owned_by_run(self, run_cmd):
        record = SimpleNamespace(
            run_id="run-1",
            issue_number=1,
            stage="failed",
            status="failed",
            worktree_path="worktree",
        )
        store = Mock()
        store.list_events.return_value = []
        worktrees = Mock()
        self.assertTrue(run.cleanup_run_artifacts(
            record,
            store,
            worktrees,
            remove_worktree=False,
            remove_label=True,
        ))
        run_cmd.assert_not_called()
        worktrees.remove.assert_not_called()


class RoundFlowTests(QuietRunTest):
    @patch("orchestration.workflow.execute_task", return_value=False)
    @patch("orchestration.workflow.plan_task")
    @patch("orchestration.workflow.confirm_issue", return_value=1)
    @patch("orchestration.workflow.show_analysis")
    def test_round_creates_persisted_task(
        self,
        show_analysis,
        confirm_issue,
        plan_task,
        execute_task,
    ):
        analyst = Mock()
        analyst.analyze.return_value = (1, "analysis")
        developer = Mock()
        reviewer = Mock()
        submitter = Mock()
        queued = SimpleNamespace(run_id="run-1")
        planning = SimpleNamespace(run_id="run-1")
        ready = SimpleNamespace(run_id="run-1")
        running = SimpleNamespace(run_id="run-1")
        store = Mock()
        store.enqueue.return_value = queued
        store.claim_for_planning.return_value = planning
        plan_task.return_value = ready
        store.claim_ready.return_value = running
        worktrees = Mock()
        with patch("orchestration.workflow.runtime.auto_mode", True):
            self.assertFalse(run.run_round(
                analyst, developer, reviewer, submitter,
                store, worktrees, target_issue=1,
            ))
        analyst.reset_session.assert_called_once()
        developer.reset_session.assert_called_once()
        reviewer.reset_session.assert_called_once()
        submitter.reset_session.assert_called_once()
        store.enqueue.assert_called_once()
        store.claim_for_planning.assert_called_once()
        plan_task.assert_called_once()
        store.claim_ready.assert_called_once()
        execute_task.assert_called_once()


class ValidationTests(QuietRunTest):
    @patch("orchestration.github_ops.run_cmd")
    def test_base_repo_rejects_local_commits_on_main(self, run_cmd):
        run_cmd.side_effect = [
            result(stdout=""),
            result(stdout="2\n"),
        ]

        with self.assertRaisesRegex(run.NeedsHumanError, "ahead of origin/main by 2"):
            orchestration.github_ops.prepare_base_repo()

    @patch("orchestration.github_ops.run_cmd")
    def test_base_repo_fetches_when_checkout_is_safe(self, run_cmd):
        run_cmd.side_effect = [
            result(stdout=""),
            result(stdout="0\n"),
            result(),
            result(stdout=""),
            result(stdout="0\n"),
        ]

        orchestration.github_ops.prepare_base_repo()

        self.assertEqual(
            run_cmd.call_args_list[2].args[0],
            ["git", "fetch", "origin", "main"],
        )

    @patch("orchestration.github_ops.os.path.isdir", return_value=False)
    def test_missing_project_directory_fails_fast(self, isdir):
        with self.assertRaisesRegex(RuntimeError, "PROJECT_DIR does not exist"):
            orchestration.pools.validate_environment()

    @patch("orchestration.github_ops.run_cmd", return_value=result(stdout="true\n"))
    @patch("orchestration.github_ops.shutil.which")
    @patch("orchestration.github_ops.os.path.isdir", return_value=True)
    def test_only_selected_backends_are_required(self, isdir, which, run_cmd):
        which.side_effect = lambda command: None if command == "opencode" else command
        with patch("orchestration.github_ops.TASK_LEAD_BACKEND", "claude"), \
             patch("orchestration.github_ops.REVIEWER_BACKEND", "codex"):
            orchestration.pools.validate_environment()


class CliTests(QuietRunTest):
    def test_serve_accepts_worker_count(self):
        parser = run.build_parser()
        args = parser.parse_args(["--serve", "--workers", "4"])
        run._validate_args(parser, args)
        self.assertTrue(args.serve)
        self.assertEqual(args.workers, 4)

    def test_workers_requires_serve(self):
        parser = run.build_parser()
        args = parser.parse_args(["--workers", "4"])
        with self.assertRaises(SystemExit):
            run._validate_args(parser, args)

    def test_default_help_hides_advanced_options(self):
        help_text = run.build_parser().format_help()
        self.assertIn("--serve", help_text)
        self.assertIn("--help-all", help_text)
        self.assertNotIn("--worker-idle-sleep", help_text)

    def test_help_all_shows_advanced_options(self):
        help_text = run.build_parser(show_advanced=True).format_help()
        self.assertIn("--worker-idle-sleep", help_text)


class TaskLeadSessionTests(QuietRunTest):
    def test_task_lead_session_is_shared_but_reviewer_is_independent(self):
        analyst = Mock(name="analyst")
        analyst.session_id = "lead-session"
        developer = Mock(name="developer")
        developer.session_id = None
        reviewer = Mock(name="reviewer")
        reviewer.session_id = "review-session"
        submitter = run.CoordinatorAgentState()

        sessions = run._session_snapshot(analyst, developer, reviewer, submitter)
        self.assertEqual(
            sessions,
            {"task_lead": "lead-session", "reviewer": "review-session"},
        )

        record = SimpleNamespace(sessions=sessions)
        analyst.session_id = None
        reviewer.session_id = None
        run._restore_sessions(record, analyst, developer, reviewer, submitter)
        self.assertEqual(analyst.session_id, "lead-session")
        self.assertEqual(developer.session_id, "lead-session")
        self.assertEqual(reviewer.session_id, "review-session")


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
        store.cancel.return_value = SimpleNamespace(
            run_id="run-1",
            issue_number=1,
            stage="cancelled",
            status="cancelled",
            worktree_path="worktree",
        )
        store.list_events.return_value = []
        store.update.return_value = SimpleNamespace(
            run_id="run-1",
            stage="cancelled",
            status="cancelled",
            worktree_path=None,
        )
        worktrees = Mock()
        run.cancel_run(store, worktrees, "run-1")
        worktrees.remove.assert_called_once_with("worktree")
        store.update.assert_called_once_with("run-1", worktree_path=None)


class InspectTests(QuietRunTest):
    def test_show_run_detail_reads_record_and_events(self):
        store = Mock()
        store.get.return_value = SimpleNamespace(
            run_id="run-1",
            issue_number=1,
            repo="owner/repo",
            status="completed",
            stage="completed",
            risk="low",
            branch="agent-z/1-run-1",
            pr_url="https://github.com/owner/repo/pull/1",
            worktree_path=None,
            owner_pid=None,
            error=None,
            created_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-01-01T00:01:00+00:00",
        )
        store.list_events.return_value = [
            SimpleNamespace(
                event_id=1,
                created_at="2026-01-01T00:00:00+00:00",
                event_type="run_created",
                status="running",
                stage="created",
                message="Created",
                data={"issue_number": 1},
            )
        ]
        run.show_run_detail(store, "run-1")
        store.get.assert_called_once_with("run-1")
        store.list_events.assert_called_once_with("run-1")


class WorkerTests(QuietRunTest):
    @patch("orchestration.pools.execute_task")
    @patch("orchestration.pools._build_agents", return_value=(Mock(), Mock(), Mock(), Mock()))
    @patch("orchestration.pools.WorktreeManager")
    @patch("orchestration.pools.RunStore")
    @patch("orchestration.pools.validate_environment")
    def test_worker_claims_one_task_and_stops(
        self,
        validate_environment,
        store_class,
        worktree_class,
        build_agents,
        execute_task,
    ):
        record = SimpleNamespace(run_id="run-1", stage="queued", status="running")
        store = Mock()
        store.claim_ready.return_value = record
        store_class.return_value = store
        claimed = run.run_worker(max_runs=1, idle_sleep=1)
        self.assertEqual(claimed, 1)
        store.claim_ready.assert_called_once()
        execute_task.assert_called_once()
        event_types = [call.args[1] for call in store.add_event.call_args_list]
        self.assertIn("worker_started", event_types)
        self.assertIn("worker_run_started", event_types)
        self.assertIn("worker_stopped", event_types)

    @patch("orchestration.pools.execute_task", side_effect=run.NeedsHumanError("submission blocked"))
    @patch("orchestration.pools._build_agents", return_value=(Mock(), Mock(), Mock(), Mock()))
    @patch("orchestration.pools.WorktreeManager")
    @patch("orchestration.pools.RunStore")
    @patch("orchestration.pools.validate_environment")
    def test_worker_emits_needs_human_event_and_continues(
        self,
        validate_environment,
        store_class,
        worktree_class,
        build_agents,
        execute_task,
    ):
        record = SimpleNamespace(run_id="run-1", stage="submitting", status="running")
        needs_human = SimpleNamespace(
            run_id="run-1",
            stage="submitting",
            status="needs_human",
        )
        store = Mock()
        store.claim_ready.return_value = record
        store.get.return_value = needs_human
        store_class.return_value = store
        self.assertEqual(run.run_worker(max_runs=1, idle_sleep=1), 1)
        event_types = [call.args[1] for call in store.add_event.call_args_list]
        self.assertIn("worker_run_needs_human", event_types)
        self.assertNotIn("worker_run_failed", event_types)

    @patch("orchestration.pools.WORKER_PREFLIGHT_MAX_RETRIES", 3)
    @patch("orchestration.pools.execute_task", side_effect=RuntimeError("bad preflight"))
    @patch("orchestration.pools._build_agents", return_value=(Mock(), Mock(), Mock(), Mock()))
    @patch("orchestration.pools.WorktreeManager")
    @patch("orchestration.pools.RunStore")
    @patch("orchestration.pools.validate_environment")
    def test_worker_quarantines_repeated_preflight_failure(
        self,
        validate_environment,
        store_class,
        worktree_class,
        build_agents,
        execute_task,
    ):
        record = SimpleNamespace(run_id="run-1", stage="ready", status="running")
        store = Mock()
        store.claim_ready.return_value = record
        store.get.return_value = SimpleNamespace(
            run_id="run-1",
            stage="ready",
            status="running",
        )
        store.count_events.return_value = 2
        store_class.return_value = store

        self.assertEqual(run.run_worker(max_runs=1, idle_sleep=1), 1)

        store.update.assert_called_once_with(
            "run-1",
            status="needs_human",
            stage="ready",
            error="bad preflight",
        )
        event_types = [call.args[1] for call in store.add_event.call_args_list]
        self.assertIn("worker_preflight_exhausted", event_types)

    @patch("orchestration.pools.wait_with_status")
    @patch("orchestration.pools.execute_task", side_effect=RuntimeError(
        "file lock conflict with active run(s): other: module:src/parser"
    ))
    @patch("orchestration.pools._build_agents", return_value=(Mock(), Mock(), Mock(), Mock()))
    @patch("orchestration.pools.WorktreeManager")
    @patch("orchestration.pools.RunStore")
    @patch("orchestration.pools.validate_environment")
    def test_worker_defers_resource_conflict_without_consuming_retry_budget(
        self,
        validate_environment,
        store_class,
        worktree_class,
        build_agents,
        execute_task,
        wait,
    ):
        record = SimpleNamespace(run_id="run-1", stage="ready", status="running")
        store = Mock()
        store.claim_ready.return_value = record
        store.get.return_value = SimpleNamespace(
            run_id="run-1", stage="ready", status="running",
        )
        store_class.return_value = store

        self.assertEqual(run.run_worker(max_runs=1, idle_sleep=1), 1)

        store.count_events.assert_not_called()
        event_types = [call.args[1] for call in store.add_event.call_args_list]
        self.assertIn("worker_resource_deferred", event_types)
        self.assertNotIn("worker_preflight_retry", event_types)


class PlannerTests(QuietRunTest):
    @patch("orchestration.pools.PLANNER_RETRY_BASE_DELAY", 1)
    @patch("orchestration.pools.wait_with_status")
    @patch("orchestration.pools.plan_task", side_effect=RuntimeError("network timeout"))
    @patch("orchestration.pools.RunStore")
    @patch("orchestration.pools.validate_environment")
    def test_planner_requeues_transient_failure(
        self, validate_environment, store_class, plan_task, wait
    ):
        record = SimpleNamespace(run_id="run-1")
        store = Mock()
        store.claim_for_planning.return_value = record
        store.count_events.return_value = 0
        store_class.return_value = store

        self.assertEqual(run.run_planner(max_runs=1, idle_sleep=1), 1)

        plan_task.assert_called_once()
        self.assertFalse(plan_task.call_args.kwargs["fail_on_error"])
        store.update.assert_called_once_with(
            "run-1",
            status="queued",
            stage="queued",
            error="network timeout",
            owner_pid=None,
            lease_role=None,
            lease_expires_at=None,
        )
        event_types = [call.args[1] for call in store.add_event.call_args_list]
        self.assertIn("planner_retry", event_types)

    @patch("orchestration.pools.error")
    @patch("orchestration.pools.plan_task", side_effect=RuntimeError("invalid plan"))
    @patch("orchestration.pools.RunStore")
    @patch("orchestration.pools.validate_environment")
    def test_planner_does_not_retry_deterministic_failure(
        self, validate_environment, store_class, plan_task, error
    ):
        record = SimpleNamespace(run_id="run-1")
        store = Mock()
        store.claim_for_planning.return_value = record
        store.count_events.return_value = 0
        store_class.return_value = store

        self.assertEqual(run.run_planner(max_runs=1, idle_sleep=1), 1)

        store.update.assert_called_once_with(
            "run-1",
            status="failed",
            stage="analyzing",
            error="invalid plan",
        )
        event_types = [call.args[1] for call in store.add_event.call_args_list]
        self.assertIn("planner_failed", event_types)
        self.assertNotIn("planner_retry", event_types)


class ReconcilerTests(QuietRunTest):
    @patch("orchestration.pools._find_open_pr_for_branch", return_value="https://example/pr/1")
    @patch("orchestration.pools.RunStore")
    def test_reconciler_recovers_stranded_submission(self, store_class, find_pr):
        record = SimpleNamespace(
            run_id="run-1",
            branch="agent-z/1-run",
        )
        store = Mock()
        store.reconcile_expired.return_value = []
        store.list_submission_recovery_candidates.return_value = [record]
        store_class.return_value = store
        self.assertEqual(run.run_reconciler(once=True, interval=1), 1)
        store.update.assert_called_once_with(
            "run-1",
            status="ready",
            stage="waiting_checks",
            pr_url="https://example/pr/1",
            error=None,
        )
        self.assertEqual(store.add_event.call_args_list[-1].args[1], "external_pr_adopted")

    @patch("orchestration.pools.SUBMISSION_NO_CHANGES_MAX_RETRIES", 1)
    @patch("orchestration.pools.branch_has_commits", return_value=False)
    @patch("orchestration.pools._find_open_pr_for_branch", return_value="")
    @patch("orchestration.pools.RunStore")
    def test_reconciler_requeues_stranded_submission_without_commits(
        self, store_class, find_pr, has_commits
    ):
        record = SimpleNamespace(
            run_id="run-1",
            branch="agent-z/1-run",
            worktree_path="worktree",
        )
        store = Mock()
        store.reconcile_expired.return_value = []
        store.list_submission_recovery_candidates.return_value = [record]
        store.count_events.return_value = 0
        store_class.return_value = store

        self.assertEqual(run.run_reconciler(once=True, interval=1), 1)

        store.update.assert_called_once_with(
            "run-1",
            status="ready",
            stage="developing",
            error=None,
        )
        self.assertEqual(
            store.add_event.call_args_list[-1].args[1],
            "submission_no_changes_retry",
        )


if __name__ == "__main__":
    unittest.main()
