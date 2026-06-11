# Agent-Z — 多智能体 GitHub Issue 自动修复

Multi-Agent 自动化脚本，使用 Claude Code 自动分析、修复 GitHub Issue，创建 PR，并配合 CodeRabbitAI Review 迭代修复。

```
Analyst -> Developer -> Reviewer -> Submitter -> CodeRabbit -> Developer
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

## 使用

### 交互模式

```powershell
python run.py
```

每轮可选让 Agent 自动推荐 Issue 或手动指定编号，修复后需确认是否继续。

### 自动模式

```powershell
python run.py --loop 5
```

连续跑 5 轮，跳过所有确认，全自动运行。

## 配置

编辑 `agents/base.py` 中的全局配置：

```python
PROJECT_DIR = r"G:\Code\workspace_aieng"       # 目标项目路径
GITHUB_REPO = "owner/repo"                      # GitHub 仓库
```

编辑 `run.py` 中的运行参数：

```python
CODERABBIT_POLL_INTERVAL = 45    # CodeRabbit 轮询间隔 (秒)
CODERABBIT_MAX_WAIT = 600        # 最大等待时间 (秒)
MAX_REVIEW_ROUNDS = 3            # 最大 Review 轮次
MAX_LOCAL_REVIEW_ROUNDS = 2      # 本地 Review 最大轮次
```

## 架构

```
run.py                    主协调器，控制流程和 session 管理
agents/
  base.py                  Agent 基类、日志、命令执行
  analyst.py               分析 Agent — 评估 Issue 优先级和范围
  developer.py             开发 Agent — 修复代码和处理 Review
  reviewer.py              审阅 Agent — 本地 Code Review
  submitter.py             提交 Agent — 创建分支、commit、PR
```

所有 Agent 通过 `--continue` 共享同一个 Claude Code session，上下文自然继承，无需重复读取代码。

## 工作流程

1. **准备环境** — git checkout main, git pull
2. **选择模式** — Agent 推荐或手动指定 Issue
3. **Analyst** — 获取 open issues，排除已有 PR 的，推荐最优先项
4. **Developer** — 修复代码（通过 `--continue` 复用 Analyst 的上下文）
5. **Reviewer** — 本地预审，发现的问题交 Developer 修复
6. **Submitter** — 创建分支、commit、push、创建 PR
7. **CodeRabbit** — 等待 Review，Developer 根据 Review 修复，本地 Reviewer 复查通过后 push，循环直到通过
