#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Multi-agent automated issue fixing with a lightweight TUI."""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager

from rich.align import Align
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt, Confirm, IntPrompt
from rich.table import Table

AUTO_MODE = False
TOTAL_LOOPS = 0
CURRENT_LOOP = 0
FORCE_DEVELOP = False

from agents.base import log, step, done, warn, error, run_cmd, PROJECT_DIR, GITHUB_REPO
from agents import __version__
from agents.analyst import AnalystAgent
from agents.developer import DeveloperAgent
from agents.reviewer import ReviewerAgent
from agents.submitter import SubmitterAgent

console = Console()

from config import (
    ANALYST_BACKEND,
    DEVELOPER_BACKEND,
    REVIEWER_BACKEND,
    SUBMITTER_BACKEND,
    PR_CHECKS_INTERVAL,
    PR_CHECKS_MAX_WAIT,
    MAX_REVIEW_ROUNDS,
    MAX_LOCAL_REVIEW_ROUNDS,
)


def show_banner():
    console.print()
    console.print(Align.center(
        Panel.fit(
            f"[bold cyan]Agent-Z v{__version__}[/bold cyan]\n"
            f"[dim]Target: {GITHUB_REPO} | "
            f"A:{ANALYST_BACKEND} D:{DEVELOPER_BACKEND} "
            f"R:{REVIEWER_BACKEND} S:{SUBMITTER_BACKEND}[/dim]",
            border_style="cyan",
            padding=(0, 8),
        )
    ))
    console.print(Align.center(
        "[dim]🔍 Analyst[/dim] → [dim]🔧 Developer[/dim] → [dim]👁 Reviewer[/dim] → [dim]🚀 Submitter[/dim] → [dim]🐰 CodeRabbit[/dim]"
    ))


def validate_environment():
    if not os.path.isdir(PROJECT_DIR):
        raise RuntimeError(f"PROJECT_DIR does not exist: {PROJECT_DIR}")
    if not GITHUB_REPO or "/" not in GITHUB_REPO:
        raise RuntimeError(f"GITHUB_REPO must use owner/repo format, got {GITHUB_REPO!r}")
    selected_backends = {
        ANALYST_BACKEND,
        DEVELOPER_BACKEND,
        REVIEWER_BACKEND,
        SUBMITTER_BACKEND,
    }
    supported_backends = {"claude", "codex", "opencode"}
    unknown = selected_backends - supported_backends
    if unknown:
        raise RuntimeError(f"unsupported agent backend(s): {', '.join(sorted(unknown))}")
    for command in ("git", "gh", *sorted(selected_backends)):
        if not shutil.which(command):
            raise RuntimeError(f"required command not found: {command}")
    result = run_cmd(["git", "rev-parse", "--is-inside-work-tree"], check=False)
    if result.returncode != 0 or result.stdout.strip() != "true":
        raise RuntimeError(f"PROJECT_DIR is not a Git work tree: {PROJECT_DIR}")


def _find_stash_ref(message: str) -> str | None:
    result = run_cmd(["git", "stash", "list", "--format=%gd%x09%gs"], check=False)
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        ref, _, subject = line.partition("\t")
        if message in subject:
            return ref
    return None


@contextmanager
def managed_environment():
    step("📦 Prepare environment")
    original_branch = run_cmd(
        ["git", "symbolic-ref", "--short", "-q", "HEAD"], check=False
    ).stdout.strip()
    original_ref = original_branch or run_cmd(["git", "rev-parse", "HEAD"]).stdout.strip()
    stash_message = f"agent-z-auto-stash-{uuid.uuid4().hex}"
    created_stash = False

    status = run_cmd(["git", "status", "--short"], check=False)
    if status.stdout.strip():
        log("Stashing local changes...")
        result = run_cmd(["git", "stash", "push", "-u", "-m", stash_message], check=False)
        created_stash = result.returncode == 0 and _find_stash_ref(stash_message) is not None
        if not created_stash:
            raise RuntimeError("failed to stash local changes; aborting before checkout")

    try:
        run_cmd(["git", "checkout", "main"])
        run_cmd(["git", "pull", "origin", "main"], verbose=True)
        done("Environment ready")
        yield
    finally:
        step("📦 Restore original workspace")
        checkout = run_cmd(["git", "checkout", original_ref], check=False)
        if checkout.returncode != 0:
            warn("Could not restore the original ref; the automatic stash was preserved")
        elif created_stash:
            stash_ref = _find_stash_ref(stash_message)
            if not stash_ref:
                warn("Could not find the automatic stash; inspect `git stash list`")
            else:
                applied = run_cmd(["git", "stash", "apply", stash_ref], check=False)
                if applied.returncode != 0:
                    warn(f"Restoring local changes caused conflicts; preserved {stash_ref}")
                else:
                    run_cmd(["git", "stash", "drop", stash_ref], check=False)
                    done("Original workspace restored")
        else:
            done("Original workspace restored")


def _get_pr_checks(pr_url: str) -> list[dict] | None:
    result = run_cmd(
        ["gh", "pr", "checks", pr_url, "--json", "name,bucket"],
        check=False,
    )
    if result.returncode != 0:
        message = f"{result.stdout}\n{result.stderr}".lower()
        if "no checks reported" in message:
            return []
        return None
    if not result.stdout.strip():
        return None
    try:
        checks = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    return checks if isinstance(checks, list) else None


def wait_for_pr_checks(pr_url: str) -> bool:
    step("✅ Wait for PR checks")
    deadline = time.monotonic() + PR_CHECKS_MAX_WAIT
    checks = _get_pr_checks(pr_url)
    while checks == [] and time.monotonic() < deadline:
        log("[dim]No checks reported yet; retrying...[/dim]")
        time.sleep(min(PR_CHECKS_INTERVAL, max(0, deadline - time.monotonic())))
        checks = _get_pr_checks(pr_url)

    if checks is None:
        warn("Could not query PR checks")
        return False
    if not checks:
        warn("Timed out before PR checks were reported")
        return False

    remaining = max(1, int(deadline - time.monotonic()))
    try:
        with console.status("[dim]Watching PR checks...[/dim]", spinner="dots"):
            run_cmd(
                [
                    "gh", "pr", "checks", pr_url, "--watch",
                    "--interval", str(PR_CHECKS_INTERVAL),
                ],
                check=False,
                timeout=remaining,
                verbose=True,
            )
    except subprocess.TimeoutExpired:
        warn("Timed out while waiting for PR checks")
        return False

    checks = _get_pr_checks(pr_url)
    if checks is None:
        warn("Could not confirm final PR check status")
        return False
    if not checks or any(check.get("bucket") == "pending" for check in checks):
        warn("PR checks are still pending")
        return False

    failed = [check.get("name", "(unnamed)") for check in checks if check.get("bucket") == "fail"]
    if failed:
        warn(f"PR checks completed with failures: {', '.join(failed)}")
    else:
        done("All PR checks completed")
    return True


def _get_issue_title(issue_number: int) -> str:
    try:
        result = run_cmd(
            ["gh", "issue", "view", str(issue_number), "--json", "title,labels,state"],
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            data = json.loads(result.stdout)
            title = data.get("title", "(untitled)")
            labels = "  ".join(f"[{l['name']}]" for l in data.get("labels", []))
            return f"{title}\n[dim]{labels}[/dim]" if labels else title
    except Exception:
        pass
    return "(unable to fetch title)"


def choose_issue() -> int | None:
    console.print()
    if AUTO_MODE:
        loop_info = f" (round {CURRENT_LOOP}/{TOTAL_LOOPS})" if TOTAL_LOOPS > 1 else ""
        console.print(Panel(
            f"[bold]Autonomous mode: Agent selects an issue{loop_info}[/bold]",
            border_style="cyan",
        ))
        return None

    table = Table(show_header=False, box=None, padding=(0, 4))
    table.add_column(style="cyan")
    table.add_column()
    table.add_row("[bold][1][/bold]", "Let the Agent recommend the highest-priority issue")
    table.add_row("[bold][2][/bold]", "Enter an issue number")
    console.print(Panel(table, title="[bold]Select mode[/bold]", border_style="cyan"))
    choice = Prompt.ask("", choices=["1", "2"], default="1")
    if choice == "1":
        return None
    else:
        return IntPrompt.ask("Issue number")


def confirm_issue(issue_number: int) -> int | None:
    title = _get_issue_title(issue_number)
    console.print(Panel(
        title,
        title=f"[bold green]#{issue_number}[/bold green]",
        border_style="green",
        subtitle="Confirm? (y/n or another issue number)",
        subtitle_align="left",
    ))

    if AUTO_MODE:
        return issue_number

    while True:
        user_input = Prompt.ask("", default="y").strip()
        if user_input.lower() in ("y", "yes", ""):
            return issue_number
        elif user_input.lower() in ("n", "no"):
            return None
        elif user_input.isdigit():
            new_num = int(user_input)
            title = _get_issue_title(new_num)
            console.print(f"  → Switched to [bold]#{new_num}[/bold]")
            console.print(Panel(title, title=f"[bold green]#{new_num}[/bold green]", border_style="green"))
            return new_num
        else:
            warn("Invalid input")


def show_analysis(issue_number: int, analysis: str):
    """Display the analysis in a panel."""
    display = analysis[:2000] + ("..." if len(analysis) > 2000 else "")
    console.print(Panel(display, title=f"[bold cyan]🔍 Analyst → Issue #{issue_number}[/bold cyan]", border_style="blue"))


def run_local_review(
    issue_number: int,
    reviewer: ReviewerAgent,
    developer: DeveloperAgent,
    *,
    follow_up: bool = False,
) -> bool:
    for local_round in range(MAX_LOCAL_REVIEW_ROUNDS):
        if follow_up:
            step(f"👁 Local Reviewer follow-up (round {local_round + 1})")
        review_comments = reviewer.review(issue_number, resume_session=True)
        if not review_comments:
            done("Reviewer approved (LGTM)")
            return True

        warn(f"Reviewer found [bold]{len(review_comments)}[/bold] issue(s) (round {local_round + 1})")
        for i, comment in enumerate(review_comments, 1):
            console.print(f"    [yellow]{i}.[/yellow] {comment[:300]}")
        developer.apply_review(
            issue_number,
            "",
            review_comments=review_comments,
            resume_session=True,
        )

    warn(f"Reached the local review limit ({MAX_LOCAL_REVIEW_ROUNDS}); stopping this run")
    return False


def run_round(
    analyst: AnalystAgent,
    developer: DeveloperAgent,
    reviewer: ReviewerAgent,
    submitter: SubmitterAgent,
) -> bool:
    for agent in (analyst, developer, reviewer, submitter):
        agent.reset_session()

    with managed_environment():
        target = choose_issue()

        step("🔍 Analyst")
        if target:
            issue_number, analysis = analyst.analyze(target_issue=target, resume_session=False)
        else:
            issue_number, analysis = analyst.analyze(resume_session=False)

        if issue_number is None:
            error("Analyst did not recommend an issue")
            return False

        show_analysis(issue_number, analysis)
        issue_number = confirm_issue(issue_number)
        if issue_number is None:
            log("Skipped; moving to the next round")
            return False

        step("🔍 Impact assessment")
        impact, risk = analyst.assess_impact(issue_number, resume_session=True)
        console.print(Panel(impact[:2500], title=f"Impact assessment [yellow]risk: {risk}[/yellow]", border_style="yellow"))

        if AUTO_MODE and not FORCE_DEVELOP:
            if risk in ("high", "very_high"):
                warn(f"Risk is [{risk}]; skipping automatically")
                return False
        else:
            console.print("\n[dim]Risk: [bold]{0}[/bold] | ask a question | [bold]skip[/bold] issue | [bold]done[/bold]/Enter to develop[/dim]".format(risk))
            while True:
                question = Prompt.ask("", default="").strip()
                if not question:
                    break
                if question.lower() in ("skip", "s"):
                    log("Issue skipped by user")
                    return False
                if question.lower() in ("done", "ok", "go", "y"):
                    break
                answer = analyst.chat(question)
                console.print(Panel(answer[:2000], border_style="dim"))

        step("🔧 Developer")
        developer.fix(issue_number, resume_session=False)

        step("👁 Local Reviewer")
        if not run_local_review(issue_number, reviewer, developer):
            return False

        step("🚀 Submitter")
        pr_url = submitter.submit(issue_number, resume_session=False)
        if not pr_url:
            error("Submitter did not create a PR")
            return False
        console.print(Panel(
            f"[link={pr_url}]{pr_url}[/link]",
            title="[bold green]🚀 PR created[/bold green]",
            border_style="green",
        ))

        for review_count in range(MAX_REVIEW_ROUNDS):
            if not wait_for_pr_checks(pr_url):
                return False

            step(f"🔧 Developer handles PR feedback (round {review_count + 1}/{MAX_REVIEW_ROUNDS})")
            dev_output = developer.apply_review(issue_number, pr_url, resume_session=True)
            if "NO_ACTION_NEEDED" in dev_output.upper():
                done("Developer reported no action needed")
                break

            if not run_local_review(issue_number, reviewer, developer, follow_up=True):
                return False
            done("Local Reviewer approved; pushing")
            developer.push_and_notify(pr_url, resume_session=True)
        else:
            warn(f"Reached the PR feedback limit ({MAX_REVIEW_ROUNDS}); stopping this run")
            return False

        console.print(Panel(
            f"[bold green]✅ Issue #{issue_number} completed[/bold green]\n[dim]{pr_url}[/dim]",
            border_style="green",
        ))
        return True


def main():
    validate_environment()
    show_banner()
    analyst = AnalystAgent()
    developer = DeveloperAgent()
    reviewer = ReviewerAgent()
    submitter = SubmitterAgent()

    while True:
        try:
            run_round(analyst, developer, reviewer, submitter)

        except KeyboardInterrupt:
            console.print("\n[yellow]Interrupted by user[/yellow]")
            break
        except Exception as e:
            error(str(e))
            import traceback
            traceback.print_exc()

        console.print()
        if AUTO_MODE:
            break
        if not Confirm.ask("[bold]Start another round?[/bold]", default=True):
            done("Exiting")
            break


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Multi-agent automated issue fixing")
    parser.add_argument("--loop", type=int, default=0, metavar="N",
                        help="autonomous mode: run N rounds without confirmation")
    parser.add_argument("--force", action="store_true",
                        help="develop high-risk issues too (requires --loop)")
    args = parser.parse_args()
    if args.force and args.loop <= 0:
        parser.error("--force requires --loop N")

    if args.loop > 0:
        AUTO_MODE = True
        TOTAL_LOOPS = args.loop
        FORCE_DEVELOP = args.force
        console.print(f"[bold cyan]Autonomous mode[/bold cyan] [dim]{TOTAL_LOOPS} round(s)[/dim]")
        if FORCE_DEVELOP:
            console.print("[yellow]⚠ Force mode: risk levels are ignored[/yellow]")
        for i in range(TOTAL_LOOPS):
            CURRENT_LOOP = i + 1
            main()
        done(f"Completed all {TOTAL_LOOPS} round(s)")
    else:
        main()
