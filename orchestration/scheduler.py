from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Callable

from agents.base import run_cmd
from agents.scheduler import SchedulerAgent, SchedulerDecision
from config import (
    GITHUB_REPO,
    SCHEDULER_AGENT_CANDIDATE_LIMIT,
    SCHEDULER_BATCH_SIZE,
    SCHEDULER_BLOCK_LABELS,
    SCHEDULER_ELIGIBLE_LABELS,
    SCHEDULER_ISSUE_LIMIT,
    SCHEDULER_PRIORITY_LABELS,
    SCHEDULER_SKIP_ASSIGNED_ISSUES,
    SKIP_LABELS,
)
from orchestration.github_ops import _get_related_open_prs
from orchestration.store import RunRecord, RunStore


DEPENDENCY_PATTERN = re.compile(
    r"(?im)^\s*(?:[-*]\s*)?(?:blocked\s+by|depends?\s+on|requires?)\s*:?\s*"
    r"((?:#\d+)(?:\s*(?:,|and)\s*#\d+)*)"
)
ISSUE_NUMBER_PATTERN = re.compile(r"#(\d+)")


@dataclass(frozen=True)
class ScheduleCandidate:
    issue_number: int
    title: str
    body: str
    labels: tuple[str, ...]
    dependencies: tuple[int, ...]
    priority: int
    created_at: str


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
        candidates.append(ScheduleCandidate(
            issue_number=issue_number,
            title=str(issue.get("title") or "(untitled)"),
            body=str(issue.get("body") or ""),
            labels=labels,
            dependencies=dependencies,
            priority=_priority(labels, priorities),
            created_at=str(issue.get("createdAt") or ""),
        ))

    candidates.sort(key=lambda item: (item.priority, item.created_at, item.issue_number))
    selected = []
    for candidate in candidates:
        if has_open_pr is not None and has_open_pr(candidate.issue_number):
            continue
        selected.append(candidate)
        if len(selected) >= batch_size:
            break
    return selected


def _list_open_issues() -> list[dict]:
    result = run_cmd(
        [
            "gh", "issue", "list",
            "--repo", GITHUB_REPO,
            "--state", "open",
            "--limit", str(SCHEDULER_ISSUE_LIMIT),
            "--json", "number,title,body,labels,assignees,createdAt",
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


def _issue_has_open_pr(issue_number: int) -> bool:
    prs = _get_related_open_prs(issue_number)
    # A failed query is treated as occupied so the scheduler fails closed.
    return prs is None or bool(prs)


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
    candidates = select_schedulable_issues(
        issues,
        active_issue_numbers=active_issue_numbers,
        dependency_is_open=_issue_is_open,
        eligible_labels=SCHEDULER_ELIGIBLE_LABELS,
        block_labels=SCHEDULER_BLOCK_LABELS,
        skip_labels=SKIP_LABELS,
        priority_labels=SCHEDULER_PRIORITY_LABELS,
        skip_assigned=SCHEDULER_SKIP_ASSIGNED_ISSUES,
        has_open_pr=_issue_has_open_pr,
        batch_size=SCHEDULER_AGENT_CANDIDATE_LIMIT,
    )
    scheduler_agent = scheduler_agent or SchedulerAgent()
    selected, decisions = _selected_by_agent(candidates, scheduler_agent)
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
        message=f"Scheduler Agent selected {len(selected)} of {len(candidates)} candidates",
        data={
            "candidate_issue_numbers": [candidate.issue_number for candidate in candidates],
            "selected_issue_numbers": sorted(selected_numbers),
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
    enqueued = []
    for candidate, decision in selected:
        if candidate.issue_number in scheduler_queued:
            continue
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
    return enqueued
