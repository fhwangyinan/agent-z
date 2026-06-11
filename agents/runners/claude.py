import shutil
import subprocess
from .base import AgentRunner


class ClaudeRunner(AgentRunner):
    """Claude Code 执行器：通过 claude -p 调用"""

    def __init__(self, flags: list[str] | None = None):
        self.cmd = shutil.which("claude") or "claude"
        self.flags = flags or ["--dangerously-skip-permissions"]

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

        try:
            result = subprocess.run(
                args,
                cwd=cwd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            # 第一次超时，用 --continue 重试一次
            retry_args = [self.cmd, "--continue", "-p"] + self.flags + ["continue"]
            try:
                result = subprocess.run(
                    retry_args,
                    cwd=cwd,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    timeout=3600,
                )
            except subprocess.TimeoutExpired:
                raise RuntimeError("Agent timed out after retry")

        if result.returncode != 0:
            # 非零返回码，也重试一次
            retry_args = [self.cmd, "--continue", "-p"] + self.flags + ["continue"]
            try:
                result = subprocess.run(
                    retry_args,
                    cwd=cwd,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    timeout=3600,
                )
            except subprocess.TimeoutExpired:
                raise RuntimeError("Agent failed after retry")

            if result.returncode != 0:
                raise RuntimeError(f"Agent returned code {result.returncode}")

        return result.stdout.strip()
