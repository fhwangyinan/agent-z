# Agent-Z

[English](README.md)

轻量级、后端无关的 coding-agent 自动化工具，实现自主开发循环。

通过 Claude Code、Codex 或 OpenCode，将确定性控制平面、灵活的 Task Lead、独立 Reviewer、可扩缩 Worker 与 Reconciler 组合起来。

```
Open Issues/PRs ← 按需探索 ← Task Lead（选择 + 规划 + 开发）
                               ↓
持久化队列 → Worker Pool → 独立 Reviewer → 确定性 Coordinator
                  ↘              Reconciler              ↗
```

## 前置条件

- Python 3.11+
- [GitHub CLI](https://cli.github.com/) (`gh`) 已认证
- 至少安装一个受支持的 coding-agent CLI：`claude`、`codex` 或 `opencode`
- 可选：目标仓库安装 [CodeRabbitAI](https://coderabbit.ai/) GitHub App

## 安装

```bash
python -m venv .venv
.venv\Scripts\activate      # Windows
source .venv/bin/activate   # Linux/macOS
pip install -r requirements.txt
cp .env.example .env        # 按需修改配置
```

## 使用

```bash
python run.py                       # 交互模式：每轮确认 + 影响评估问答
python run.py --loop 5              # 自主模式：5 轮，跳过高风险 issue
python run.py --loop 5 --force      # 自主模式：忽略风险级别，全部开发
python run.py --issue 123           # 在独立 worktree 中无人值守处理一个 issue
python run.py --enqueue 123         # 将 issue 加入持久化队列
python run.py --plan-next           # 单次规划最早的排队 Issue
python run.py --run-next            # 领取并执行最早的 ready 任务
python run.py --resume RUN_ID       # 从持久化阶段恢复任务
python run.py --list-runs           # 查看最近及活跃任务
python run.py --inspect RUN_ID      # 查看任务元数据和结构化事件
python run.py --cancel RUN_ID       # 释放锁并清理废弃 worktree
python run.py --worker              # 持续领取已完成规划的 ready 任务
python run.py --planner             # 持续分析 issue 并生成结构化执行计划
python run.py --reconciler          # 持续回收过期租约
python run.py --reconcile-once      # 执行一次租约回收
```

`--cancel` 会拒绝删除仍由活跃 Agent-Z 进程持有的任务；它适用于排队、已停止或废弃任务。

不同池使用独立进程运行，可以分别扩缩：

```bash
python run.py --planner
python run.py --worker
python run.py --worker
python run.py --reconciler
```

### TUI 可观测性

终端输出同时适合交互观察和无人值守日志留存：

- 每个关键阶段展示完整 Run ID、Issue 编号、状态、阶段、租约角色和累计耗时。
- Agent 调用展示后端/session 模式与执行耗时。
- Worker、Planner、Reconciler 展示启动配置、空闲心跳、已领取数量、运行时间和下次扫描时间。
- PR Checks 使用结果表展示，并包含总等待时间。
- 完成、跳过、失败、中断和 `needs_human` 使用统一终态摘要。
- `--list-runs` 展示带颜色的状态总览、任务年龄、租约、PR 和错误。
- `--inspect RUN_ID` 展示运行详情、持久化计划和带相对时间的事件时间线。

### 交互模式

- 让 Agent 自动推荐 issue，或手动指定编号
- 查看影响评估，与 Analyst 追问讨论
- 输入 `skip` 换 issue，`done` 或回车开始开发

### 自主模式

- Agent 自动推荐并修复 issue，无人值守
- 风险评估：**high / very_high** 跳过（加 `--force` 则忽略）
- 可用参数：

| 参数 | 效果 |
|------|------|
| `--loop N` | 自动运行 N 轮 |
| `--force` | 忽略风险级别，全部开发 |
| `--issue N` | 立即执行一个 Issue |
| `--enqueue N` | 将 Issue 加入 SQLite 队列 |
| `--plan-next` | 单次规划最早的排队 Issue |
| `--run-next` | 有空闲并发槽时领取最早的已规划 ready 任务 |
| `--resume RUN_ID` | 恢复失败或中断的任务 |
| `--inspect RUN_ID` | 查看持久化元数据和结构化事件时间线 |
| `--planner` | 启动一个可独立扩缩的 Planner 进程 |
| `--worker` | 启动一个可独立扩缩的开发 Worker |
| `--reconciler` | 持续恢复过期 Planner/Worker 租约 |
| `--reconcile-once` | 执行一次过期租约恢复 |
| `--worker-max-runs N` | worker 领取 N 个任务后停止（`0` 表示持续运行） |
| `--keep-worktree` | 成功后保留任务 worktree |

## 工作流

每轮执行：

1. **Issue 入队** — 将 Issue 持久化到分析队列。
2. **Task Lead 规划** — 按需探索相关 open Issue/PR，分析 Issue、评估影响、把可读结论写入 Issue，并持久化版本化结构执行计划：
   - `very_low` — 无影响
   - `low` — 轻微影响
   - `medium` — 中等影响
   - `high` — 显著影响（行为/API 变化）→ 自动跳过
   - `very_high` — 破坏性变更（改变流程/输出）→ 自动跳过
   - 已有评估则追加更新，不重复创建
3. **交互问答**（仅交互模式）— 就影响评估与 Analyst 对话；`skip` 换 issue
4. **Worker Preflight** — 开工前重新检查 Issue 状态、label、相关 PR、计划新鲜度和预测文件冲突。
5. **标记已认领** — 开发开始前给 Issue 添加 `SKIP_LABELS` 中的第一个标签
6. **任务隔离** — 为每个任务创建独立分支和 Git worktree
7. **开发修复** — 在同一个 Task Lead session 中继续，并在独立 worktree 中修复代码
8. **本地审查** — 独立 Reviewer 审查；发现的问题显式交给 Developer 修复
9. **提交 PR** — Coordinator 确定性地 commit、push、创建并验证 PR
10. **PR Checks** — 使用 `gh pr checks --watch` 等待全部 checks → Developer 读取 CI 与 review 反馈 → 修复 → 本地 Reviewer 复查通过 → push → 循环直到无需修改

## 架构

```
run.py                    精简 CLI 入口与兼容导出
config.py                 从 .env 加载的配置
orchestration/
  store.py                SQLite 队列、流程状态、Issue 与文件锁
  worktree.py             独立 worktree 生命周期
  runtime.py              可变 CLI/运行时选项
  errors.py               共享流程异常
  tui.py                  Rich 终端渲染与任务检查
  github_ops.py           Git/GitHub 查询、label、preflight 与清理
  submission.py           Commit 元数据、push、PR 创建与 PR 接管
  workflow.py             规划与任务执行状态机
  pools.py                Planner、Worker 与 Reconciler 进程循环
agents/
  base.py                 Agent 基类，可插拔 runner
  analyst.py              Task Lead 的 Issue 选择、规划、影响评估与交互问答
  developer.py            Task Lead 的代码修复与 review 处理
  reviewer.py             本地 Code Review (diff + 测试)
  runners/
    base.py               后端契约、能力声明与 AgentResult
    claude.py             Claude Code 适配器，支持显式 session 恢复
    codex.py              Codex CLI 适配器，解析 JSONL 事件
    opencode.py           OpenCode 适配器，解析 JSON 事件
```

规划与开发共享一个持久化的 `task_lead` 后端 session。Reviewer 保持独立 session，Coordinator 则负责确定性的生命周期与 GitHub 操作。

每个持久化任务拥有唯一 Run ID、结构化执行计划、流程阶段和角色租约。任意数量的 Planner 与 Worker 进程可以分别竞争各自队列；SQLite 提供原子领取和 Issue 锁，预测文件与实际改动文件登记会阻止活跃任务冲突。Reconciler 会重新排队废弃的 Planner 租约，并把废弃开发任务转为 `needs_human`。

开发前，Agent-Z 会确认必需的进行中 label 已真正添加。提交阶段由 Task Lead 根据最终 diff 生成 commit message、PR 标题和 PR 描述；Coordinator 校验这些元数据后，确定性地提交残留改动、推送任务分支、调用 `gh pr create`，并通过 GitHub 验证结果。Agent 元数据无效时会使用安全模板兜底。分支上预先存在的 PR 会明确记录为“接管外部 PR”。

完成和取消的任务会移除本任务添加的 active-work label，并清理 worktree。失败和 `needs_human` 任务默认保留分支、label 和 worktree 以便恢复，但仍会释放租约。设置 `CLEANUP_FAILED_WORKTREES=true` 可清理失败 worktree，同时保留分支和 active-work label。

日常发现查询只读取 open 数据：选题只列出 open Issue 和 open PR，Worker preflight 只检查相关 open PR，提交恢复也只接管任务分支上的 open PR。只有已经持有明确 PR URL、确需检查该对象时，才会查询单个历史 PR。

Task Lead 只在需要时通过有针对性、可分页的查询探索 open Issue 和 open PR。Coordinator 不再把 backlog 快照注入 prompt，因此既保留探索灵活性，也避免反复加载仓库全局状态。

结构化事件会写入 SQLite，覆盖队列、worker、阶段、状态、恢复、取消、跳过标签和文件声明等变化。远程或无人值守任务需要诊断时，可用 `python run.py --inspect RUN_ID` 查看时间线。

## 配置

复制 `.env.example` 为 `.env` 后修改。全部选项：

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `PROJECT_DIR` | 目标项目路径 | — |
| `GITHUB_REPO` | 目标仓库 (owner/repo) | — |
| `SKIP_LABELS` | 逗号分隔的跳过标签；开发前会添加第一个标签 | `ongoing` |
| `AGENT_Z_HOME` | 运行状态目录 | `.agent-z` |
| `STATE_DB` | SQLite 状态数据库 | `.agent-z/state.db` |
| `WORKTREE_ROOT` | 独立 worktree 目录 | `.agent-z/worktrees` |
| `DEFAULT_BACKEND` | 默认后端：`claude`、`codex` 或 `opencode` | `claude` |
| `TASK_LEAD_BACKEND` | Issue 选择、规划和开发共享的后端 | `ANALYST_BACKEND` 或 `DEFAULT_BACKEND` |
| `REVIEWER_BACKEND` | Reviewer 后端覆盖 | `DEFAULT_BACKEND` |
| `CLAUDE_FLAGS` | Claude 参数 | `--dangerously-skip-permissions` |
| `CODEX_FLAGS` | Codex 参数 | `--dangerously-bypass-approvals-and-sandbox` |
| `OPENCODE_FLAGS` | OpenCode 参数 | — |
| `TIMEOUT_ANALYST` | Analyst 超时 (秒) | 3600 |
| `TIMEOUT_DEVELOPER` | Developer 超时 (秒) | 10800 |
| `TIMEOUT_REVIEWER` | Reviewer 超时 (秒) | 1800 |
| `RETRY_TIMEOUT` | 重试超时 (秒) | 3600 |
| `PR_CHECKS_INTERVAL` | PR Checks watch 间隔 (秒) | 10 |
| `PR_CHECKS_MAX_WAIT` | PR Checks 最大等待 (秒) | 900 |
| `MAX_REVIEW_ROUNDS` | Review 最大轮次 | 5 |
| `MAX_LOCAL_REVIEW_ROUNDS` | 本地 Review 最大轮次 | 5 |
| `MAX_PARALLEL_TASKS` | 跨进程最大活跃任务数 | 2 |
| `MAX_RUN_SECONDS` | 单次执行时间预算 | 21600 |
| `CLEANUP_COMPLETED_WORKTREES` | 成功后删除 worktree | `true` |
| `CLEANUP_FAILED_WORKTREES` | 失败/needs-human 后删除 worktree，但保留分支与 label | `false` |
| `WORKER_IDLE_SLEEP` | 队列 worker 空闲时的 sleep 秒数 | 30 |
| `PLANNER_IDLE_SLEEP` | Planner 无待分析 Issue 时的 sleep 秒数 | 30 |
| `RECONCILER_INTERVAL` | 过期租约扫描间隔秒数 | 60 |
| `PLANNER_LEASE_SECONDS` | Planner 租约时长 | 7200 |
| `WORKER_LEASE_SECONDS` | Worker 租约时长 | 21600 |

旧变量 `CODERABBIT_POLL_INTERVAL` 和 `CODERABBIT_MAX_WAIT` 仍可作为兼容回退。

## 后端选择

全部角色使用同一后端：

```env
DEFAULT_BACKEND=codex
```

也可以让 Task Lead 在规划与开发间保留上下文，同时使用独立 Reviewer：

```env
TASK_LEAD_BACKEND=claude
REVIEWER_BACKEND=codex
```

启动时仅要求已选中的后端命令可用。若配置了 `opencode` 但命令不在 `PATH`，Agent-Z 会快速失败并给出明确错误。
