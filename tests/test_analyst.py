import unittest
from unittest.mock import patch

from agents.analyst import AnalystAgent


class AnalystPromptTests(unittest.TestCase):
    @patch("agents.base.done")
    @patch("agents.base.agent_status")
    @patch("agents.analyst.AnalystAgent.run", return_value="RECOMMENDED_ISSUE=5")
    def test_auto_selection_prompt_skips_configured_labels(self, run_agent, status, done):
        with patch("agents.analyst.SKIP_LABELS", ["ongoing", "blocked"]):
            analyst = AnalystAgent()
            issue_number, _ = analyst.analyze()
            self.assertEqual(issue_number, 5)
            prompt = run_agent.call_args.args[0]
            self.assertIn("exclude issues with any of these labels", prompt.lower())
            self.assertIn("`ongoing`", prompt)
            self.assertIn("`blocked`", prompt)
            self.assertIn("Explore open issues and open PRs as needed", prompt)
            self.assertIn("Avoid loading the full open backlog", prompt)

    @patch("agents.base.done")
    @patch("agents.base.agent_status")
    @patch("agents.analyst.AnalystAgent.run", return_value="RECOMMENDED_ISSUE=5")
    def test_target_issue_checks_only_open_prs(self, run_agent, status, done):
        AnalystAgent().analyze(target_issue=5)
        prompt = run_agent.call_args.args[0]
        self.assertIn("gh pr list --state open", prompt)
        self.assertIn("Do not list closed or merged PRs", prompt)



if __name__ == "__main__":
    unittest.main()
