# Agent-Z

[![CI](https://github.com/fhwangyinan/agent-z/actions/workflows/ci.yml/badge.svg)](https://github.com/fhwangyinan/agent-z/actions/workflows/ci.yml)

[English](README.md)

轻量级、后端无关的 coding-agent 自动化工具，实现自主开发循环。

通过 Claude Code、Codex 或 OpenCode，将确定性控制平面、灵活的 Task Lead、独立 Reviewer、可扩缩 Worker 与 Reconciler 组合起来。

```
Open Issues/PRs → Scheduler（资格 + 优先级 + 依赖）→ 持久化队列
                                                        ↓
                         Planner Pool → Worker Pool → 独立 Reviewer → 确定性 Coordinator
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

日常使用只需要记住三个入口：

```bash
python run.py --serve              # 推荐：一键启动完整自治服务
python run.py --list-runs          # 查看最近任务
python run.py --inspect RUN_ID     # 查看某个任务及事件时间线
```

`--serve` 会启动一个 Scheduler、一个 Planner、一个 Reconciler，以及默认
`SERVICE_WORKERS` 个 Worker。任一子进程异常退出后会指数退避重启，连续失败后
打开熔断；按 `Ctrl+C`
统一停止全部进程。可用 `python run.py --serve --workers 4` 临时覆盖 Worker 数量，
并将本次服务的最大并发任务数设为 4。
默认 `--help` 只展示日常命令；使用 `python run.py --help-all` 查看池级和调参命令。

`--serve` 使用固定一屏的全屏 Dashboard，不再把所有子进程输出持续堆到终端。
界面展示进程健康状态、最近任务、选中详情和最近结构化事件。使用 `Tab` 在
Processes 与 Tasks 间切换，方向键或 `j/k` 选择条目，`Enter` 或 `Space`
展开任务详情或实时进程日志尾部，`q` 统一停止服务。新任务出现时会按 Run ID
保持当前任务选择。每个子进程的完整原始输出保存在
`.agent-z/logs/<process>.log`。

### CI 与 CodeRabbit

GitHub Actions 会在 Python 3.11、3.12、3.13 上运行完整单元测试，并执行独立的
编译检查。Workflow 使用只读仓库权限，可以在 `main` 分支保护规则中设为必需检查。

仓库也包含 `.coderabbit.yaml`，自动使用中文审查状态机、并发、恢复、外部 CLI
调用和测试覆盖。要启用审查，需要在 [coderabbit.ai](https://coderabbit.ai/)
为本仓库安装 CodeRabbit GitHub App；标准 GitHub App 集成不需要仓库 Secret。

以下命令用于单任务处理、调试或独立扩缩：

```bash
python run.py                       # 交互模式：每轮确认 + 影响评估问答
python run.py --serve               # 一键启动完整自治服务
python run.py --serve --workers 4   # 一键启动并使用 4 个 Worker
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
python run.py --scheduler           # 持续发现、排序并入队可执行 issue
python run.py --schedule-once       # 执行一次 issue 调度扫描
python run.py --reconciler          # 持续回收过期租约
python run.py --reconcile-once      # 执行一次租约回收
```

`--cancel` 会拒绝删除仍由活跃 Agent-Z 进程持有的任务；它适用于排队、已停止或废弃任务。

不同池使用独立进程运行，可以分别扩缩：

```bash
python run.py --planner
python run.py --scheduler
python run.py --worker
python run.py --worker
python run.py --reconciler
```

### TUI 可观测性

终端输出同时适合交互观察和无人值守日志留存：

- 每个关键阶段展示完整 Run ID、Issue 编号、状态、阶段、租约角色和累计耗时。
- Agent 调用展示后端/session 模式与执行耗时。
- Worker、Planner、Scheduler、Reconciler 的空闲倒计时和运行时间在同一行实时刷新，不持续堆积 heartbeat 日志。
- `--serve` 使用固定一屏的进程/任务 Dashboard，可展开实时日志尾部；子进程完整输出写入 `.agent-z/logs/`。
- Scheduler 每轮只批量获取一次 open PR，并按精确 Issue 引用映射，避免逐候选查询。
- Worker 在开发前同时锁定预测文件与保守模块资源，Planner 瞬时失败会指数退避重试。
- Agent 调用会实时显示已运行时间；PR checks 注册等待、检查等待和服务重启会显示动态时间状态。
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
| `--serve` | 启动 Scheduler、Planner、Worker 与 Reconciler 完整服务 |
| `--workers N` | 设置 `--serve` 启动的 Worker 数量 |
| `--loop N` | 自动运行 N 轮 |
| `--force` | 忽略风险级别，全部开发 |
| `--issue N` | 立即执行一个 Issue |
| `--enqueue N` | 将 Issue 加入 SQLite 队列 |
| `--plan-next` | 单次规划最早的排队 Issue |
| `--run-next` | 有空闲并发槽时领取最早的已规划 ready 任务 |
| `--resume RUN_ID` | 恢复失败或中断的任务 |
| `--inspect RUN_ID` | 查看持久化元数据和结构化事件时间线 |
| `--planner` | 启动一个可独立扩缩的 Planner 进程 |
| `--scheduler` | 持续扫描并按优先级入队无阻塞 Issue |
| `--schedule-once` | 执行一次调度扫描 |
| `--worker` | 启动一个可独立扩缩的开发 Worker |
| `--reconciler` | 持续恢复过期 Planner/Worker 租约 |
| `--reconcile-once` | 执行一次过期租约恢复 |
| `--worker-max-runs N` | worker 领取 N 个任务后停止（`0` 表示持续运行） |
| `--keep-worktree` | 成功后保留任务 worktree |

## 工作流

每轮执行：

1. **Issue 调度与入队** — Scheduler 先用确定性规则跳过已有 assignee、活动标签、相关 open PR 或未关闭依赖的 Issue，再只把候选 Issue 编号交给专职 Scheduler Agent，由 Agent 自行通过 `gh` 查看最新正文、标签、引用、相关 PR 和代码，判断候选是否是可独立交付的代码任务，并按预期收益、紧迫度、影响面、置信度、成本、风险和解锁价值排序。Tracking/meta issue、epic、roadmap、讨论、模糊请求等会被拒绝；标签只作为提示。Issue 正文可用 `Blocked by #123`、`Depends on #123` 或 `Requires #123` 声明依赖；互不依赖的高价值 Issue 会批量入队并可并行执行。每次轻量扫描会持久化候选 Issue 的 `updatedAt`、相关 open PR 状态和队列状态；只有首次扫描、候选快照变化，或上次评估后 queued 任务被消费而需要补位时才调用 Agent。补位只补到 `SCHEDULER_BATCH_SIZE`，未变化但未满的队列不会被反复评估。Agent 输出异常时本轮不会入队，决策会写入事件日志。旧版 Scheduler 已入队但尚未被 Planner 领取的任务也会重新审查；手动入队和已领取任务不会被自动取消。
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
  scheduler.py            Issue 安全过滤、Agent 语义调度与依赖解析
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

每个持久化任务拥有唯一 Run ID、结构化执行计划、流程阶段和角色租约。Scheduler 以 GitHub 为共享协作事实源，避开已分配、已有活动标签或相关 open PR 的 Issue；任意数量的 Planner 与 Worker 进程可以分别竞争各自队列。SQLite 提供本实例内的原子领取和 Issue 锁，预测文件与实际改动文件登记会阻止活跃任务冲突。Worker 开工前会再次检查 GitHub 状态，降低与外部协作者同时开工的风险。Reconciler 会重新排队废弃的 Planner 租约，并把废弃开发任务转为 `needs_human`。

跨团队协作时，建议所有人统一遵守“开始处理就 assign 自己或添加 `SKIP_LABELS` 中的活动标签，尽早创建 draft PR”的约定。GitHub label/assignee 本身不提供原子抢锁，因此极端情况下两个参与者在同一瞬间开工仍可能竞争；需要严格互斥时，应在仓库侧增加 GitHub Action 或外部锁服务。

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
| `GITHUB_RETRY_ATTEMPTS` | GitHub CLI 与显式网络 Git 操作的最大尝试次数 | 3 |
| `GITHUB_RETRY_BASE_DELAY` | 瞬时网络失败首次重试等待秒数 | 2 |
| `GITHUB_RETRY_MAX_DELAY` | 指数退避最大等待秒数 | 30 |
| `GITHUB_COMMAND_TIMEOUT` | 未显式设置 timeout 的单次 GitHub/Git 网络命令超时秒数 | 60 |
| `PR_CHECKS_INTERVAL` | PR Checks watch 间隔 (秒) | 10 |
| `PR_CHECKS_MAX_WAIT` | PR Checks 最大等待 (秒) | 900 |
| `MAX_REVIEW_ROUNDS` | Review 最大轮次 | 5 |
| `MAX_LOCAL_REVIEW_ROUNDS` | 本地 Review 最大轮次 | 5 |
| `MAX_PARALLEL_TASKS` | 跨进程最大活跃任务数 | 2 |
| `SERVICE_WORKERS` | `--serve` 默认启动的 Worker 数量 | `MAX_PARALLEL_TASKS` |
| `SERVICE_RESTART_DELAY` | 服务子进程异常退出后的重启等待秒数 | 5 |
| `SERVICE_RESTART_MAX_DELAY` | 服务子进程指数重启退避最大秒数 | 60 |
| `SERVICE_RESTART_MAX_ATTEMPTS` | 连续重启多少次后打开熔断 | 5 |
| `SERVICE_RESTART_RESET_SECONDS` | 连续健康运行多少秒后重置失败计数 | 300 |
| `SERVICE_LOG_MAX_BYTES` | 单个服务子进程日志达到该字节数后轮转 | 5242880 |
| `SERVICE_LOG_BACKUPS` | 保留的服务轮转日志数量 | 3 |
| `MAX_RUN_SECONDS` | 单次执行时间预算 | 21600 |
| `CLEANUP_COMPLETED_WORKTREES` | 成功后删除 worktree | `true` |
| `CLEANUP_FAILED_WORKTREES` | 失败/needs-human 后删除 worktree，但保留分支与 label | `false` |
| `WORKER_IDLE_SLEEP` | 队列 worker 空闲时的 sleep 秒数 | 30 |
| `WORKER_PREFLIGHT_MAX_RETRIES` | 预检失败多少次后转为需要人工处理 | 3 |
| `PLANNER_IDLE_SLEEP` | Planner 无待分析 Issue 时的 sleep 秒数 | 30 |
| `PLANNER_MAX_RETRIES` | Planner 瞬时失败的最大尝试次数 | 3 |
| `PLANNER_RETRY_BASE_DELAY` | Planner 首次重试等待秒数 | 10 |
| `SCHEDULER_IDLE_SLEEP` | Scheduler 扫描间隔秒数 | 60 |
| `SCHEDULER_BATCH_SIZE` | 每轮最多入队的 Issue 数 | 10 |
| `SCHEDULER_ISSUE_LIMIT` | 每轮最多扫描的 open Issue 数 | 100 |
| `SCHEDULER_PR_LIMIT` | 每轮一次性获取的 open PR 最大数量 | 500 |
| `SCHEDULER_AGENT_CANDIDATE_LIMIT` | 每轮最多交给 Scheduler Agent 判断的候选数 | `SCHEDULER_ISSUE_LIMIT` |
| `SCHEDULER_ELIGIBLE_LABELS` | 可调度标签；留空表示所有 open Issue | — |
| `SCHEDULER_BLOCK_LABELS` | 阻止自动入队的标签 | `blocked` |
| `SCHEDULER_SKIP_ASSIGNED_ISSUES` | 跳过已有 assignee 的 Issue | `true` |
| `SCHEDULER_PRIORITY_LABELS` | 构建 Agent 候选短名单时使用的优先级标签提示 | `priority:critical,...` |
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
