import json

from agents.base import log, run_cmd, warn
from agents.developer import DeveloperAgent
from config import GITHUB_REPO
from orchestration.errors import NeedsHumanError
from orchestration.store import RunRecord, RunStore

def _verify_pr_url(pr_url: str) -> str:
    if not pr_url:
        return ""
    result = run_cmd(
        [
            "gh", "pr", "view", pr_url,
            "--repo", GITHUB_REPO,
            "--json", "url,state",
        ],
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return ""
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return ""
    return str(data.get("url", ""))

def _find_open_pr_for_branch(branch: str | None) -> str:
    if not branch:
        return ""
    result = run_cmd(
        [
            "gh", "pr", "list",
            "--repo", GITHUB_REPO,
            "--head", branch,
            "--state", "open",
            "--limit", "10",
            "--json", "url,state",
        ],
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return ""
    try:
        prs = json.loads(result.stdout)
    except json.JSONDecodeError:
        return ""
    if not isinstance(prs, list):
        return ""
    for pr in prs:
        if str(pr.get("state", "")).upper() == "OPEN" and pr.get("url"):
            return str(pr["url"])
    return ""

def _issue_title_for_pr(issue_number: int) -> str:
    result = run_cmd(
        [
            "gh", "issue", "view", str(issue_number),
            "--repo", GITHUB_REPO,
            "--json", "title",
        ],
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return f"Resolve issue #{issue_number}"
    try:
        return str(json.loads(result.stdout).get("title") or f"Resolve issue #{issue_number}")
    except json.JSONDecodeError:
        return f"Resolve issue #{issue_number}"

def _normalize_submission_metadata(record: RunRecord, metadata: dict | None) -> dict:
    metadata = metadata or {}
    def first_line(value) -> str:
        lines = str(value or "").splitlines()
        return lines[0].strip() if lines else ""

    commit_message = first_line(metadata.get("commit_message"))
    pr_title = first_line(metadata.get("pr_title"))
    pr_body = str(metadata.get("pr_body") or "").strip()
    commit_message = commit_message[:200] or f"fix: resolve issue #{record.issue_number}"
    pr_title = pr_title[:256] or _issue_title_for_pr(record.issue_number)
    if not pr_body:
        pr_body = f"Resolve issue #{record.issue_number}."
    pr_body = pr_body[:9000]
    closing = f"Closes #{record.issue_number}"
    if closing.lower() not in pr_body.lower():
        pr_body = f"{pr_body}\n\n{closing}"
    run_marker = f"Agent-Z run: `{record.run_id}`"
    if run_marker.lower() not in pr_body.lower():
        pr_body += f"\n\n{run_marker}"
    return {
        "commit_message": commit_message,
        "pr_title": pr_title,
        "pr_body": pr_body,
    }

def _prepare_submission_metadata(
    record: RunRecord,
    developer: DeveloperAgent | None,
    store: RunStore | None,
) -> dict:
    if store is not None:
        for event in reversed(store.list_events(record.run_id)):
            if event.event_type == "submission_metadata_prepared":
                metadata = _normalize_submission_metadata(record, event.data)
                store.add_event(
                    record.run_id,
                    "submission_metadata_reused",
                    stage=record.stage,
                    status=record.status,
                    message="Reused persisted commit and PR metadata",
                )
                return metadata
    generated = {}
    if developer is not None:
        try:
            generated = developer.prepare_submission(
                record.issue_number,
                plan=record.plan,
                resume_session=bool(developer.session_id),
            )
        except Exception as exc:
            warn(f"Task Lead could not generate submission metadata; using fallback: {exc}")
    metadata = _normalize_submission_metadata(record, generated)
    if store is not None:
        store.add_event(
            record.run_id,
            "submission_metadata_prepared",
            stage=record.stage,
            status=record.status,
            message="Prepared commit and PR metadata",
            data=metadata,
        )
    return metadata

def _create_pr_deterministically(record: RunRecord, metadata: dict | None = None) -> str:
    if not record.worktree_path or not record.branch:
        raise NeedsHumanError("cannot create PR without a worktree and branch")
    metadata = _normalize_submission_metadata(record, metadata)

    status = run_cmd(
        ["git", "status", "--porcelain"],
        cwd=record.worktree_path,
        check=False,
    )
    if status.returncode != 0:
        raise NeedsHumanError("could not inspect worktree before deterministic submission")
    if status.stdout.strip():
        run_cmd(["git", "add", "-A"], cwd=record.worktree_path)
        commit = run_cmd(
            ["git", "commit", "-m", metadata["commit_message"]],
            cwd=record.worktree_path,
            check=False,
        )
        if commit.returncode != 0:
            details = (commit.stderr or commit.stdout).strip()
            raise NeedsHumanError(f"could not commit changes before PR creation: {details}")

    push = run_cmd(
        ["git", "push", "--set-upstream", "origin", record.branch],
        cwd=record.worktree_path,
        check=False,
    )
    if push.returncode != 0:
        details = (push.stderr or push.stdout).strip()
        raise NeedsHumanError(f"could not push branch before PR creation: {details}")

    existing = _find_open_pr_for_branch(record.branch)
    if existing:
        return existing

    result = run_cmd(
        [
            "gh", "pr", "create",
            "--repo", GITHUB_REPO,
            "--base", "main",
            "--head", record.branch,
            "--title", metadata["pr_title"],
            "--body", metadata["pr_body"],
        ],
        cwd=record.worktree_path,
        check=False,
    )
    if result.returncode != 0:
        details = (result.stderr or result.stdout).strip()
        raise NeedsHumanError(f"deterministic PR creation failed: {details}")
    candidate = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
    return _verify_pr_url(candidate) or _find_open_pr_for_branch(record.branch)

def resolve_submission(
    record: RunRecord,
    developer: DeveloperAgent | None = None,
    store: RunStore | None = None,
) -> tuple[str, str]:
    existing = _find_open_pr_for_branch(record.branch)
    if existing:
        return existing, "external_existing"

    log("Coordinator is creating the PR deterministically")
    metadata = _prepare_submission_metadata(record, developer, store)
    created = _create_pr_deterministically(record, metadata)
    return created, "coordinator_created" if created else ""
