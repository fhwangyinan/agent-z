# Agent-Z — Multi-Agent GitHub Issue Auto-Fix

Multi-Agent 自动化脚本，使用 Claude Code 驱动多个专项 Agent，自动分析、修复 GitHub Issue，创建 PR，配合 CodeRabbitAI Review 迭代修复。

```
Analyst → Developer → Reviewer → Submitter → CodeRabbit → Developer
```

## 前置条件

- Python 3.11+
- [GitHub CLI](https://cli.github.com/) (`gh`) 已认证
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) (`claude`) 已安装并配置
- 目标仓库已安装 [CodeRabbitAI](https://coderabbit.ai/) GitHub App

## 安装

```bash
# 创建虚拟环境
python -m venv .venv

# 激活虚拟环境 (Windows)
.venv\Scripts\activate

# 激活虚拟环境 (Linux/macOS)
source .venv/bin/activate

# 安装依赖
pip install -r requirements.txt
```

## 配置

```bash
# 复制模板
cp .env.example .env

# 按需修改
notepad .env
```

所有配置项见 `.env.example`，主要：

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `PROJECT_DIR` | 目标项目路径 | — |
| `GITHUB_REPO` | 目标仓库 (owner/repo) | — |
| `TIMEOUT_DEVELOPER` | Developer 超时 (秒) | 10800 |
| `CODERABBIT_MAX_WAIT` | CodeRabbit 最大等待 (秒) | 900 |
| `MAX_REVIEW_ROUNDS` | 最大 Review 轮次 | 5 |

## 使用

### 交互模式

```powershell
python run.py
```

每轮可选：Agent 自动推荐 Issue 或手动指定编号。

### 自动模式

```powershell
python run.py --loop 5
```

连续跑 5 轮，跳过所有确认，全自动运行。

## 架构

```
run.py                    主协调器，控制流程和 session 管理
config.py                 统一配置（从 .env 加载）
agents/
  base.py                 Agent 基类、日志、命令执行
  analyst.py              分析 Agent — 评估 Issue 优先级和范围
  developer.py            开发 Agent — 修复代码和处理 Review
  reviewer.py             审阅 Agent — 本地 Code Review
  submitter.py            提交 Agent — 创建分支、commit、PR
  runners/
    base.py               AgentRunner 抽象基类
    claude.py             ClaudeRunner — claude -p 实现
```

## Agent 工作流

1. **准备环境** — git checkout main, git pull
2. **选择模式** — Agent 推荐或手动指定 Issue
3. **Analyst** — 获取 open issues，排除已有完整 PR 的，推荐最优先项
4. **Developer** — 通过 `--continue` 共享 session，直接修复
5. **Reviewer** — 本地预审 (git diff + 测试)，发现问题交 Developer 修复
6. **Submitter** — 创建分支、commit、push、创建 PR
7. **CodeRabbit** — 等待 check 完成 → Developer 读取 review → 修复 → 本地 Reviewer 复查通过 → push + @coderabbitai → 循环直到通过或无需修改

## 接入新 Agent

实现 `AgentRunner` 接口，替换 `DEFAULT_RUNNER` 即可：

```python
# agents/runners/custom.py
from .base import AgentRunner
class CustomRunner(AgentRunner):
    def execute(self, prompt, timeout, cwd, continue_session):
        # 调用你的 agent CLI
        ...

# agents/base.py
DEFAULT_RUNNER = CustomRunner()
```
