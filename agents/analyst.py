from .base import Agent, PROJECT_DIR, GITHUB_REPO
from config import TIMEOUT_ANALYST


class AnalystAgent(Agent):
    def __init__(self):
        super().__init__("Analyst")

    def analyze(self, target_issue: int | None = None, continue_session: bool = False) -> tuple[int, str]:
        if target_issue:
            prompt = (
                f"GitHub 仓库: {GITHUB_REPO} "
                f"用 gh pr list --search '{target_issue} in:body,title' 检查是否已有关联 PR（含未 merge 的），"
                f"如有则判断该 PR 是否已完整解决（非 draft、非 partial），未完整解决则继续分析。"
                f"用 gh issue view {target_issue} 查看详情和引用的关联 issue，"
                f"只读项目 {PROJECT_DIR} 中与该 issue 直接相关的关键文件，"
                f"评估修复方案。最后一行输出 RECOMMENDED_ISSUE={target_issue}"
            )
        else:
            prompt = (
                f"项目路径: {PROJECT_DIR} GitHub 仓库: {GITHUB_REPO} "
                f"用 gh issue list --state open 获取 open issues，用 gh pr list --state open 获取已有 PR，检查是否已有关联 PR（含未 merge 的），排除这些已有PR的issue。"
                f"查看issue详情和任何相关 issue。"
                f"只读剩余 issue 关联的关键文件判断是否仍适用或已过时，"
                f"排除无意义 issue 后推荐最优先修复的一个，并评估修复方案。最后一行输出 RECOMMENDED_ISSUE=<数字>"
            )
        output = self.run(prompt, timeout=TIMEOUT_ANALYST, continue_session=continue_session)
        issue_number = self.extract_number(output, r"RECOMMENDED_ISSUE=(\d+)")
        if issue_number is None:
            issue_number = self.extract_number(output, r"#(\d+)")
        if issue_number is None and target_issue:
            issue_number = target_issue
        return issue_number, output
