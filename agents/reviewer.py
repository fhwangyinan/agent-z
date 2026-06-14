from .base import Agent, prompt_with_context
from config import REVIEWER_BACKEND, TIMEOUT_REVIEWER

class ReviewerAgent(Agent):
    def __init__(self):
        super().__init__("Reviewer", REVIEWER_BACKEND)

    def review(self, issue_number: int, resume_session: bool = False) -> list[str]:
        prompt = prompt_with_context(
            "Review the local fix for the target issue. Inspect the exact diff and latest commit, "
            "and run relevant tests. Focus on correctness, completeness, regressions, unrelated edits, "
            "project conventions, and hidden failure modes. Number each actionable finding. Output LGTM "
            "only when there are no actionable issues.",
            issue_number=issue_number,
        )
        output = self.run(prompt, timeout=TIMEOUT_REVIEWER, resume_session=resume_session)
        if "LGTM" in output.upper():
            return []
        return [s.strip() for s in output.split("\n\n") if s.strip()]
