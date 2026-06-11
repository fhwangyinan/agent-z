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
        with self.assertRaisesRegex(RuntimeError, "file lock conflict"):
            self.store.claim_files(second.run_id, ["src/shared.py", "src/other.py"])

    def test_queue_claims_oldest_task_when_slot_available(self):
        first = self.store.enqueue("owner/repo", 1)
        self.store.enqueue("owner/repo", 2)
        claimed = self.store.claim_next(max_parallel=1)
        self.assertEqual(claimed.run_id, first.run_id)
        self.assertEqual(claimed.status, "running")
        self.assertIsNone(self.store.claim_next(max_parallel=1))

    def test_queued_issue_is_locked(self):
        self.store.enqueue("owner/repo", 1)
        with self.assertRaisesRegex(RuntimeError, "queued or active"):
            self.store.enqueue("owner/repo", 1)

    @patch("orchestration.store._pid_alive", return_value=True)
    def test_resume_rejects_run_owned_by_another_live_process(self, pid_alive):
        record = self.store.create("owner/repo", 1, max_parallel=1)
        self.store.update(record.run_id, owner_pid=getpid() + 1000)
        with self.assertRaisesRegex(RuntimeError, "active process"):
            self.store.resume(record.run_id, max_parallel=1)

    def test_resume_allows_current_process_to_continue_claimed_task(self):
        record = self.store.enqueue("owner/repo", 1)
        self.store.claim_next(max_parallel=1)
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
