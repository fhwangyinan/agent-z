import shutil
import subprocess
from pathlib import Path


class WorktreeManager:
    def __init__(self, project_dir: str | Path, root: str | Path):
        self.project_dir = Path(project_dir).resolve()
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, run_id: str) -> Path:
        return self.root / run_id

    def create(self, run_id: str, branch: str, base_ref: str = "origin/main") -> Path:
        path = self.path_for(run_id)
        if path.exists():
            return path
        exists = subprocess.run(
            ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
            cwd=self.project_dir,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        ).returncode == 0
        command = ["git", "worktree", "add", str(path), branch]
        if not exists:
            command = ["git", "worktree", "add", "-b", branch, str(path), base_ref]
        result = subprocess.run(
            command,
            cwd=self.project_dir,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode != 0:
            details = (result.stderr or result.stdout).strip()
            raise RuntimeError(f"failed to create worktree: {details}")
        return path

    def validate(self, path: str | Path) -> Path:
        resolved = Path(path).resolve()
        if not resolved.is_dir():
            raise RuntimeError(f"worktree does not exist: {resolved}")
        result = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=resolved,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode != 0 or result.stdout.strip() != "true":
            raise RuntimeError(f"invalid worktree: {resolved}")
        return resolved

    def remove(self, path: str | Path):
        resolved = Path(path).resolve()
        if not resolved.exists():
            return
        result = subprocess.run(
            ["git", "worktree", "remove", "--force", str(resolved)],
            cwd=self.project_dir,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode != 0:
            details = (result.stderr or result.stdout).strip()
            raise RuntimeError(f"failed to remove worktree: {details}")
        if resolved.exists():
            shutil.rmtree(resolved)
