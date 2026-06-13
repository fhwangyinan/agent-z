import re
import os
import subprocess
import sys
import threading
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
    GITHUB_COMMAND_TIMEOUT,
    GITHUB_RETRY_ATTEMPTS,
    GITHUB_RETRY_BASE_DELAY,
    GITHUB_RETRY_MAX_DELAY,
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
    "Scheduler": "S",
    "Analyst":   "A",
    "Developer": "D",
    "Reviewer":  "R",
}

AGENT_COLORS = {
    "Scheduler": "blue",
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


_TRANSIENT_COMMAND_ERRORS = (
    "connection reset",
    "connection refused",
    "connection timed out",
    "could not resolve host",
    "context deadline exceeded",
    "eof",
    "failed to connect",
    "gateway timeout",
    "http 429",
    "http 500",
    "http 502",
    "http 503",
    "http 504",
    "internal server error",
    "network is unreachable",
    "rate limit",
    "remote end hung up",
    "secondary rate limit",
    "service unavailable",
    "stream error",
    "temporary failure",
    "the operation timed out",
    "tls handshake timeout",
    "unexpected eof",
)


def _is_transient_command_failure(result) -> bool:
    details = f"{getattr(result, 'stdout', '')}\n{getattr(result, 'stderr', '')}".lower()
    return any(marker in details for marker in _TRANSIENT_COMMAND_ERRORS)


def wait_with_countdown(label: str, seconds: int, *, style: str = "yellow"):
    if os.environ.get("AGENT_Z_QUIET_LIVE") == "1":
        time.sleep(seconds)
        return
    started = time.monotonic()
    stopped = threading.Event()
    with console.status("", spinner="dots") as status:
        def refresh():
            while not stopped.is_set():
                remaining = max(0, seconds - (time.monotonic() - started))
                status.update(
                    f"[bold {style}]{label}[/bold {style}] "
                    f"[dim]| in:{format_duration(remaining)}[/dim]"
                )
                stopped.wait(0.25)

        thread = threading.Thread(target=refresh, daemon=True)
        thread.start()
        try:
            time.sleep(seconds)
        finally:
            stopped.set()
            thread.join(timeout=1)


def run_cmd(
    cmd,
    cwd=PROJECT_DIR,
    check=True,
    capture_output=True,
    shell=False,
    timeout=None,
    verbose=False,
    retry: bool | None = None,
):
    if isinstance(cmd, str):
        cmd = [cmd]
    retry_enabled = cmd[0] == "gh" if retry is None else retry
    attempts = GITHUB_RETRY_ATTEMPTS if retry_enabled else 1
    command_timeout = timeout
    if retry_enabled and command_timeout is None:
        command_timeout = GITHUB_COMMAND_TIMEOUT

    result = None
    for attempt in range(1, attempts + 1):
        if verbose:
            log(f"[dim]{' '.join(cmd)}[/dim]")
        try:
            result = subprocess.run(
                cmd, cwd=cwd, shell=shell, capture_output=capture_output,
                text=True, encoding="utf-8", errors="replace", check=False,
                timeout=command_timeout,
            )
        except subprocess.TimeoutExpired:
            if attempt >= attempts:
                raise
            result = None
        else:
            if result.returncode == 0 or not _is_transient_command_failure(result):
                break
            if attempt >= attempts:
                break

        delay = min(
            GITHUB_RETRY_MAX_DELAY,
            GITHUB_RETRY_BASE_DELAY * (2 ** (attempt - 1)),
        )
        wait_with_countdown(
            f"Transient command failure; retrying {attempt + 1}/{attempts}",
            delay,
        )

    if verbose and capture_output and result is not None:
        if result.stdout:
            print(result.stdout)
        if result.stderr:
            print(result.stderr, file=sys.stderr)
    if result is None:
        raise RuntimeError(f"cmd failed without a result: {' '.join(cmd)}")
    if check and result.returncode != 0:
        details = _short_output(result.stderr) or _short_output(result.stdout)
        suffix = f"\n{details}" if details else ""
        raise RuntimeError(f"cmd failed: {' '.join(cmd)} (exit {result.returncode}){suffix}")
    return result


@contextmanager
def elapsed_status(label: str, *, style: str = "cyan", details=None):
    if os.environ.get("AGENT_Z_QUIET_LIVE") == "1":
        yield
        return
    started = time.monotonic()
    stopped = threading.Event()
    with console.status("", spinner="dots") as status:
        def refresh():
            while not stopped.is_set():
                extra = details() if callable(details) else details
                suffix = f" | {extra}" if extra else ""
                status.update(
                    f"[bold {style}]{label}[/bold {style}] [dim]| "
                    f"elapsed:{format_duration(time.monotonic() - started)}"
                    f"{suffix}[/dim]"
                )
                stopped.wait(1)

        thread = threading.Thread(target=refresh, daemon=True)
        thread.start()
        try:
            yield
        finally:
            stopped.set()
            thread.join(timeout=1)


@contextmanager
def agent_status(name: str, action: str):
    icon = AGENT_ICONS.get(name, "?")
    color = AGENT_COLORS.get(name, "white")
    with elapsed_status(f"{icon} [{name}] {action}", style=color):
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
        if os.environ.get("AGENT_Z_LOG_AGENT_STATUS") == "1":
            log(
                f"[{self.color}]{self.name}[/{self.color}] starting "
                f"[dim]| {mode} | workspace:{self.cwd}[/dim]"
            )
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
