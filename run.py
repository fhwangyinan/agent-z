#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Multi-Agent 自动 Issue 修复 - TUI 版

Usage:
  python run.py                交互模式，每轮确认
  python run.py --loop 5       自动模式，跑 5 轮，无需确认
"""

import argparse
import json
import sys
import time
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn
from rich.prompt import Prompt, Confirm, IntPrompt
from rich.table import Table
from rich.text import Text

AUTO_MODE = False
TOTAL_LOOPS = 0
CURRENT_LOOP = 0
from rich.align import Align

from agents.base import log, step, done, warn, error, run_cmd, PROJECT_DIR, GITHUB_REPO, agent_status
from agents.analyst import AnalystAgent
from agents.developer import DeveloperAgent
from agents.reviewer import ReviewerAgent
from agents.submitter import SubmitterAgent

console = Console()

from config import (
    CODERABBIT_POLL_INTERVAL,
    CODERABBIT_MAX_WAIT,
    MAX_REVIEW_ROUNDS,
    MAX_LOCAL_REVIEW_ROUNDS,
)


def show_banner():
    console.print()
    console.print(Align.center(
        Panel.fit(
            "[bold cyan]Multi-Agent Issue Fix[/bold cyan]\n"
            f"[dim]Target: {GITHUB_REPO}[/dim]",
            border_style="cyan",
            padding=(0, 8),
        )
    ))
    console.print(Align.center(
        "[dim]🔍 Analyst[/dim] → [dim]🔧 Developer[/dim] → [dim]👁 Reviewer[/dim] → [dim]🚀 Submitter[/dim] → [dim]🐰 CodeRabbit[/dim]"
    ))


def prepare_environment():
    step("📦 准备环境")
    status = run_cmd(["git", "status", "--short"], check=False)
    if status.stdout.strip():
        log("储藏未提交变更...")
        run_cmd(["git", "stash", "push", "-m", "auto-fix-stash"], check=False)
    run_cmd(["git", "checkout", "main"])
    run_cmd(["git", "pull", "origin", "main"], verbose=True)
    done("环境就绪")


def _coderabbit_check_state(pr_url: str) -> str:
    result = run_cmd(["gh", "pr", "view", pr_url, "--json", "statusCheckRollup"], check=False)
    if result.returncode != 0 or not result.stdout.strip():
        return "not_found"
    try:
        for item in json.loads(result.stdout).get("statusCheckRollup", []):
            key = item.get("context", "") or item.get("name", "")
            if "coderabbit" in key.lower():
                state = (item.get("state", "") or item.get("status", "")).lower()
                if state in ("success", "failure", "error", "completed"):
                    return "done"
                return "pending"
    except Exception:
        pass
    return "not_found"


def wait_for_coderabbit_review(pr_url: str) -> bool:
    step("🐰 等待 CodeRabbitAI Review")
    elapsed = 0
    with Progress(
        SpinnerColumn(spinner_name="dots"),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(bar_width=30),
        console=console,
        transient=True,
    ) as progress:
        task = progress.add_task("[dim]等待 CodeRabbitAI check...[/dim]", total=CODERABBIT_MAX_WAIT)
        while elapsed < CODERABBIT_MAX_WAIT:
            time.sleep(CODERABBIT_POLL_INTERVAL)
            elapsed += CODERABBIT_POLL_INTERVAL
            state = _coderabbit_check_state(pr_url)
            desc = f"[dim]CodeRabbit 运行中 [yellow]{elapsed}s[/yellow] / {CODERABBIT_MAX_WAIT}s[/dim]"
            if state == "done":
                done("CodeRabbitAI review 完成 -> 交给 Developer")
                return True
            if state == "pending":
                desc = f"[dim]CodeRabbit 运行中 [yellow]{elapsed}s[/yellow] / {CODERABBIT_MAX_WAIT}s[/dim]"
            else:
                desc = f"[dim]等待 CodeRabbit review... [yellow]{elapsed}s[/yellow] / {CODERABBIT_MAX_WAIT}s[/dim]"
            progress.update(task, completed=elapsed, description=desc)
    warn("等待超时")
    return False


def _get_issue_title(issue_number: int) -> str:
    try:
        result = run_cmd(
            ["gh", "issue", "view", str(issue_number), "--json", "title,labels,state"],
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            data = json.loads(result.stdout)
            title = data.get("title", "(无标题)")
            labels = "  ".join(f"[{l['name']}]" for l in data.get("labels", []))
            return f"{title}\n[dim]{labels}[/dim]" if labels else title
    except Exception:
        pass
    return "(无法获取标题)"


def choose_issue() -> int | None:
    console.print()
    if AUTO_MODE:
        loop_info = f" (第 {CURRENT_LOOP}/{TOTAL_LOOPS} 轮)" if TOTAL_LOOPS > 1 else ""
        console.print(Panel(
            f"[bold]自动模式：Agent 自主推荐 issue{loop_info}[/bold]",
            border_style="cyan",
        ))
        return None

    table = Table(show_header=False, box=None, padding=(0, 4))
    table.add_column(style="cyan")
    table.add_column()
    table.add_row("[bold][1][/bold]", "让 Agent 自动分析并推荐最优先的 issue")
    table.add_row("[bold][2][/bold]", "手动指定 issue 编号")
    console.print(Panel(table, title="[bold]选择模式[/bold]", border_style="cyan"))
    choice = Prompt.ask("", choices=["1", "2"], default="1")
    if choice == "1":
        return None
    else:
        return IntPrompt.ask("请输入 Issue 编号")


def confirm_issue(issue_number: int) -> int | None:
    title = _get_issue_title(issue_number)
    console.print(Panel(
        title,
        title=f"[bold green]#{issue_number}[/bold green]",
        border_style="green",
        subtitle="确认? (y/n 或输入其他编号)",
        subtitle_align="left",
    ))

    if AUTO_MODE:
        return issue_number

    while True:
        user_input = Prompt.ask("", default="y").strip()
        if user_input.lower() in ("y", "yes", "是", ""):
            return issue_number
        elif user_input.lower() in ("n", "no", "否"):
            return None
        elif user_input.isdigit():
            new_num = int(user_input)
            title = _get_issue_title(new_num)
            console.print(f"  → 切换至 [bold]#{new_num}[/bold]")
            console.print(Panel(title, title=f"[bold green]#{new_num}[/bold green]", border_style="green"))
            return new_num
        else:
            warn("无效输入")


def show_analysis(issue_number: int, analysis: str):
    """用 Panel 展示分析结果"""
    display = analysis[:2000] + ("..." if len(analysis) > 2000 else "")
    console.print(Panel(display, title=f"[bold cyan]🔍 Analyst → Issue #{issue_number}[/bold cyan]", border_style="blue"))


def main():
    show_banner()

    analyst = AnalystAgent()
    developer = DeveloperAgent()
    reviewer = ReviewerAgent()
    submitter = SubmitterAgent()

    while True:
        try:
            prepare_environment()

            # 选择模式
            target = choose_issue()

            # Analyst
            step("🔍 Analyst 分析")
            if target:
                issue_number, analysis = analyst.analyze(target_issue=target, continue_session=False)
            else:
                issue_number, analysis = analyst.analyze(continue_session=False)

            if issue_number is None:
                error("Analyst 未能推荐 issue")
                continue

            show_analysis(issue_number, analysis)

            # 确认
            issue_number = confirm_issue(issue_number)
            if issue_number is None:
                log("跳过，下一轮")
                continue

            # 影响评估
            step("🔍 影响评估")
            impact, risk = analyst.assess_impact(issue_number, continue_session=True)
            console.print(Panel(impact[:2500], title=f"影响评估 [yellow]风险: {risk}[/yellow]", border_style="yellow"))

            if AUTO_MODE:
                if risk in ("medium", "high", "very_high"):
                    warn(f"风险 [{risk}]，自动跳过 → 换下一个 issue")
                    continue
            else:
                console.print("\n[dim]风险等级: [bold]{0}[/bold]  有疑问可输入问题，空回车继续[/dim]".format(risk))
                while True:
                    q = Prompt.ask("", default="").strip()
                    if not q:
                        break
                    if q.lower() in ("skip", "s"):
                        log("用户跳过 → 换下一个 issue")
                        issue_number = None
                        break
                    if q.lower() in ("done", "ok", "go", "y"):
                        break
                    answer = analyst.chat(q)
                    console.print(Panel(answer[:2000], border_style="dim"))
                if issue_number is None:
                    continue

            # Developer
            step("🔧 Developer 修复")
            developer.fix(issue_number, continue_session=True)

            # Reviewer
            step("👁 Reviewer 本地预审")
            for local_round in range(MAX_LOCAL_REVIEW_ROUNDS):
                review_comments = reviewer.review(issue_number, continue_session=True)
                if not review_comments:
                    done("Reviewer 通过 (LGTM)")
                    break

                warn(f"Reviewer 发现 [bold]{len(review_comments)}[/bold] 条问题 (第 {local_round + 1} 轮)")
                for i, c in enumerate(review_comments, 1):
                    console.print(f"    [yellow]{i}.[/yellow] {c[:300]}")

                developer.apply_review(issue_number, "", continue_session=True)
            else:
                warn("多轮 Review 后仍有未解决问题，继续提交")

            # Submitter
            step("🚀 Submitter 创建 PR")
            pr_url = submitter.submit(issue_number, continue_session=True)
            if not pr_url:
                error("Submitter 未能创建 PR")
                continue
            console.print(Panel(
                f"[link={pr_url}]{pr_url}[/link]",
                title="[bold green]🚀 PR 已创建[/bold green]",
                border_style="green",
            ))

            # CodeRabbit
            review_count = 0
            while review_count < MAX_REVIEW_ROUNDS:
                has_review = wait_for_coderabbit_review(pr_url)
                if not has_review:
                    break

                step(f"🔧 Developer 修复 CodeRabbit Review (第 {review_count + 1}/{MAX_REVIEW_ROUNDS} 轮)")
                dev_output = developer.apply_review(issue_number, pr_url, continue_session=True)
                if "NO_ACTION_NEEDED" in dev_output.upper():
                    done("Developer 判断无需修改 → 结束")
                    break

                # 本地 Reviewer 审查
                for local_round in range(MAX_LOCAL_REVIEW_ROUNDS):
                    step(f"👁 本地 Reviewer 复查 (第 {local_round + 1} 轮)")
                    review_comments = reviewer.review(issue_number, continue_session=True)
                    if not review_comments:
                        done("本地 Reviewer 通过 → push")
                        developer.push_and_notify(pr_url, continue_session=True)
                        break
                    warn(f"本地 Reviewer 发现 [bold]{len(review_comments)}[/bold] 条问题")
                    developer.apply_review(issue_number, "", continue_session=True)

                review_count += 1
                time.sleep(120)
            else:
                warn(f"达到最大 Review 轮次 ({MAX_REVIEW_ROUNDS})")

            console.print(Panel(
                f"[bold green]✅ Issue #{issue_number} 处理完成[/bold green]\n[dim]{pr_url}[/dim]",
                border_style="green",
            ))

        except KeyboardInterrupt:
            console.print("\n[yellow]用户中断[/yellow]")
            break
        except Exception as e:
            error(str(e))
            import traceback
            traceback.print_exc()

        console.print()
        if AUTO_MODE:
            break
        if not Confirm.ask("[bold]是否进入下一轮?[/bold]", default=True):
            done("脚本退出")
            break


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Multi-Agent 自动 Issue 修复")
    parser.add_argument("--loop", type=int, default=0, metavar="N",
                        help="自动模式：跑 N 轮，跳过所有确认")
    args = parser.parse_args()

    if args.loop > 0:
        AUTO_MODE = True
        TOTAL_LOOPS = args.loop
        console.print(f"[bold cyan]自动模式[/bold cyan] [dim]共 {TOTAL_LOOPS} 轮[/dim]")
        for i in range(TOTAL_LOOPS):
            CURRENT_LOOP = i + 1
            main()
        done(f"全部 {TOTAL_LOOPS} 轮完成")
    else:
        main()
