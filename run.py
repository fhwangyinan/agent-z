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

from rich.align import Align
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt, Confirm, IntPrompt
from rich.table import Table

AUTO_MODE = False
TOTAL_LOOPS = 0
CURRENT_LOOP = 0
FORCE_DEVELOP = False
KEEP_WORKTREE = False

from agents.base import log, step, done, warn, error, run_cmd, PROJECT_DIR, GITHUB_REPO
from agents import __version__
from agents.analyst import AnalystAgent
from agents.developer import DeveloperAgent
from agents.reviewer import ReviewerAgent
from agents.submitter import SubmitterAgent
from orchestration import RunRecord, RunStore, WorktreeManager

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
    STATE_DB,
    WORKTREE_ROOT,
    MAX_PARALLEL_TASKS,
    MAX_RUN_SECONDS,
    CLEANUP_COMPLETED_WORKTREES,
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
        "[dim]🔍 Analyst[/dim] → [dim]🔧 Developer[/dim] → [dim]👁 Reviewer[/dim] → [dim]🚀 Submitter[/dim] → [dim]✅ PR Checks[/dim]"
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


def prepare_base_repo():
    step("📦 Refresh base repository")
    run_cmd(["git", "fetch", "origin", "main"], verbose=True)
    done("Base repository refreshed")


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


def _agents(analyst, developer, reviewer, submitter):
    return (analyst, developer, reviewer, submitter)


def _session_snapshot(analyst, developer, reviewer, submitter) -> dict[str, str]:
    return {
        agent.name.lower(): agent.session_id
        for agent in _agents(analyst, developer, reviewer, submitter)
        if agent.session_id
    }


def _restore_sessions(record: RunRecord, analyst, developer, reviewer, submitter):
    for agent in _agents(analyst, developer, reviewer, submitter):
        agent.reset_session()
        agent.session_id = record.sessions.get(agent.name.lower())


def _set_workspace(path: str, analyst, developer, reviewer, submitter):
    for agent in _agents(analyst, developer, reviewer, submitter):
        agent.set_workspace(path)


def _checkpoint(
    store: RunStore,
    record: RunRecord,
    analyst,
    developer,
    reviewer,
    submitter,
    **fields,
) -> RunRecord:
    fields["sessions"] = _session_snapshot(analyst, developer, reviewer, submitter)
    return store.update(record.run_id, **fields)


def _check_run_budget(started: float):
    if time.monotonic() - started >= MAX_RUN_SECONDS:
        raise RuntimeError(f"run exceeded MAX_RUN_SECONDS ({MAX_RUN_SECONDS})")


def _interactive_impact_qa(analyst: AnalystAgent, risk: str) -> bool:
    console.print(
        "\n[dim]Risk: [bold]{0}[/bold] | ask a question | [bold]skip[/bold] issue | "
        "[bold]done[/bold]/Enter to develop[/dim]".format(risk)
    )
    while True:
        question = Prompt.ask("", default="").strip()
        if not question or question.lower() in ("done", "ok", "go", "y"):
            return True
        if question.lower() in ("skip", "s"):
            return False
        answer = analyst.chat(question)
        console.print(Panel(answer[:2000], border_style="dim"))


def _claim_changed_files(store: RunStore, record: RunRecord) -> RunRecord:
    changed = run_cmd(
        ["git", "diff", "--name-only", "origin/main"],
        cwd=record.worktree_path,
        check=False,
    )
    untracked = run_cmd(
        ["git", "ls-files", "--others", "--exclude-standard"],
        cwd=record.worktree_path,
        check=False,
    )
    if changed.returncode != 0 or untracked.returncode != 0:
        raise RuntimeError("could not determine changed files for conflict detection")
    files = [
        line.strip()
        for output in (changed.stdout, untracked.stdout)
        for line in output.splitlines()
        if line.strip()
    ]
    return store.claim_files(record.run_id, files)


def execute_task(
    record: RunRecord,
    store: RunStore,
    worktrees: WorktreeManager,
    analyst: AnalystAgent,
    developer: DeveloperAgent,
    reviewer: ReviewerAgent,
    submitter: SubmitterAgent,
) -> bool:
    started = time.monotonic()
    _restore_sessions(record, analyst, developer, reviewer, submitter)

    if record.worktree_path:
        workspace = worktrees.validate(record.worktree_path)
        _set_workspace(str(workspace), analyst, developer, reviewer, submitter)

    try:
        if record.stage in {"queued", "analyzing"}:
            record = _checkpoint(
                store, record, analyst, developer, reviewer, submitter,
                status="running", stage="analyzing",
            )
            step("🔍 Analyst")
            _, analysis = analyst.analyze(
                target_issue=record.issue_number,
                resume_session=bool(analyst.session_id),
            )
            show_analysis(record.issue_number, analysis)
            record = _checkpoint(
                store, record, analyst, developer, reviewer, submitter,
                stage="created",
            )

        if record.stage in {"created", "assessing"}:
            record = _checkpoint(
                store, record, analyst, developer, reviewer, submitter,
                status="running", stage="assessing",
            )
            step("🔍 Impact assessment")
            impact, risk = analyst.assess_impact(
                record.issue_number,
                resume_session=bool(analyst.session_id),
            )
            console.print(Panel(
                impact[:2500],
                title=f"Impact assessment [yellow]risk: {risk}[/yellow]",
                border_style="yellow",
            ))
            record = _checkpoint(
                store, record, analyst, developer, reviewer, submitter,
                risk=risk, stage="assessed",
            )
        else:
            risk = record.risk or "unknown"

        if AUTO_MODE and not FORCE_DEVELOP and risk in ("high", "very_high"):
            warn(f"Risk is [{risk}]; skipping automatically")
            _checkpoint(
                store, record, analyst, developer, reviewer, submitter,
                status="skipped", stage="skipped",
            )
            return False
        if not AUTO_MODE and record.stage == "assessed":
            if not _interactive_impact_qa(analyst, risk):
                log("Issue skipped by user")
                _checkpoint(
                    store, record, analyst, developer, reviewer, submitter,
                    status="skipped", stage="skipped",
                )
                return False

        _check_run_budget(started)
        if not record.worktree_path:
            prepare_base_repo()
            branch = record.branch or f"agent-z/{record.issue_number}-{record.run_id}"
            workspace = worktrees.create(record.run_id, branch)
            record = _checkpoint(
                store, record, analyst, developer, reviewer, submitter,
                branch=branch, worktree_path=str(workspace), stage="developing",
            )
            _set_workspace(str(workspace), analyst, developer, reviewer, submitter)

        if record.stage in {"assessed", "developing"}:
            record = _checkpoint(
                store, record, analyst, developer, reviewer, submitter,
                status="running", stage="developing",
            )
            step("🔧 Developer")
            developer.fix(record.issue_number, resume_session=bool(developer.session_id))
            record = _checkpoint(
                store, record, analyst, developer, reviewer, submitter,
                stage="reviewing",
            )
            record = _claim_changed_files(store, record)

        _check_run_budget(started)
        if record.stage == "reviewing":
            step("👁 Local Reviewer")
            if not run_local_review(record.issue_number, reviewer, developer):
                _checkpoint(
                    store, record, analyst, developer, reviewer, submitter,
                    status="needs_human", stage="reviewing",
                    error="local review limit reached",
                )
                return False
            record = _claim_changed_files(store, record)
            record = _checkpoint(
                store, record, analyst, developer, reviewer, submitter,
                stage="submitting",
            )

        if record.stage == "submitting":
            step("🚀 Submitter")
            pr_url = submitter.submit(
                record.issue_number,
                resume_session=bool(submitter.session_id),
            )
            if not pr_url:
                raise RuntimeError("Submitter did not create a PR")
            console.print(Panel(
                f"[link={pr_url}]{pr_url}[/link]",
                title="[bold green]🚀 PR created[/bold green]",
                border_style="green",
            ))
            record = _checkpoint(
                store, record, analyst, developer, reviewer, submitter,
                status="waiting_checks", stage="waiting_checks", pr_url=pr_url,
            )

        for review_count in range(MAX_REVIEW_ROUNDS):
            _check_run_budget(started)
            if record.stage not in {"waiting_checks", "handling_feedback"}:
                break
            if record.stage == "waiting_checks":
                if not wait_for_pr_checks(record.pr_url):
                    _checkpoint(
                        store, record, analyst, developer, reviewer, submitter,
                        status="needs_human", stage="waiting_checks",
                        error="PR checks did not reach a final state",
                    )
                    return False
                record = _checkpoint(
                    store, record, analyst, developer, reviewer, submitter,
                    status="running", stage="handling_feedback",
                )

            step(f"🔧 Developer handles PR feedback (round {review_count + 1}/{MAX_REVIEW_ROUNDS})")
            dev_output = developer.apply_review(
                record.issue_number,
                record.pr_url,
                resume_session=True,
            )
            if "NO_ACTION_NEEDED" in dev_output.upper():
                done("Developer reported no action needed")
                break
            if not run_local_review(record.issue_number, reviewer, developer, follow_up=True):
                _checkpoint(
                    store, record, analyst, developer, reviewer, submitter,
                    status="needs_human", stage="handling_feedback",
                    error="local review limit reached after PR feedback",
                )
                return False
            record = _claim_changed_files(store, record)
            done("Local Reviewer approved; pushing")
            developer.push_and_notify(record.pr_url, resume_session=True)
            record = _checkpoint(
                store, record, analyst, developer, reviewer, submitter,
                status="waiting_checks", stage="waiting_checks",
            )
        else:
            _checkpoint(
                store, record, analyst, developer, reviewer, submitter,
                status="needs_human", stage=record.stage,
                error="PR feedback limit reached",
            )
            return False

        record = _checkpoint(
            store, record, analyst, developer, reviewer, submitter,
            status="completed", stage="completed", error=None,
        )
        console.print(Panel(
            f"[bold green]✅ Issue #{record.issue_number} completed[/bold green]\n"
            f"[dim]Run: {record.run_id}\n{record.pr_url}[/dim]",
            border_style="green",
        ))
        if CLEANUP_COMPLETED_WORKTREES and not KEEP_WORKTREE and record.worktree_path:
            try:
                worktrees.remove(record.worktree_path)
                store.update(record.run_id, worktree_path=None)
            except Exception as exc:
                warn(f"Run completed, but worktree cleanup failed: {exc}")
        return True
    except KeyboardInterrupt:
        _checkpoint(
            store, record, analyst, developer, reviewer, submitter,
            status="needs_human", error="interrupted by user",
        )
        raise
    except Exception as exc:
        status = "needs_human" if "file lock conflict" in str(exc) else "failed"
        _checkpoint(
            store, record, analyst, developer, reviewer, submitter,
            status=status, error=str(exc),
        )
        raise


def run_round(
    analyst: AnalystAgent,
    developer: DeveloperAgent,
    reviewer: ReviewerAgent,
    submitter: SubmitterAgent,
    store: RunStore,
    worktrees: WorktreeManager,
    target_issue: int | None = None,
) -> bool:
    for agent in (analyst, developer, reviewer, submitter):
        agent.reset_session()
    target = target_issue if target_issue is not None else choose_issue()
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
    record = store.create(GITHUB_REPO, issue_number, MAX_PARALLEL_TASKS)
    record = store.update(
        record.run_id,
        sessions=_session_snapshot(analyst, developer, reviewer, submitter),
    )
    console.print(f"  [dim]Run ID: {record.run_id}[/dim]")
    return execute_task(
        record, store, worktrees, analyst, developer, reviewer, submitter
    )


def _build_agents():
    return AnalystAgent(), DeveloperAgent(), ReviewerAgent(), SubmitterAgent()


def show_runs(store: RunStore):
    table = Table(title="Agent-Z runs")
    table.add_column("Run ID", style="cyan")
    table.add_column("Issue")
    table.add_column("Status")
    table.add_column("Stage")
    table.add_column("PR")
    table.add_column("Updated")
    for record in store.list():
        table.add_row(
            record.run_id,
            f"#{record.issue_number}",
            record.status,
            record.stage,
            record.pr_url or "",
            record.updated_at.replace("T", " ")[:19],
        )
    console.print(table)


def cancel_run(store: RunStore, worktrees: WorktreeManager, run_id: str):
    record = store.cancel(run_id)
    if record.worktree_path:
        worktrees.remove(record.worktree_path)
        store.update(run_id, worktree_path=None)
    done(f"Cancelled run {run_id}")


def main(target_issue: int | None = None, resume_id: str | None = None):
    validate_environment()
    show_banner()
    store = RunStore(STATE_DB)
    worktrees = WorktreeManager(PROJECT_DIR, WORKTREE_ROOT)
    analyst, developer, reviewer, submitter = _build_agents()

    if resume_id:
        record = store.resume(resume_id, MAX_PARALLEL_TASKS)
        return execute_task(
            record, store, worktrees, analyst, developer, reviewer, submitter
        )

    return run_round(
        analyst, developer, reviewer, submitter,
        store, worktrees, target_issue=target_issue,
    ) if target_issue is not None else _interactive_loop(
        analyst, developer, reviewer, submitter, store, worktrees
    )


def _interactive_loop(analyst, developer, reviewer, submitter, store, worktrees):
    result = False

    while True:
        try:
            result = run_round(
                analyst, developer, reviewer, submitter,
                store, worktrees,
            )

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
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Multi-agent automated issue fixing")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--issue", type=int, metavar="N",
                      help="run one unattended task for issue N")
    mode.add_argument("--enqueue", type=int, metavar="N",
                      help="enqueue issue N without starting it")
    mode.add_argument("--run-next", action="store_true",
                      help="claim and run the oldest queued task")
    mode.add_argument("--resume", metavar="RUN_ID",
                      help="resume an existing run")
    mode.add_argument("--list-runs", action="store_true",
                      help="list recent persisted runs")
    mode.add_argument("--cancel", metavar="RUN_ID",
                      help="cancel a run, release its lock, and remove its worktree")
    parser.add_argument("--loop", type=int, default=0, metavar="N",
                        help="autonomous mode: run N rounds without confirmation")
    parser.add_argument("--force", action="store_true",
                        help="develop high-risk issues too")
    parser.add_argument("--keep-worktree", action="store_true",
                        help="keep a completed run's worktree")
    args = parser.parse_args()
    if args.loop > 0 and any((args.issue, args.enqueue, args.run_next, args.resume, args.list_runs, args.cancel)):
        parser.error("--loop cannot be combined with another run mode")
    if args.force and args.loop <= 0 and not args.issue and not args.resume and not args.run_next:
        parser.error("--force requires --loop, --issue, --resume, or --run-next")

    FORCE_DEVELOP = args.force
    KEEP_WORKTREE = args.keep_worktree
    if args.list_runs:
        show_runs(RunStore(STATE_DB))
        raise SystemExit(0)
    if args.enqueue:
        record = RunStore(STATE_DB).enqueue(GITHUB_REPO, args.enqueue)
        done(f"Enqueued issue #{args.enqueue} as run {record.run_id}")
        raise SystemExit(0)
    if args.cancel:
        cancel_run(
            RunStore(STATE_DB),
            WorktreeManager(PROJECT_DIR, WORKTREE_ROOT),
            args.cancel,
        )
        raise SystemExit(0)
    if args.run_next:
        store = RunStore(STATE_DB)
        record = store.claim_next(MAX_PARALLEL_TASKS)
        if record is None:
            warn("No queued task is available or all parallel slots are occupied")
            raise SystemExit(1)
        AUTO_MODE = True
        main(resume_id=record.run_id)
        raise SystemExit(0)
    if args.resume:
        AUTO_MODE = True
        main(resume_id=args.resume)
        raise SystemExit(0)
    if args.issue:
        AUTO_MODE = True
        TOTAL_LOOPS = 1
        CURRENT_LOOP = 1
        main(target_issue=args.issue)
        raise SystemExit(0)

    if args.loop > 0:
        AUTO_MODE = True
        TOTAL_LOOPS = args.loop
        console.print(f"[bold cyan]Autonomous mode[/bold cyan] [dim]{TOTAL_LOOPS} round(s)[/dim]")
        if FORCE_DEVELOP:
            console.print("[yellow]⚠ Force mode: risk levels are ignored[/yellow]")
        for i in range(TOTAL_LOOPS):
            CURRENT_LOOP = i + 1
            main()
        done(f"Completed all {TOTAL_LOOPS} round(s)")
    else:
        main()
