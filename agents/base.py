import re
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime

from rich.console import Console
from rich.markup import escape

from .runners import AgentRunner, ClaudeRunner, CodexRunner, OpenCodeRunner
from config import (
    PROJECT_DIR,
    GITHUB_REPO,
    CLAUDE_FLAGS,
    CODEX_FLAGS,
    OPENCODE_FLAGS,
    RETRY_TIMEOUT,
)

console = Console()

def create_runner(name: str) -> AgentRunner:
    runners = {
        "claude": lambda: ClaudeRunner(flags=CLAUDE_FLAGS, retry_timeout=RETRY_TIMEOUT),
        "codex": lambda: CodexRunner(flags=CODEX_FLAGS, retry_timeout=RETRY_TIMEOUT),
        "opencode": lambda: OpenCodeRunner(flags=OPENCODE_FLAGS, retry_timeout=RETRY_TIMEOUT),
    }
    try:
        return runners[name.lower()]()
    except KeyError as exc:
        supported = ", ".join(sorted(runners))
        raise ValueError(f"Unknown backend {name!r}. Supported backends: {supported}") from exc

AGENT_ICONS = {
    "Analyst":   "A",
    "Developer": "D",
    "Reviewer":  "R",
}

AGENT_COLORS = {
    "Analyst":   "cyan",
    "Developer": "green",
    "Reviewer":  "magenta",
}


def log(msg: str):
    now = datetime.now().strftime("%H:%M:%S")
    console.print(f"  [dim]{now}[/dim] {msg}")


def step(msg: str):
    console.print()
    console.rule(f"[bold white]{msg}[/bold white]", style="dim")


def done(msg: str):
    console.print(f"  [green]+[/green] {msg}")


def warn(msg: str):
    console.print(f"  [yellow]![/yellow] {msg}")


def error(msg: str):
    console.print(f"  [red]x[/red] {escape(msg)}")


def format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {seconds:02d}s"
    if minutes:
        return f"{minutes}m {seconds:02d}s"
    return f"{seconds}s"


def _short_output(text: str | None, limit: int = 1000) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit] + "..."


def run_cmd(cmd, cwd=PROJECT_DIR, check=True, capture_output=True, shell=False, timeout=None, verbose=False):
    if isinstance(cmd, str):
        cmd = [cmd]
    if verbose:
        log(f"[dim]{' '.join(cmd)}[/dim]")
    result = subprocess.run(
        cmd, cwd=cwd, shell=shell, capture_output=capture_output,
        text=True, encoding="utf-8", check=False, timeout=timeout,
    )
    if verbose and capture_output:
        if result.stdout:
            print(result.stdout)
        if result.stderr:
            print(result.stderr, file=sys.stderr)
    if check and result.returncode != 0:
        details = _short_output(result.stderr) or _short_output(result.stdout)
        suffix = f"\n{details}" if details else ""
        raise RuntimeError(f"cmd failed: {' '.join(cmd)} (exit {result.returncode}){suffix}")
    return result


@contextmanager
def agent_status(name: str, action: str):
    icon = AGENT_ICONS.get(name, "?")
    color = AGENT_COLORS.get(name, "white")
    with console.status(f"[{color}]{icon} [{name}][/{color}] [dim]{action}...[/dim]", spinner="dots"):
        yield


class Agent:
    def __init__(self, name: str, backend: str, runner: AgentRunner | None = None):
        self.name = name
        self.color = AGENT_COLORS.get(name, "white")
        self.runner = runner or create_runner(backend)
        self.session_id: str | None = None
        self.cwd = PROJECT_DIR

    def reset_session(self):
        self.session_id = None

    def set_workspace(self, cwd: str):
        self.cwd = cwd

    def run(self, prompt: str, timeout: int = 600, resume_session: bool = False) -> str:
        session_id = self.session_id if resume_session else None
        mode = f"{self.runner.name}:{'resume' if session_id else 'new'}"
        started = time.monotonic()
        with agent_status(self.name, mode):
            result = self.runner.execute(
                prompt=prompt,
                timeout=timeout,
                cwd=self.cwd,
                session_id=session_id,
            )
        self.session_id = result.session_id
        done(
            f"[{self.color}]{self.name}[/{self.color}] done "
            f"[dim]in {format_duration(time.monotonic() - started)} | {mode}[/dim]"
        )
        return result.output

    def extract(self, text: str, pattern: str, default=None):
        match = re.search(pattern, text)
        return match.group(1) if match else default

    def extract_number(self, text: str, pattern: str, default=None):
        match = re.search(pattern, text)
        return int(match.group(1)) if match else default
