# Agent-Z

[中文文档](README.zh.md)

Lightweight coding-agent-driven automation for autonomous development loops.

Agent-Z orchestrates multiple specialized agents powered by Claude Code, forming a fully autonomous cycle: pick issue → assess impact → fix code → review locally → open PR → iterate on CI feedback.

```
Analyst → Impact Assessment → Developer → Reviewer → Submitter → CodeRabbit → Developer → ...
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
python run.py                       # interactive: confirm & Q&A each round
python run.py --loop 5              # autonomous: 5 rounds, skip high-risk issues
python run.py --loop 5 --force      # autonomous: develop all issues regardless of risk
```

### Interactive Mode

- Let the agent auto-recommend an issue, or specify one by number
- Review impact assessment and Q&A with the Analyst
- Type `skip` to move on, `done` or Enter to start development

### Autonomous Mode

- Agent auto-recommends and fixes issues without prompts
- Risk assessment: issues rated **high / very_high** are skipped (unless `--force` is used)
- Available flags:

| Flag | Effect |
|------|--------|
| `--loop N` | Run N rounds automatically |
| `--force` | Ignore risk levels, develop all issues |

## Workflow

Each round:

1. **Pick Issue** — Agent recommends the best open issue (filtering out issues with existing complete PRs)
2. **Impact Assessment** — Analyzes potential impact, assigns a risk level, writes English report to the issue:
   - `very_low` — no impact
   - `low` — minor impact
   - `medium` — moderate impact
   - `high` — significant (behavior/API changes) → auto-skipped
   - `very_high` — destructive (alters workflows/outputs) → auto-skipped
   - If an assessment already exists, updates it rather than creating a duplicate
3. **Q&A** (interactive only) — Discuss impacts with the Analyst; `skip` to move on
4. **Develop** — Fix code (shares session with Analyst via `--continue`)
5. **Review** — Local code review (git diff + tests); Developer fixes feedback
6. **Submit** — Branch, commit, push, open PR
7. **CodeRabbit** — Wait for check → Developer reads review → fix → local Reviewer validates → push + @coderabbitai → repeat until approved or `NO_ACTION_NEEDED`

## Architecture

```
run.py                    Orchestrator — loop control and session management
config.py                 Configuration loaded from .env
agents/
  base.py                 Agent base class with pluggable runner
  analyst.py              Issue analysis, impact assessment, Q&A
  developer.py            Code fixes, review handling
  reviewer.py             Local code review (diff + tests)
  submitter.py            Branch, commit, push, PR
  runners/
    base.py               Abstract AgentRunner interface
    claude.py             ClaudeRunner — claude -p backend
```

All agents share a single Claude Code session via `--continue`, so context flows naturally — no redundant re-reading.

## Configuration

Copy `.env.example` to `.env` and edit. Full options:

| Variable | Description | Default |
|----------|-------------|---------|
| `PROJECT_DIR` | Target project path | — |
| `GITHUB_REPO` | Target repo (owner/repo) | — |
| `CLAUDE_FLAGS` | Claude Code flags | `--dangerously-skip-permissions` |
| `TIMEOUT_ANALYST` | Analyst timeout (s) | 3600 |
| `TIMEOUT_DEVELOPER` | Developer timeout (s) | 10800 |
| `TIMEOUT_REVIEWER` | Reviewer timeout (s) | 1800 |
| `TIMEOUT_SUBMITTER` | Submitter timeout (s) | 600 |
| `RETRY_TIMEOUT` | Retry timeout (s) | 3600 |
| `CODERABBIT_POLL_INTERVAL` | Poll interval (s) | 45 |
| `CODERABBIT_MAX_WAIT` | Max wait for CodeRabbit (s) | 900 |
| `MAX_REVIEW_ROUNDS` | Max review-fix loops | 5 |
| `MAX_LOCAL_REVIEW_ROUNDS` | Max local review loops | 5 |

## Custom Agent Backend

Implement `AgentRunner` to swap the LLM:

```python
from agents.runners.base import AgentRunner

class CustomRunner(AgentRunner):
    def execute(self, prompt, timeout, cwd, continue_session):
        ...

# agents/base.py
DEFAULT_RUNNER = CustomRunner()
```
