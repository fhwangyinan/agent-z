import json
import shutil
import subprocess

from .base import AgentResult, AgentRunner, BackendCapabilities


class OpenCodeRunner(AgentRunner):
    """OpenCode CLI backend using JSON events and explicit sessions."""

    name = "opencode"
    capabilities = BackendCapabilities(
        session_mode="explicit",
        output_mode="events",
        isolation="permissions",
        stdin_prompt=False,
    )

    def __init__(self, flags: list[str] | None = None, retry_timeout: int = 3600):
        self.command = shutil.which("opencode") or "opencode"
        self.flags = flags if flags is not None else []
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
    def _parse_events(stdout: str, fallback_session_id: str | None = None) -> AgentResult:
        session_id = fallback_session_id
        parts: list[str] = []
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            session_id = event.get("sessionID") or session_id
            part = event.get("part", {})
            if event.get("type") == "text" and part.get("text"):
                parts.append(part["text"])
        return AgentResult("\n".join(parts).strip() or stdout.strip(), session_id)

    def _args(self, prompt: str, session_id: str | None) -> list[str]:
        args = [self.command, "run", "--format", "json", *self.flags]
        if session_id:
            args.extend(["--session", session_id])
        args.append(prompt)
        return args

    def execute(
        self,
        prompt: str,
        timeout: int = 600,
        cwd: str = ".",
        session_id: str | None = None,
    ) -> AgentResult:
        try:
            result = self._run(self._args(prompt, session_id), cwd, timeout)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("OpenCode timed out") from exc
        if result.returncode != 0:
            details = (result.stderr or result.stdout or "").strip()
            raise RuntimeError(f"OpenCode returned code {result.returncode}: {details[:1000]}")
        return self._parse_events(result.stdout, session_id)
