import json
import shutil
import subprocess
import uuid

from .base import AgentResult, AgentRunner, BackendCapabilities


class ClaudeRunner(AgentRunner):
    """Claude Code backend using explicit session IDs."""

    name = "claude"
    capabilities = BackendCapabilities(
        session_mode="explicit",
        output_mode="events",
        isolation="permissions",
        stdin_prompt=False,
    )

    def __init__(self, flags: list[str] | None = None, retry_timeout: int = 3600):
        self.command = shutil.which("claude") or "claude"
        self.flags = flags if flags is not None else ["--dangerously-skip-permissions"]
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
        return f"Claude Code returned code {result.returncode}{suffix}"

    @staticmethod
    def _parse_result(stdout: str, fallback_session_id: str) -> AgentResult:
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError:
            return AgentResult(stdout.strip(), fallback_session_id)
        output = payload.get("result", "")
        session_id = payload.get("session_id") or fallback_session_id
        return AgentResult(str(output).strip(), session_id)

    def _args(self, prompt: str, session_id: str | None) -> tuple[list[str], str]:
        active_session = session_id or str(uuid.uuid4())
        args = [self.command]
        if session_id:
            args.extend(["--resume", session_id])
        else:
            args.extend(["--session-id", active_session])
        args.extend(["-p", "--output-format", "json", *self.flags, prompt])
        return args, active_session

    def execute(
        self,
        prompt: str,
        timeout: int = 600,
        cwd: str = ".",
        session_id: str | None = None,
    ) -> AgentResult:
        args, active_session = self._args(prompt, session_id)
        try:
            result = self._run(args, cwd, timeout)
        except subprocess.TimeoutExpired:
            result = None

        if result is None or result.returncode != 0:
            retry_args, _ = self._args("Continue the previous task.", active_session)
            try:
                result = self._run(retry_args, cwd, self.retry_timeout)
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError("Claude Code timed out after retry") from exc

        if result.returncode != 0:
            raise RuntimeError(self._failure_message(result))
        return self._parse_result(result.stdout, active_session)
