import hashlib
import time
from datetime import datetime, timezone

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt

from agents.base import done, error, format_duration, log, run_cmd, step, warn
from agents.analyst import AnalystAgent
from agents.developer import DeveloperAgent
from agents.reviewer import ReviewerAgent
from config import (
    CLEANUP_COMPLETED_WORKTREES,
    CLEANUP_FAILED_WORKTREES,
    GITHUB_REPO,
    MAX_LOCAL_REVIEW_ROUNDS,
    MAX_PARALLEL_TASKS,
    MAX_REVIEW_ROUNDS,
    MAX_RUN_SECONDS,
    PLANNER_LEASE_SECONDS,
    SUBMISSION_NO_CHANGES_MAX_RETRIES,
    WORKER_LEASE_SECONDS,
)
from orchestration.errors import NeedsHumanError, NoChangesError
from orchestration.github_ops import (
    _base_sha,
    _get_issue_snapshot,
    cleanup_run_artifacts,
    mark_issue_with_skip_label,
    preflight_worker,
    prepare_base_repo,
    pr_feedback_fingerprint,
    wait_for_pr_checks,
)
from orchestration.runtime import runtime
from orchestration.submission import resolve_submission
from orchestration.store import RunRecord, RunStore
from orchestration.tui import (
    STAGE_LABELS,
    confirm_issue,
    choose_issue,
    run_step,
    show_analysis,
    show_run_summary,
)
from orchestration.worktree import WorktreeManager

console = Console()


def cleanup_failed_run(record: RunRecord, store: RunStore, worktrees: WorktreeManager):
    if not CLEANUP_FAILED_WORKTREES or runtime.keep_worktree:
        store.add_event(
            record.run_id,
            "failure_artifacts_preserved",
            stage=record.stage,
            status=record.status,
            message="Preserved failed-run artifacts for recovery",
            data={
                "branch": record.branch,
                "worktree_path": record.worktree_path,
                "active_work_label_preserved": True,
            },
        )
        warn(
            f"Preserved recovery artifacts [dim]| run:{record.run_id} | "
            f"branch:{record.branch or '-'} | worktree:{record.worktree_path or '-'}[/dim]"
        )
    cleanup_run_artifacts(
        record,
        store,
        worktrees,
        remove_worktree=CLEANUP_FAILED_WORKTREES and not runtime.keep_worktree,
        remove_label=False,
    )

class CoordinatorAgentState:
    """Session-compatible placeholder for deterministic coordinator-owned work."""

    name = "Coordinator"
    session_id = None

    def reset_session(self):
        self.session_id = None

    def set_workspace(self, cwd: str):
        pass

def run_local_review(
    issue_number: int,
    reviewer: ReviewerAgent,
    developer: DeveloperAgent,
    *,
    follow_up: bool = False,
    record: RunRecord | None = None,
    store: RunStore | None = None,
) -> bool:
    for local_round in range(MAX_LOCAL_REVIEW_ROUNDS):
        fingerprint = _workspace_fingerprint(
            record.worktree_path if record is not None else None
        )
        cached = (
            store.latest_event(record.run_id, "local_review_completed")
            if store is not None and record is not None and fingerprint
            else None
        )
        if cached is not None and cached.data.get("fingerprint") == fingerprint:
            store.add_event(
                record.run_id,
                "local_review_cache_hit",
                stage=record.stage,
                status=record.status,
                message="Reused local review for unchanged workspace",
                data={
                    "fingerprint": fingerprint,
                    "approved": bool(cached.data.get("approved")),
                },
            )
            if cached.data.get("approved"):
                done("Local Reviewer skipped; unchanged workspace matched approved review")
                return True
            cached_findings = cached.data.get("findings")
            review_comments = cached_findings if isinstance(cached_findings, list) else None
        else:
            review_comments = None
        if follow_up:
            step(f"[REVIEW] Local Reviewer follow-up (round {local_round + 1})")
        if review_comments is None:
            review_comments = reviewer.review(issue_number, resume_session=True)
        else:
            log("Local Reviewer reused cached findings for unchanged workspace")
        if store is not None and record is not None and fingerprint:
            store.add_event(
                record.run_id,
                "local_review_completed",
                stage=record.stage,
                status=record.status,
                message="Completed local review",
                data={
                    "fingerprint": fingerprint,
                    "approved": not review_comments,
                    "findings": review_comments,
                },
            )
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

def _workspace_fingerprint(worktree_path: str | None) -> str | None:
    if not worktree_path:
        return None
    head = run_cmd(["git", "rev-parse", "HEAD"], cwd=worktree_path, check=False)
    status = run_cmd(["git", "status", "--porcelain"], cwd=worktree_path, check=False)
    diff = run_cmd(["git", "diff", "--binary", "HEAD"], cwd=worktree_path, check=False)
    untracked = run_cmd(
        ["git", "ls-files", "--others", "--exclude-standard"],
        cwd=worktree_path,
        check=False,
    )
    if any(result.returncode != 0 for result in (head, status, diff, untracked)):
        return None
    untracked_files = [line for line in untracked.stdout.splitlines() if line.strip()]
    hashes = ""
    if untracked_files:
        result = run_cmd(
            ["git", "hash-object", "--", *untracked_files],
            cwd=worktree_path,
            check=False,
        )
        if result.returncode != 0:
            return None
        hashes = result.stdout
    payload = "\0".join((head.stdout, status.stdout, diff.stdout, untracked.stdout, hashes))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()

def _planner_phase(
    store: RunStore,
    run_id: str,
    event_type: str,
    planning_input: dict,
) -> dict:
    event = store.latest_event(run_id, event_type)
    if event is None or not isinstance(event.data, dict):
        return {}
    if event.data.get("planning_input") != planning_input:
        return {}
    return event.data

def _save_planner_phase(
    store: RunStore,
    record: RunRecord,
    analyst: AnalystAgent,
    event_type: str,
    data: dict,
    planning_input: dict,
):
    store.add_event(
        record.run_id,
        event_type,
        stage="analyzing",
        status="planning",
        message=f"Cached Planner phase: {event_type.removeprefix('planner_')}",
        data={**data, "planning_input": planning_input},
    )
    store.update(
        record.run_id,
        sessions={"task_lead": analyst.session_id} if analyst.session_id else {},
        error=None,
    )
    store.heartbeat(record.run_id, "planner", PLANNER_LEASE_SECONDS)

def _record_planner_cache_hit(store: RunStore, record: RunRecord, phase: str):
    store.add_event(
        record.run_id,
        "planner_phase_cache_hit",
        stage="analyzing",
        status="planning",
        message=f"Reused cached Planner {phase}",
        data={"phase": phase},
    )

def _agents(analyst, developer, reviewer, submitter):
    return (analyst, developer, reviewer, submitter)

def _session_snapshot(analyst, developer, reviewer, submitter) -> dict[str, str]:
    sessions = {}
    task_lead_session = developer.session_id or analyst.session_id
    if task_lead_session:
        sessions["task_lead"] = task_lead_session
    if reviewer.session_id:
        sessions["reviewer"] = reviewer.session_id
    return sessions

def _restore_sessions(record: RunRecord, analyst, developer, reviewer, submitter):
    for agent in _agents(analyst, developer, reviewer, submitter):
        agent.reset_session()
    task_lead_session = (
        record.sessions.get("task_lead")
        or record.sessions.get("developer")
        or record.sessions.get("analyst")
    )
    analyst.session_id = task_lead_session
    developer.session_id = task_lead_session
    reviewer.session_id = record.sessions.get("reviewer")

def _sync_task_lead_session(analyst, developer):
    shared = developer.session_id or analyst.session_id
    analyst.session_id = shared
    developer.session_id = shared

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
    updated = store.update(record.run_id, **fields)
    if (
        updated.lease_role == "worker"
        and updated.status in {"running", "waiting_checks"}
    ):
        updated = store.heartbeat(updated.run_id, "worker", WORKER_LEASE_SECONDS)
    return updated

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

def plan_task(
    record: RunRecord,
    store: RunStore,
    analyst: AnalystAgent,
    *,
    fail_on_error: bool = True,
) -> RunRecord:
    started = time.monotonic()
    analyst.reset_session()
    analyst.session_id = (
        record.sessions.get("task_lead")
        or record.sessions.get("analyst")
    )
    try:
        run_step(record, "[PLAN] Task Lead plans issue", started=started)
        snapshot = _get_issue_snapshot(record.issue_number) or {}
        planning_input = {
            "issue_updated_at": snapshot.get("updatedAt"),
            "base_sha": _base_sha(),
        }
        analysis_cache = _planner_phase(
            store, record.run_id, "planner_analysis_completed", planning_input
        )
        analysis = str(analysis_cache.get("analysis") or "")
        if not analysis:
            _, analysis = analyst.analyze(
                target_issue=record.issue_number,
                resume_session=bool(analyst.session_id),
            )
            _save_planner_phase(
                store, record, analyst, "planner_analysis_completed",
                {"analysis": analysis},
                planning_input,
            )
        else:
            _record_planner_cache_hit(store, record, "analysis")
            log(f"Planner reused cached analysis for issue #{record.issue_number}")
        show_analysis(record.issue_number, analysis)
        impact_cache = _planner_phase(
            store, record.run_id, "planner_impact_completed", planning_input
        )
        impact = str(impact_cache.get("impact") or "")
        risk = str(impact_cache.get("risk") or "")
        if not impact or not risk:
            impact, risk = analyst.assess_impact(
                record.issue_number,
                resume_session=bool(analyst.session_id),
            )
            _save_planner_phase(
                store, record, analyst, "planner_impact_completed",
                {"impact": impact, "risk": risk},
                planning_input,
            )
        else:
            _record_planner_cache_hit(store, record, "impact")
            log(f"Planner reused cached impact assessment for issue #{record.issue_number}")
        plan_cache = _planner_phase(
            store, record.run_id, "planner_plan_completed", planning_input
        )
        plan = plan_cache.get("plan")
        if not isinstance(plan, dict) or not plan:
            plan = analyst.build_plan(
                record.issue_number,
                analysis,
                impact,
                risk,
                resume_session=bool(analyst.session_id),
            )
            _save_planner_phase(
                store, record, analyst, "planner_plan_completed",
                {"plan": plan},
                planning_input,
            )
        else:
            _record_planner_cache_hit(store, record, "plan")
            log(f"Planner reused cached implementation plan for issue #{record.issue_number}")
        plan.update({
            "issue_number": record.issue_number,
            "issue_updated_at": planning_input["issue_updated_at"],
            "planned_at": datetime.now(timezone.utc).isoformat(),
            "base_sha": planning_input["base_sha"],
            "plan_version": 1,
        })
        planned = store.finish_planning(
            record.run_id,
            plan=plan,
            risk=risk,
            sessions={"task_lead": analyst.session_id} if analyst.session_id else {},
        )
        store.add_event(
            record.run_id,
            "planning_completed",
            stage="ready",
            status="ready",
            data={
                "risk": risk,
                "predicted_files": plan.get("predicted_files", []),
                "base_sha": plan.get("base_sha", ""),
            },
        )
        show_run_summary(
            planned,
            title="Planning completed",
            elapsed=time.monotonic() - started,
            message=(
                f"{len(plan.get('predicted_files', []))} predicted file(s) | "
                f"{len(plan.get('acceptance_criteria', []))} acceptance criterion/criteria"
            ),
            border_style="blue",
        )
        return planned
    except Exception as exc:
        failed = store.update(
            record.run_id,
            sessions={"task_lead": analyst.session_id} if analyst.session_id else {},
            error=str(exc),
            **({"status": "failed", "stage": "analyzing"} if fail_on_error else {}),
        )
        show_run_summary(
            failed,
            title="Planning failed",
            elapsed=time.monotonic() - started,
            message=str(exc),
            border_style="red",
        )
        raise

def execute_task(
    record: RunRecord,
    store: RunStore,
    worktrees: WorktreeManager,
    analyst: AnalystAgent,
    developer: DeveloperAgent,
    reviewer: ReviewerAgent,
    submitter: CoordinatorAgentState,
) -> bool:
    started = time.monotonic()
    _restore_sessions(record, analyst, developer, reviewer, submitter)
    show_run_summary(
        record,
        title="Worker claimed run",
        elapsed=0,
        message=f"Resuming from {STAGE_LABELS.get(record.stage, record.stage)}",
        border_style="cyan",
    )

    if record.worktree_path:
        workspace = worktrees.validate(record.worktree_path)
        _set_workspace(str(workspace), analyst, developer, reviewer, submitter)

    try:
        if record.stage in {"queued", "analyzing"}:
            record = _checkpoint(
                store, record, analyst, developer, reviewer, submitter,
                status="running", stage="analyzing",
            )
            run_step(record, "[ANALYZE] Task Lead analyzes issue", started=started)
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
            run_step(record, "[ASSESS] Impact assessment", started=started)
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

        if runtime.auto_mode and not runtime.force_develop and risk in ("high", "very_high"):
            warn(f"Risk is [{risk}]; skipping automatically")
            record = _checkpoint(
                store, record, analyst, developer, reviewer, submitter,
                status="skipped", stage="skipped",
            )
            show_run_summary(
                record,
                title="Run skipped",
                elapsed=time.monotonic() - started,
                message=f"Risk level {risk} requires explicit force",
                border_style="yellow",
            )
            return False
        if not runtime.auto_mode and record.stage == "assessed":
            if not _interactive_impact_qa(analyst, risk):
                log("Issue skipped by user")
                record = _checkpoint(
                    store, record, analyst, developer, reviewer, submitter,
                    status="skipped", stage="skipped",
                )
                show_run_summary(
                    record,
                    title="Run skipped",
                    elapsed=time.monotonic() - started,
                    message="Skipped by user",
                    border_style="yellow",
                )
                return False

        _check_run_budget(started)
        if not record.worktree_path:
            run_step(record, "[PREFLIGHT] Worker validates task", started=started)
            record = preflight_worker(record, store)
            if record.status in {"queued", "skipped"}:
                warn(record.error or "Worker preflight stopped the run")
                show_run_summary(
                    record,
                    title="Worker preflight stopped run",
                    elapsed=time.monotonic() - started,
                    message=record.error,
                    border_style="yellow",
                )
                return False
            done(
                f"Preflight passed [dim]| run:{record.run_id} | "
                f"predicted files:{len(record.plan.get('predicted_files', []))}[/dim]"
            )
            record = mark_issue_with_skip_label(record, store)
            prepare_base_repo()
            branch = record.branch or f"agent-z/{record.issue_number}-{record.run_id}"
            workspace = worktrees.create(record.run_id, branch)
            record = _checkpoint(
                store, record, analyst, developer, reviewer, submitter,
                branch=branch, worktree_path=str(workspace), stage="developing",
            )
            _set_workspace(str(workspace), analyst, developer, reviewer, submitter)
            done(
                f"Workspace ready [dim]| run:{record.run_id} | "
                f"branch:{record.branch} | {record.worktree_path}[/dim]"
            )

        if record.stage in {"assessed", "ready", "developing"}:
            record = _checkpoint(
                store, record, analyst, developer, reviewer, submitter,
                status="running", stage="developing",
            )
            run_step(record, "[DEVELOP] Task Lead implements fix", started=started)
            _sync_task_lead_session(analyst, developer)
            developer.fix(
                record.issue_number,
                plan=record.plan,
                resume_session=bool(developer.session_id),
                no_changes_retry=bool(
                    store.count_events(record.run_id, "submission_no_changes_retry")
                ),
            )
            record = _checkpoint(
                store, record, analyst, developer, reviewer, submitter,
                stage="reviewing",
            )
            record = _claim_changed_files(store, record)

        _check_run_budget(started)
        if record.stage == "reviewing":
            run_step(record, "[REVIEW] Independent local review", started=started)
            if not run_local_review(
                record.issue_number, reviewer, developer, record=record, store=store
            ):
                record = _checkpoint(
                    store, record, analyst, developer, reviewer, submitter,
                    status="needs_human", stage="reviewing",
                    error="local review limit reached",
                )
                cleanup_failed_run(record, store, worktrees)
                show_run_summary(
                    record,
                    title="Run needs attention",
                    elapsed=time.monotonic() - started,
                    message=record.error,
                    border_style="red",
                )
                return False
            record = _claim_changed_files(store, record)
            record = _checkpoint(
                store, record, analyst, developer, reviewer, submitter,
                stage="submitting",
            )

        if record.stage == "submitting":
            run_step(record, "[SUBMIT] Coordinator creates PR", started=started)
            pr_url, submission_source = resolve_submission(record, developer, store)
            if not pr_url:
                raise NeedsHumanError(
                    "Coordinator did not create a verifiable PR"
                )
            store.add_event(
                record.run_id,
                "submission_resolved",
                stage="submitting",
                status=record.status,
                message="Resolved PR for submission",
                data={"pr_url": pr_url, "source": submission_source},
            )
            console.print(Panel(
                f"[link={pr_url}]{pr_url}[/link]\n"
                f"[dim]Run {record.run_id} | source: {submission_source} | "
                f"elapsed: {format_duration(time.monotonic() - started)}[/dim]",
                title="[bold green]PR ready[/bold green]",
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
                if not wait_for_pr_checks(record.pr_url, record):
                    record = _checkpoint(
                        store, record, analyst, developer, reviewer, submitter,
                        status="needs_human", stage="waiting_checks",
                        error="PR checks did not reach a final state",
                    )
                    cleanup_failed_run(record, store, worktrees)
                    show_run_summary(
                        record,
                        title="Run needs attention",
                        elapsed=time.monotonic() - started,
                        message=record.error,
                        border_style="red",
                    )
                    return False
                record = _checkpoint(
                    store, record, analyst, developer, reviewer, submitter,
                    status="running", stage="handling_feedback",
                )

            run_step(
                record,
                f"[FEEDBACK] Task Lead handles PR feedback "
                f"({review_count + 1}/{MAX_REVIEW_ROUNDS})",
                started=started,
            )
            feedback_fingerprint = pr_feedback_fingerprint(record.pr_url)
            feedback_cache = store.latest_event(record.run_id, "pr_feedback_assessed")
            if (
                feedback_fingerprint
                and feedback_cache is not None
                and feedback_cache.data.get("fingerprint") == feedback_fingerprint
            ):
                action_needed = bool(feedback_cache.data.get("action_needed"))
                dev_output = str(feedback_cache.data.get("output") or "")
                store.add_event(
                    record.run_id,
                    "pr_feedback_cache_hit",
                    stage=record.stage,
                    status=record.status,
                    message="Reused unchanged PR feedback assessment",
                    data={
                        "fingerprint": feedback_fingerprint,
                        "action_needed": action_needed,
                    },
                )
                log("Task Lead reused cached PR feedback assessment")
            else:
                dev_output = developer.apply_review(
                    record.issue_number,
                    record.pr_url,
                    resume_session=True,
                )
                action_needed = "NO_ACTION_NEEDED" not in dev_output.upper()
                if feedback_fingerprint:
                    store.add_event(
                        record.run_id,
                        "pr_feedback_assessed",
                        stage=record.stage,
                        status=record.status,
                        message="Cached PR feedback assessment",
                        data={
                            "fingerprint": feedback_fingerprint,
                            "action_needed": action_needed,
                            "output": dev_output,
                        },
                    )
            if not action_needed:
                done("Developer reported no action needed")
                break
            if not run_local_review(
                record.issue_number,
                reviewer,
                developer,
                follow_up=True,
                record=record,
                store=store,
            ):
                record = _checkpoint(
                    store, record, analyst, developer, reviewer, submitter,
                    status="needs_human", stage="handling_feedback",
                    error="local review limit reached after PR feedback",
                )
                cleanup_failed_run(record, store, worktrees)
                show_run_summary(
                    record,
                    title="Run needs attention",
                    elapsed=time.monotonic() - started,
                    message=record.error,
                    border_style="red",
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
            record = _checkpoint(
                store, record, analyst, developer, reviewer, submitter,
                status="needs_human", stage=record.stage,
                error="PR feedback limit reached",
            )
            cleanup_failed_run(record, store, worktrees)
            show_run_summary(
                record,
                title="Run needs attention",
                elapsed=time.monotonic() - started,
                message=record.error,
                border_style="red",
            )
            return False

        record = _checkpoint(
            store, record, analyst, developer, reviewer, submitter,
            status="completed", stage="completed", error=None,
        )
        show_run_summary(
            record,
            title="Run completed",
            elapsed=time.monotonic() - started,
            message=f"{len(record.touched_files)} changed file(s)",
            border_style="green",
        )
        cleanup_run_artifacts(
            record,
            store,
            worktrees,
            remove_worktree=(
                CLEANUP_COMPLETED_WORKTREES
                and not runtime.keep_worktree
                and bool(record.worktree_path)
            ),
            remove_label=True,
        )
        return True
    except KeyboardInterrupt:
        record = _checkpoint(
            store, record, analyst, developer, reviewer, submitter,
            status="needs_human", error="interrupted by user",
        )
        cleanup_failed_run(record, store, worktrees)
        show_run_summary(
            record,
            title="Run interrupted",
            elapsed=time.monotonic() - started,
            message="Artifacts preserved for recovery",
            border_style="yellow",
        )
        raise
    except NoChangesError as exc:
        retry_count = store.count_events(record.run_id, "submission_no_changes_retry")
        if retry_count < SUBMISSION_NO_CHANGES_MAX_RETRIES:
            attempt = retry_count + 1
            record = _checkpoint(
                store, record, analyst, developer, reviewer, submitter,
                status="ready",
                stage="developing",
                error=str(exc),
                owner_pid=None,
                lease_role=None,
                lease_expires_at=None,
            )
            store.add_event(
                record.run_id,
                "submission_no_changes_retry",
                stage="developing",
                status="ready",
                message="Submission had no commits; requeued for another development pass",
                data={
                    "attempt": attempt,
                    "max_retries": SUBMISSION_NO_CHANGES_MAX_RETRIES,
                },
            )
            show_run_summary(
                record,
                title="Run requeued",
                elapsed=time.monotonic() - started,
                message=(
                    f"No changes were produced; retrying development "
                    f"({attempt}/{SUBMISSION_NO_CHANGES_MAX_RETRIES})"
                ),
                border_style="yellow",
            )
            return False
        record = _checkpoint(
            store, record, analyst, developer, reviewer, submitter,
            status="needs_human",
            error=(
                f"{exc}; no changes after "
                f"{SUBMISSION_NO_CHANGES_MAX_RETRIES} automatic retry attempt(s)"
            ),
        )
        cleanup_failed_run(record, store, worktrees)
        show_run_summary(
            record,
            title="Run needs attention",
            elapsed=time.monotonic() - started,
            message=record.error,
            border_style="red",
        )
        raise
    except Exception as exc:
        status = (
            "needs_human"
            if isinstance(exc, NeedsHumanError) or "file lock conflict" in str(exc)
            else "failed"
        )
        record = _checkpoint(
            store, record, analyst, developer, reviewer, submitter,
            status=status, error=str(exc),
        )
        cleanup_failed_run(record, store, worktrees)
        show_run_summary(
            record,
            title="Run needs attention" if status == "needs_human" else "Run failed",
            elapsed=time.monotonic() - started,
            message=str(exc),
            border_style="red",
        )
        raise

def run_round(
    analyst: AnalystAgent,
    developer: DeveloperAgent,
    reviewer: ReviewerAgent,
    submitter: CoordinatorAgentState,
    store: RunStore,
    worktrees: WorktreeManager,
    target_issue: int | None = None,
) -> bool:
    for agent in (analyst, developer, reviewer, submitter):
        agent.reset_session()
    target = target_issue if target_issue is not None else choose_issue()
    if target:
        issue_number, analysis = target, ""
    else:
        step("[SELECT] Task Lead selects issue")
        issue_number, analysis = analyst.analyze(resume_session=False)
    if issue_number is None:
        error("Analyst did not recommend an issue")
        return False
    if analysis:
        show_analysis(issue_number, analysis)
    issue_number = confirm_issue(issue_number)
    if issue_number is None:
        log("Skipped; moving to the next round")
        return False
    record = store.enqueue(GITHUB_REPO, issue_number)
    if analyst.session_id:
        record = store.update(
            record.run_id,
            sessions={"task_lead": analyst.session_id},
        )
    show_run_summary(
        record,
        title="Run created",
        elapsed=0,
        message="Queued for Task Lead planning",
        border_style="cyan",
    )
    record = store.claim_for_planning(PLANNER_LEASE_SECONDS, record.run_id)
    record = plan_task(record, store, analyst)
    record = store.claim_ready(MAX_PARALLEL_TASKS, WORKER_LEASE_SECONDS, record.run_id)
    if record is None:
        warn("Planning completed, but no development slot is currently available")
        return False
    return execute_task(
        record, store, worktrees, analyst, developer, reviewer, submitter
    )

def _build_agents():
    return AnalystAgent(), DeveloperAgent(), ReviewerAgent(), CoordinatorAgentState()
