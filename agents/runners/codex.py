import json
import shutil
import subprocess

from .base import AgentResult, AgentRunner, BackendCapabilities


class CodexRunner(AgentRunner):
    """Codex CLI backend using JSONL events and explicit session resume."""

    name = "codex"
    capabilities = BackendCapabilities(
        session_mode="explicit",
        output_mode="events",
        isolation="sandbox",
        stdin_prompt=True,
    )

    def __init__(self, flags: list[str] | None = None, retry_timeout: int = 3600):
        self.command = shutil.which("codex") or "codex"
        self.flags = flags if flags is not None else [
            "--dangerously-bypass-approvals-and-sandbox",
        ]
        self.retry_timeout = retry_timeout

    def _run(self, args: list[str], prompt: str, cwd: str, timeout: int):
        return subprocess.run(
            args,
            input=prompt,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
        )

    @staticmethod
    def _parse_events(stdout: str, fallback_session_id: str | None = None) -> AgentResult:
        session_id = fallback_session_id
        output = ""
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "thread.started":
                session_id = event.get("thread_id") or session_id
            item = event.get("item", {})
            if item.get("type") == "agent_message":
                output = item.get("text", output)
        return AgentResult(output.strip() or stdout.strip(), session_id)

    @staticmethod
    def _failure_message(result) -> str:
        details = (result.stderr or result.stdout or "").strip()
        if len(details) > 1000:
            details = details[:1000] + "..."
        return f"Codex returned code {result.returncode}: {details}"

    def _args(self, session_id: str | None) -> list[str]:
        args = [self.command, *self.flags, "exec"]
        if session_id:
            args.extend(["resume", "--json", session_id, "-"])
        else:
            args.extend(["--json", "-"])
        return args

    def execute(
        self,
        prompt: str,
        timeout: int = 600,
        cwd: str = ".",
        session_id: str | None = None,
    ) -> AgentResult:
        try:
            result = self._run(self._args(session_id), prompt, cwd, timeout)
        except subprocess.TimeoutExpired:
            result = None

        parsed = self._parse_events(result.stdout, session_id) if result else AgentResult("", session_id)
        if result is None or result.returncode != 0:
            if not parsed.session_id:
                if result is not None:
                    raise RuntimeError(self._failure_message(result))
                raise RuntimeError("Codex timed out before creating a resumable session")
            try:
                result = self._run(
                    self._args(parsed.session_id),
                    "Continue the previous task.",
                    cwd,
                    self.retry_timeout,
                )
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError("Codex timed out after retry") from exc

        if result.returncode != 0:
            raise RuntimeError(self._failure_message(result))
        return self._parse_events(result.stdout, parsed.session_id)
