from .base import Agent, PROJECT_DIR, GITHUB_REPO
from config import DEVELOPER_BACKEND, TIMEOUT_DEVELOPER


class DeveloperAgent(Agent):
    def __init__(self):
        super().__init__("Developer", DEVELOPER_BACKEND)

    def fix(self, issue_number: int, resume_session: bool = False) -> str:
        prompt = f"Fix issue #{issue_number}. Gather any additional information you need."
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
