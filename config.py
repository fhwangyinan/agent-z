# Load configuration from .env. Unset values use defaults.

import os
import shlex

_BASE = os.path.dirname(os.path.abspath(__file__))


def _load_dotenv():
    """Parse a small .env file without an external dependency."""
    env_path = os.path.join(_BASE, ".env")
    if not os.path.exists(env_path):
        return
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k not in os.environ:
                os.environ[k] = v


_load_dotenv()


def _get(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def _get_positive_int(key: str, default: int, fallback_key: str | None = None) -> int:
    raw = os.environ.get(key)
    source = key
    if not raw and fallback_key:
        raw = os.environ.get(fallback_key)
        source = fallback_key
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{source} must be a positive integer, got {raw!r}") from exc
    if value <= 0:
        raise ValueError(f"{source} must be a positive integer, got {raw!r}")
    return value


def _get_bool(key: str, default: bool) -> bool:
    raw = os.environ.get(key)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{key} must be a boolean, got {raw!r}")


def _get_csv(key: str, default: str) -> list[str]:
    raw = os.environ.get(key, default)
    return [part.strip() for part in raw.split(",") if part.strip()]


# ---- Project ----
PROJECT_DIR = _get("PROJECT_DIR", r"G:\Code\workspace_aieng")
GITHUB_REPO = _get("GITHUB_REPO", "armpro24-blip/cad-cae-copilot")
AGENT_Z_HOME = _get("AGENT_Z_HOME", os.path.join(_BASE, ".agent-z"))
STATE_DB = _get("STATE_DB", os.path.join(AGENT_Z_HOME, "state.db"))
WORKTREE_ROOT = _get("WORKTREE_ROOT", os.path.join(AGENT_Z_HOME, "worktrees"))
SKIP_LABELS = _get_csv("SKIP_LABELS", "ongoing")

# ---- Agent backends ----
DEFAULT_BACKEND = _get("DEFAULT_BACKEND", "claude").lower()
TASK_LEAD_BACKEND = _get(
    "TASK_LEAD_BACKEND",
    _get("ANALYST_BACKEND", DEFAULT_BACKEND),
).lower()
ANALYST_BACKEND = TASK_LEAD_BACKEND
DEVELOPER_BACKEND = TASK_LEAD_BACKEND
REVIEWER_BACKEND = _get("REVIEWER_BACKEND", DEFAULT_BACKEND).lower()

CLAUDE_FLAGS = shlex.split(_get("CLAUDE_FLAGS", "--dangerously-skip-permissions"))
CODEX_FLAGS = shlex.split(_get("CODEX_FLAGS", "--dangerously-bypass-approvals-and-sandbox"))
OPENCODE_FLAGS = shlex.split(_get("OPENCODE_FLAGS", ""))

# ---- Timeouts (seconds) ----
TIMEOUT_ANALYST = _get_positive_int("TIMEOUT_ANALYST", 3600)
TIMEOUT_DEVELOPER = _get_positive_int("TIMEOUT_DEVELOPER", 10800)
TIMEOUT_REVIEWER = _get_positive_int("TIMEOUT_REVIEWER", 1800)
RETRY_TIMEOUT = _get_positive_int("RETRY_TIMEOUT", 3600)
GITHUB_RETRY_ATTEMPTS = _get_positive_int("GITHUB_RETRY_ATTEMPTS", 3)
GITHUB_RETRY_BASE_DELAY = _get_positive_int("GITHUB_RETRY_BASE_DELAY", 2)
GITHUB_RETRY_MAX_DELAY = _get_positive_int("GITHUB_RETRY_MAX_DELAY", 30)
GITHUB_COMMAND_TIMEOUT = _get_positive_int("GITHUB_COMMAND_TIMEOUT", 60)

# ---- PR checks ----
# Legacy CODERABBIT_* names remain supported as fallbacks.
PR_CHECKS_INTERVAL = _get_positive_int(
    "PR_CHECKS_INTERVAL", 10, fallback_key="CODERABBIT_POLL_INTERVAL"
)
PR_CHECKS_MAX_WAIT = _get_positive_int(
    "PR_CHECKS_MAX_WAIT", 900, fallback_key="CODERABBIT_MAX_WAIT"
)
MAX_REVIEW_ROUNDS = _get_positive_int("MAX_REVIEW_ROUNDS", 5)
MAX_LOCAL_REVIEW_ROUNDS = _get_positive_int("MAX_LOCAL_REVIEW_ROUNDS", 5)

# ---- Run control ----
MAX_PARALLEL_TASKS = _get_positive_int("MAX_PARALLEL_TASKS", 2)
SERVICE_WORKERS = _get_positive_int("SERVICE_WORKERS", MAX_PARALLEL_TASKS)
SERVICE_RESTART_DELAY = _get_positive_int("SERVICE_RESTART_DELAY", 5)
SERVICE_RESTART_MAX_DELAY = _get_positive_int("SERVICE_RESTART_MAX_DELAY", 60)
SERVICE_RESTART_MAX_ATTEMPTS = _get_positive_int("SERVICE_RESTART_MAX_ATTEMPTS", 5)
SERVICE_RESTART_RESET_SECONDS = _get_positive_int("SERVICE_RESTART_RESET_SECONDS", 300)
SERVICE_LOG_MAX_BYTES = _get_positive_int("SERVICE_LOG_MAX_BYTES", 5 * 1024 * 1024)
SERVICE_LOG_BACKUPS = _get_positive_int("SERVICE_LOG_BACKUPS", 3)
MAX_RUN_SECONDS = _get_positive_int("MAX_RUN_SECONDS", 21600)
CLEANUP_COMPLETED_WORKTREES = _get_bool("CLEANUP_COMPLETED_WORKTREES", True)
CLEANUP_FAILED_WORKTREES = _get_bool("CLEANUP_FAILED_WORKTREES", False)
WORKER_IDLE_SLEEP = _get_positive_int("WORKER_IDLE_SLEEP", 30)
WORKER_PREFLIGHT_MAX_RETRIES = _get_positive_int("WORKER_PREFLIGHT_MAX_RETRIES", 3)
SUBMISSION_NO_CHANGES_MAX_RETRIES = _get_positive_int(
    "SUBMISSION_NO_CHANGES_MAX_RETRIES", 1
)
SUBMISSION_USE_AGENT_METADATA = _get_bool("SUBMISSION_USE_AGENT_METADATA", False)
PLANNER_IDLE_SLEEP = _get_positive_int("PLANNER_IDLE_SLEEP", 30)
PLANNER_MAX_RETRIES = _get_positive_int("PLANNER_MAX_RETRIES", 3)
PLANNER_RETRY_BASE_DELAY = _get_positive_int("PLANNER_RETRY_BASE_DELAY", 10)
SCHEDULER_IDLE_SLEEP = _get_positive_int("SCHEDULER_IDLE_SLEEP", 60)
SCHEDULER_EMPTY_QUEUE_REEVALUATE_SECONDS = _get_positive_int(
    "SCHEDULER_EMPTY_QUEUE_REEVALUATE_SECONDS", 1800
)
SCHEDULER_BATCH_SIZE = _get_positive_int("SCHEDULER_BATCH_SIZE", 10)
SCHEDULER_ISSUE_LIMIT = _get_positive_int("SCHEDULER_ISSUE_LIMIT", 100)
SCHEDULER_PR_LIMIT = _get_positive_int("SCHEDULER_PR_LIMIT", 500)
SCHEDULER_AGENT_CANDIDATE_LIMIT = _get_positive_int(
    "SCHEDULER_AGENT_CANDIDATE_LIMIT",
    SCHEDULER_ISSUE_LIMIT,
)
SCHEDULER_ELIGIBLE_LABELS = _get_csv("SCHEDULER_ELIGIBLE_LABELS", "")
SCHEDULER_BLOCK_LABELS = _get_csv("SCHEDULER_BLOCK_LABELS", "blocked")
SCHEDULER_SKIP_ASSIGNED_ISSUES = _get_bool("SCHEDULER_SKIP_ASSIGNED_ISSUES", True)
SCHEDULER_PRIORITY_LABELS = _get_csv(
    "SCHEDULER_PRIORITY_LABELS",
    "priority:critical,priority:high,priority:medium,priority:low",
)
RECONCILER_INTERVAL = _get_positive_int("RECONCILER_INTERVAL", 60)
PLANNER_LEASE_SECONDS = _get_positive_int("PLANNER_LEASE_SECONDS", 7200)
WORKER_LEASE_SECONDS = _get_positive_int("WORKER_LEASE_SECONDS", MAX_RUN_SECONDS)
