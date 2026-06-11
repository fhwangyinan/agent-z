from .base import Agent
from config import SUBMITTER_BACKEND, TIMEOUT_SUBMITTER


class SubmitterAgent(Agent):
    def __init__(self):
        super().__init__("Submitter", SUBMITTER_BACKEND)

    def submit(self, issue_number: int, resume_session: bool = False) -> str:
        prompt = (
            f"Use the current worktree branch for the fix to issue #{issue_number}. Commit, push, and "
            f"open a PR against main. Link issue #{issue_number} in the PR body and leave an English issue comment "
            f"summarizing the completed work. End with PR_URL=<url>."
        )
        output = self.run(prompt, timeout=TIMEOUT_SUBMITTER, resume_session=resume_session)
        pr_url = self.extract(output, r"PR_URL=(\S+)")
        if pr_url is None:
            pr_url = self.extract(output, r"https://github\.com/[^\s]+/pull/\d+")
        return pr_url or ""
