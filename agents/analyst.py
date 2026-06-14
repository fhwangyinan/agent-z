import json
import re

from .base import Agent, PROJECT_DIR, GITHUB_REPO, prompt_with_context
from config import ANALYST_BACKEND, SKIP_LABELS, TIMEOUT_ANALYST


class AnalystAgent(Agent):
    def __init__(self):
        super().__init__("Analyst", ANALYST_BACKEND)

    def analyze(
        self,
        target_issue: int | None = None,
        resume_session: bool = False,
    ) -> tuple[int, str]:
        if target_issue:
            prompt = prompt_with_context(
                "Analyze the target GitHub issue. Check only open related PRs with `gh pr list "
                "--state open --search '<issue_number> in:body,title'`. Do not list closed or "
                "merged PRs. If an open related PR exists, determine whether it fully resolves the "
                "issue and continue only when it does not. Read the issue, its references, and only "
                "directly relevant files. Assess a practical fix. End with "
                "RECOMMENDED_ISSUE=<issue_number>.",
                github_repository=GITHUB_REPO,
                project_checkout=PROJECT_DIR,
                issue_number=target_issue,
            )
        else:
            prompt = prompt_with_context(
                "Explore open issues and open "
                "PRs as needed, using targeted or paginated queries. Do not inspect closed issues or "
                "closed/merged PRs unless a specific reference requires it. Avoid loading the full open "
                "backlog when a smaller targeted query is sufficient. Exclude issues with any configured "
                "skip label. Exclude issues already covered by an open PR that appears complete, "
                "inspect promising issue details and directly relevant code, then recommend the highest-value "
                "actionable issue and assess a fix. End with RECOMMENDED_ISSUE=<number>.",
                github_repository=GITHUB_REPO,
                project_checkout=PROJECT_DIR,
                skip_labels=SKIP_LABELS,
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
        prompt = prompt_with_context(
            "Before fixing the target issue, assess its potential impact. Cover user behavior, APIs "
            "and output formats, security and permissions, data integrity, affected modules, downstream "
            "dependencies, and likely regressions. Assign one risk: very_low, low, medium, high, or "
            "very_high. Check existing issue comments for an Impact Assessment; create one if absent "
            "or add an update if present. Write the report in English. End with RISK=<risk_level>.",
            issue_number=issue_number,
            project_checkout=PROJECT_DIR,
        )
        output = self.run(prompt, timeout=TIMEOUT_ANALYST, resume_session=resume_session)

        risk = self.extract(output, r"RISK=(\S+)") or "unknown"
        risk = risk.lower().strip()
        return output, risk

    def build_plan(
        self,
        issue_number: int,
        analysis: str,
        impact: str,
        risk: str,
        resume_session: bool = False,
    ) -> dict:
        prompt = prompt_with_context(
            "Create a concrete implementation plan for the target issue from the analysis and "
            "impact assessment already in this session. Return one JSON object between PLAN_JSON_START "
            "and PLAN_JSON_END. It must contain: summary (string), recommended_changes (string array), "
            "acceptance_criteria (string array), affected_modules (string array), predicted_files "
            "(string array), risks (string array), and test_plan (string array). Publish or update a "
            "concise English `Agent-Z Execution Plan` comment on the issue for humans. Do not make code changes.",
            issue_number=issue_number,
        )
        output = self.run(prompt, timeout=TIMEOUT_ANALYST, resume_session=resume_session)
        match = re.search(
            r"PLAN_JSON_START\s*(\{.*?\})\s*PLAN_JSON_END",
            output,
            re.DOTALL,
        )
        if match:
            try:
                plan = json.loads(match.group(1))
                if isinstance(plan, dict):
                    plan["risk"] = risk
                    return plan
            except json.JSONDecodeError:
                pass
        return {
            "summary": analysis.strip(),
            "recommended_changes": [],
            "acceptance_criteria": [],
            "affected_modules": [],
            "predicted_files": [],
            "risks": [impact.strip()],
            "test_plan": [],
            "risk": risk,
            "planner_output": output.strip(),
        }

    def chat(self, question: str) -> str:
        """Continue the Analyst session for interactive questions."""
        return self.run(question, timeout=TIMEOUT_ANALYST, resume_session=True)
