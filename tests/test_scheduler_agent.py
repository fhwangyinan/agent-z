import unittest
from unittest.mock import patch

from agents.scheduler import SchedulerAgent, parse_scheduler_decisions


class SchedulerAgentTests(unittest.TestCase):
    def test_parses_complete_structured_decisions(self):
        output = """
        SCHEDULER_JSON_START
        {"decisions": [
          {"issue_number": 2, "action": "enqueue", "score": 90, "reason": "High value"},
          {"issue_number": 1, "action": "reject", "score": 0, "reason": "Tracking issue"}
        ]}
        SCHEDULER_JSON_END
        """
        decisions = parse_scheduler_decisions(output, {1, 2})
        self.assertEqual([decision.issue_number for decision in decisions], [2, 1])
        self.assertEqual(decisions[1].action, "reject")

    def test_rejects_unknown_or_omitted_candidates(self):
        unknown = (
            'SCHEDULER_JSON_START {"decisions": ['
            '{"issue_number": 9, "action": "enqueue", "score": 90, "reason": "Surprise"}'
            "]} SCHEDULER_JSON_END"
        )
        with self.assertRaisesRegex(RuntimeError, "unknown issue"):
            parse_scheduler_decisions(unknown, {1})

        omitted = 'SCHEDULER_JSON_START {"decisions": []} SCHEDULER_JSON_END'
        with self.assertRaisesRegex(RuntimeError, "omitted candidate"):
            parse_scheduler_decisions(omitted, {1})

    @patch("agents.scheduler.SchedulerAgent.run")
    def test_prompt_explicitly_rejects_tracking_and_meta_issues(self, run):
        run.return_value = (
            'SCHEDULER_JSON_START {"decisions": ['
            '{"issue_number": 1, "action": "reject", "score": 0, "reason": "Tracking issue"}'
            "]} SCHEDULER_JSON_END"
        )
        SchedulerAgent().rank([1])
        prompt = run.call_args.args[0].lower()
        self.assertIn("tracking/meta issues", prompt)
        self.assertIn("independently deliverable implementation task", prompt)
        self.assertIn("labels are hints, not truth", prompt)
        self.assertIn('"candidate_issue_numbers"', prompt)
        self.assertIn("inspect every candidate yourself with gh", prompt)
        self.assertGreater(prompt.index('"candidate_issue_numbers"'), prompt.index("task context"))

    @patch("agents.scheduler.SchedulerAgent.run")
    def test_variable_candidates_do_not_change_scheduler_prompt_prefix(self, run):
        run.return_value = (
            'SCHEDULER_JSON_START {"decisions": ['
            '{"issue_number": 1, "action": "reject", "score": 0, "reason": "Tracking issue"}'
            "]} SCHEDULER_JSON_END"
        )
        SchedulerAgent().rank([1])
        first = run.call_args.args[0]
        run.return_value = (
            'SCHEDULER_JSON_START {"decisions": ['
            '{"issue_number": 2, "action": "reject", "score": 0, "reason": "Tracking issue"}'
            "]} SCHEDULER_JSON_END"
        )
        SchedulerAgent().rank([2])
        second = run.call_args.args[0]
        self.assertEqual(
            first.split("TASK CONTEXT", 1)[0],
            second.split("TASK CONTEXT", 1)[0],
        )

    @patch("agents.scheduler.SchedulerAgent.run")
    def test_issues_are_reconsidered_each_scan(self, run):
        run.return_value = (
            'SCHEDULER_JSON_START {"decisions": ['
            '{"issue_number": 1, "action": "reject", "score": 0, "reason": "Tracking issue"}'
            "]} SCHEDULER_JSON_END"
        )
        agent = SchedulerAgent()
        agent.rank([1])
        agent.rank([1])
        self.assertEqual(run.call_count, 2)

    @patch("agents.scheduler.SchedulerAgent.run")
    def test_deferred_issues_are_reconsidered_next_scan(self, run):
        run.return_value = (
            'SCHEDULER_JSON_START {"decisions": ['
            '{"issue_number": 1, "action": "defer", "score": 40, "reason": "Lower priority today"}'
            "]} SCHEDULER_JSON_END"
        )
        agent = SchedulerAgent()
        agent.rank([1])
        agent.rank([1])
        self.assertEqual(run.call_count, 2)

    @patch("agents.scheduler.SchedulerAgent.run")
    def test_malformed_decision_gets_one_same_session_correction(self, run):
        run.side_effect = [
            "I recommend issue #1.",
            (
                'SCHEDULER_JSON_START {"decisions": ['
                '{"issue_number": 1, "action": "enqueue", "score": 80, '
                '"reason": "Concrete task"}'
                "]} SCHEDULER_JSON_END"
            ),
        ]

        decisions = SchedulerAgent().rank([1])

        self.assertEqual(decisions[0].action, "enqueue")
        self.assertEqual(run.call_count, 2)
        self.assertTrue(run.call_args.kwargs["resume_session"])
        self.assertIn("could not be parsed", run.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
