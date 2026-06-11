from .base import Agent, PROJECT_DIR
from config import REVIEWER_BACKEND, TIMEOUT_REVIEWER

class ReviewerAgent(Agent):
    def __init__(self):
        super().__init__("Reviewer", REVIEWER_BACKEND)

    def review(self, issue_number: int, resume_session: bool = False) -> list[str]:
        prompt = (
            f"Review the local fix for issue #{issue_number}. Inspect the exact diff and latest commit, "
            f"and run relevant tests. Focus on correctness, completeness, regressions, unrelated edits, "
            f"project conventions, and hidden failure modes. Number each actionable finding. Output LGTM "
            f"only when there are no actionable issues."
        )
        output = self.run(prompt, timeout=TIMEOUT_REVIEWER, resume_session=resume_session)
        if "LGTM" in output.upper():
            return []
        return [s.strip() for s in output.split("\n\n") if s.strip()]
