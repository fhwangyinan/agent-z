# Agent-Z

[中文文档](README.zh.md)

Lightweight, backend-agnostic coding-agent automation for autonomous development loops.

Agent-Z orchestrates specialized agents powered by Claude Code, Codex, or OpenCode, forming a fully autonomous cycle: pick issue → assess impact → fix code → review locally → open PR → wait for and iterate on PR checks.

```
Analyst → Impact Assessment → Developer → Reviewer → Submitter → PR Checks → Developer → ...
```

## Prerequisites

- Python 3.11+
- [GitHub CLI](https://cli.github.com/) (`gh`) authenticated
- At least one supported coding-agent CLI installed: `claude`, `codex`, or `opencode`
- Optional: [CodeRabbitAI](https://coderabbit.ai/) GitHub App on target repo

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
python run.py --issue 123           # run one unattended issue in an isolated worktree
python run.py --enqueue 123         # add an issue to the persistent queue
python run.py --run-next            # claim and run the oldest queued issue
python run.py --resume RUN_ID       # resume from the persisted workflow stage
python run.py --list-runs           # inspect recent and active runs
python run.py --cancel RUN_ID       # release locks and remove an abandoned worktree
```

`--cancel` refuses to remove a task that is still owned by a live Agent-Z process. Use it for queued, stopped, or abandoned runs.

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
| `--issue N` | Run one issue immediately |
| `--enqueue N` | Add an issue to the SQLite queue |
| `--run-next` | Claim the oldest queued task when a slot is available |
| `--resume RUN_ID` | Resume a failed or interrupted task |
| `--keep-worktree` | Keep a completed task's worktree |

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
4. **Isolate** — Create a dedicated branch and Git worktree for the run
5. **Develop** — Fix code in the isolated worktree
6. **Review** — Independent local review; findings are explicitly handed back to Developer
7. **Submit** — Commit, push, open PR
8. **PR Checks** — Wait for all checks with `gh pr checks --watch` → Developer reads CI and review feedback → fixes → local Reviewer validates → push → repeat until no action is needed

## Architecture

```
run.py                    Orchestrator — loop control and session management
config.py                 Configuration loaded from .env
orchestration/
  store.py                SQLite queue, workflow state, issue and file locks
  worktree.py             Isolated worktree lifecycle
agents/
  base.py                 Agent base class with pluggable runner
  analyst.py              Issue analysis, impact assessment, Q&A
  developer.py            Code fixes, review handling
  reviewer.py             Local code review (diff + tests)
  submitter.py            Branch, commit, push, PR
  runners/
    base.py               Backend contract, capabilities, and AgentResult
    claude.py             Claude Code adapter with explicit session resume
    codex.py              Codex CLI adapter with JSONL event parsing
    opencode.py           OpenCode adapter with JSON event parsing
```

Each role owns an independent backend session. Analyst, Developer, Reviewer, and Submitter can use different CLIs. The workspace, GitHub issue, PR feedback, and explicit review findings provide cross-role context without sharing an implicit latest session.

Every persisted run has a unique ID and workflow stage. Separate Agent-Z processes can safely execute tasks in parallel up to `MAX_PARALLEL_TASKS`; SQLite enforces Issue locks, and changed-file claims stop overlapping active tasks before submission. Failed, interrupted, and `needs_human` runs keep their worktrees for inspection and resume.

## Configuration

Copy `.env.example` to `.env` and edit. Full options:

| Variable | Description | Default |
|----------|-------------|---------|
| `PROJECT_DIR` | Target project path | — |
| `GITHUB_REPO` | Target repo (owner/repo) | — |
| `AGENT_Z_HOME` | Runtime state directory | `.agent-z` |
| `STATE_DB` | SQLite state database | `.agent-z/state.db` |
| `WORKTREE_ROOT` | Isolated worktree directory | `.agent-z/worktrees` |
| `DEFAULT_BACKEND` | Default backend: `claude`, `codex`, or `opencode` | `claude` |
| `ANALYST_BACKEND` | Analyst backend override | `DEFAULT_BACKEND` |
| `DEVELOPER_BACKEND` | Developer backend override | `DEFAULT_BACKEND` |
| `REVIEWER_BACKEND` | Reviewer backend override | `DEFAULT_BACKEND` |
| `SUBMITTER_BACKEND` | Submitter backend override | `DEFAULT_BACKEND` |
| `CLAUDE_FLAGS` | Claude Code flags | `--dangerously-skip-permissions` |
| `CODEX_FLAGS` | Codex CLI flags | `--dangerously-bypass-approvals-and-sandbox` |
| `OPENCODE_FLAGS` | OpenCode CLI flags | — |
| `TIMEOUT_ANALYST` | Analyst timeout (s) | 3600 |
| `TIMEOUT_DEVELOPER` | Developer timeout (s) | 10800 |
| `TIMEOUT_REVIEWER` | Reviewer timeout (s) | 1800 |
| `TIMEOUT_SUBMITTER` | Submitter timeout (s) | 600 |
| `RETRY_TIMEOUT` | Retry timeout (s) | 3600 |
| `PR_CHECKS_INTERVAL` | PR checks watch interval (s) | 10 |
| `PR_CHECKS_MAX_WAIT` | Max wait for PR checks (s) | 900 |
| `MAX_REVIEW_ROUNDS` | Max review-fix loops | 5 |
| `MAX_LOCAL_REVIEW_ROUNDS` | Max local review loops | 5 |
| `MAX_PARALLEL_TASKS` | Maximum active tasks across processes | 2 |
| `MAX_RUN_SECONDS` | Per-attempt runtime budget | 21600 |
| `CLEANUP_COMPLETED_WORKTREES` | Remove worktrees after successful completion | `true` |

Legacy variables `CODERABBIT_POLL_INTERVAL` and `CODERABBIT_MAX_WAIT` remain supported as fallbacks.

## Backend Selection

Use one backend for every role:

```env
DEFAULT_BACKEND=codex
```

Or mix backends while keeping sessions isolated:

```env
ANALYST_BACKEND=claude
DEVELOPER_BACKEND=claude
REVIEWER_BACKEND=codex
SUBMITTER_BACKEND=claude
```

Only selected backends are required at startup. If `opencode` is configured but not available on `PATH`, Agent-Z fails fast with a clear error.
