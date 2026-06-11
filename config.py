# 从 .env 文件加载配置，未设置的项使用默认值
# .env 不做版本管理，参见 .env.example

import os

_BASE = os.path.dirname(os.path.abspath(__file__))


def _load_dotenv():
    """简单解析 .env，不依赖外部库"""
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


def _get_int(key: str, default: int) -> int:
    v = os.environ.get(key)
    return int(v) if v else default


# ---- 项目配置 ----
PROJECT_DIR = _get("PROJECT_DIR", r"G:\Code\workspace_aieng")
GITHUB_REPO = _get("GITHUB_REPO", "armpro24-blip/cad-cae-copilot")

# ---- Claude Runner 配置 ----
CLAUDE_FLAGS = _get("CLAUDE_FLAGS", "--dangerously-skip-permissions").split()

# ---- 超时配置 (秒) ----
TIMEOUT_ANALYST = _get_int("TIMEOUT_ANALYST", 3600)
TIMEOUT_DEVELOPER = _get_int("TIMEOUT_DEVELOPER", 10800)
TIMEOUT_REVIEWER = _get_int("TIMEOUT_REVIEWER", 1800)
TIMEOUT_SUBMITTER = _get_int("TIMEOUT_SUBMITTER", 600)
RETRY_TIMEOUT = _get_int("RETRY_TIMEOUT", 3600)

# ---- CodeRabbit 配置 ----
CODERABBIT_POLL_INTERVAL = _get_int("CODERABBIT_POLL_INTERVAL", 45)
CODERABBIT_MAX_WAIT = _get_int("CODERABBIT_MAX_WAIT", 900)
MAX_REVIEW_ROUNDS = _get_int("MAX_REVIEW_ROUNDS", 5)
MAX_LOCAL_REVIEW_ROUNDS = _get_int("MAX_LOCAL_REVIEW_ROUNDS", 5)
