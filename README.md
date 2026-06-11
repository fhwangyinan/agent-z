# Agent-Z

[中文文档](README.zh.md)

Lightweight coding-agent-driven automation for autonomous development loops.

Agent-Z orchestrates multiple specialized agents (analyst, developer, reviewer, submitter) powered by Claude Code, forming a fully autonomous cycle: analyze issues → fix code → review locally → open PR → iterate on CI feedback.

```
Analyst → Developer → Reviewer → Submitter → CodeRabbit → Developer → ...
```

## Prerequisites

- Python 3.11+
- [GitHub CLI](https://cli.github.com/) (`gh`) authenticated
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) (`claude`) installed
- [CodeRabbitAI](https://coderabbit.ai/) GitHub App on target repo

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate      # Windows
source .venv/bin/activate   # Linux/macOS
pip install -r requirements.txt
cp .env.example .env        # edit with your settings
```

## Usage

```bash
python run.py               # interactive, confirmation prompts each round
python run.py --loop 5      # autonomous: 5 rounds, no prompts
```

Each round the agent team autonomously:

1. Picks the best open issue (or you specify one)
2. Reads the codebase and fixes it
3. Local reviewer validates the fix
4. Opens a PR
5. Waits for CI / CodeRabbit feedback, fixes, pushes — repeats until approved

## How It Works

```
run.py                    Orchestrator — manages the loop and session lifecycle
config.py                 Config loaded from .env
agents/
  base.py                 Agent base class with context-aware runner
  analyst.py              Analyzes repo issues and recommends what to fix
  developer.py            Reads code, writes fixes, handles review feedback
  reviewer.py             Local code review (diff + tests) before push
  submitter.py            Branches, commits, pushes, opens PR
  runners/
    base.py               Abstract runner interface — swap backends easily
    claude.py             Claude Code runner (`claude -p`)
```

All agents share a single session via `--continue`, so context flows naturally — the developer sees the analyst's findings, the reviewer sees the developer's changes. No re-reading, no copy-pasting prompts.

## Configuration

See `.env.example`. Key options:

| Variable | Description | Default |
|----------|-------------|---------|
| `PROJECT_DIR` | Target project | — |
| `GITHUB_REPO` | owner/repo | — |
| `TIMEOUT_DEVELOPER` | Dev agent timeout (s) | 10800 |
| `CODERABBIT_MAX_WAIT` | Max wait for review (s) | 600 |
| `MAX_REVIEW_ROUNDS` | Max review-fix loops | 3 |

## Bring Your Own Agent

Swap the backend by implementing `AgentRunner`:

```python
from agents.runners.base import AgentRunner

class GeminiRunner(AgentRunner):
    def execute(self, prompt, timeout, cwd, continue_session):
        ...  # call your CLI

# agents/base.py
DEFAULT_RUNNER = GeminiRunner()
```
