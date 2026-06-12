import os
import time

from agents.analyst import AnalystAgent
from agents.base import done, error, format_duration, log, warn
from config import (
    MAX_PARALLEL_TASKS,
    PLANNER_IDLE_SLEEP,
    PLANNER_LEASE_SECONDS,
    RECONCILER_INTERVAL,
    STATE_DB,
    WORKER_IDLE_SLEEP,
    WORKER_LEASE_SECONDS,
    WORKTREE_ROOT,
    PROJECT_DIR,
)
from orchestration.github_ops import cleanup_run_artifacts, validate_environment
from orchestration.submission import _find_open_pr_for_branch
from orchestration.store import RunStore
from orchestration.tui import show_banner, show_pool_status
from orchestration.workflow import _build_agents, execute_task, plan_task
from orchestration.worktree import WorktreeManager

def cancel_run(store: RunStore, worktrees: WorktreeManager, run_id: str):
    record = store.cancel(run_id)
    cleanup_run_artifacts(
        record,
        store,
        worktrees,
        remove_worktree=bool(record.worktree_path),
        remove_label=True,
    )
    done(f"Cancelled run {run_id}")

def run_worker(*, max_runs: int = 0, idle_sleep: int = WORKER_IDLE_SLEEP) -> int:
    started = time.monotonic()
    validate_environment()
    show_banner()
    store = RunStore(STATE_DB)
    worktrees = WorktreeManager(PROJECT_DIR, WORKTREE_ROOT)
    claimed = 0
    store.add_event(
        None,
        "worker_started",
        message="Worker started",
        data={"max_runs": max_runs, "idle_sleep": idle_sleep},
    )
    show_pool_status(
        "Worker",
        "READY",
        f"PID {os.getpid()} | max runs: {max_runs or 'unlimited'} | "
        f"idle sleep: {idle_sleep}s | lease: {format_duration(WORKER_LEASE_SECONDS)}",
        style="green",
    )
    while True:
        record = store.claim_ready(MAX_PARALLEL_TASKS, WORKER_LEASE_SECONDS)
        if record is None:
            log(
                f"Worker idle | claimed:{claimed} | "
                f"uptime:{format_duration(time.monotonic() - started)} | "
                f"next scan:{idle_sleep}s"
            )
            store.add_event(
                None,
                "worker_idle",
                message="No ready task available",
                data={"idle_sleep": idle_sleep},
            )
            time.sleep(idle_sleep)
            continue

        claimed += 1
        store.add_event(
            record.run_id,
            "worker_run_started",
            stage=record.stage,
            status=record.status,
            message="Worker started run",
            data={"claimed": claimed},
        )
        analyst, developer, reviewer, submitter = _build_agents()
        try:
            execute_task(record, store, worktrees, analyst, developer, reviewer, submitter)
            store.add_event(
                record.run_id,
                "worker_run_finished",
                message="Worker finished run",
                data={"claimed": claimed},
            )
        except KeyboardInterrupt:
            store.add_event(
                record.run_id,
                "worker_interrupted",
                message="Worker interrupted",
                data={"claimed": claimed},
            )
            raise
        except Exception as exc:
            current = store.get(record.run_id)
            event_type = "worker_run_failed"
            if current.status == "needs_human":
                event_type = "worker_run_needs_human"
                warn(
                    f"Run {record.run_id} needs human attention: {exc}; "
                    "worker will continue with other ready tasks"
                )
            else:
                error(str(exc))
            store.add_event(
                record.run_id,
                event_type,
                stage=current.stage,
                status=current.status,
                message=str(exc),
                data={"claimed": claimed},
            )

        if max_runs and claimed >= max_runs:
            show_pool_status(
                "Worker",
                "STOPPED",
                f"{claimed} claimed run(s) | uptime {format_duration(time.monotonic() - started)}",
                style="yellow",
            )
            store.add_event(
                None,
                "worker_stopped",
                message="Worker reached max_runs",
                data={"claimed": claimed},
            )
            return claimed

def run_planner(*, max_runs: int = 0, idle_sleep: int = PLANNER_IDLE_SLEEP) -> int:
    started = time.monotonic()
    validate_environment()
    show_banner()
    store = RunStore(STATE_DB)
    claimed = 0
    store.add_event(None, "planner_started", data={"max_runs": max_runs})
    show_pool_status(
        "Planner",
        "READY",
        f"PID {os.getpid()} | max runs: {max_runs or 'unlimited'} | "
        f"idle sleep: {idle_sleep}s | lease: {format_duration(PLANNER_LEASE_SECONDS)}",
        style="cyan",
    )
    while True:
        record = store.claim_for_planning(PLANNER_LEASE_SECONDS)
        if record is None:
            log(
                f"Planner idle | planned:{claimed} | "
                f"uptime:{format_duration(time.monotonic() - started)} | "
                f"next scan:{idle_sleep}s"
            )
            time.sleep(idle_sleep)
            continue
        claimed += 1
        try:
            plan_task(record, store, AnalystAgent())
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            error(str(exc))
        if max_runs and claimed >= max_runs:
            store.add_event(None, "planner_stopped", data={"claimed": claimed})
            show_pool_status(
                "Planner",
                "STOPPED",
                f"{claimed} planned run(s) | uptime {format_duration(time.monotonic() - started)}",
                style="yellow",
            )
            return claimed

def run_reconciler(*, once: bool = False, interval: int = RECONCILER_INTERVAL) -> int:
    started = time.monotonic()
    store = RunStore(STATE_DB)
    total = 0
    store.add_event(None, "reconciler_started", data={"once": once, "interval": interval})
    show_pool_status(
        "Reconciler",
        "SCANNING" if once else "READY",
        f"PID {os.getpid()} | interval: {interval}s | mode: {'once' if once else 'continuous'}",
        style="magenta",
    )
    while True:
        reconciled = store.reconcile_expired()
        total += len(reconciled)
        for record in reconciled:
            warn(
                f"Reconciled expired {record.run_id}: "
                f"status={record.status} stage={record.stage}"
            )
        for record in store.list_submission_recovery_candidates():
            pr_url = _find_open_pr_for_branch(record.branch)
            if not pr_url:
                continue
            store.update(
                record.run_id,
                status="ready",
                stage="waiting_checks",
                pr_url=pr_url,
                error=None,
            )
            store.add_event(
                record.run_id,
                "external_pr_adopted",
                stage="waiting_checks",
                status="ready",
                message="Adopted an externally created PR for the stranded branch",
                data={"pr_url": pr_url, "branch": record.branch},
            )
            done(f"Adopted external PR for run {record.run_id}: {pr_url}")
            total += 1
        if once:
            show_pool_status(
                "Reconciler",
                "SCAN COMPLETE",
                f"{total} recovered item(s) | elapsed {format_duration(time.monotonic() - started)}",
                style="magenta",
            )
            return total
        log(
            f"Reconciler heartbeat | recovered total:{total} | "
            f"uptime:{format_duration(time.monotonic() - started)} | "
            f"next scan:{interval}s"
        )
        time.sleep(interval)
