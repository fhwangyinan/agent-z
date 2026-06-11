import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import run
from orchestration.store import RunStore


def fake_agent(name):
    agent = Mock()
    agent.name = name
    agent.session_id = None
    agent.reset_session.side_effect = lambda: setattr(agent, "session_id", None)
    return agent


class TaskResumeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = RunStore(Path(self.temp.name) / "state.db")
        record = self.store.create("owner/repo", 12, max_parallel=2)
        self.worktree = Path(self.temp.name) / "worktree"
        self.worktree.mkdir()
        self.record = self.store.update(
            record.run_id,
            status="waiting_checks",
            stage="waiting_checks",
            worktree_path=str(self.worktree),
            branch="agent-z/12-run",
            pr_url="https://example/pr/1",
            risk="low",
            sessions={"developer": "dev-session"},
        )

    @patch("run.CLEANUP_COMPLETED_WORKTREES", False)
    @patch("run.wait_for_pr_checks", return_value=True)
    @patch("run.done")
    @patch("run.step")
    @patch("run.console")
    def test_resume_waiting_checks_does_not_repeat_development(
        self, console, step, done, wait_for_checks
    ):
        analyst = fake_agent("Analyst")
        developer = fake_agent("Developer")
        reviewer = fake_agent("Reviewer")
        submitter = fake_agent("Submitter")
        developer.apply_review.return_value = "NO_ACTION_NEEDED"
        worktrees = Mock()
        worktrees.validate.return_value = self.worktree

        self.assertTrue(run.execute_task(
            self.record,
            self.store,
            worktrees,
            analyst,
            developer,
            reviewer,
            submitter,
        ))

        developer.fix.assert_not_called()
        submitter.submit.assert_not_called()
        self.assertEqual(self.store.get(self.record.run_id).status, "completed")


if __name__ == "__main__":
    unittest.main()
