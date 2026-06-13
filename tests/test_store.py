import tempfile
import unittest
from os import getpid
from pathlib import Path
from unittest.mock import patch

from orchestration.store import RunStore


class RunStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = RunStore(Path(self.temp.name) / "state.db")

    def test_create_update_and_resume(self):
        record = self.store.create("owner/repo", 12, max_parallel=2)
        self.assertEqual(record.status, "running")
        updated = self.store.update(
            record.run_id,
            stage="reviewing",
            sessions={"developer": "session-1"},
        )
        self.assertEqual(updated.sessions["developer"], "session-1")
        self.store.update(record.run_id, status="failed", error="boom")
        failed = self.store.get(record.run_id)
        self.assertIsNone(failed.owner_pid)
        resumed = self.store.resume(record.run_id, max_parallel=2)
        self.assertEqual(resumed.status, "running")
        self.assertIsNotNone(resumed.owner_pid)
        self.assertIsNone(resumed.error)
        events = self.store.list_events(record.run_id)
        event_types = [event.event_type for event in events]
        self.assertIn("run_created", event_types)
        self.assertIn("status_changed", event_types)
        self.assertIn("run_resumed", event_types)

    def test_active_issue_lock_prevents_duplicate(self):
        self.store.create("owner/repo", 12, max_parallel=2)
        with self.assertRaisesRegex(RuntimeError, "already has an active run"):
            self.store.create("owner/repo", 12, max_parallel=2)

    def test_parallel_limit_is_enforced(self):
        self.store.create("owner/repo", 1, max_parallel=1)
        with self.assertRaisesRegex(RuntimeError, "parallel run limit"):
            self.store.create("owner/repo", 2, max_parallel=1)

    def test_final_run_releases_issue_lock(self):
        record = self.store.create("owner/repo", 12, max_parallel=1)
        self.store.update(record.run_id, status="completed")
        replacement = self.store.create("owner/repo", 12, max_parallel=1)
        self.assertNotEqual(record.run_id, replacement.run_id)

    def test_file_claim_detects_conflicting_active_run(self):
        first = self.store.create("owner/repo", 1, max_parallel=2)
        second = self.store.create("owner/repo", 2, max_parallel=2)
        self.store.claim_files(first.run_id, ["src/shared.py"])
        event = self.store.list_events(first.run_id)[-1]
        self.assertEqual(event.event_type, "files_claimed")
        self.assertEqual(event.data["files"], ["src/shared.py"])
        with self.assertRaisesRegex(RuntimeError, "file lock conflict"):
            self.store.claim_files(second.run_id, ["src/shared.py", "src/other.py"])

    def test_file_claim_detects_same_module_conflict(self):
        first = self.store.create("owner/repo", 1, max_parallel=2)
        second = self.store.create("owner/repo", 2, max_parallel=2)
        self.store.claim_files(first.run_id, ["src/parser/reader.py"])
        with self.assertRaisesRegex(RuntimeError, "module:src/parser"):
            self.store.claim_files(second.run_id, ["src/parser/writer.py"])

    def test_queue_claims_oldest_task_when_slot_available(self):
        first = self.store.enqueue("owner/repo", 1)
        self.store.enqueue("owner/repo", 2)
        planning = self.store.claim_for_planning(lease_seconds=60)
        self.assertEqual(planning.run_id, first.run_id)
        self.store.finish_planning(
            planning.run_id,
            plan={"summary": "fix it"},
            risk="low",
        )
        claimed = self.store.claim_ready(max_parallel=1, lease_seconds=60)
        self.assertEqual(claimed.run_id, first.run_id)
        self.assertEqual(claimed.status, "running")
        self.assertEqual(self.store.list_events(first.run_id)[-1].event_type, "worker_claimed")
        self.assertIsNone(self.store.claim_ready(max_parallel=1, lease_seconds=60))

    def test_worker_cannot_claim_unplanned_issue(self):
        self.store.enqueue("owner/repo", 1)
        self.assertIsNone(self.store.claim_ready(max_parallel=1, lease_seconds=60))

    def test_worker_preserves_recovered_resume_stage(self):
        queued = self.store.enqueue("owner/repo", 1)
        self.store.update(
            queued.run_id,
            status="ready",
            stage="waiting_checks",
            pr_url="https://example/pr/1",
        )
        claimed = self.store.claim_ready(max_parallel=1, lease_seconds=60)
        self.assertEqual(claimed.stage, "waiting_checks")

    def test_planning_persists_structured_plan(self):
        queued = self.store.enqueue("owner/repo", 1)
        planning = self.store.claim_for_planning(60, queued.run_id)
        ready = self.store.finish_planning(
            planning.run_id,
            plan={"summary": "fix it", "predicted_files": ["src/a.py"]},
            risk="medium",
        )
        self.assertEqual(ready.status, "ready")
        self.assertEqual(ready.plan["predicted_files"], ["src/a.py"])
        self.assertIsNone(ready.owner_pid)

    @patch("orchestration.store._pid_alive", return_value=False)
    def test_reconciler_requeues_expired_planner_lease(self, pid_alive):
        queued = self.store.enqueue("owner/repo", 1)
        planning = self.store.claim_for_planning(60, queued.run_id)
        self.store.update(planning.run_id, lease_expires_at="2000-01-01T00:00:00+00:00")
        reconciled = self.store.reconcile_expired()
        self.assertEqual(reconciled[0].status, "queued")
        self.assertEqual(reconciled[0].stage, "queued")

    def test_reconciler_quarantines_expired_worker_lease(self):
        queued = self.store.enqueue("owner/repo", 1)
        self.store.claim_for_planning(60, queued.run_id)
        self.store.finish_planning(queued.run_id, plan={}, risk="low")
        running = self.store.claim_ready(max_parallel=1, lease_seconds=60)
        self.store.update(running.run_id, lease_expires_at="2000-01-01T00:00:00+00:00")
        reconciled = self.store.reconcile_expired()
        self.assertEqual(reconciled[0].status, "needs_human")
        self.assertEqual(reconciled[0].stage, "ready")

    @patch("orchestration.store._pid_alive", return_value=False)
    def test_reconciler_immediately_requeues_dead_planner_owner(self, pid_alive):
        queued = self.store.enqueue("owner/repo", 1)
        planning = self.store.claim_for_planning(3600, queued.run_id)

        recovered = self.store.reconcile_dead_owners()

        self.assertEqual(recovered[0].status, "queued")
        self.assertEqual(recovered[0].stage, "queued")
        self.assertIsNone(recovered[0].owner_pid)
        self.assertEqual(
            self.store.list_events(planning.run_id)[-1].event_type,
            "dead_owner_recovered",
        )

    @patch("orchestration.store._pid_alive", return_value=False)
    def test_reconciler_releases_dead_worker_at_checkpoint(self, pid_alive):
        queued = self.store.enqueue("owner/repo", 1)
        self.store.claim_for_planning(60, queued.run_id)
        self.store.finish_planning(queued.run_id, plan={}, risk="low")
        running = self.store.claim_ready(max_parallel=1, lease_seconds=3600)
        self.store.update(running.run_id, stage="submitting")

        recovered = self.store.reconcile_dead_owners()

        self.assertEqual(recovered[0].status, "ready")
        self.assertEqual(recovered[0].stage, "submitting")
        self.assertIsNone(recovered[0].owner_pid)

    @patch("orchestration.store._pid_alive", return_value=True)
    def test_reconciler_leaves_live_owner_untouched(self, pid_alive):
        queued = self.store.enqueue("owner/repo", 1)
        self.store.claim_for_planning(3600, queued.run_id)

        self.assertEqual(self.store.reconcile_dead_owners(), [])
        self.assertEqual(self.store.get(queued.run_id).status, "planning")

    def test_heartbeat_atomically_requires_an_active_owned_lease(self):
        queued = self.store.enqueue("owner/repo", 1)
        planning = self.store.claim_for_planning(60, queued.run_id)
        event_count = len(self.store.list_events(planning.run_id))
        renewed = self.store.heartbeat(planning.run_id, "planner", 120)
        self.assertEqual(renewed.lease_role, "planner")
        self.assertEqual(len(self.store.list_events(planning.run_id)), event_count)
        self.store.finish_planning(planning.run_id, plan={}, risk="low")
        with self.assertRaisesRegex(RuntimeError, "not leased"):
            self.store.heartbeat(planning.run_id, "planner", 120)

    def test_lists_stranded_submissions_for_recovery(self):
        run = self.store.create("owner/repo", 1, max_parallel=1)
        self.store.update(
            run.run_id,
            status="failed",
            stage="submitting",
            worktree_path="worktree",
            branch="agent-z/1-run",
        )
        candidates = self.store.list_submission_recovery_candidates()
        self.assertEqual([candidate.run_id for candidate in candidates], [run.run_id])

    def test_lists_nonactive_runs_with_prs_for_recovery(self):
        run = self.store.create("owner/repo", 1, max_parallel=1)
        self.store.update(
            run.run_id,
            status="needs_human",
            stage="waiting_checks",
            pr_url="https://example/pr/1",
        )

        candidates = self.store.list_pr_recovery_candidates()

        self.assertEqual([candidate.run_id for candidate in candidates], [run.run_id])

    def test_queued_issue_is_locked(self):
        self.store.enqueue("owner/repo", 1)
        with self.assertRaisesRegex(RuntimeError, "queued or active"):
            self.store.enqueue("owner/repo", 1)

    def test_scheduler_can_release_only_its_own_unclaimed_queue(self):
        scheduled = self.store.enqueue("owner/repo", 1)
        self.store.add_event(scheduled.run_id, "scheduler_enqueued")
        manual = self.store.enqueue("owner/repo", 2)
        self.assertEqual(
            [record.issue_number for record in self.store.list_scheduler_queued("owner/repo")],
            [1],
        )
        self.assertTrue(self.store.release_scheduler_queued(
            scheduled.run_id,
            action="reject",
            reason="Tracking issue",
        ))
        self.assertEqual(self.store.get(scheduled.run_id).status, "skipped")
        self.assertFalse(self.store.release_scheduler_queued(
            manual.run_id,
            action="reject",
            reason="Should not change",
        ))
        self.assertEqual(self.store.get(manual.run_id).status, "queued")

    def test_active_issue_numbers_only_returns_non_final_runs(self):
        active = self.store.enqueue("owner/repo", 1)
        final = self.store.enqueue("owner/repo", 2)
        self.store.update(final.run_id, status="completed")
        self.store.enqueue("other/repo", 3)
        self.assertEqual(self.store.active_issue_numbers("owner/repo"), {active.issue_number})

    def test_scheduler_snapshot_and_queue_state_are_persisted(self):
        queued = self.store.enqueue("owner/repo", 1)
        planning = self.store.enqueue("owner/repo", 2)
        self.store.claim_for_planning(60, planning.run_id)
        self.assertEqual(
            self.store.scheduler_queue_state("owner/repo"),
            {"1": "queued", "2": "planning"},
        )
        candidates = {"3": {"updated_at": "now", "open_pr": False}}
        queue_state = self.store.scheduler_queue_state("owner/repo")
        self.store.save_scheduler_snapshot(
            "owner/repo",
            candidate_state=candidates,
            queue_state=queue_state,
            policy_state={"version": 1},
            agent_evaluated=True,
        )
        snapshot = self.store.get_scheduler_snapshot("owner/repo")
        self.assertEqual(snapshot["candidate_state"], candidates)
        self.assertEqual(snapshot["queue_state"], queue_state)
        self.assertEqual(snapshot["policy_state"], {"version": 1})
        self.assertIsNotNone(snapshot["agent_evaluated_at"])
        self.assertTrue(snapshot["updated_at"])

    def test_lists_recent_global_events_without_run_events(self):
        run = self.store.enqueue("owner/repo", 1)
        self.store.add_event(None, "scheduler_scan_failed", message="network")
        self.store.add_event(run.run_id, "worker_claimed")
        events = self.store.list_global_events()
        self.assertEqual([event.event_type for event in events], ["scheduler_scan_failed"])

    def test_counts_events_by_type(self):
        run = self.store.enqueue("owner/repo", 1)
        self.store.add_event(run.run_id, "worker_preflight_retry")
        self.store.add_event(run.run_id, "worker_preflight_retry")
        self.assertEqual(
            self.store.count_events(run.run_id, "worker_preflight_retry"),
            2,
        )

    @patch("orchestration.store._pid_alive", return_value=True)
    def test_resume_rejects_run_owned_by_another_live_process(self, pid_alive):
        record = self.store.create("owner/repo", 1, max_parallel=1)
        self.store.update(record.run_id, owner_pid=getpid() + 1000)
        with self.assertRaisesRegex(RuntimeError, "active process"):
            self.store.resume(record.run_id, max_parallel=1)

    def test_resume_allows_current_process_to_continue_claimed_task(self):
        record = self.store.enqueue("owner/repo", 1)
        self.store.claim_for_planning(60, record.run_id)
        self.store.finish_planning(record.run_id, plan={}, risk="low")
        self.store.claim_ready(max_parallel=1, lease_seconds=60)
        resumed = self.store.resume(record.run_id, max_parallel=1)
        self.assertEqual(resumed.owner_pid, getpid())

    def test_cancelled_run_cannot_be_resumed(self):
        record = self.store.enqueue("owner/repo", 1)
        self.store.update(record.run_id, status="cancelled")
        with self.assertRaisesRegex(RuntimeError, "cannot be resumed"):
            self.store.resume(record.run_id, max_parallel=1)

    def test_cancel_releases_queued_issue_lock(self):
        record = self.store.enqueue("owner/repo", 1)
        cancelled = self.store.cancel(record.run_id)
        self.assertEqual(cancelled.status, "cancelled")
        replacement = self.store.enqueue("owner/repo", 1)
        self.assertNotEqual(record.run_id, replacement.run_id)

    @patch("orchestration.store._pid_alive", return_value=True)
    def test_cancel_rejects_live_owner(self, pid_alive):
        record = self.store.create("owner/repo", 1, max_parallel=1)
        with self.assertRaisesRegex(RuntimeError, "active process"):
            self.store.cancel(record.run_id)


if __name__ == "__main__":
    unittest.main()
