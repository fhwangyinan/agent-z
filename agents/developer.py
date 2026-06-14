import json
import re

from .base import Agent, prompt_with_context
from config import DEVELOPER_BACKEND, TIMEOUT_DEVELOPER


class DeveloperAgent(Agent):
    def __init__(self):
        super().__init__("Developer", DEVELOPER_BACKEND)

    def fix(
        self,
        issue_number: int,
        plan: dict | None = None,
        resume_session: bool = False,
        no_changes_retry: bool = False,
    ) -> str:
        retry_context = ""
        if no_changes_retry:
            retry_context = (
                " The previous development pass reached submission without producing any commit "
                "relative to main. Re-read the issue and inspect the repository carefully. Implement "
                "and test the required change; do not stop merely because the current tree is clean."
            )
        prompt = prompt_with_context(
            "Fix the target issue. Follow the persisted Planner execution plan when provided. Verify "
            "its assumptions against the current code before editing, satisfy its acceptance criteria, "
            f"and gather any additional information you need.{retry_context}",
            issue_number=issue_number,
            execution_plan=plan,
        )
        return self.run(prompt, timeout=TIMEOUT_DEVELOPER, resume_session=resume_session)

    def apply_review(
        self,
        issue_number: int,
        pr_url: str,
        review_comments: list[str] | None = None,
        resume_session: bool = False,
    ) -> str:
        if pr_url:
            prompt = prompt_with_context(
                "PR checks have completed. Read failed checks with `gh pr checks` and all review "
                "feedback with `gh pr view --comments`. If checks pass and no feedback requires "
                "changes, output NO_ACTION_NEEDED. Otherwise fix actionable feedback and commit "
                "locally, but do not push yet.",
                issue_number=issue_number,
                pr_url=pr_url,
            )
        else:
            prompt = prompt_with_context(
                "Address the local Reviewer's findings for the target issue and commit the changes "
                "locally.",
                issue_number=issue_number,
                review_findings=review_comments or [],
            )
        return self.run(prompt, timeout=TIMEOUT_DEVELOPER, resume_session=resume_session)

    def push_and_notify(self, pr_url: str, resume_session: bool = False) -> str:
        prompt = prompt_with_context(
            "Push the current branch, then comment on the target PR with a concise English summary "
            "of the changes and mention @coderabbitai.",
            pr_url=pr_url,
        )
        return self.run(prompt, timeout=600, resume_session=resume_session)

    def prepare_submission(
        self,
        issue_number: int,
        plan: dict | None = None,
        resume_session: bool = False,
    ) -> dict:
        prompt = prompt_with_context(
            "Prepare submission metadata for the completed fix for the target issue. "
            "Inspect the final diff and test results, but do not edit files, commit, push, or create a PR. "
            "Return exactly one JSON object between SUBMISSION_JSON_START and SUBMISSION_JSON_END with "
            "commit_message (a concise imperative conventional-commit subject), pr_title, and pr_body. "
            "The PR body must concisely describe the change, testing performed, and any notable risks.",
            issue_number=issue_number,
            execution_plan=plan,
        )
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
