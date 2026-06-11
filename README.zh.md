# Agent-Z

[English](README.md)

轻量级、后端无关的 coding-agent 自动化工具，实现自主开发循环。

通过 Claude Code、Codex 或 OpenCode 驱动多个专项 Agent，构成全自动闭环：挑选 issue → 影响评估 → 修复代码 → 本地审查 → 提 PR → 等待并迭代 PR Checks 反馈。

```
Analyst → 影响评估 → Developer → Reviewer → Submitter → PR Checks → Developer → ...
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
```

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

## 工作流

每轮执行：

1. **挑选 Issue** — Agent 推荐最优 open issue（排除已有完整 PR 的）
2. **影响评估** — 分析潜在影响，评定风险等级，英文报告写入 issue：
   - `very_low` — 无影响
   - `low` — 轻微影响
   - `medium` — 中等影响
   - `high` — 显著影响（行为/API 变化）→ 自动跳过
   - `very_high` — 破坏性变更（改变流程/输出）→ 自动跳过
   - 已有评估则追加更新，不重复创建
3. **交互问答**（仅交互模式）— 就影响评估与 Analyst 对话；`skip` 换 issue
4. **开发修复** — 在目标工作区修复代码
5. **本地审查** — 独立 Reviewer 审查；发现的问题显式交给 Developer 修复
6. **提交 PR** — 创建分支、commit、push、创建 PR
7. **PR Checks** — 使用 `gh pr checks --watch` 等待全部 checks → Developer 读取 CI 与 review 反馈 → 修复 → 本地 Reviewer 复查通过 → push → 循环直到无需修改

## 架构

```
run.py                    主协调器 — 循环控制和 session 管理
config.py                 从 .env 加载的配置
agents/
  base.py                 Agent 基类，可插拔 runner
  analyst.py              分析 Issue、影响评估、交互问答
  developer.py            代码修复、review 处理
  reviewer.py             本地 Code Review (diff + 测试)
  submitter.py            分支、commit、push、PR
  runners/
    base.py               后端契约、能力声明与 AgentResult
    claude.py             Claude Code 适配器，支持显式 session 恢复
    codex.py              Codex CLI 适配器，解析 JSONL 事件
    opencode.py           OpenCode 适配器，解析 JSON 事件
```

每个角色持有独立后端 session。Analyst、Developer、Reviewer 和 Submitter 可以使用不同 CLI；角色间通过工作区、GitHub Issue、PR 反馈与显式 Review findings 传递上下文，不依赖隐式 latest session。

## 配置

复制 `.env.example` 为 `.env` 后修改。全部选项：

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `PROJECT_DIR` | 目标项目路径 | — |
| `GITHUB_REPO` | 目标仓库 (owner/repo) | — |
| `DEFAULT_BACKEND` | 默认后端：`claude`、`codex` 或 `opencode` | `claude` |
| `ANALYST_BACKEND` | Analyst 后端覆盖 | `DEFAULT_BACKEND` |
| `DEVELOPER_BACKEND` | Developer 后端覆盖 | `DEFAULT_BACKEND` |
| `REVIEWER_BACKEND` | Reviewer 后端覆盖 | `DEFAULT_BACKEND` |
| `SUBMITTER_BACKEND` | Submitter 后端覆盖 | `DEFAULT_BACKEND` |
| `CLAUDE_FLAGS` | Claude 参数 | `--dangerously-skip-permissions` |
| `CODEX_FLAGS` | Codex 参数 | `--dangerously-bypass-approvals-and-sandbox` |
| `OPENCODE_FLAGS` | OpenCode 参数 | — |
| `TIMEOUT_ANALYST` | Analyst 超时 (秒) | 3600 |
| `TIMEOUT_DEVELOPER` | Developer 超时 (秒) | 10800 |
| `TIMEOUT_REVIEWER` | Reviewer 超时 (秒) | 1800 |
| `TIMEOUT_SUBMITTER` | Submitter 超时 (秒) | 600 |
| `RETRY_TIMEOUT` | 重试超时 (秒) | 3600 |
| `PR_CHECKS_INTERVAL` | PR Checks watch 间隔 (秒) | 10 |
| `PR_CHECKS_MAX_WAIT` | PR Checks 最大等待 (秒) | 900 |
| `MAX_REVIEW_ROUNDS` | Review 最大轮次 | 5 |
| `MAX_LOCAL_REVIEW_ROUNDS` | 本地 Review 最大轮次 | 5 |

旧变量 `CODERABBIT_POLL_INTERVAL` 和 `CODERABBIT_MAX_WAIT` 仍可作为兼容回退。

## 后端选择

全部角色使用同一后端：

```env
DEFAULT_BACKEND=codex
```

也可以混合使用并保持 session 隔离：

```env
ANALYST_BACKEND=claude
DEVELOPER_BACKEND=claude
REVIEWER_BACKEND=codex
SUBMITTER_BACKEND=claude
```

启动时仅要求已选中的后端命令可用。若配置了 `opencode` 但命令不在 `PATH`，Agent-Z 会快速失败并给出明确错误。
