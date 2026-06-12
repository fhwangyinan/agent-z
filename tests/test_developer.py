import unittest
from unittest.mock import patch

from agents.developer import DeveloperAgent


class DeveloperSubmissionTests(unittest.TestCase):
    @patch(
        "agents.developer.DeveloperAgent.run",
        return_value=(
            "SUBMISSION_JSON_START\n"
            '{"commit_message":"fix: handle edge case","pr_title":"Handle edge case",'
            '"pr_body":"Summary\\n\\nTests: unit tests"}'
            "\nSUBMISSION_JSON_END"
        ),
    )
    def test_prepare_submission_parses_structured_metadata(self, run_agent):
        developer = DeveloperAgent.__new__(DeveloperAgent)
        developer.session_id = "lead-session"
        metadata = developer.prepare_submission(5, resume_session=True)
        self.assertEqual(metadata["commit_message"], "fix: handle edge case")
        self.assertEqual(metadata["pr_title"], "Handle edge case")
        prompt = run_agent.call_args.args[0]
        self.assertIn("do not edit files, commit, push, or create a PR", prompt)

    @patch("agents.developer.DeveloperAgent.run", return_value="not json")
    def test_prepare_submission_returns_empty_metadata_for_invalid_output(self, run_agent):
        developer = DeveloperAgent.__new__(DeveloperAgent)
        developer.session_id = "lead-session"
        self.assertEqual(developer.prepare_submission(5, resume_session=True), {})


if __name__ == "__main__":
    unittest.main()
