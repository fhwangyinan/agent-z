from .base import Agent, PROJECT_DIR, GITHUB_REPO
from config import ANALYST_BACKEND, TIMEOUT_ANALYST


class AnalystAgent(Agent):
    def __init__(self):
        super().__init__("Analyst", ANALYST_BACKEND)

    def analyze(self, target_issue: int | None = None, resume_session: bool = False) -> tuple[int, str]:
        if target_issue:
            prompt = (
                f"GitHub repository: {GITHUB_REPO}. Check for related PRs with "
                f"`gh pr list --search '{target_issue} in:body,title'`. If a related PR exists, "
                f"determine whether it fully resolves the issue and continue only when it does not. "
                f"Read issue {target_issue}, its references, and only directly relevant files in "
                f"{PROJECT_DIR}. Assess a practical fix. End with RECOMMENDED_ISSUE={target_issue}."
            )
        else:
            prompt = (
                f"Project: {PROJECT_DIR}. GitHub repository: {GITHUB_REPO}. List open issues and "
                f"open PRs, exclude issues already covered by a complete PR, inspect relevant issue "
                f"details and code, discard obsolete or meaningless issues, then recommend the "
                f"highest-value actionable issue and assess a fix. End with RECOMMENDED_ISSUE=<number>."
            )
        output = self.run(prompt, timeout=TIMEOUT_ANALYST, resume_session=resume_session)
        issue_number = self.extract_number(output, r"RECOMMENDED_ISSUE=(\d+)")
        if issue_number is None:
            issue_number = self.extract_number(output, r"#(\d+)")
        if issue_number is None and target_issue:
            issue_number = target_issue
        return issue_number, output

    def assess_impact(self, issue_number: int, resume_session: bool = False) -> tuple[str, str]:
        """Assess the proposed fix and return (impact_report, risk_level)."""
        prompt = (
            f"Before fixing issue #{issue_number}, assess its potential impact on {PROJECT_DIR}. "
            f"Cover user behavior, APIs and output formats, security and permissions, data integrity, "
            f"affected modules, downstream dependencies, and likely regressions. Assign one risk: "
            f"very_low, low, medium, high, or very_high. Check existing issue comments for an Impact "
            f"Assessment; create one if absent or add an update if present. Write the report in English. "
            f"End with RISK=<risk_level>."
        )
        output = self.run(prompt, timeout=TIMEOUT_ANALYST, resume_session=resume_session)

        risk = self.extract(output, r"RISK=(\S+)") or "unknown"
        risk = risk.lower().strip()
        return output, risk

    def chat(self, question: str) -> str:
        """Continue the Analyst session for interactive questions."""
        return self.run(question, timeout=TIMEOUT_ANALYST, resume_session=True)
