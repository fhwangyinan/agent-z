from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from agents.base import log, run_cmd
from agents.scheduler import SchedulerAgent, SchedulerDecision
from config import (
    GITHUB_REPO,
    SCHEDULER_AGENT_CANDIDATE_LIMIT,
    SCHEDULER_BATCH_SIZE,
    SCHEDULER_BLOCK_LABELS,
    SCHEDULER_EMPTY_QUEUE_REEVALUATE_SECONDS,
    SCHEDULER_ELIGIBLE_LABELS,
    SCHEDULER_ISSUE_LIMIT,
    SCHEDULER_PRIORITY_LABELS,
    SCHEDULER_PR_LIMIT,
    SCHEDULER_SKIP_ASSIGNED_ISSUES,
    SKIP_LABELS,
)
from orchestration.github_ops import extract_issue_references
from orchestration.store import RunRecord, RunStore


DEPENDENCY_PATTERN = re.compile(
    r"(?im)^\s*(?:[-*]\s*)?(?:blocked\s+by|depends?\s+on|requires?)\s*:?\s*"
    r"((?:#\d+)(?:\s*(?:,|and)\s*#\d+)*)"
)
ISSUE_NUMBER_PATTERN = re.compile(r"#(\d+)")
SCHEDULER_POLICY_VERSION = 2


@dataclass(frozen=True)
class ScheduleCandidate:
    issue_number: int
    title: str
    body: str
    labels: tuple[str, ...]
    dependencies: tuple[int, ...]
    priority: int
    created_at: str
    updated_at: str


def extract_dependencies(body: str) -> tuple[int, ...]:
    dependencies = {
        int(number)
        for match in DEPENDENCY_PATTERN.finditer(body or "")
        for number in ISSUE_NUMBER_PATTERN.findall(match.group(1))
    }
    return tuple(sorted(dependencies))


def _labels(issue: dict) -> tuple[str, ...]:
    return tuple(
        str(label.get("name", "")).strip()
        for label in issue.get("labels", [])
        if str(label.get("name", "")).strip()
    )


def _priority(labels: tuple[str, ...], priority_labels: list[str]) -> int:
    normalized = {label.lower() for label in labels}
    for index, label in enumerate(priority_labels):
        if label.lower() in normalized:
            return index
    return len(priority_labels)


def select_schedulable_issues(
    issues: list[dict],
    *,
    active_issue_numbers: set[int],
    dependency_is_open: Callable[[int], bool],
    eligible_labels: list[str] | None = None,
    block_labels: list[str] | None = None,
    skip_labels: list[str] | None = None,
    priority_labels: list[str] | None = None,
    skip_assigned: bool = False,
    has_open_pr: Callable[[int], bool] | None = None,
    candidate_state: dict[str, dict] | None = None,
    batch_size: int = SCHEDULER_BATCH_SIZE,
) -> list[ScheduleCandidate]:
    eligible = {label.lower() for label in (eligible_labels or [])}
    blocked = {label.lower() for label in (block_labels or [])}
    skipped = {label.lower() for label in (skip_labels or [])}
    priorities = priority_labels or []
    candidates = []

    for issue in issues:
        issue_number = int(issue["number"])
        labels = _labels(issue)
        normalized = {label.lower() for label in labels}
        if issue_number in active_issue_numbers:
            continue
        if skip_assigned and issue.get("assignees"):
            continue
        if eligible and not (eligible & normalized):
            continue
        if normalized & (blocked | skipped):
            continue

        dependencies = extract_dependencies(str(issue.get("body") or ""))
        if any(dependency_is_open(number) for number in dependencies):
            continue
        candidate = ScheduleCandidate(
            issue_number=issue_number,
            title=str(issue.get("title") or "(untitled)"),
            body=str(issue.get("body") or ""),
            labels=labels,
            dependencies=dependencies,
            priority=_priority(labels, priorities),
            created_at=str(issue.get("createdAt") or ""),
            updated_at=str(issue.get("updatedAt") or ""),
        )
        open_pr = has_open_pr(candidate.issue_number) if has_open_pr is not None else False
        if candidate_state is not None:
            candidate_state[str(candidate.issue_number)] = {
                "updated_at": candidate.updated_at,
                "open_pr": bool(open_pr),
            }
        if open_pr:
            continue
        candidates.append(candidate)

    candidates.sort(key=lambda item: (item.priority, item.created_at, item.issue_number))
    return candidates[:batch_size]


def _list_open_issues() -> list[dict]:
    result = run_cmd(
        [
            "gh", "issue", "list",
            "--repo", GITHUB_REPO,
            "--state", "open",
            "--limit", str(SCHEDULER_ISSUE_LIMIT),
            "--json", "number,title,body,labels,assignees,createdAt,updatedAt",
        ],
        check=False,
    )
    if result.returncode != 0:
        details = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"could not list open issues for scheduling: {details}")
    try:
        issues = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise RuntimeError("could not parse open issues for scheduling") from exc
    if not isinstance(issues, list):
        raise RuntimeError("GitHub issue list returned an unexpected payload")
    return issues


def _issue_is_open(issue_number: int) -> bool:
    result = run_cmd(
        [
            "gh", "issue", "view", str(issue_number),
            "--repo", GITHUB_REPO,
            "--json", "state",
        ],
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        # Unknown dependency state is treated as blocked.
        return True
    try:
        return str(json.loads(result.stdout).get("state", "")).upper() == "OPEN"
    except json.JSONDecodeError:
        return True


def _open_pr_issue_numbers() -> set[int] | None:
    result = run_cmd(
        [
            "gh", "pr", "list",
            "--repo", GITHUB_REPO,
            "--state", "open",
            "--limit", str(SCHEDULER_PR_LIMIT),
            "--json", "title,body",
        ],
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        prs = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(prs, list):
        return None
    return {
        issue_number
        for pr in prs
        for issue_number in extract_issue_references(
            f"{pr.get('title') or ''}\n{pr.get('body') or ''}"
        )
    }


def _selected_by_agent(
    candidates: list[ScheduleCandidate],
    scheduler_agent: SchedulerAgent,
) -> tuple[list[tuple[ScheduleCandidate, SchedulerDecision]], list[SchedulerDecision]]:
    if not candidates:
        return [], []
    by_number = {candidate.issue_number: candidate for candidate in candidates}
    decisions = scheduler_agent.rank([candidate.issue_number for candidate in candidates])
    selected = [
        (by_number[decision.issue_number], decision)
        for decision in decisions
        if decision.action == "enqueue"
    ]
    selected.sort(key=lambda item: (-item[1].score, item[0].issue_number))
    return selected[:SCHEDULER_BATCH_SIZE], decisions


def _queued_count(queue_state: dict[str, str]) -> int:
    return sum(status == "queued" for status in queue_state.values())


def _candidates_for_agent(
    candidates: list[ScheduleCandidate],
    snapshot: dict | None,
    trigger: str,
) -> tuple[list[ScheduleCandidate], int]:
    if snapshot is None or trigger in {"initial_scan", "policy_changed"}:
        return candidates, 0
    previous_candidates = snapshot.get("candidate_state") or {}
    decisions = snapshot.get("decision_state") or {}
    selected = []
    cached_rejects = 0
    for candidate in candidates:
        key = str(candidate.issue_number)
        changed = previous_candidates.get(key) != {
            "updated_at": candidate.updated_at,
            "open_pr": False,
        }
        cached_action = (decisions.get(key) or {}).get("action")
        if changed or cached_action in {None, "defer"}:
            selected.append(candidate)
        elif cached_action == "reject":
            cached_rejects += 1
    return selected, cached_rejects


def _merged_decision_state(
    candidates: list[ScheduleCandidate],
    previous: dict | None,
    decisions: list[SchedulerDecision],
) -> dict[str, dict]:
    candidate_numbers = {str(candidate.issue_number) for candidate in candidates}
    merged = {
        str(number): value
        for number, value in (previous or {}).items()
        if str(number) in candidate_numbers
    }
    for decision in decisions:
        merged[str(decision.issue_number)] = {
            "action": decision.action,
            "score": decision.score,
            "reason": decision.reason,
        }
    return merged


def _policy_state() -> dict:
    return {
        "version": SCHEDULER_POLICY_VERSION,
        "eligible_labels": sorted(SCHEDULER_ELIGIBLE_LABELS),
        "block_labels": sorted(SCHEDULER_BLOCK_LABELS),
        "skip_labels": sorted(SKIP_LABELS),
        "priority_labels": list(SCHEDULER_PRIORITY_LABELS),
        "skip_assigned": SCHEDULER_SKIP_ASSIGNED_ISSUES,
        "issue_limit": SCHEDULER_ISSUE_LIMIT,
        "pr_limit": SCHEDULER_PR_LIMIT,
        "candidate_limit": SCHEDULER_AGENT_CANDIDATE_LIMIT,
        "batch_size": SCHEDULER_BATCH_SIZE,
        "empty_queue_reevaluate_seconds": SCHEDULER_EMPTY_QUEUE_REEVALUATE_SECONDS,
    }


def _agent_trigger(
    snapshot: dict | None,
    *,
    candidate_state: dict[str, dict],
    queue_state: dict[str, str],
    policy_state: dict | None = None,
) -> str | None:
    if snapshot is None:
        return "initial_scan"
    if policy_state is not None and snapshot.get("policy_state") != policy_state:
        return "policy_changed"
    if snapshot.get("candidate_state") != candidate_state:
        return "candidate_state_changed"
    previous_queued = _queued_count(snapshot.get("queue_state") or {})
    current_queued = _queued_count(queue_state)
    if current_queued < SCHEDULER_BATCH_SIZE and current_queued < previous_queued:
        return "queue_needs_replenishment"
    if current_queued == 0:
        evaluated_at = snapshot.get("agent_evaluated_at")
        if not evaluated_at:
            return "empty_queue_reassessment"
        try:
            evaluated = datetime.fromisoformat(str(evaluated_at).replace("Z", "+00:00"))
        except ValueError:
            return "empty_queue_reassessment"
        if (
            datetime.now(timezone.utc) - evaluated
        ).total_seconds() >= SCHEDULER_EMPTY_QUEUE_REEVALUATE_SECONDS:
            return "empty_queue_reassessment"
    return None


def schedule_once(
    store: RunStore,
    scheduler_agent: SchedulerAgent | None = None,
) -> list[RunRecord]:
    issues = _list_open_issues()
    scheduler_queued = {
        record.issue_number: record
        for record in store.list_scheduler_queued(GITHUB_REPO)
    }
    active_issue_numbers = store.active_issue_numbers(GITHUB_REPO)
    active_issue_numbers -= set(scheduler_queued)
    open_pr_issue_numbers = _open_pr_issue_numbers()
    candidate_state: dict[str, dict] = {}
    candidates = select_schedulable_issues(
        issues,
        active_issue_numbers=active_issue_numbers,
        dependency_is_open=_issue_is_open,
        eligible_labels=SCHEDULER_ELIGIBLE_LABELS,
        block_labels=SCHEDULER_BLOCK_LABELS,
        skip_labels=SKIP_LABELS,
        priority_labels=SCHEDULER_PRIORITY_LABELS,
        skip_assigned=SCHEDULER_SKIP_ASSIGNED_ISSUES,
        # A failed bulk PR query fails closed for every candidate.
        has_open_pr=lambda issue_number: (
            open_pr_issue_numbers is None or issue_number in open_pr_issue_numbers
        ),
        candidate_state=candidate_state,
        batch_size=SCHEDULER_AGENT_CANDIDATE_LIMIT,
    )
    fetched_issue_numbers = {int(issue["number"]) for issue in issues}
    eligible_issue_numbers = {
        int(number)
        for number, state in candidate_state.items()
        if open_pr_issue_numbers is None or not state.get("open_pr")
    }
    for issue_number, queued in scheduler_queued.items():
        if (
            issue_number in fetched_issue_numbers
            and issue_number not in eligible_issue_numbers
        ):
            store.release_scheduler_queued(
                queued.run_id,
                action="defer",
                reason="Scheduler safety filters no longer allow this issue",
            )
    queue_state = store.scheduler_queue_state(GITHUB_REPO)
    snapshot = store.get_scheduler_snapshot(GITHUB_REPO)
    decision_state = (snapshot or {}).get("decision_state") or {}
    policy_state = _policy_state()
    trigger = _agent_trigger(
        snapshot,
        candidate_state=candidate_state,
        queue_state=queue_state,
        policy_state=policy_state,
    )
    if not candidates:
        message = (
            f"Scheduler found no eligible candidates among {len(issues)} open issue(s)"
        )
        store.add_event(
            None,
            "scheduler_no_candidates",
            message=message,
            data={
                "open_issues": len(issues),
                "active_issues": len(active_issue_numbers),
                "open_pr_snapshot": open_pr_issue_numbers is not None,
            },
        )
        log(message)
        store.save_scheduler_snapshot(
            GITHUB_REPO,
            candidate_state=candidate_state,
            queue_state=queue_state,
            policy_state=policy_state,
            decision_state={},
        )
        return []
    if trigger is None:
        message = (
            f"Scheduler Agent skipped: {len(candidates)} candidate(s) unchanged; "
            f"{_queued_count(queue_state)} queued"
        )
        store.add_event(
            None,
            "scheduler_agent_skipped",
            message=message,
            data={
                "candidate_issue_numbers": [candidate.issue_number for candidate in candidates],
                "queued": _queued_count(queue_state),
            },
        )
        log(message)
        store.save_scheduler_snapshot(
            GITHUB_REPO,
            candidate_state=candidate_state,
            queue_state=queue_state,
            policy_state=policy_state,
            decision_state=_merged_decision_state(candidates, decision_state, []),
        )
        return []

    agent_candidates, cached_rejects = _candidates_for_agent(candidates, snapshot, trigger)
    if not agent_candidates:
        message = (
            f"Scheduler Agent skipped: {cached_rejects} unchanged rejected candidate(s) "
            "served from decision cache"
        )
        store.add_event(
            None,
            "scheduler_agent_cache_hit",
            message=message,
            data={"cached_rejects": cached_rejects, "trigger": trigger},
        )
        log(message)
        store.save_scheduler_snapshot(
            GITHUB_REPO,
            candidate_state=candidate_state,
            queue_state=queue_state,
            policy_state=policy_state,
            decision_state=_merged_decision_state(candidates, decision_state, []),
            agent_evaluated=True,
        )
        return []

    scheduler_agent = scheduler_agent or SchedulerAgent()
    candidate_numbers = [candidate.issue_number for candidate in agent_candidates]
    message = (
        f"Scheduler Agent evaluating {len(agent_candidates)} candidate(s)"
        f" ({cached_rejects} cached reject(s)): "
        + ", ".join(f"#{number}" for number in candidate_numbers)
    )
    store.add_event(
        None,
        "scheduler_agent_started",
        message=message,
        data={
            "candidate_issue_numbers": candidate_numbers,
            "cached_rejects": cached_rejects,
            "trigger": trigger,
        },
    )
    log(message)
    selected, decisions = _selected_by_agent(agent_candidates, scheduler_agent)
    decision_state = _merged_decision_state(candidates, decision_state, decisions)
    selected_numbers = {candidate.issue_number for candidate, _ in selected}
    for decision in decisions:
        queued = scheduler_queued.get(decision.issue_number)
        if queued is None or decision.action == "enqueue":
            continue
        store.release_scheduler_queued(
            queued.run_id,
            action=decision.action,
            reason=f"Scheduler Agent {decision.action}: {decision.reason}",
        )
    store.add_event(
        None,
        "scheduler_agent_evaluated",
        message=f"Scheduler Agent selected {len(selected)} of {len(agent_candidates)} candidates",
        data={
            "candidate_issue_numbers": candidate_numbers,
            "cached_rejects": cached_rejects,
            "selected_issue_numbers": sorted(selected_numbers),
            "trigger": trigger,
            "decisions": [
                {
                    "issue_number": decision.issue_number,
                    "action": decision.action,
                    "score": decision.score,
                    "reason": decision.reason,
                }
                for decision in decisions
            ],
        },
    )
    log(
        f"Scheduler Agent selected {len(selected)} of {len(agent_candidates)} candidate(s)"
    )
    enqueued = []
    available_slots = max(
        0,
        SCHEDULER_BATCH_SIZE - _queued_count(store.scheduler_queue_state(GITHUB_REPO)),
    )
    for candidate, decision in selected:
        if candidate.issue_number in scheduler_queued:
            continue
        if available_slots <= 0:
            break
        try:
            record = store.enqueue(GITHUB_REPO, candidate.issue_number)
        except RuntimeError as exc:
            if "already has a queued or active run" in str(exc):
                continue
            raise
        store.add_event(
            record.run_id,
            "scheduler_enqueued",
            stage=record.stage,
            status=record.status,
            message=f"Scheduler enqueued issue #{candidate.issue_number}",
            data={
                "title": candidate.title,
                "labels": list(candidate.labels),
                "dependencies": list(candidate.dependencies),
                "priority": candidate.priority,
                "agent_score": decision.score,
                "agent_reason": decision.reason,
            },
        )
        enqueued.append(record)
        available_slots -= 1
    store.save_scheduler_snapshot(
        GITHUB_REPO,
        candidate_state=candidate_state,
        queue_state=store.scheduler_queue_state(GITHUB_REPO),
        policy_state=policy_state,
        decision_state=decision_state,
        agent_evaluated=True,
    )
    return enqueued
