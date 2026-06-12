import json
import shutil
import subprocess
import uuid

from .base import AgentResult, AgentRunner, BackendCapabilities, decode_subprocess_output


class ClaudeRunner(AgentRunner):
    """Claude Code backend using explicit session IDs."""

    name = "claude"
    capabilities = BackendCapabilities(
        session_mode="explicit",
        output_mode="events",
        isolation="permissions",
        stdin_prompt=True,
    )

    def __init__(self, flags: list[str] | None = None, retry_timeout: int = 3600):
        self.command = shutil.which("claude") or "claude"
        self.flags = flags if flags is not None else ["--dangerously-skip-permissions"]
        self.retry_timeout = retry_timeout

    def _run(self, args: list[str], prompt: str, cwd: str, timeout: int):
        result = subprocess.run(
            args,
            input=prompt.encode("utf-8"),
            cwd=cwd,
            capture_output=True,
            timeout=timeout,
        )
        result.stdout = decode_subprocess_output(result.stdout)
        result.stderr = decode_subprocess_output(result.stderr)
        return result

    @staticmethod
    def _failure_message(result) -> str:
        details = (result.stderr or result.stdout or "").strip()
        if len(details) > 1000:
            details = details[:1000] + "..."
        suffix = f": {details}" if details else ""
        return f"Claude Code returned code {result.returncode}{suffix}"

    @staticmethod
    def _missing_session(result) -> bool:
        details = f"{result.stderr or ''}\n{result.stdout or ''}".lower()
        return "no conversation found with session id" in details

    @staticmethod
    def _parse_result(stdout: str, fallback_session_id: str) -> AgentResult:
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError:
            return AgentResult(stdout.strip(), fallback_session_id)
        output = payload.get("result", "")
        session_id = payload.get("session_id") or fallback_session_id
        return AgentResult(str(output).strip(), session_id)

    def _args(self, session_id: str | None) -> tuple[list[str], str]:
        active_session = session_id or str(uuid.uuid4())
        args = [self.command]
        if session_id:
            args.extend(["--resume", session_id])
        else:
            args.extend(["--session-id", active_session])
        args.extend(["-p", "--output-format", "json", *self.flags])
        return args, active_session

    def execute(
        self,
        prompt: str,
        timeout: int = 600,
        cwd: str = ".",
        session_id: str | None = None,
    ) -> AgentResult:
        args, active_session = self._args(session_id)
        try:
            result = self._run(args, prompt, cwd, timeout)
        except subprocess.TimeoutExpired:
            result = None

        if result is None or result.returncode != 0:
            missing_session = result is not None and self._missing_session(result)
            retry_prompt = prompt if missing_session else "Continue the previous task."
            retry_session = None if missing_session else active_session
            retry_args, retry_active_session = self._args(retry_session)
            try:
                result = self._run(retry_args, retry_prompt, cwd, self.retry_timeout)
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError("Claude Code timed out after retry") from exc
            active_session = retry_active_session

        if result.returncode != 0 and self._missing_session(result):
            retry_args, active_session = self._args(None)
            try:
                result = self._run(retry_args, prompt, cwd, self.retry_timeout)
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError("Claude Code timed out after fresh-session retry") from exc

        if result.returncode != 0:
            raise RuntimeError(self._failure_message(result))
        return self._parse_result(result.stdout, active_session)
