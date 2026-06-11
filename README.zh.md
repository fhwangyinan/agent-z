# Agent-Z

[English](README.md)

轻量级 coding-agent 驱动脚本，实现自主开发循环。

通过 Claude Code 驱动多个专项 Agent，构成全自动闭环：分析 issue → 影响评估 → 修复代码 → 本地审查 → 提 PR → 迭代 CI 反馈。

```
Analyst → 影响评估 → Developer → Reviewer → Submitter → CodeRabbit → Developer → ...
```

## 前置条件

- Python 3.11+
- [GitHub CLI](https://cli.github.com/) (`gh`) 已认证
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) (`claude`) 已安装
- 目标仓库已安装 [CodeRabbitAI](https://coderabbit.ai/) GitHub App

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
python run.py               # 交互模式：每轮确认 + 影响评估问答
python run.py --loop 5      # 自主模式：连续 5 轮无人值守
```

### 交互模式

每轮可以：
- 让 Agent 自动推荐 issue，或手动指定编号
- 查看影响评估报告，向 Analyst 追问细节
- 输入 `skip` 跳过当前 issue 换下一个
- 输入 `done` 或空回车开始开发

### 自主模式

- Agent 自动推荐并修复 issue，无人值守
- 风险评估：**中等及以上风险**自动跳过换下一个 issue
- 适合 cron 定时任务

## 工作流

每轮执行以下步骤：

1. **挑选 Issue** — Agent 推荐最优 open issue（或手动指定）
2. **影响评估** — 评估修复对项目的潜在影响并给出风险等级：
   - `very_low` — 无影响
   - `low` — 轻微影响
   - `medium` — 中等影响
   - `high` — 显著影响（行为/API 变化）
   - `very_high` — 破坏性变更（改变现有流程或输出结果）
   - 结果以英文写入 issue comment
3. **交互问答**（仅交互模式）— 就影响评估结果与 Analyst 对话讨论
4. **开发修复** — 修复代码（通过 `--continue` 复用 Analyst 的上下文）
5. **本地审查** — 本地 Code Review (git diff + 测试)；Developer 修复反馈
6. **提交 PR** — 创建分支、commit、push、创建 PR
7. **CodeRabbit** — 等待 check 完成 → Developer 读取 review → 修复 → 本地 Reviewer 复查通过 → push + @coderabbitai → 循环直到通过或输出 `NO_ACTION_NEEDED`

## 架构

```
run.py                    主协调器 — 循环控制和 session 管理
config.py                 从 .env 加载的配置
agents/
  base.py                 Agent 基类，可插拔 runner
  analyst.py              分析 Issue、影响评估、交互问答
  developer.py            阅读代码、修复、处理 review 反馈
  reviewer.py             提交前本地 Code Review (git diff + 测试)
  submitter.py            创建分支、commit、push、PR
  runners/
    base.py               抽象 AgentRunner 接口
    claude.py             ClaudeRunner — claude -p 实现
```

所有 Agent 通过 `--continue` 共享同一个 Claude Code session，上下文自然流转 — 开发者能看到分析师的分析结论和影响评估，审阅者能看到开发者的改动。无需重复读代码。

## 配置

全部配置项见 `.env.example`。

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `PROJECT_DIR` | 目标项目路径 | — |
| `GITHUB_REPO` | 目标仓库 (owner/repo) | — |
| `CLAUDE_FLAGS` | Claude Code 参数 | `--dangerously-skip-permissions` |
| `TIMEOUT_ANALYST` | Analyst 超时 (秒) | 3600 |
| `TIMEOUT_DEVELOPER` | Developer 超时 (秒) | 10800 |
| `TIMEOUT_REVIEWER` | Reviewer 超时 (秒) | 1800 |
| `TIMEOUT_SUBMITTER` | Submitter 超时 (秒) | 600 |
| `RETRY_TIMEOUT` | 失败重试超时 (秒) | 3600 |
| `CODERABBIT_POLL_INTERVAL` | 轮询间隔 (秒) | 45 |
| `CODERABBIT_MAX_WAIT` | 最大等待 (秒) | 900 |
| `MAX_REVIEW_ROUNDS` | Review 最大轮次 | 5 |
| `MAX_LOCAL_REVIEW_ROUNDS` | 本地 Review 最大轮次 | 5 |

## 自定义 Agent 后端

实现 `AgentRunner` 接口即可切换 LLM 后端：

```python
from agents.runners.base import AgentRunner

class CustomRunner(AgentRunner):
    def execute(self, prompt, timeout, cwd, continue_session):
        ...  # 调用你的 CLI

# agents/base.py
DEFAULT_RUNNER = CustomRunner()
```
