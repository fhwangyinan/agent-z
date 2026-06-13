import os
import threading
import time
from contextlib import contextmanager

from agents.analyst import AnalystAgent
from agents.base import done, error, format_duration, log, warn
from agents.scheduler import SchedulerAgent
from config import (
    MAX_PARALLEL_TASKS,
    PLANNER_IDLE_SLEEP,
    PLANNER_LEASE_SECONDS,
    PLANNER_MAX_RETRIES,
    PLANNER_RETRY_BASE_DELAY,
    RECONCILER_INTERVAL,
    SCHEDULER_IDLE_SLEEP,
    STATE_DB,
    SUBMISSION_NO_CHANGES_MAX_RETRIES,
    WORKER_IDLE_SLEEP,
    WORKER_LEASE_SECONDS,
    WORKER_PREFLIGHT_MAX_RETRIES,
    WORKTREE_ROOT,
    PROJECT_DIR,
)
from orchestration.github_ops import cleanup_run_artifacts, validate_environment
from orchestration.scheduler import schedule_once
from orchestration.submission import (
    _find_open_pr_for_branch,
    _get_pr_snapshot,
    branch_has_commits,
)
from orchestration.store import RunStore
from orchestration.tui import show_banner, show_pool_status, wait_with_status
from orchestration.workflow import _build_agents, execute_task, plan_task
from orchestration.worktree import WorktreeManager

TRANSIENT_ERROR_MARKERS = (
    "timeout",
    "timed out",
    "connection",
    "network",
    "unreachable",
    "refused",
    "reset",
    "broken pipe",
    "temporary failure",
    "temporarily unavailable",
    "rate limit",
    "too many requests",
    "eof",
)


def _is_transient_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return any(marker in message for marker in TRANSIENT_ERROR_MARKERS)


@contextmanager
def maintain_lease(
    store: RunStore,
    run_id: str,
    role: str,
    lease_seconds: int,
    *,
    interval: float | None = None,
):
    stop = threading.Event()
    heartbeat_errors: list[RuntimeError] = []
    refresh_interval = interval or max(1.0, min(30.0, lease_seconds / 3))

    def refresh():
        while not stop.wait(refresh_interval):
            try:
                store.heartbeat(run_id, role, lease_seconds)
            except RuntimeError as exc:
                heartbeat_errors.append(exc)
                stop.set()
                return
            except Exception as exc:
                store.add_event(
                    run_id,
                    "lease_heartbeat_failed",
                    message=str(exc),
                    data={"role": role, "lease_seconds": lease_seconds},
                )

    thread = threading.Thread(
        target=refresh,
        name=f"{role}-lease-{run_id}",
        daemon=True,
    )
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=max(1.0, refresh_interval + 1))
    if heartbeat_errors:
        raise heartbeat_errors[0]


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
            store.add_event(
                None,
                "worker_idle",
                message="No ready task available",
                data={"idle_sleep": idle_sleep},
            )
            wait_with_status(
                "Worker",
                lambda: (
                    f"claimed:{claimed} | "
                    f"uptime:{format_duration(time.monotonic() - started)}"
                ),
                idle_sleep,
                style="green",
            )
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
            with maintain_lease(
                store, record.run_id, "worker", WORKER_LEASE_SECONDS
            ):
                execute_task(
                    record, store, worktrees, analyst, developer, reviewer, submitter
                )
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
            error_msg = str(exc)
            is_network_err = _is_transient_error(exc)
            if current.stage == "ready" and "file lock conflict" in error_msg.lower():
                store.update(
                    record.run_id,
                    status="ready",
                    stage="ready",
                    error=error_msg,
                    owner_pid=None,
                    lease_role=None,
                    lease_expires_at=None,
                )
                store.add_event(
                    record.run_id,
                    "worker_resource_deferred",
                    stage="ready",
                    status="ready",
                    message=error_msg,
                    data={"claimed": claimed},
                )
                warn(
                    f"Run {record.run_id} conflicts with active resources; "
                    "moved behind other ready work"
                )
                wait_with_status(
                    "Worker",
                    f"resource conflict backoff | run:{record.run_id}",
                    idle_sleep,
                    style="yellow",
                )
            elif current.stage == "ready":
                retry_count = store.count_events(
                    record.run_id, "worker_preflight_retry"
                ) + 1
                if retry_count >= WORKER_PREFLIGHT_MAX_RETRIES:
                    store.update(
                        record.run_id,
                        status="needs_human",
                        stage="ready",
                        error=error_msg,
                    )
                    store.add_event(
                        record.run_id,
                        "worker_preflight_exhausted",
                        stage="ready",
                        status="needs_human",
                        message=error_msg,
                        data={
                            "attempt": retry_count,
                            "max_retries": WORKER_PREFLIGHT_MAX_RETRIES,
                        },
                    )
                    warn(
                        f"Run {record.run_id} preflight failed {retry_count} times; "
                        "marked needs_human"
                    )
                else:
                    # Release the lease and retry after a bounded backoff.
                    store.update(
                        record.run_id,
                        status="ready",
                        stage="ready",
                        error=error_msg,
                        owner_pid=None,
                        lease_role=None,
                        lease_expires_at=None,
                    )
                    store.add_event(
                        record.run_id,
                        "worker_preflight_retry",
                        stage="ready",
                        status="ready",
                        message=error_msg,
                        data={
                            "attempt": retry_count,
                            "max_retries": WORKER_PREFLIGHT_MAX_RETRIES,
                            "network_error": is_network_err,
                        },
                    )
                    warn(
                        f"Run {record.run_id} preflight failed, retry "
                        f"{retry_count}/{WORKER_PREFLIGHT_MAX_RETRIES}: {exc}"
                    )
                    wait_with_status(
                        "Worker",
                        f"preflight retry backoff | run:{record.run_id}",
                        idle_sleep,
                        style="yellow",
                    )
            elif current.status == "needs_human":
                event_type = "worker_run_needs_human"
                warn(
                    f"Run {record.run_id} needs human attention: {exc}; "
                    "worker will continue with other ready tasks"
                )
            else:
                error(str(exc))
            if current.stage != "ready":
                store.add_event(
                    record.run_id,
                    event_type,
                    stage=current.stage,
                    status=current.status,
                    message=error_msg,
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
            wait_with_status(
                "Planner",
                lambda: (
                    f"planned:{claimed} | "
                    f"uptime:{format_duration(time.monotonic() - started)}"
                ),
                idle_sleep,
                style="cyan",
            )
            continue
        claimed += 1
        try:
            with maintain_lease(
                store, record.run_id, "planner", PLANNER_LEASE_SECONDS
            ):
                plan_task(record, store, AnalystAgent(), fail_on_error=False)
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            retry_count = store.count_events(record.run_id, "planner_retry") + 1
            if _is_transient_error(exc) and retry_count < PLANNER_MAX_RETRIES:
                delay = PLANNER_RETRY_BASE_DELAY * (2 ** (retry_count - 1))
                store.update(
                    record.run_id,
                    status="queued",
                    stage="queued",
                    error=str(exc),
                    owner_pid=None,
                    lease_role=None,
                    lease_expires_at=None,
                )
                store.add_event(
                    record.run_id,
                    "planner_retry",
                    stage="queued",
                    status="queued",
                    message=str(exc),
                    data={
                        "attempt": retry_count,
                        "max_retries": PLANNER_MAX_RETRIES,
                        "delay": delay,
                    },
                )
                warn(
                    f"Planning run {record.run_id} hit a transient failure; "
                    f"retry {retry_count}/{PLANNER_MAX_RETRIES} in {delay}s"
                )
                wait_with_status(
                    "Planner",
                    f"retry backoff | run:{record.run_id}",
                    delay,
                    style="yellow",
                )
            else:
                store.update(
                    record.run_id,
                    status="failed",
                    stage="analyzing",
                    error=str(exc),
                )
                store.add_event(
                    record.run_id,
                    "planner_failed",
                    stage="analyzing",
                    status="failed",
                    message=str(exc),
                    data={
                        "transient": _is_transient_error(exc),
                        "attempt": retry_count,
                        "max_retries": PLANNER_MAX_RETRIES,
                    },
                )
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

def run_scheduler(*, once: bool = False, interval: int = SCHEDULER_IDLE_SLEEP) -> int:
    started = time.monotonic()
    validate_environment()
    show_banner()
    store = RunStore(STATE_DB)
    scheduler_agent = SchedulerAgent()
    total = 0
    store.add_event(None, "scheduler_started", data={"once": once, "interval": interval})
    show_pool_status(
        "Scheduler",
        "SCANNING" if once else "READY",
        f"PID {os.getpid()} | interval: {interval}s | mode: {'once' if once else 'continuous'}",
        style="blue",
    )
    while True:
        scan_started = time.monotonic()
        store.add_event(
            None,
            "scheduler_scan_started",
            message="Scheduler scan started",
            data={"pid": os.getpid(), "interval": interval},
        )
        log("Scheduler scan started")
        try:
            records = schedule_once(store, scheduler_agent=scheduler_agent)
        except Exception as exc:
            store.add_event(
                None,
                "scheduler_scan_failed",
                message=str(exc),
                data={"interval": interval},
            )
            if once:
                raise
            warn(f"Scheduler scan failed: {exc}; retrying in {interval}s")
            wait_with_status(
                "Scheduler",
                lambda: (
                    f"last scan failed | "
                    f"uptime:{format_duration(time.monotonic() - started)}"
                ),
                interval,
                style="yellow",
            )
            continue
        total += len(records)
        for record in records:
            done(f"Scheduler enqueued issue #{record.issue_number} as run {record.run_id}")
        scan_message = (
            f"Scheduler scan completed: {len(records)} enqueued in "
            f"{format_duration(time.monotonic() - scan_started)}"
        )
        store.add_event(
            None,
            "scheduler_scan_completed",
            message=scan_message,
            data={"pid": os.getpid(), "enqueued": len(records), "total_enqueued": total},
        )
        log(scan_message)
        if once:
            show_pool_status(
                "Scheduler",
                "SCAN COMPLETE",
                f"{len(records)} enqueued | elapsed {format_duration(time.monotonic() - started)}",
                style="blue",
            )
            return len(records)
        wait_with_status(
            "Scheduler",
            lambda: (
                f"enqueued total:{total} | "
                f"uptime:{format_duration(time.monotonic() - started)}"
            ),
            interval,
            style="blue",
        )

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
        dead_owner_recoveries = store.reconcile_dead_owners()
        total += len(dead_owner_recoveries)
        for record in dead_owner_recoveries:
            warn(
                f"Recovered dead owner for {record.run_id}: "
                f"status={record.status} stage={record.stage}"
            )
        reconciled = store.reconcile_expired()
        total += len(reconciled)
        for record in reconciled:
            warn(
                f"Reconciled expired {record.run_id}: "
                f"status={record.status} stage={record.stage}"
            )
        for record in store.list_pr_recovery_candidates():
            snapshot = _get_pr_snapshot(record.pr_url)
            if not snapshot or str(snapshot.get("state", "")).upper() != "MERGED":
                continue
            store.update(
                record.run_id,
                status="completed",
                stage="completed",
                error=None,
            )
            store.add_event(
                record.run_id,
                "merged_pr_reconciled",
                stage="completed",
                status="completed",
                message="Recovered run from already merged PR",
                data={
                    "pr_url": snapshot.get("url") or record.pr_url,
                    "merged_at": snapshot.get("mergedAt"),
                },
            )
            done(f"Completed recovered run {record.run_id}: PR already merged")
            total += 1
        for record in store.list_submission_recovery_candidates():
            pr_url = _find_open_pr_for_branch(record.branch)
            if pr_url:
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
                continue
            retry_count = store.count_events(
                record.run_id, "submission_no_changes_retry"
            )
            if (
                retry_count < SUBMISSION_NO_CHANGES_MAX_RETRIES
                and branch_has_commits(record.worktree_path) is False
            ):
                attempt = retry_count + 1
                store.update(
                    record.run_id,
                    status="ready",
                    stage="developing",
                    error=None,
                )
                store.add_event(
                    record.run_id,
                    "submission_no_changes_retry",
                    stage="developing",
                    status="ready",
                    message=(
                        "Reconciler requeued submission with no commits for "
                        "another development pass"
                    ),
                    data={
                        "attempt": attempt,
                        "max_retries": SUBMISSION_NO_CHANGES_MAX_RETRIES,
                    },
                )
                done(
                    f"Requeued no-change submission {record.run_id} "
                    f"({attempt}/{SUBMISSION_NO_CHANGES_MAX_RETRIES})"
                )
                total += 1
        if once:
            show_pool_status(
                "Reconciler",
                "SCAN COMPLETE",
                f"{total} recovered item(s) | elapsed {format_duration(time.monotonic() - started)}",
                style="magenta",
            )
            return total
        wait_with_status(
            "Reconciler",
            lambda: (
                f"recovered total:{total} | "
                f"uptime:{format_duration(time.monotonic() - started)}"
            ),
            interval,
            style="magenta",
        )
