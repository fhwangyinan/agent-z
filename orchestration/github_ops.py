import json
import hashlib
import os
import re
import shutil
import subprocess
import time
from datetime import datetime

from rich.console import Console

from agents.base import done, elapsed_status, format_duration, log, run_cmd, step, warn
from agents.developer import DeveloperAgent
from config import (
    GITHUB_REPO,
    MAX_PARALLEL_TASKS,
    PR_CHECKS_INTERVAL,
    PR_CHECKS_MAX_WAIT,
    PROJECT_DIR,
    REVIEWER_BACKEND,
    SKIP_LABELS,
    TASK_LEAD_BACKEND,
)
from orchestration.errors import NeedsHumanError
from orchestration.store import RunRecord, RunStore
from orchestration.tui import _show_pr_checks, run_step, wait_with_status
from orchestration.worktree import WorktreeManager

console = Console()

def validate_environment(*, require_backends: bool = True):
    if not os.path.isdir(PROJECT_DIR):
        raise RuntimeError(f"PROJECT_DIR does not exist: {PROJECT_DIR}")
    if not GITHUB_REPO or "/" not in GITHUB_REPO:
        raise RuntimeError(f"GITHUB_REPO must use owner/repo format, got {GITHUB_REPO!r}")
    if not SKIP_LABELS:
        raise RuntimeError("SKIP_LABELS must include at least one label")
    selected_backends = {
        TASK_LEAD_BACKEND,
        REVIEWER_BACKEND,
    }
    supported_backends = {"claude", "codex", "opencode"}
    unknown = selected_backends - supported_backends
    if unknown:
        raise RuntimeError(f"unsupported agent backend(s): {', '.join(sorted(unknown))}")
    commands = ["git", "gh"]
    if require_backends:
        commands.extend(sorted(selected_backends))
    for command in commands:
        if not shutil.which(command):
            raise RuntimeError(f"required command not found: {command}")
    result = run_cmd(["git", "rev-parse", "--is-inside-work-tree"], check=False)
    if result.returncode != 0 or result.stdout.strip() != "true":
        raise RuntimeError(f"PROJECT_DIR is not a Git work tree: {PROJECT_DIR}")

def prepare_base_repo():
    step("[SETUP] Refresh base repository")
    _assert_base_repo_safe()
    run_cmd(["git", "fetch", "origin", "main"], verbose=True, retry=True)
    _assert_base_repo_safe()
    done("Base repository refreshed")

def _assert_base_repo_safe():
    status = run_cmd(["git", "status", "--porcelain"], check=False)
    if status.returncode != 0:
        raise NeedsHumanError("could not inspect protected main checkout")
    if status.stdout.strip():
        raise NeedsHumanError(
            "protected main checkout has uncommitted changes; refusing autonomous work"
        )
    ahead = run_cmd(
        ["git", "rev-list", "--count", "origin/main..HEAD"],
        check=False,
    )
    if ahead.returncode != 0:
        raise NeedsHumanError("could not compare protected main checkout with origin/main")
    try:
        ahead_count = int(ahead.stdout.strip())
    except ValueError as exc:
        raise NeedsHumanError(
            "could not parse protected main checkout divergence"
        ) from exc
    if ahead_count:
        raise NeedsHumanError(
            f"protected main checkout is ahead of origin/main by {ahead_count} commit(s); "
            "refusing autonomous work"
        )

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

def _stable_feedback(value):
    if isinstance(value, dict):
        return {key: _stable_feedback(item) for key, item in sorted(value.items())}
    if isinstance(value, list):
        normalized = [_stable_feedback(item) for item in value]
        return sorted(
            normalized,
            key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")),
        )
    return value

def pr_feedback_fingerprint(pr_url: str) -> str | None:
    checks = _get_pr_checks(pr_url)
    if checks is None:
        return None
    result = run_cmd(
        [
            "gh", "pr", "view", pr_url,
            "--repo", GITHUB_REPO,
            "--json", "headRefOid,comments,reviews",
        ],
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        feedback = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    payload = {
        "checks": sorted(
            (
                {"name": str(check.get("name") or ""), "bucket": str(check.get("bucket") or "")}
                for check in checks
            ),
            key=lambda check: (check["name"], check["bucket"]),
        ),
        "feedback": _stable_feedback(feedback),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

def wait_for_pr_checks(pr_url: str, record: RunRecord | None = None) -> bool:
    started = time.monotonic()
    if record is not None:
        run_step(record, "[CHECKS] Wait for PR checks", started=started)
    else:
        step("[CHECKS] Wait for PR checks")
    deadline = time.monotonic() + PR_CHECKS_MAX_WAIT
    checks = _get_pr_checks(pr_url)
    while checks == [] and time.monotonic() < deadline:
        delay = min(PR_CHECKS_INTERVAL, max(0, deadline - time.monotonic()))
        wait_with_status(
            "PR checks",
            lambda: (
                f"waiting for checks to register | "
                f"elapsed:{format_duration(time.monotonic() - started)} | "
                f"budget left:{format_duration(deadline - time.monotonic())}"
            ),
            max(1, int(delay)),
            style="magenta",
            sleep=time.sleep,
        )
        checks = _get_pr_checks(pr_url)

    if checks is None:
        warn("Could not query PR checks")
        return False
    if not checks:
        warn("Timed out before PR checks were reported")
        return False

    remaining = max(1, int(deadline - time.monotonic()))
    try:
        with elapsed_status(
            "Watching PR checks",
            style="magenta",
            details=lambda: (
                f"budget left:{format_duration(deadline - time.monotonic())}"
            ),
        ):
            run_cmd(
                [
                    "gh", "pr", "checks", pr_url, "--watch",
                    "--interval", str(PR_CHECKS_INTERVAL),
                ],
                check=False,
                timeout=remaining,
                verbose=True,
                retry=False,
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
    _show_pr_checks(checks, time.monotonic() - started)
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

def _get_issue_labels(issue_number: int) -> list[str] | None:
    result = run_cmd(
        [
            "gh", "issue", "view", str(issue_number),
            "--repo", GITHUB_REPO,
            "--json", "labels",
        ],
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    return [label.get("name", "") for label in data.get("labels", [])]

def _get_issue_snapshot(issue_number: int) -> dict | None:
    result = run_cmd(
        [
            "gh", "issue", "view", str(issue_number),
            "--repo", GITHUB_REPO,
            "--json", "state,labels,assignees,updatedAt",
        ],
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None

def extract_issue_references(text: str) -> set[int]:
    matches = re.findall(
        r"(?i)(?<!\d)#\s*(\d+)(?!\d)|\bissues?[\s/#:-]+(\d+)\b",
        text or "",
    )
    return {
        int(hash_reference or issue_reference)
        for hash_reference, issue_reference in matches
    }

def _get_related_open_prs(issue_number: int) -> list[dict] | None:
    result = run_cmd(
        [
            "gh", "pr", "list",
            "--repo", GITHUB_REPO,
            "--state", "open",
            "--search", f"{issue_number} in:title,body",
            "--json", "number,title,body,url,state",
        ],
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, list):
        return None
    return [
        pr
        for pr in data
        if issue_number in extract_issue_references(
            f"{pr.get('title') or ''}\n{pr.get('body') or ''}"
        )
    ]

def _base_sha() -> str:
    result = run_cmd(["git", "rev-parse", "origin/main"], check=False)
    return result.stdout.strip() if result.returncode == 0 else ""

def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None

def preflight_worker(record: RunRecord, store: RunStore) -> RunRecord:
    completed = store.find_completed_issue(
        record.repo,
        record.issue_number,
        exclude_run_id=record.run_id,
    )
    if completed:
        return store.update(
            record.run_id,
            status="skipped",
            stage="skipped",
            error=f"issue was already completed by run {completed.run_id}",
        )

    snapshot = _get_issue_snapshot(record.issue_number)
    if snapshot is None:
        raise RuntimeError(f"could not read issue #{record.issue_number} during preflight")
    if str(snapshot.get("state", "")).upper() != "OPEN":
        return store.update(
            record.run_id,
            status="skipped",
            stage="skipped",
            error="issue is no longer open",
        )

    labels = {
        label.get("name", "").lower()
        for label in snapshot.get("labels", [])
    }
    conflicts = [label for label in SKIP_LABELS if label.lower() in labels]
    if conflicts:
        return store.update(
            record.run_id,
            status="skipped",
            stage="skipped",
            error=f"issue has active-work label(s): {', '.join(conflicts)}",
        )

    assignees = [
        assignee.get("login", "")
        for assignee in snapshot.get("assignees", [])
        if assignee.get("login")
    ]
    if assignees:
        return store.update(
            record.run_id,
            status="skipped",
            stage="skipped",
            error=f"issue is assigned to: {', '.join(assignees)}",
        )

    prs = _get_related_open_prs(record.issue_number)
    if prs is None:
        raise RuntimeError(f"could not query related open PRs for issue #{record.issue_number}")
    if prs:
        return store.update(
            record.run_id,
            status="skipped",
            stage="skipped",
            error=f"related open PR already exists: {prs[0].get('url', '')}",
        )

    plan = dict(record.plan)
    planned_updated_at = plan.get("issue_updated_at")
    planned_at = _parse_timestamp(plan.get("planned_at"))
    issue_updated_at = _parse_timestamp(snapshot.get("updatedAt"))
    stale = (
        planned_at is not None
        and issue_updated_at is not None
        and issue_updated_at > planned_at
    )
    if planned_at is None and planned_updated_at:
        stale = planned_updated_at != snapshot.get("updatedAt")
    if stale:
        return store.update(
            record.run_id,
            status="queued",
            stage="queued",
            owner_pid=None,
            lease_role=None,
            lease_expires_at=None,
            error="analysis stale because issue changed",
        )

    predicted_files = plan.get("predicted_files") or []
    if predicted_files:
        record = store.claim_files(record.run_id, predicted_files)
    store.add_event(
        record.run_id,
        "worker_preflight_passed",
        stage=record.stage,
        status=record.status,
        data={"predicted_files": predicted_files},
    )
    return store.get(record.run_id)

def _label_exists(label: str) -> bool | None:
    result = run_cmd(
        [
            "gh", "label", "list",
            "--repo", GITHUB_REPO,
            "--search", label,
            "--limit", "100",
            "--json", "name",
        ],
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        labels = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    return any(
        str(candidate.get("name", "")).lower() == label.lower()
        for candidate in labels
    )

def _ensure_label_exists(label: str):
    # 1. Check if label already exists
    exists = _label_exists(label)
    if exists:
        return
    if exists is None:
        # Query failed, but don't abort — try to create anyway
        pass

    # 2. Create the label
    result = run_cmd(
        [
            "gh", "label", "create", label,
            "--repo", GITHUB_REPO,
            "--color", "FBCA04",
            "--description", "Agent-Z is actively working on this issue",
        ],
        check=False,
    )
    if result.returncode == 0:
        return

    # 3. Creation failed — maybe another process created it in the meantime
    if _label_exists(label):
        return

    details = (result.stderr or result.stdout).strip()
    raise NeedsHumanError(
        f"could not create or verify required label {label!r}: {details}"
    )

def mark_issue_with_skip_label(record: RunRecord, store: RunStore) -> RunRecord:
    if any(
        event.event_type == "issue_labeled_skip"
        for event in store.list_events(record.run_id)
    ):
        return record

    labels = _get_issue_labels(record.issue_number)
    if labels is None:
        raise RuntimeError(
            f"could not read labels for issue #{record.issue_number}; "
            "refusing to start development"
        )
    normalized = {label.lower() for label in labels or []}
    conflicting = [
        label for label in SKIP_LABELS
        if label.lower() in normalized
    ]
    if conflicting:
        raise RuntimeError(
            f"issue #{record.issue_number} already has skip label(s): "
            f"{', '.join(conflicting)}; "
            "skipping to avoid duplicate active work"
        )

    claim_label = SKIP_LABELS[0]
    _ensure_label_exists(claim_label)
    result = run_cmd(
        [
            "gh", "issue", "edit", str(record.issue_number),
            "--repo", GITHUB_REPO,
            "--add-label", claim_label,
        ],
        check=False,
    )
    if result.returncode != 0:
        details = (result.stderr or result.stdout).strip()
        raise NeedsHumanError(
            f"could not add label {claim_label!r} to issue "
            f"#{record.issue_number}: {details}"
        )
    updated_labels = _get_issue_labels(record.issue_number)
    if (
        updated_labels is None
        or claim_label.lower() not in {label.lower() for label in updated_labels}
    ):
        raise NeedsHumanError(
            f"could not verify label {claim_label!r} on issue "
            f"#{record.issue_number} after update"
        )

    store.add_event(
        record.run_id,
        "issue_labeled_skip",
        stage=record.stage,
        status=record.status,
        message=f"Added label {claim_label!r} to issue #{record.issue_number}",
        data={
            "label": claim_label,
            "skip_labels": SKIP_LABELS,
            "issue_number": record.issue_number,
        },
    )
    done(f"Marked issue #{record.issue_number} with {claim_label!r}")
    return store.get(record.run_id)

def _remove_run_claim_label(record: RunRecord, store: RunStore):
    events = store.list_events(record.run_id)
    label_event = next(
        (event for event in events if event.event_type == "issue_labeled_skip"),
        None,
    )
    if label_event is None:
        return
    label = str(label_event.data.get("label") or SKIP_LABELS[0])
    labels = _get_issue_labels(record.issue_number)
    if labels is None:
        raise RuntimeError(
            f"could not read labels for issue #{record.issue_number} during cleanup"
        )
    if label.lower() not in {item.lower() for item in labels}:
        return
    result = run_cmd(
        [
            "gh", "issue", "edit", str(record.issue_number),
            "--repo", GITHUB_REPO,
            "--remove-label", label,
        ],
        check=False,
    )
    updated_labels = None
    if result.returncode != 0:
        updated_labels = _get_issue_labels(record.issue_number)
        if (
            updated_labels is None
            or label.lower() in {item.lower() for item in updated_labels}
        ):
            details = (result.stderr or result.stdout).strip()
            raise RuntimeError(
                f"could not remove active-work label {label!r} from "
                f"issue #{record.issue_number}: {details}"
            )
    if updated_labels is None:
        updated_labels = _get_issue_labels(record.issue_number)
    if (
        updated_labels is None
        or label.lower() in {item.lower() for item in updated_labels}
    ):
        raise RuntimeError(
            f"could not verify removal of active-work label {label!r} "
            f"from issue #{record.issue_number}"
        )
    store.add_event(
        record.run_id,
        "issue_claim_label_removed",
        stage=record.stage,
        status=record.status,
        message=f"Removed active-work label {label!r}",
        data={"label": label, "issue_number": record.issue_number},
    )
    done(f"Released issue claim [dim]| run:{record.run_id} | label:{label}[/dim]")

def cleanup_run_artifacts(
    record: RunRecord,
    store: RunStore,
    worktrees: WorktreeManager,
    *,
    remove_worktree: bool,
    remove_label: bool,
) -> bool:
    failures = []
    if remove_label:
        try:
            _remove_run_claim_label(record, store)
        except Exception as exc:
            failures.append(str(exc))
    if remove_worktree and record.worktree_path:
        try:
            worktrees.remove(record.worktree_path)
            record = store.update(record.run_id, worktree_path=None)
            store.add_event(
                record.run_id,
                "worktree_removed",
                stage=record.stage,
                status=record.status,
                message="Removed run worktree",
            )
            done(f"Removed worktree [dim]| run:{record.run_id}[/dim]")
        except Exception as exc:
            failures.append(f"worktree cleanup failed: {exc}")
    if failures:
        message = "; ".join(failures)
        store.add_event(
            record.run_id,
            "cleanup_failed",
            stage=record.stage,
            status=record.status,
            message=message,
        )
        warn(message)
        return False
    return True







