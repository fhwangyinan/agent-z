# Agent-Z

[中文文档](README.zh.md)

Lightweight, backend-agnostic coding-agent automation for autonomous development loops.

Agent-Z combines a deterministic control plane with a flexible Task Lead, an independent Reviewer, scalable Workers, and a Reconciler, powered by Claude Code, Codex, or OpenCode.

```
Open Issues/PRs ← on-demand exploration ← Task Lead (select + plan + develop)
                                         ↓
Durable Queue → Worker Pool → Independent Reviewer → Deterministic Coordinator
                     ↘              Reconciler              ↗
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

For normal operation, only three commands are needed:

```bash
python run.py --serve              # recommended: start the complete autonomous service
python run.py --list-runs          # list recent runs
python run.py --inspect RUN_ID     # inspect one run and its event timeline
```

`--serve` starts one Scheduler, one Planner, one Reconciler, and
`SERVICE_WORKERS` Worker processes. Crashed child processes are restarted, and
`Ctrl+C` stops the complete service. Use `--serve --workers 4` to override the
Worker count and set that service's task concurrency to four. Default `--help`
shows normal commands only; use `--help-all` for pool-level and tuning options.

The remaining commands are intended for single-task operation, diagnostics, or
independent scaling:

```bash
python run.py                       # interactive: confirm & Q&A each round
python run.py --serve               # start the complete autonomous service
python run.py --serve --workers 4   # start the service with four Workers
python run.py --loop 5              # autonomous: 5 rounds, skip high-risk issues
python run.py --loop 5 --force      # autonomous: develop all issues regardless of risk
python run.py --issue 123           # run one unattended issue in an isolated worktree
python run.py --enqueue 123         # add an issue to the persistent queue
python run.py --plan-next           # plan the oldest queued issue once
python run.py --run-next            # claim and run the oldest ready issue
python run.py --resume RUN_ID       # resume from the persisted workflow stage
python run.py --list-runs           # inspect recent and active runs
python run.py --inspect RUN_ID      # show run metadata and structured events
python run.py --cancel RUN_ID       # release locks and remove an abandoned worktree
python run.py --worker              # continuously claim planned, ready tasks
python run.py --planner             # continuously turn queued issues into execution plans
python run.py --scheduler           # continuously discover and enqueue eligible issues
python run.py --schedule-once       # run one scheduling scan
python run.py --reconciler          # continuously recover expired leases
python run.py --reconcile-once      # recover expired leases once
```

The scheduler uses deterministic checks only as safety gates, then asks a
dedicated Scheduler Agent to semantically assess and rank the remaining issues.
The Agent rejects tracking/meta issues, epics, roadmaps, discussions, vague
requests, and other work that is not independently deliverable. It ranks
actionable work by expected value, urgency, benefit breadth, confidence, cost,
risk, and whether it unlocks other work. Labels are treated as hints.

GitHub remains the shared coordination source. Before Agent assessment, the
scheduler skips assigned issues, active-work labels, related open PRs, and
same-repository dependencies declared with `Blocked by #123`, `Depends on
#123`, or `Requires #123`. The Scheduler Agent receives only the candidate issue
numbers and inspects their current GitHub state itself. Agent decisions are
fail-closed, recorded in the event log, and reconsidered on each scan.
Previously Scheduler-enqueued tasks that have not been claimed by a Planner are
also re-evaluated, while manually enqueued and already claimed tasks are left
untouched.

`--cancel` refuses to remove a task that is still owned by a live Agent-Z process. Use it for queued, stopped, or abandoned runs.

Run each pool in separate processes and scale them independently:

```bash
python run.py --planner
python run.py --worker
python run.py --worker
python run.py --reconciler
```

### TUI Observability

The terminal output is designed to remain useful both interactively and in unattended logs:

- Every major stage displays the full Run ID, Issue number, status, stage, lease role, and cumulative elapsed time.
- Agent calls report backend/session mode and execution time.
- Worker, Planner, Scheduler, and Reconciler idle countdowns refresh in place instead of appending heartbeat lines.
- `--serve` displays one live service health line; task, error, and restart events remain normal log lines.
- Agent calls show live elapsed time; PR check registration, check watching, and service restarts show dynamic timing status.
- PR checks render as a result table with total wait time.
- Completed, skipped, failed, interrupted, and `needs_human` runs render consistent terminal summaries.
- `--list-runs` shows a color-coded status overview, run age, leases, PRs, and errors.
- `--inspect RUN_ID` shows runtime details, the persisted plan, and an event timeline with relative timestamps.

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
| `--serve` | Start the complete Scheduler, Planner, Worker, and Reconciler service |
| `--workers N` | Set the Worker count started by `--serve` |
| `--loop N` | Run N rounds automatically |
| `--force` | Ignore risk levels, develop all issues |
| `--issue N` | Run one issue immediately |
| `--enqueue N` | Add an issue to the SQLite queue |
| `--plan-next` | Plan the oldest queued issue once |
| `--run-next` | Claim the oldest planned, ready task when a slot is available |
| `--resume RUN_ID` | Resume a failed or interrupted task |
| `--inspect RUN_ID` | Show persisted metadata and structured event history |
| `--planner` | Run one independently scalable Planner process |
| `--worker` | Run one independently scalable development Worker |
| `--reconciler` | Recover expired Planner/Worker leases continuously |
| `--reconcile-once` | Recover expired leases once |
| `--worker-max-runs N` | Stop worker after N claimed runs (`0` = forever) |
| `--keep-worktree` | Keep a completed task's worktree |

## Workflow

Each round:

1. **Queue Issue** — Persist an issue in the planning queue.
2. **Task Lead Planning** — Explore relevant open issues/PRs on demand, analyze the issue, assess impact, publish a human-readable issue comment, and persist a versioned structured execution plan.
   - `very_low` — no impact
   - `low` — minor impact
   - `medium` — moderate impact
   - `high` — significant (behavior/API changes) → auto-skipped
   - `very_high` — destructive (alters workflows/outputs) → auto-skipped
   - If an assessment already exists, updates it rather than creating a duplicate
3. **Q&A** (interactive only) — Discuss impacts with the Analyst; `skip` to move on
4. **Worker Preflight** — Re-check issue state, labels, related PRs, plan freshness, and predicted file conflicts.
5. **Mark Claimed** — Add the first `SKIP_LABELS` label before development starts
6. **Isolate** — Create a dedicated branch and Git worktree for the run
7. **Develop** — Continue in the same Task Lead session and fix code in the isolated worktree
8. **Review** — Independent local review; findings are explicitly handed back to Developer
9. **Submit** — The Coordinator deterministically commits, pushes, opens, and verifies the PR
10. **PR Checks** — Wait for all checks with `gh pr checks --watch` → Developer reads CI and review feedback → fixes → local Reviewer validates → push → repeat until no action is needed

## Architecture

```
run.py                    Thin CLI entry point and compatibility exports
config.py                 Configuration loaded from .env
orchestration/
  store.py                SQLite queue, workflow state, issue and file locks
  worktree.py             Isolated worktree lifecycle
  runtime.py              Mutable CLI/runtime options
  errors.py               Shared workflow exceptions
  tui.py                  Rich terminal rendering and run inspection
  github_ops.py           Git/GitHub queries, labels, preflight, and cleanup
  submission.py           Commit metadata, push, PR creation, and PR adoption
  workflow.py             Planning and task execution state machine
  pools.py                Planner, Worker, and Reconciler process loops
agents/
  base.py                 Agent base class with pluggable runner
  analyst.py              Task Lead issue selection, planning, impact assessment, Q&A
  developer.py            Task Lead code fixes and review handling
  reviewer.py             Local code review (diff + tests)
  runners/
    base.py               Backend contract, capabilities, and AgentResult
    claude.py             Claude Code adapter with explicit session resume
    codex.py              Codex CLI adapter with JSONL event parsing
    opencode.py           OpenCode adapter with JSON event parsing
```

Planning and development share one persisted `task_lead` backend session. The Reviewer keeps an independent session, while the Coordinator performs deterministic lifecycle and GitHub operations.

Every persisted run has a unique ID, structured execution plan, workflow stage, and role lease. Any number of Planner and Worker processes can compete safely for their respective queues. SQLite performs atomic claims and Issue locks; predicted and actual changed-file claims stop overlapping active tasks. Reconciler processes requeue abandoned planning leases and quarantine abandoned development for human inspection.

Before development, Agent-Z verifies that the required active-work label was actually applied. During submission, the Task Lead generates the commit message, PR title, and PR body from the final diff; the Coordinator validates that metadata, deterministically commits pending changes, pushes the run branch, calls `gh pr create`, and verifies the result through GitHub. Invalid Agent metadata falls back to safe templates. A pre-existing branch PR is explicitly recorded as externally adopted.

Completed and cancelled runs remove their owned active-work label and clean their worktree. Failed and `needs_human` runs keep the branch, label, and worktree by default for recovery; leases are still released. Set `CLEANUP_FAILED_WORKTREES=true` to remove failed worktrees while preserving the branch and active-work label.

Routine discovery queries are open-only: issue selection lists open issues and open PRs, Worker preflight checks related open PRs, and submission recovery adopts only open PRs from the task branch. Closed and merged history is queried only by explicit item URL when a known PR must be inspected.

The Task Lead explores open issues and open PRs only when needed using targeted, paginated queries. The Coordinator does not inject a backlog snapshot into prompts, preserving flexible exploration without repeatedly loading repository-wide state.

Structured run events are stored in SQLite for queue, worker, stage, status, resume, cancel, skip-label, and file-claim transitions. Use `python run.py --inspect RUN_ID` to review the timeline when a remote or unattended run needs diagnosis.

## Configuration

Copy `.env.example` to `.env` and edit. Full options:

| Variable | Description | Default |
|----------|-------------|---------|
| `PROJECT_DIR` | Target project path | — |
| `GITHUB_REPO` | Target repo (owner/repo) | — |
| `SKIP_LABELS` | Comma-separated labels to skip; the first label is added before development | `ongoing` |
| `AGENT_Z_HOME` | Runtime state directory | `.agent-z` |
| `STATE_DB` | SQLite state database | `.agent-z/state.db` |
| `WORKTREE_ROOT` | Isolated worktree directory | `.agent-z/worktrees` |
| `DEFAULT_BACKEND` | Default backend: `claude`, `codex`, or `opencode` | `claude` |
| `TASK_LEAD_BACKEND` | Shared issue selection, planning, and development backend | `ANALYST_BACKEND` or `DEFAULT_BACKEND` |
| `REVIEWER_BACKEND` | Reviewer backend override | `DEFAULT_BACKEND` |
| `CLAUDE_FLAGS` | Claude Code flags | `--dangerously-skip-permissions` |
| `CODEX_FLAGS` | Codex CLI flags | `--dangerously-bypass-approvals-and-sandbox` |
| `OPENCODE_FLAGS` | OpenCode CLI flags | — |
| `TIMEOUT_ANALYST` | Analyst timeout (s) | 3600 |
| `TIMEOUT_DEVELOPER` | Developer timeout (s) | 10800 |
| `TIMEOUT_REVIEWER` | Reviewer timeout (s) | 1800 |
| `RETRY_TIMEOUT` | Retry timeout (s) | 3600 |
| `GITHUB_RETRY_ATTEMPTS` | Maximum attempts for GitHub CLI and explicit network Git operations | 3 |
| `GITHUB_RETRY_BASE_DELAY` | Initial transient-failure retry delay (s) | 2 |
| `GITHUB_RETRY_MAX_DELAY` | Maximum exponential backoff delay (s) | 30 |
| `GITHUB_COMMAND_TIMEOUT` | Per-command timeout for GitHub/Git network calls without an explicit timeout (s) | 60 |
| `PR_CHECKS_INTERVAL` | PR checks watch interval (s) | 10 |
| `PR_CHECKS_MAX_WAIT` | Max wait for PR checks (s) | 900 |
| `MAX_REVIEW_ROUNDS` | Max review-fix loops | 5 |
| `MAX_LOCAL_REVIEW_ROUNDS` | Max local review loops | 5 |
| `MAX_PARALLEL_TASKS` | Maximum active tasks across processes | 2 |
| `SERVICE_WORKERS` | Default Worker count for `--serve` | `MAX_PARALLEL_TASKS` |
| `SERVICE_RESTART_DELAY` | Delay before restarting a crashed service child (s) | 5 |
| `MAX_RUN_SECONDS` | Per-attempt runtime budget | 21600 |
| `CLEANUP_COMPLETED_WORKTREES` | Remove worktrees after successful completion | `true` |
| `CLEANUP_FAILED_WORKTREES` | Remove failed/needs-human worktrees while preserving branch and label | `false` |
| `WORKER_IDLE_SLEEP` | Queue worker sleep when no task is available | 30 |
| `PLANNER_IDLE_SLEEP` | Planner sleep when no issue awaits analysis | 30 |
| `SCHEDULER_IDLE_SLEEP` | Seconds between Scheduler scans | 60 |
| `SCHEDULER_BATCH_SIZE` | Maximum Agent-approved issues enqueued per scan | 10 |
| `SCHEDULER_ISSUE_LIMIT` | Maximum open issues fetched per scan | 100 |
| `SCHEDULER_AGENT_CANDIDATE_LIMIT` | Maximum hard-filtered candidates assessed by the Scheduler Agent per scan | `SCHEDULER_ISSUE_LIMIT` |
| `SCHEDULER_ELIGIBLE_LABELS` | Required scheduling labels; empty allows all open issues | — |
| `SCHEDULER_BLOCK_LABELS` | Labels that prevent automatic scheduling | `blocked` |
| `SCHEDULER_SKIP_ASSIGNED_ISSUES` | Skip issues with assignees | `true` |
| `SCHEDULER_PRIORITY_LABELS` | Label hints used to build the Agent candidate shortlist | `priority:critical,...` |
| `RECONCILER_INTERVAL` | Seconds between expired-lease scans | 60 |
| `PLANNER_LEASE_SECONDS` | Planner claim lifetime | 7200 |
| `WORKER_LEASE_SECONDS` | Worker claim lifetime | 21600 |

Legacy variables `CODERABBIT_POLL_INTERVAL` and `CODERABBIT_MAX_WAIT` remain supported as fallbacks.

## Backend Selection

Use one backend for every role:

```env
DEFAULT_BACKEND=codex
```

Or keep Task Lead context across planning and development while using an independent Reviewer:

```env
TASK_LEAD_BACKEND=claude
REVIEWER_BACKEND=codex
```

Only selected backends are required at startup. If `opencode` is configured but not available on `PATH`, Agent-Z fails fast with a clear error.
