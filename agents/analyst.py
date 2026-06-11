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

    def assess_impact(self, issue_number: int, continue_session: bool = False) -> tuple[str, str]:
        """评估 fix 对项目的潜在影响，返回 (impact_report, risk_level)"""
        prompt = (
            f"在修复 Issue #{issue_number} 之前，先评估此修复对项目 {PROJECT_DIR} 的潜在影响。"
            f"重点关注：1.是否改变现有流程或用户行为  2.是否影响 API/接口/输出格式（破坏性变更）"
            f" 3.是否涉及安全/权限/数据一致性  4.受影响的模块和下游依赖  5.潜在风险。"
            f"评估后给出风险等级："
            f"very_low（无影响）、low（轻微影响）、medium（中等影响）、high（显著影响，如行为/API变化）、very_high（破坏性变更，改变现有流程或输出结果）。"
            f"用英文撰写评估报告。"
            f"先用 gh issue view {issue_number} --comments 检查是否已有 Impact Assessment 评论，"
            f"若无则用 gh issue comment {issue_number} 新增，若有则在已有评论下回复补充更新。"
            f"最后一行输出 RISK=<风险等级>"
        )
        output = self.run(prompt, timeout=TIMEOUT_ANALYST, continue_session=continue_session)

        risk = self.extract(output, r"RISK=(\S+)") or "unknown"
        risk = risk.lower().strip()
        return output, risk

    def chat(self, question: str) -> str:
        """交互问答，继续当前 session"""
        return self.run(question, timeout=TIMEOUT_ANALYST, continue_session=True)
