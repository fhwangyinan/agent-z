# Agent 基础类
import json
import os
import re
import shutil
import subprocess
import sys
from contextlib import contextmanager
from datetime import datetime

from rich.console import Console
from rich.markup import escape

from .runners import ClaudeRunner, AgentRunner

console = Console()

# 项目全局配置
PROJECT_DIR = r"G:\Code\workspace_aieng"
GITHUB_REPO = "armpro24-blip/cad-cae-copilot"

# 默认 Runner（切换其他 Agent 只需替换这里）
DEFAULT_RUNNER = ClaudeRunner()

AGENT_ICONS = {
    "Analyst":   "A",
    "Developer": "D",
    "Reviewer":  "R",
    "Submitter": "S",
}

AGENT_COLORS = {
    "Analyst":   "cyan",
    "Developer": "green",
    "Reviewer":  "magenta",
    "Submitter": "yellow",
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
        raise RuntimeError(f"cmd failed: {' '.join(cmd)} rc={result.returncode}")
    return result


@contextmanager
def agent_status(name: str, action: str):
    icon = AGENT_ICONS.get(name, "?")
    color = AGENT_COLORS.get(name, "white")
    with console.status(f"[{color}]{icon} [{name}][/{color}] [dim]{action}...[/dim]", spinner="dots"):
        yield


class Agent:
    def __init__(self, name: str, runner: AgentRunner | None = None):
        self.name = name
        self.color = AGENT_COLORS.get(name, "white")
        self.runner = runner or DEFAULT_RUNNER

    def run(self, prompt: str, timeout: int = 600, continue_session: bool = False) -> str:
        mode = "continue" if continue_session else "new"
        with agent_status(self.name, mode):
            output = self.runner.execute(
                prompt=prompt,
                timeout=timeout,
                cwd=PROJECT_DIR,
                continue_session=continue_session,
            )
        done(f"[{self.color}]{self.name}[/{self.color}] done")
        return output

    def extract(self, text: str, pattern: str, default=None):
        match = re.search(pattern, text)
        return match.group(1) if match else default

    def extract_number(self, text: str, pattern: str, default=None):
        match = re.search(pattern, text)
        return int(match.group(1)) if match else default
