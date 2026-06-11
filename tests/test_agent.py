import unittest
from unittest.mock import patch

from agents.base import Agent
from agents.runners.base import AgentResult, BackendCapabilities


class FakeRunner:
    name = "fake"
    command = "fake"
    capabilities = BackendCapabilities(
        session_mode="explicit",
        output_mode="text",
        isolation="none",
        stdin_prompt=True,
    )

    def __init__(self, prefix):
        self.prefix = prefix
        self.sessions = []

    def execute(self, prompt, timeout=600, cwd=".", session_id=None):
        self.sessions.append(session_id)
        return AgentResult(prompt, session_id or f"{self.prefix}-{len(self.sessions)}")


class AgentSessionTests(unittest.TestCase):
    @patch("agents.base.done")
    @patch("agents.base.agent_status")
    def test_each_agent_owns_an_independent_session(self, status, done):
        first_runner = FakeRunner("developer")
        second_runner = FakeRunner("reviewer")
        first = Agent("Developer", "fake", runner=first_runner)
        second = Agent("Reviewer", "fake", runner=second_runner)

        first.run("initial", resume_session=True)
        second.run("review", resume_session=True)
        first.run("follow-up", resume_session=True)

        self.assertEqual(first_runner.sessions, [None, "developer-1"])
        self.assertEqual(second_runner.sessions, [None])
        self.assertNotEqual(first.session_id, second.session_id)

        first.reset_session()
        first.run("new task", resume_session=True)
        self.assertEqual(first_runner.sessions[-1], None)


if __name__ == "__main__":
    unittest.main()
