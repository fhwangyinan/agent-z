import shutil
import subprocess
from .base import AgentRunner


class ClaudeRunner(AgentRunner):
    """Claude Code 执行器：通过 claude -p 调用"""

    def __init__(self, flags: list[str] | None = None, retry_timeout: int = 3600):
        self.cmd = shutil.which("claude") or "claude"
        self.flags = flags or ["--dangerously-skip-permissions"]
        self.retry_timeout = retry_timeout

    def _run(self, args: list[str], cwd: str, timeout: int):
        return subprocess.run(
            args,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
        )

    @staticmethod
    def _failure_message(result) -> str:
        details = (result.stderr or result.stdout or "").strip()
        if len(details) > 1000:
            details = details[:1000] + "..."
        suffix = f": {details}" if details else ""
        return f"Agent returned code {result.returncode}{suffix}"

    def execute(
        self,
        prompt: str,
        timeout: int = 600,
        cwd: str = ".",
        continue_session: bool = False,
    ) -> str:
        args = [self.cmd]
        if continue_session:
            args.append("--continue")
        args.extend(["-p"] + self.flags + [prompt])

        retry_args = [self.cmd, "--continue", "-p"] + self.flags + ["continue"]
        try:
            result = self._run(args, cwd, timeout)
        except subprocess.TimeoutExpired:
            result = None

        if result is None or result.returncode != 0:
            try:
                result = self._run(retry_args, cwd, self.retry_timeout)
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError("Agent timed out after retry") from exc

        if result.returncode != 0:
            raise RuntimeError(self._failure_message(result))

        return result.stdout.strip()
