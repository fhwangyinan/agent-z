import json
import re

from .base import Agent, PROJECT_DIR, GITHUB_REPO
from config import DEVELOPER_BACKEND, TIMEOUT_DEVELOPER


class DeveloperAgent(Agent):
    def __init__(self):
        super().__init__("Developer", DEVELOPER_BACKEND)

    def fix(
        self,
        issue_number: int,
        plan: dict | None = None,
        resume_session: bool = False,
    ) -> str:
        plan_context = ""
        if plan:
            import json
            plan_context = (
                "\nFollow this persisted Planner execution plan. Verify assumptions against the "
                "current code before editing, and satisfy its acceptance criteria:\n"
                f"{json.dumps(plan, ensure_ascii=True, indent=2)}"
            )
        prompt = f"Fix issue #{issue_number}.{plan_context} Gather any additional information you need."
        return self.run(prompt, timeout=TIMEOUT_DEVELOPER, resume_session=resume_session)

    def apply_review(
        self,
        issue_number: int,
        pr_url: str,
        review_comments: list[str] | None = None,
        resume_session: bool = False,
    ) -> str:
        if pr_url:
            prompt = (
                f"Checks for PR {pr_url} have completed. Read failed checks with `gh pr checks` and "
                f"all review feedback with `gh pr view --comments`. If checks pass and no feedback "
                f"requires changes, output NO_ACTION_NEEDED. Otherwise fix actionable feedback and "
                f"commit locally, but do not push yet."
            )
        else:
            findings = "\n".join(f"- {comment}" for comment in review_comments or [])
            prompt = (
                f"Address the local Reviewer's findings for issue #{issue_number} and commit the "
                f"changes locally. Findings:\n{findings}"
            )
        return self.run(prompt, timeout=TIMEOUT_DEVELOPER, resume_session=resume_session)

    def push_and_notify(self, pr_url: str, resume_session: bool = False) -> str:
        prompt = (
            f"Push the current branch, then comment on PR {pr_url} with a concise English summary "
            f"of the changes and mention @coderabbitai."
        )
        return self.run(prompt, timeout=600, resume_session=resume_session)

    def prepare_submission(
        self,
        issue_number: int,
        plan: dict | None = None,
        resume_session: bool = False,
    ) -> dict:
        prompt = (
            f"Prepare submission metadata for the completed fix for issue #{issue_number}. "
            "Inspect the final diff and test results, but do not edit files, commit, push, or create a PR. "
            "Return exactly one JSON object between SUBMISSION_JSON_START and SUBMISSION_JSON_END with "
            "commit_message (a concise imperative conventional-commit subject), pr_title, and pr_body. "
            "The PR body must concisely describe the change, testing performed, and any notable risks."
        )
        if plan:
            prompt += f"\nOriginal execution plan:\n{json.dumps(plan, ensure_ascii=True, indent=2)}"
        output = self.run(
            prompt,
            timeout=TIMEOUT_DEVELOPER,
            resume_session=resume_session,
        )
        match = re.search(
            r"SUBMISSION_JSON_START\s*(\{.*?\})\s*SUBMISSION_JSON_END",
            output,
            re.DOTALL,
        )
        if not match:
            return {}
        try:
            metadata = json.loads(match.group(1))
        except json.JSONDecodeError:
            return {}
        return metadata if isinstance(metadata, dict) else {}
