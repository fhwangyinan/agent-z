import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from orchestration.worktree import WorktreeManager


def result(returncode=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


class WorktreeManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.project = root / "project"
        self.project.mkdir()
        self.manager = WorktreeManager(self.project, root / "worktrees")

    @patch("orchestration.worktree.subprocess.run", side_effect=[result(1), result()])
    def test_create_uses_isolated_branch_and_origin_main(self, execute):
        path = self.manager.create("run-1", "agent-z/12-run-1")
        command = execute.call_args.args[0]
        self.assertEqual(command[:4], ["git", "worktree", "add", "-b"])
        self.assertIn("agent-z/12-run-1", command)
        self.assertEqual(command[-1], "origin/main")
        self.assertEqual(path, self.manager.root / "run-1")

    @patch("orchestration.worktree.subprocess.run", side_effect=[result(), result()])
    def test_create_reuses_existing_branch(self, execute):
        self.manager.create("run-1", "agent-z/12-run-1")
        command = execute.call_args_list[1].args[0]
        self.assertEqual(
            command,
            ["git", "worktree", "add", str(self.manager.path_for("run-1")), "agent-z/12-run-1"],
        )

    @patch(
        "orchestration.worktree.subprocess.run",
        side_effect=[result(stdout="true\n"), result(stdout="agent-z/12-run-1\n")],
    )
    def test_validate_accepts_git_worktree(self, execute):
        path = self.manager.path_for("run-1")
        path.mkdir()
        self.assertEqual(self.manager.validate(path), path)
        self.assertEqual(execute.call_args_list[0].kwargs["errors"], "replace")

    def test_validate_rejects_protected_main_checkout(self):
        with self.assertRaisesRegex(RuntimeError, "protected main checkout"):
            self.manager.validate(self.project)

    @patch(
        "orchestration.worktree.subprocess.run",
        side_effect=[result(stdout="true\n"), result(stdout="main\n")],
    )
    def test_validate_rejects_main_branch(self, execute):
        path = self.manager.path_for("run-1")
        path.mkdir()
        with self.assertRaisesRegex(RuntimeError, "not on an isolated branch"):
            self.manager.validate(path)


if __name__ == "__main__":
    unittest.main()
