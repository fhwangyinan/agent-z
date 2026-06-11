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
import os
import shutil
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager

from rich.align import Align
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt, Confirm, IntPrompt
from rich.table import Table

AUTO_MODE = False
TOTAL_LOOPS = 0
CURRENT_LOOP = 0
FORCE_DEVELOP = False

from agents.base import log, step, done, warn, error, run_cmd, PROJECT_DIR, GITHUB_REPO
from agents.analyst import AnalystAgent
from agents.developer import DeveloperAgent
from agents.reviewer import ReviewerAgent
from agents.submitter import SubmitterAgent

console = Console()

from config import (
    PR_CHECKS_INTERVAL,
    PR_CHECKS_MAX_WAIT,
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


def validate_environment():
    if not os.path.isdir(PROJECT_DIR):
        raise RuntimeError(f"PROJECT_DIR does not exist: {PROJECT_DIR}")
    if not GITHUB_REPO or "/" not in GITHUB_REPO:
        raise RuntimeError(f"GITHUB_REPO must use owner/repo format, got {GITHUB_REPO!r}")
    for command in ("git", "gh", "claude"):
        if not shutil.which(command):
            raise RuntimeError(f"required command not found: {command}")
    result = run_cmd(["git", "rev-parse", "--is-inside-work-tree"], check=False)
    if result.returncode != 0 or result.stdout.strip() != "true":
        raise RuntimeError(f"PROJECT_DIR is not a Git work tree: {PROJECT_DIR}")


def _find_stash_ref(message: str) -> str | None:
    result = run_cmd(["git", "stash", "list", "--format=%gd%x09%gs"], check=False)
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        ref, _, subject = line.partition("\t")
        if message in subject:
            return ref
    return None


@contextmanager
def managed_environment():
    step("📦 准备环境")
    original_branch = run_cmd(
        ["git", "symbolic-ref", "--short", "-q", "HEAD"], check=False
    ).stdout.strip()
    original_ref = original_branch or run_cmd(["git", "rev-parse", "HEAD"]).stdout.strip()
    stash_message = f"agent-z-auto-stash-{uuid.uuid4().hex}"
    created_stash = False

    status = run_cmd(["git", "status", "--short"], check=False)
    if status.stdout.strip():
        log("储藏未提交变更...")
        result = run_cmd(["git", "stash", "push", "-u", "-m", stash_message], check=False)
        created_stash = result.returncode == 0 and _find_stash_ref(stash_message) is not None
        if not created_stash:
            raise RuntimeError("failed to stash local changes; aborting before checkout")

    try:
        run_cmd(["git", "checkout", "main"])
        run_cmd(["git", "pull", "origin", "main"], verbose=True)
        done("环境就绪")
        yield
    finally:
        step("📦 恢复原始工作区")
        checkout = run_cmd(["git", "checkout", original_ref], check=False)
        if checkout.returncode != 0:
            warn("无法切回原始分支/提交；保留自动 stash，避免覆盖当前修改")
        elif created_stash:
            stash_ref = _find_stash_ref(stash_message)
            if not stash_ref:
                warn("未找到本轮自动 stash，请检查 git stash list")
            else:
                applied = run_cmd(["git", "stash", "apply", stash_ref], check=False)
                if applied.returncode != 0:
                    warn(f"恢复本地修改发生冲突，已保留 {stash_ref}")
                else:
                    run_cmd(["git", "stash", "drop", stash_ref], check=False)
                    done("原始工作区已恢复")
        else:
            done("原始工作区已恢复")


def _get_pr_checks(pr_url: str) -> list[dict] | None:
    result = run_cmd(
        ["gh", "pr", "checks", pr_url, "--json", "name,bucket"],
        check=False,
    )
    if result.returncode != 0:
        message = f"{result.stdout}\n{result.stderr}".lower()
        if "no checks reported" in message:
            return []
        return None
    if not result.stdout.strip():
        return None
    try:
        checks = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    return checks if isinstance(checks, list) else None


def wait_for_pr_checks(pr_url: str) -> bool:
    step("✅ 等待 PR Checks")
    deadline = time.monotonic() + PR_CHECKS_MAX_WAIT
    checks = _get_pr_checks(pr_url)
    while checks == [] and time.monotonic() < deadline:
        log("[dim]Checks 尚未注册，稍后重试...[/dim]")
        time.sleep(min(PR_CHECKS_INTERVAL, max(0, deadline - time.monotonic())))
        checks = _get_pr_checks(pr_url)

    if checks is None:
        warn("无法查询 PR Checks")
        return False
    if not checks:
        warn("等待超时，PR Checks 仍未注册")
        return False

    remaining = max(1, int(deadline - time.monotonic()))
    try:
        with console.status("[dim]gh pr checks --watch 运行中...[/dim]", spinner="dots"):
            run_cmd(
                [
                    "gh", "pr", "checks", pr_url, "--watch",
                    "--interval", str(PR_CHECKS_INTERVAL),
                ],
                check=False,
                timeout=remaining,
                verbose=True,
            )
    except subprocess.TimeoutExpired:
        warn("等待 PR Checks 超时")
        return False

    checks = _get_pr_checks(pr_url)
    if checks is None:
        warn("无法确认 PR Checks 最终状态")
        return False
    if not checks or any(check.get("bucket") == "pending" for check in checks):
        warn("PR Checks 尚未全部结束")
        return False

    failed = [check.get("name", "(unnamed)") for check in checks if check.get("bucket") == "fail"]
    if failed:
        warn(f"PR Checks 完成，失败项: {', '.join(failed)}")
    else:
        done("PR Checks 已全部结束")
    return True


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


def run_local_review(
    issue_number: int,
    reviewer: ReviewerAgent,
    developer: DeveloperAgent,
    *,
    follow_up: bool = False,
) -> bool:
    for local_round in range(MAX_LOCAL_REVIEW_ROUNDS):
        if follow_up:
            step(f"👁 本地 Reviewer 复查 (第 {local_round + 1} 轮)")
        review_comments = reviewer.review(issue_number, continue_session=True)
        if not review_comments:
            done("Reviewer 通过 (LGTM)")
            return True

        warn(f"Reviewer 发现 [bold]{len(review_comments)}[/bold] 条问题 (第 {local_round + 1} 轮)")
        for i, comment in enumerate(review_comments, 1):
            console.print(f"    [yellow]{i}.[/yellow] {comment[:300]}")
        developer.apply_review(issue_number, "", continue_session=True)

    warn(f"达到本地 Review 最大轮次 ({MAX_LOCAL_REVIEW_ROUNDS})，停止当前流程")
    return False


def run_round(
    analyst: AnalystAgent,
    developer: DeveloperAgent,
    reviewer: ReviewerAgent,
    submitter: SubmitterAgent,
) -> bool:
    with managed_environment():
        target = choose_issue()

        step("🔍 Analyst 分析")
        if target:
            issue_number, analysis = analyst.analyze(target_issue=target, continue_session=False)
        else:
            issue_number, analysis = analyst.analyze(continue_session=False)

        if issue_number is None:
            error("Analyst 未能推荐 issue")
            return False

        show_analysis(issue_number, analysis)
        issue_number = confirm_issue(issue_number)
        if issue_number is None:
            log("跳过，下一轮")
            return False

        step("🔍 影响评估")
        impact, risk = analyst.assess_impact(issue_number, continue_session=True)
        console.print(Panel(impact[:2500], title=f"影响评估 [yellow]风险: {risk}[/yellow]", border_style="yellow"))

        if AUTO_MODE and not FORCE_DEVELOP:
            if risk in ("high", "very_high"):
                warn(f"风险 [{risk}]，自动跳过 → 换下一个 issue")
                return False
        else:
            console.print("\n[dim]风险: [bold]{0}[/bold]  |  输入问题追问  |  [bold]skip[/bold] 换 issue  |  [bold]done[/bold]/回车 开始开发[/dim]".format(risk))
            while True:
                question = Prompt.ask("", default="").strip()
                if not question:
                    break
                if question.lower() in ("skip", "s"):
                    log("用户跳过 → 换下一个 issue")
                    return False
                if question.lower() in ("done", "ok", "go", "y"):
                    break
                answer = analyst.chat(question)
                console.print(Panel(answer[:2000], border_style="dim"))

        step("🔧 Developer 修复")
        developer.fix(issue_number, continue_session=True)

        step("👁 Reviewer 本地预审")
        if not run_local_review(issue_number, reviewer, developer):
            return False

        step("🚀 Submitter 创建 PR")
        pr_url = submitter.submit(issue_number, continue_session=True)
        if not pr_url:
            error("Submitter 未能创建 PR")
            return False
        console.print(Panel(
            f"[link={pr_url}]{pr_url}[/link]",
            title="[bold green]🚀 PR 已创建[/bold green]",
            border_style="green",
        ))

        for review_count in range(MAX_REVIEW_ROUNDS):
            if not wait_for_pr_checks(pr_url):
                return False

            step(f"🔧 Developer 处理 PR 反馈 (第 {review_count + 1}/{MAX_REVIEW_ROUNDS} 轮)")
            dev_output = developer.apply_review(issue_number, pr_url, continue_session=True)
            if "NO_ACTION_NEEDED" in dev_output.upper():
                done("Developer 判断无需修改 → 结束")
                break

            if not run_local_review(issue_number, reviewer, developer, follow_up=True):
                return False
            done("本地 Reviewer 通过 → push")
            developer.push_and_notify(pr_url, continue_session=True)
        else:
            warn(f"达到最大 Review 轮次 ({MAX_REVIEW_ROUNDS})，停止当前流程")
            return False

        console.print(Panel(
            f"[bold green]✅ Issue #{issue_number} 处理完成[/bold green]\n[dim]{pr_url}[/dim]",
            border_style="green",
        ))
        return True


def main():
    validate_environment()
    show_banner()
    analyst = AnalystAgent()
    developer = DeveloperAgent()
    reviewer = ReviewerAgent()
    submitter = SubmitterAgent()

    while True:
        try:
            run_round(analyst, developer, reviewer, submitter)

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
    parser.add_argument("--force", action="store_true",
                        help="即便高风险也继续开发（需配合 --loop）")
    args = parser.parse_args()
    if args.force and args.loop <= 0:
        parser.error("--force requires --loop N")

    if args.loop > 0:
        AUTO_MODE = True
        TOTAL_LOOPS = args.loop
        FORCE_DEVELOP = args.force
        console.print(f"[bold cyan]自动模式[/bold cyan] [dim]共 {TOTAL_LOOPS} 轮[/dim]")
        if FORCE_DEVELOP:
            console.print("[yellow]⚠ 强制模式：忽略风险级别[/yellow]")
        for i in range(TOTAL_LOOPS):
            CURRENT_LOOP = i + 1
            main()
        done(f"全部 {TOTAL_LOOPS} 轮完成")
    else:
        main()
