# -*- coding: utf-8 -*-
"""Agent-Z command-line entry point.

Implementation lives in focused orchestration modules. Selected names are
re-exported here for backward compatibility with existing integrations.
"""

import argparse
import os
import shutil
import sys
import time
from datetime import datetime

from rich.prompt import Confirm, Prompt

from agents.analyst import AnalystAgent
from agents.base import done, error, log, run_cmd, step, warn
from config import (
    GITHUB_REPO,
    MAX_PARALLEL_TASKS,
    PLANNER_IDLE_SLEEP,
    PLANNER_LEASE_SECONDS,
    PROJECT_DIR,
    RECONCILER_INTERVAL,
    SCHEDULER_IDLE_SLEEP,
    SERVICE_WORKERS,
    STATE_DB,
    TASK_LEAD_BACKEND,
    REVIEWER_BACKEND,
    WORKER_IDLE_SLEEP,
    WORKER_LEASE_SECONDS,
    WORKTREE_ROOT,
)
from orchestration.errors import NeedsHumanError
from orchestration.github_ops import (
    _base_sha,
    _ensure_label_exists,
    _get_issue_labels,
    _get_issue_snapshot,
    _get_pr_checks,
    _get_related_open_prs,
    _label_exists,
    _remove_run_claim_label,
    cleanup_run_artifacts,
    mark_issue_with_skip_label,
    preflight_worker,
    prepare_base_repo,
    validate_environment,
    wait_for_pr_checks,
)
from orchestration.pools import (
    cancel_run,
    run_planner,
    run_reconciler,
    run_scheduler,
    run_worker,
)
from orchestration.runtime import runtime
from orchestration.service import run_service
from orchestration.submission import (
    _create_pr_deterministically,
    _find_open_pr_for_branch,
    _issue_title_for_pr,
    _normalize_submission_metadata,
    _prepare_submission_metadata,
    _verify_pr_url,
    resolve_submission,
)
from orchestration.store import RunStore
from orchestration.tui import (
    _parse_iso,
    _record_age,
    _run_context_line,
    confirm_issue,
    console,
    show_analysis,
    show_banner,
    show_run_detail,
    show_runs,
)
from orchestration.workflow import (
    CoordinatorAgentState,
    _build_agents,
    _interactive_impact_qa,
    _restore_sessions,
    _session_snapshot,
    execute_task,
    plan_task,
    run_local_review,
    run_round,
)
from orchestration.worktree import WorktreeManager


def main(target_issue: int | None = None, resume_id: str | None = None):
    validate_environment()
    show_banner()
    store = RunStore(STATE_DB)
    worktrees = WorktreeManager(PROJECT_DIR, WORKTREE_ROOT)
    analyst, developer, reviewer, coordinator = _build_agents()

    if resume_id:
        record = store.resume(resume_id, MAX_PARALLEL_TASKS)
        return execute_task(
            record, store, worktrees, analyst, developer, reviewer, coordinator
        )

    if target_issue is not None:
        return run_round(
            analyst,
            developer,
            reviewer,
            coordinator,
            store,
            worktrees,
            target_issue=target_issue,
        )
    return _interactive_loop(
        analyst, developer, reviewer, coordinator, store, worktrees
    )


def _interactive_loop(analyst, developer, reviewer, coordinator, store, worktrees):
    result = False
    while True:
        try:
            result = run_round(
                analyst, developer, reviewer, coordinator, store, worktrees
            )
        except KeyboardInterrupt:
            console.print("\n[yellow]Interrupted by user[/yellow]")
            break
        except Exception as exc:
            error(str(exc))
            import traceback

            traceback.print_exc()

        console.print()
        if runtime.auto_mode:
            break
        if not Confirm.ask("[bold]Start another round?[/bold]", default=True):
            done("Exiting")
            break
    return result


def build_parser(*, show_advanced: bool = False) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Multi-agent automated issue fixing")
    advanced_help = (lambda text: text) if show_advanced else (lambda text: argparse.SUPPRESS)
    parser.add_argument(
        "--help-all",
        action="store_true",
        help="show advanced pool and tuning options",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--serve", action="store_true", help="start the complete autonomous service")
    mode.add_argument("--issue", type=int, metavar="N", help="run one unattended task for issue N")
    mode.add_argument("--enqueue", type=int, metavar="N", help="enqueue issue N without starting it")
    mode.add_argument("--run-next", action="store_true", help=advanced_help("claim and run the oldest planned, ready task"))
    mode.add_argument("--plan-next", action="store_true", help=advanced_help("claim and plan the oldest queued issue"))
    mode.add_argument("--resume", metavar="RUN_ID", help="resume an existing run")
    mode.add_argument("--list-runs", action="store_true", help="list recent persisted runs")
    mode.add_argument("--inspect", metavar="RUN_ID", help="show a run and its structured event log")
    mode.add_argument("--cancel", metavar="RUN_ID", help="cancel a run and remove its worktree")
    mode.add_argument("--worker", action="store_true", help=advanced_help("continuously claim planned, ready tasks"))
    mode.add_argument("--planner", action="store_true", help=advanced_help("continuously analyze queued issues"))
    mode.add_argument("--scheduler", action="store_true", help=advanced_help("continuously discover and enqueue eligible issues"))
    mode.add_argument("--schedule-once", action="store_true", help=advanced_help("discover and enqueue eligible issues once"))
    mode.add_argument("--reconciler", action="store_true", help=advanced_help("continuously recover expired leases"))
    mode.add_argument("--reconcile-once", action="store_true", help=advanced_help("recover expired leases once and exit"))
    parser.add_argument("--loop", type=int, default=0, metavar="N", help=advanced_help("run N autonomous rounds"))
    parser.add_argument("--force", action="store_true", help="develop high-risk issues too")
    parser.add_argument("--keep-worktree", action="store_true", help="keep completed worktrees")
    parser.add_argument("--workers", type=int, default=SERVICE_WORKERS, metavar="N", help="worker processes for --serve")
    parser.add_argument("--worker-max-runs", type=int, default=0, metavar="N", help=advanced_help("stop a worker after N claimed runs"))
    parser.add_argument("--worker-idle-sleep", type=int, default=WORKER_IDLE_SLEEP, metavar="SECONDS", help=advanced_help("worker idle polling interval"))
    parser.add_argument("--planner-max-runs", type=int, default=0, metavar="N", help=advanced_help("stop a planner after N planned runs"))
    parser.add_argument("--planner-idle-sleep", type=int, default=PLANNER_IDLE_SLEEP, metavar="SECONDS", help=advanced_help("planner idle polling interval"))
    parser.add_argument("--scheduler-interval", type=int, default=SCHEDULER_IDLE_SLEEP, metavar="SECONDS", help=advanced_help("scheduler scan interval"))
    parser.add_argument("--reconciler-interval", type=int, default=RECONCILER_INTERVAL, metavar="SECONDS", help=advanced_help("reconciler scan interval"))
    return parser


def _validate_args(parser: argparse.ArgumentParser, args):
    if args.loop > 0 and any((
        args.issue, args.enqueue, args.run_next, args.plan_next, args.resume,
        args.list_runs, args.inspect, args.cancel, args.worker, args.planner,
        args.scheduler, args.schedule_once, args.reconciler, args.reconcile_once,
        args.serve,
    )):
        parser.error("--loop cannot be combined with another run mode")
    if (
        args.force
        and args.loop <= 0
        and not args.issue
        and not args.resume
        and not args.run_next
        and not args.worker
        and not args.serve
    ):
        parser.error("--force requires --serve, --worker, --loop, --issue, --resume, or --run-next")
    if args.workers <= 0:
        parser.error("--workers must be positive")
    if args.worker_max_runs < 0 or args.planner_max_runs < 0:
        parser.error("worker/planner max runs must be zero or greater")
    if (
        args.worker_idle_sleep <= 0
        or args.planner_idle_sleep <= 0
        or args.scheduler_interval <= 0
        or args.reconciler_interval <= 0
    ):
        parser.error("worker, planner, scheduler, and reconciler intervals must be positive")
    if (args.worker_max_runs or args.worker_idle_sleep != WORKER_IDLE_SLEEP) and not args.worker:
        parser.error("--worker-max-runs and --worker-idle-sleep require --worker")
    if (args.planner_max_runs or args.planner_idle_sleep != PLANNER_IDLE_SLEEP) and not args.planner:
        parser.error("--planner-max-runs and --planner-idle-sleep require --planner")
    if args.scheduler_interval != SCHEDULER_IDLE_SLEEP and not args.scheduler:
        parser.error("--scheduler-interval requires --scheduler")
    if args.workers != SERVICE_WORKERS and not args.serve:
        parser.error("--workers requires --serve")


def cli(argv: list[str] | None = None):
    argv = sys.argv[1:] if argv is None else argv
    if "--help-all" in argv:
        build_parser(show_advanced=True).print_help()
        return 0
    parser = build_parser()
    args = parser.parse_args(argv)
    _validate_args(parser, args)
    runtime.force_develop = args.force
    runtime.keep_worktree = args.keep_worktree

    if args.serve:
        runtime.auto_mode = True
        return run_service(
            workers=args.workers,
            force=args.force,
            keep_worktree=args.keep_worktree,
        )
    elif args.list_runs:
        show_runs(RunStore(STATE_DB))
    elif args.inspect:
        show_run_detail(RunStore(STATE_DB), args.inspect)
    elif args.enqueue:
        record = RunStore(STATE_DB).enqueue(GITHUB_REPO, args.enqueue)
        done(f"Enqueued issue #{args.enqueue} as run {record.run_id}")
    elif args.cancel:
        cancel_run(RunStore(STATE_DB), WorktreeManager(PROJECT_DIR, WORKTREE_ROOT), args.cancel)
    elif args.run_next:
        store = RunStore(STATE_DB)
        record = store.claim_ready(MAX_PARALLEL_TASKS, WORKER_LEASE_SECONDS)
        if record is None:
            warn("No ready task is available or all development slots are occupied")
            return 1
        runtime.auto_mode = True
        main(resume_id=record.run_id)
    elif args.plan_next:
        store = RunStore(STATE_DB)
        record = store.claim_for_planning(PLANNER_LEASE_SECONDS)
        if record is None:
            warn("No queued issue is awaiting planning")
            return 1
        plan_task(record, store, AnalystAgent())
    elif args.worker:
        runtime.auto_mode = True
        run_worker(max_runs=args.worker_max_runs, idle_sleep=args.worker_idle_sleep)
    elif args.planner:
        runtime.auto_mode = True
        run_planner(max_runs=args.planner_max_runs, idle_sleep=args.planner_idle_sleep)
    elif args.scheduler or args.schedule_once:
        run_scheduler(once=args.schedule_once, interval=args.scheduler_interval)
    elif args.reconciler or args.reconcile_once:
        run_reconciler(once=args.reconcile_once, interval=args.reconciler_interval)
    elif args.resume:
        runtime.auto_mode = True
        main(resume_id=args.resume)
    elif args.issue:
        runtime.auto_mode = True
        runtime.total_loops = 1
        runtime.current_loop = 1
        main(target_issue=args.issue)
    elif args.loop > 0:
        runtime.auto_mode = True
        runtime.total_loops = args.loop
        console.print(f"[bold cyan]Autonomous mode[/bold cyan] [dim]{args.loop} round(s)[/dim]")
        if runtime.force_develop:
            console.print("[yellow]! Force mode: risk levels are ignored[/yellow]")
        for index in range(args.loop):
            runtime.current_loop = index + 1
            main()
        done(f"Completed all {args.loop} round(s)")
    else:
        main()
    return 0


if __name__ == "__main__":
    raise SystemExit(cli())
