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


# ---- Project ----
PROJECT_DIR = _get("PROJECT_DIR", r"G:\Code\workspace_aieng")
GITHUB_REPO = _get("GITHUB_REPO", "armpro24-blip/cad-cae-copilot")
AGENT_Z_HOME = _get("AGENT_Z_HOME", os.path.join(_BASE, ".agent-z"))
STATE_DB = _get("STATE_DB", os.path.join(AGENT_Z_HOME, "state.db"))
WORKTREE_ROOT = _get("WORKTREE_ROOT", os.path.join(AGENT_Z_HOME, "worktrees"))

# ---- Agent backends ----
DEFAULT_BACKEND = _get("DEFAULT_BACKEND", "claude").lower()
ANALYST_BACKEND = _get("ANALYST_BACKEND", DEFAULT_BACKEND).lower()
DEVELOPER_BACKEND = _get("DEVELOPER_BACKEND", DEFAULT_BACKEND).lower()
REVIEWER_BACKEND = _get("REVIEWER_BACKEND", DEFAULT_BACKEND).lower()
SUBMITTER_BACKEND = _get("SUBMITTER_BACKEND", DEFAULT_BACKEND).lower()

CLAUDE_FLAGS = shlex.split(_get("CLAUDE_FLAGS", "--dangerously-skip-permissions"))
CODEX_FLAGS = shlex.split(_get("CODEX_FLAGS", "--dangerously-bypass-approvals-and-sandbox"))
OPENCODE_FLAGS = shlex.split(_get("OPENCODE_FLAGS", ""))

# ---- Timeouts (seconds) ----
TIMEOUT_ANALYST = _get_positive_int("TIMEOUT_ANALYST", 3600)
TIMEOUT_DEVELOPER = _get_positive_int("TIMEOUT_DEVELOPER", 10800)
TIMEOUT_REVIEWER = _get_positive_int("TIMEOUT_REVIEWER", 1800)
TIMEOUT_SUBMITTER = _get_positive_int("TIMEOUT_SUBMITTER", 600)
RETRY_TIMEOUT = _get_positive_int("RETRY_TIMEOUT", 3600)

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
MAX_RUN_SECONDS = _get_positive_int("MAX_RUN_SECONDS", 21600)
CLEANUP_COMPLETED_WORKTREES = _get_bool("CLEANUP_COMPLETED_WORKTREES", True)
WORKER_IDLE_SLEEP = _get_positive_int("WORKER_IDLE_SLEEP", 30)
