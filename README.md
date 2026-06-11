# Agent-Z

[中文文档](README.zh.md)

Lightweight coding-agent-driven automation for autonomous development loops.

Agent-Z orchestrates multiple specialized agents powered by Claude Code, forming a fully autonomous cycle: analyze issues → assess impact → fix code → review locally → open PR → iterate on CI feedback.

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
python run.py               # interactive mode: confirm & Q&A each round
python run.py --loop 5      # autonomous mode: 5 rounds, no prompts
```

### Interactive Mode

Each round you can:
- Let the agent auto-recommend an issue, or specify one by number
- Review the impact assessment and ask the Analyst follow-up questions
- Type `skip` during Q&A to skip to the next issue
- Type `done` or press Enter to start development

### Autonomous Mode

- Agent auto-recommends and fixes issues without prompts
- Risk assessment: issues rated **medium or above** are automatically skipped
- Ideal for cron jobs and scheduled tasks

## Workflow

Each round runs through:

1. **Pick Issue** — Agent recommends the best open issue (or you specify one)
2. **Impact Assessment** — Assesses potential impact and assigns a risk level:
   - `very_low` — no impact
   - `low` — minor impact
   - `medium` — moderate impact
   - `high` — significant (behavior/API changes)
   - `very_high` — destructive (alters existing workflows or outputs)
   - Result is posted as an English comment on the issue
3. **Q&A** (interactive only) — Discuss impacts with the Analyst before committing
4. **Develop** — Fix the code (shares session context with Analyst via `--continue`)
5. **Review** — Local code review (diff + tests); Developer fixes feedback
6. **Submit** — Create branch, commit, push, open PR
7. **CodeRabbit** — Wait for check → Developer reads review → fix → local Reviewer validates → push + @coderabbitai → repeat until approved or `NO_ACTION_NEEDED`

## Architecture

```
run.py                    Orchestrator — loop control and session management
config.py                 Configuration loaded from .env
agents/
  base.py                 Agent base class with pluggable runner
  analyst.py              Analyzes issues, assesses impact, answers Q&A
  developer.py            Reads code, writes fixes, handles review feedback
  reviewer.py             Local code review (git diff + tests) before pushing
  submitter.py            Creates branch, commits, pushes, opens PR
  runners/
    base.py               Abstract AgentRunner interface
    claude.py             ClaudeRunner — claude -p implementation
```

All agents share a single Claude Code session via `--continue`, so context flows naturally — the Developer sees the Analyst's findings and impact assessment, the Reviewer sees the Developer's changes. No redundant re-reading.

## Configuration

See `.env.example` for all options.

| Variable | Description | Default |
|----------|-------------|---------|
| `PROJECT_DIR` | Target project path | — |
| `GITHUB_REPO` | Target repo (owner/repo) | — |
| `CLAUDE_FLAGS` | Claude Code flags | `--dangerously-skip-permissions` |
| `TIMEOUT_ANALYST` | Analyst timeout (s) | 3600 |
| `TIMEOUT_DEVELOPER` | Developer timeout (s) | 10800 |
| `TIMEOUT_REVIEWER` | Reviewer timeout (s) | 1800 |
| `TIMEOUT_SUBMITTER` | Submitter timeout (s) | 600 |
| `RETRY_TIMEOUT` | Retry after failure (s) | 3600 |
| `CODERABBIT_POLL_INTERVAL` | Poll interval (s) | 45 |
| `CODERABBIT_MAX_WAIT` | Max wait for CodeRabbit (s) | 900 |
| `MAX_REVIEW_ROUNDS` | Max review-fix loops | 5 |
| `MAX_LOCAL_REVIEW_ROUNDS` | Max local review loops | 5 |

## Custom Agent Backend

Implement `AgentRunner` to swap the LLM backend:

```python
from agents.runners.base import AgentRunner

class CustomRunner(AgentRunner):
    def execute(self, prompt, timeout, cwd, continue_session):
        ...  # call your CLI

# agents/base.py
DEFAULT_RUNNER = CustomRunner()
```
