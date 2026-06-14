import unittest
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

from agents.base import Agent, prompt_with_context
from agents.runners.base import AgentResult, BackendCapabilities
from orchestration.errors import WorkspaceIsolationError


class FakeRunner:
    name = "fake"
    command = "fake"
    capabilities = BackendCapabilities(
        session_mode="explicit",
        output_mode="text",
        isolation="none",
        stdin_prompt=True,
    )

    def __init__(self, prefix):
        self.prefix = prefix
        self.sessions = []

    def execute(self, prompt, timeout=600, cwd=".", session_id=None):
        self.sessions.append(session_id)
        return AgentResult(prompt, session_id or f"{self.prefix}-{len(self.sessions)}")


class AgentSessionTests(unittest.TestCase):
    def test_prompt_context_keeps_variable_values_after_stable_instructions(self):
        first = prompt_with_context("Stable instructions.", issue_number=1)
        second = prompt_with_context("Stable instructions.", issue_number=2)
        self.assertEqual(
            first.split("TASK CONTEXT", 1)[0],
            second.split("TASK CONTEXT", 1)[0],
        )
        self.assertGreater(first.index('"issue_number"'), first.index("TASK CONTEXT"))

    @patch("agents.base.done")
    @patch("agents.base.agent_status")
    def test_each_agent_owns_an_independent_session(self, status, done):
        first_runner = FakeRunner("developer")
        second_runner = FakeRunner("reviewer")
        first = Agent("Developer", "fake", runner=first_runner)
        second = Agent("Reviewer", "fake", runner=second_runner)

        first.run("initial", resume_session=True)
        second.run("review", resume_session=True)
        first.run("follow-up", resume_session=True)

        self.assertEqual(first_runner.sessions, [None, "developer-1"])
        self.assertEqual(second_runner.sessions, [None])
        self.assertNotEqual(first.session_id, second.session_id)

        first.reset_session()
        first.run("new task", resume_session=True)
        self.assertEqual(first_runner.sessions[-1], None)


class AgentWorkspaceIsolationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name) / "project"
        self.worktree = Path(self.temp.name) / "worktree"
        self.project.mkdir()
        self._git(self.project, "init", "-b", "main")
        self._git(self.project, "config", "user.email", "test@example.com")
        self._git(self.project, "config", "user.name", "Test")
        (self.project / "file.txt").write_text("base", encoding="utf-8")
        self._git(self.project, "add", "file.txt")
        self._git(self.project, "commit", "-m", "base")
        self._git(self.project, "worktree", "add", "-b", "agent-z/1-run", str(self.worktree))

    @staticmethod
    def _git(cwd, *args):
        subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
        )

    @patch("agents.base.done")
    @patch("agents.base.agent_status")
    def test_task_prompt_declares_workspace_boundary(self, status, done):
        runner = FakeRunner("developer")
        agent = Agent("Developer", "fake", runner=runner)
        agent.set_workspace(str(self.worktree))

        with patch("agents.base.PROJECT_DIR", str(self.project)):
            output = agent.run("Fix it")

        self.assertIn("WORKSPACE BOUNDARY", output)
        self.assertIn(str(self.worktree.resolve()), output)
        self.assertIn("Stay on task branch: agent-z/1-run", output)
        self.assertLess(output.index("Fix it"), output.index(str(self.worktree.resolve())))

    @patch("agents.base.done")
    @patch("agents.base.agent_status")
    def test_task_agent_refuses_protected_branch(self, status, done):
        agent = Agent("Developer", "fake", runner=FakeRunner("developer"))
        agent.set_workspace(str(self.project))

        with patch("agents.base.PROJECT_DIR", str(self.worktree)):
            with self.assertRaisesRegex(WorkspaceIsolationError, "protected branch main"):
                agent.run("Fix it")

    @patch("agents.base.done")
    @patch("agents.base.agent_status")
    def test_task_agent_detects_main_head_change(self, status, done):
        runner = FakeRunner("developer")

        def change_main(*args, **kwargs):
            (self.project / "leak.txt").write_text("leak", encoding="utf-8")
            self._git(self.project, "add", "leak.txt")
            self._git(self.project, "commit", "-m", "leaked")
            return AgentResult("done", "session")

        runner.execute = change_main
        agent = Agent("Developer", "fake", runner=runner)
        agent.set_workspace(str(self.worktree))

        with patch("agents.base.PROJECT_DIR", str(self.project)):
            with self.assertRaisesRegex(WorkspaceIsolationError, "main checkout HEAD changed"):
                agent.run("Fix it")


if __name__ == "__main__":
    unittest.main()
