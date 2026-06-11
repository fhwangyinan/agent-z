# Agent-Z

[English](README.md)

轻量级 coding-agent 驱动脚本，实现自主开发循环。

通过 Claude Code 驱动多个专项 Agent（分析师、开发者、审阅者、提交者），构成全自动闭环：分析 issue → 修复代码 → 本地审查 → 提 PR → 迭代 CI 反馈 → 循环。

```
Analyst → Developer → Reviewer → Submitter → CodeRabbit → Developer → ...
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
python run.py               # 交互模式，每轮需确认
python run.py --loop 5      # 自主模式，连续 5 轮无人值守
```

每轮 Agent 团队自主完成：

1. 挑选最优 open issue（或手动指定）
2. 阅读代码并修复
3. 本地 Reviewer 审查
4. 创建 PR
5. 等待 CI / CodeRabbit 反馈，修复，push — 循环直到通过

## 架构原理

```
run.py                    主协调器 — 管理循环和 session 生命周期
config.py                 从 .env 加载配置
agents/
  base.py                 Agent 基类，上下文感知 runner
  analyst.py              分析 Agent — 评估 issue 并推荐
  developer.py            开发 Agent — 阅读代码、修复、处理 review
  reviewer.py             审阅 Agent — 提交前本地 review (diff + 测试)
  submitter.py            提交 Agent — 分支、commit、push、PR
  runners/
    base.py               抽象 runner 接口 — 可切换后端
    claude.py             Claude Code runner (`claude -p`)
```

所有 Agent 通过 `--continue` 共享同一个 session，上下文自然流转 — 开发者能看到分析师的分析结论，审阅者能看到开发者的改动。无需重复读代码，无需复制粘贴 prompt。

## 配置

全部配置项见 `.env.example`，主要：

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `PROJECT_DIR` | 目标项目路径 | — |
| `GITHUB_REPO` | owner/repo | — |
| `TIMEOUT_DEVELOPER` | 开发超时 (秒) | 10800 |
| `CODERABBIT_MAX_WAIT` | CodeRabbit 最大等待 (秒) | 600 |
| `MAX_REVIEW_ROUNDS` | 最大 Review 轮次 | 3 |

## 接入自定义 Agent

实现 `AgentRunner` 接口即可切换后端：

```python
from agents.runners.base import AgentRunner

class GeminiRunner(AgentRunner):
    def execute(self, prompt, timeout, cwd, continue_session):
        ...  # 调用你的 CLI

# agents/base.py
DEFAULT_RUNNER = GeminiRunner()
```
