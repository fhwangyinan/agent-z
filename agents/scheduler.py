import json
import re
from dataclasses import dataclass

from config import (
    GITHUB_REPO,
    PROJECT_DIR,
    SCHEDULER_BATCH_SIZE,
    TASK_LEAD_BACKEND,
    TIMEOUT_ANALYST,
)

from .base import Agent


@dataclass(frozen=True)
class SchedulerDecision:
    issue_number: int
    action: str
    score: int
    reason: str


class SchedulerAgent(Agent):
    def __init__(self):
        super().__init__("Scheduler", TASK_LEAD_BACKEND)

    def rank(self, issue_numbers: list[int]) -> list[SchedulerDecision]:
        issue_numbers = [int(number) for number in issue_numbers]
        prompt = (
            f"You are the scheduling lead for {GITHUB_REPO}. Select at most "
            f"{SCHEDULER_BATCH_SIZE} issues that an autonomous coding agent should work on next. "
            f"The project checkout is {PROJECT_DIR}. Candidate issue numbers: "
            f"{', '.join(f'#{number}' for number in issue_numbers)}. Inspect every candidate yourself "
            "with gh, including its current body, labels, references, related open PRs, and directly "
            "relevant code. Do not modify code, issues, labels, assignees, or PRs.\n\n"
            "Only enqueue issues that describe a concrete, independently deliverable implementation "
            "task with a reasonably clear completion condition. Reject tracking/meta issues, epics, "
            "roadmaps, umbrella issues, release checklists, status reports, discussions, questions, "
            "support requests, duplicate work, vague requests, and issues that mainly coordinate "
            "other issues. Reject work that is blocked, already being handled, or cannot be safely "
            "completed without unresolved product decisions.\n\n"
            "Rank actionable work by expected user/project value, urgency, breadth of benefit, "
            "confidence in the fix, implementation cost, regression risk, and whether it unlocks "
            "other work. Labels are hints, not truth. Prefer high-value low/medium-cost work over "
            "cosmetic or speculative work. Independent selected issues may run in parallel.\n\n"
            "Return exactly one JSON object between SCHEDULER_JSON_START and SCHEDULER_JSON_END. "
            "It must contain a decisions array with one entry for every candidate. Each entry must "
            'have issue_number (integer), action ("enqueue", "defer", or "reject"), score '
            "(integer 0-100), and reason (short string). Use enqueue only for the best issues, "
            "defer for actionable work that should be reconsidered in a later scan, and reject only "
            "for work that is not independently actionable. Order decisions from highest to lowest "
            "score. Never introduce an issue number outside the candidate list."
        )
        output = self.run(prompt, timeout=TIMEOUT_ANALYST)
        try:
            return parse_scheduler_decisions(output, set(issue_numbers))
        except RuntimeError as exc:
            correction = (
                f"Your previous scheduler response could not be parsed: {exc}. "
                "Return only the required structured decision object now. Include exactly one "
                "decision for every candidate issue number: "
                f"{', '.join(f'#{number}' for number in issue_numbers)}. "
                "Do not omit candidates or add new issue numbers.\n\n"
                "SCHEDULER_JSON_START\n"
                '{"decisions":[{"issue_number":123,"action":"enqueue|defer|reject",'
                '"score":0,"reason":"short reason"}]}\n'
                "SCHEDULER_JSON_END"
            )
            output = self.run(
                correction,
                timeout=TIMEOUT_ANALYST,
                resume_session=True,
            )
            return parse_scheduler_decisions(output, set(issue_numbers))


def parse_scheduler_decisions(
    output: str,
    candidate_numbers: set[int],
) -> list[SchedulerDecision]:
    match = re.search(
        r"SCHEDULER_JSON_START\s*(\{.*?\})\s*SCHEDULER_JSON_END",
        output,
        re.DOTALL,
    )
    if not match:
        raise RuntimeError("Scheduler Agent did not return structured decisions")
    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise RuntimeError("Scheduler Agent returned invalid decision JSON") from exc

    raw_decisions = payload.get("decisions") if isinstance(payload, dict) else None
    if not isinstance(raw_decisions, list):
        raise RuntimeError("Scheduler Agent decision JSON has no decisions array")

    decisions = []
    seen = set()
    for item in raw_decisions:
        if not isinstance(item, dict):
            raise RuntimeError("Scheduler Agent returned a malformed decision")
        try:
            issue_number = int(item["issue_number"])
            score = int(item["score"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("Scheduler Agent returned a malformed decision") from exc
        action = str(item.get("action") or "").lower()
        reason = str(item.get("reason") or "").strip()
        if issue_number not in candidate_numbers:
            raise RuntimeError(f"Scheduler Agent introduced unknown issue #{issue_number}")
        if issue_number in seen:
            raise RuntimeError(f"Scheduler Agent returned duplicate issue #{issue_number}")
        if action not in {"enqueue", "defer", "reject"} or not 0 <= score <= 100 or not reason:
            raise RuntimeError(f"Scheduler Agent returned an invalid decision for issue #{issue_number}")
        seen.add(issue_number)
        decisions.append(SchedulerDecision(issue_number, action, score, reason))

    missing = candidate_numbers - seen
    if missing:
        numbers = ", ".join(f"#{number}" for number in sorted(missing))
        raise RuntimeError(f"Scheduler Agent omitted candidate decisions: {numbers}")
    return decisions
