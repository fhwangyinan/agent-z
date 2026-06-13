import unittest
from types import SimpleNamespace
from unittest.mock import ANY, Mock, patch

from agents.scheduler import SchedulerDecision
from orchestration.scheduler import (
    _agent_trigger,
    _open_pr_issue_numbers,
    _policy_state,
    _selected_by_agent,
    extract_dependencies,
    schedule_once,
    select_schedulable_issues,
)


class SchedulerTests(unittest.TestCase):
    def test_agent_trigger_only_runs_for_changes_or_consumed_queue(self):
        candidates = {"1": {"updated_at": "same", "open_pr": False}}
        snapshot = {
            "candidate_state": candidates,
            "queue_state": {"10": "queued", "11": "queued"},
        }
        self.assertIsNone(_agent_trigger(
            snapshot,
            candidate_state=candidates,
            queue_state={"10": "queued", "11": "queued"},
        ))
        self.assertEqual(
            _agent_trigger(
                snapshot,
                candidate_state={"1": {"updated_at": "new", "open_pr": False}},
                queue_state={"10": "queued", "11": "queued"},
            ),
            "candidate_state_changed",
        )
        self.assertEqual(
            _agent_trigger(
                snapshot,
                candidate_state=candidates,
                queue_state={"10": "planning", "11": "queued"},
            ),
            "queue_needs_replenishment",
        )
        self.assertEqual(
            _agent_trigger(
                snapshot,
                candidate_state=candidates,
                queue_state={"10": "queued", "11": "queued"},
                policy_state={"version": 2},
            ),
            "policy_changed",
        )

    def test_extracts_explicit_dependencies(self):
        body = """
        Some context.
        Blocked by #12, #13 and #14
        Depends on: #9
        """
        self.assertEqual(extract_dependencies(body), (9, 12, 13, 14))

    def test_selects_independent_issues_in_priority_order(self):
        issues = [
            {
                "number": 3,
                "title": "Low",
                "body": "",
                "labels": [{"name": "priority:low"}],
                "createdAt": "2026-01-01T00:00:00Z",
            },
            {
                "number": 2,
                "title": "Blocked",
                "body": "Blocked by #1",
                "labels": [{"name": "priority:critical"}],
                "createdAt": "2026-01-01T00:00:00Z",
            },
            {
                "number": 4,
                "title": "High",
                "body": "",
                "labels": [{"name": "priority:high"}],
                "createdAt": "2026-01-02T00:00:00Z",
            },
        ]
        selected = select_schedulable_issues(
            issues,
            active_issue_numbers=set(),
            dependency_is_open=lambda number: number == 1,
            priority_labels=["priority:critical", "priority:high", "priority:low"],
        )
        self.assertEqual([item.issue_number for item in selected], [4, 3])

    def test_filters_active_ineligible_and_block_labels(self):
        issues = [
            {"number": 1, "title": "Active", "body": "", "labels": [{"name": "agent-ready"}]},
            {"number": 2, "title": "No label", "body": "", "labels": []},
            {
                "number": 3,
                "title": "Blocked",
                "body": "",
                "labels": [{"name": "agent-ready"}, {"name": "blocked"}],
            },
            {"number": 4, "title": "Ready", "body": "", "labels": [{"name": "agent-ready"}]},
        ]
        selected = select_schedulable_issues(
            issues,
            active_issue_numbers={1},
            dependency_is_open=lambda number: False,
            eligible_labels=["agent-ready"],
            block_labels=["blocked"],
        )
        self.assertEqual([item.issue_number for item in selected], [4])

    def test_filters_assigned_issues_and_existing_open_prs(self):
        issues = [
            {
                "number": 1,
                "title": "Assigned",
                "body": "",
                "labels": [],
                "assignees": [{"login": "someone"}],
            },
            {"number": 2, "title": "Has PR", "body": "", "labels": [], "assignees": []},
            {"number": 3, "title": "Free", "body": "", "labels": [], "assignees": []},
        ]
        selected = select_schedulable_issues(
            issues,
            active_issue_numbers=set(),
            dependency_is_open=lambda number: False,
            skip_assigned=True,
            has_open_pr=lambda number: number == 2,
        )
        self.assertEqual([item.issue_number for item in selected], [3])

    def test_candidate_snapshot_tracks_updated_at_and_excluded_open_prs(self):
        issues = [
            {"number": 1, "title": "Has PR", "body": "", "labels": [], "updatedAt": "v1"},
            {"number": 2, "title": "Free", "body": "", "labels": [], "updatedAt": "v2"},
        ]
        state = {}
        selected = select_schedulable_issues(
            issues,
            active_issue_numbers=set(),
            dependency_is_open=lambda number: False,
            has_open_pr=lambda number: number == 1,
            candidate_state=state,
        )
        self.assertEqual([item.issue_number for item in selected], [2])
        self.assertEqual(state, {
            "1": {"updated_at": "v1", "open_pr": True},
            "2": {"updated_at": "v2", "open_pr": False},
        })

    def test_agent_rejects_tracking_issue_and_ranks_actionable_work(self):
        issues = [
            {"number": 1, "title": "Track release work", "body": "Checklist", "labels": []},
            {"number": 2, "title": "Fix data loss", "body": "Concrete bug", "labels": []},
            {"number": 3, "title": "Improve tooltip", "body": "Concrete polish", "labels": []},
        ]
        candidates = select_schedulable_issues(
            issues,
            active_issue_numbers=set(),
            dependency_is_open=lambda number: False,
        )
        agent = Mock()
        agent.rank.return_value = [
            SchedulerDecision(1, "reject", 0, "Tracking issue, not an implementation task"),
            SchedulerDecision(3, "enqueue", 40, "Small user-facing improvement"),
            SchedulerDecision(2, "enqueue", 95, "Prevents high-impact data loss"),
        ]

        selected, decisions = _selected_by_agent(candidates, agent)

        self.assertEqual([candidate.issue_number for candidate, _ in selected], [2, 3])
        self.assertEqual(len(decisions), 3)

    @patch("orchestration.scheduler._open_pr_issue_numbers", return_value=set())
    @patch("orchestration.scheduler._issue_is_open", return_value=False)
    @patch("orchestration.scheduler._list_open_issues")
    def test_schedule_once_enqueues_only_agent_approved_issues(
        self, list_issues, issue_is_open, open_pr_issues
    ):
        list_issues.return_value = [
            {"number": 1, "title": "Tracking", "body": "Tracks #2", "labels": []},
            {"number": 2, "title": "Actionable", "body": "Fix it", "labels": []},
        ]
        agent = Mock()
        agent.rank.return_value = [
            SchedulerDecision(1, "reject", 0, "Tracking issue"),
            SchedulerDecision(2, "enqueue", 90, "High-value actionable bug"),
        ]
        store = Mock()
        store.list_scheduler_queued.return_value = []
        store.active_issue_numbers.return_value = set()
        store.scheduler_queue_state.side_effect = [{}, {}, {"2": "queued"}]
        store.get_scheduler_snapshot.return_value = None
        store.enqueue.return_value = SimpleNamespace(
            run_id="run-2",
            issue_number=2,
            stage="queued",
            status="queued",
        )

        records = schedule_once(store, scheduler_agent=agent)

        self.assertEqual([record.issue_number for record in records], [2])
        store.enqueue.assert_called_once_with(ANY, 2)
        evaluation = store.add_event.call_args_list[0]
        self.assertEqual(evaluation.args[1], "scheduler_agent_evaluated")
        self.assertEqual(evaluation.kwargs["data"]["selected_issue_numbers"], [2])
        self.assertEqual(evaluation.kwargs["data"]["trigger"], "initial_scan")

    @patch("orchestration.scheduler._open_pr_issue_numbers", return_value=set())
    @patch("orchestration.scheduler._issue_is_open", return_value=False)
    @patch("orchestration.scheduler._list_open_issues")
    def test_schedule_once_rejects_old_scheduler_queue_but_not_manual_queue(
        self, list_issues, issue_is_open, open_pr_issues
    ):
        list_issues.return_value = [
            {"number": 1, "title": "Tracking", "body": "Tracks work", "labels": []},
            {"number": 2, "title": "Manual", "body": "Manual queue", "labels": []},
        ]
        agent = Mock()
        agent.rank.return_value = [
            SchedulerDecision(1, "reject", 0, "Tracking issue"),
        ]
        store = Mock()
        store.list_scheduler_queued.return_value = [
            SimpleNamespace(run_id="scheduled-1", issue_number=1),
        ]
        store.active_issue_numbers.return_value = {1, 2}
        store.scheduler_queue_state.side_effect = [
            {"1": "queued", "2": "queued"},
            {"2": "queued"},
            {"2": "queued"},
        ]
        store.get_scheduler_snapshot.return_value = None

        schedule_once(store, scheduler_agent=agent)

        store.release_scheduler_queued.assert_called_once_with(
            "scheduled-1",
            action="reject",
            reason="Scheduler Agent reject: Tracking issue",
        )
        payload = agent.rank.call_args.args[0]
        self.assertEqual(payload, [1])

    @patch("orchestration.scheduler._open_pr_issue_numbers", return_value=set())
    @patch("orchestration.scheduler._issue_is_open", return_value=False)
    @patch("orchestration.scheduler._list_open_issues")
    def test_schedule_once_skips_agent_when_snapshot_and_queue_are_unchanged(
        self, list_issues, issue_is_open, open_pr_issues
    ):
        list_issues.return_value = [
            {
                "number": 1,
                "title": "Actionable",
                "body": "Fix it",
                "labels": [],
                "updatedAt": "same",
            },
        ]
        state = {"1": {"updated_at": "same", "open_pr": False}}
        store = Mock()
        store.list_scheduler_queued.return_value = []
        store.active_issue_numbers.return_value = set()
        store.scheduler_queue_state.return_value = {}
        store.get_scheduler_snapshot.return_value = {
            "candidate_state": state,
            "queue_state": {},
            "policy_state": _policy_state(),
            "updated_at": "earlier",
        }
        agent = Mock()

        self.assertEqual(schedule_once(store, scheduler_agent=agent), [])

        agent.rank.assert_not_called()
        store.add_event.assert_not_called()
        store.save_scheduler_snapshot.assert_called_once()

    @patch("orchestration.scheduler._open_pr_issue_numbers", return_value=set())
    @patch("orchestration.scheduler._issue_is_open", return_value=False)
    @patch("orchestration.scheduler._list_open_issues")
    def test_schedule_once_replenishes_after_queued_task_is_claimed(
        self, list_issues, issue_is_open, open_pr_issues
    ):
        list_issues.return_value = [
            {"number": 1, "title": "Actionable", "body": "Fix it", "labels": [], "updatedAt": "same"},
        ]
        state = {"1": {"updated_at": "same", "open_pr": False}}
        store = Mock()
        store.list_scheduler_queued.return_value = []
        store.active_issue_numbers.return_value = set()
        store.scheduler_queue_state.side_effect = [{}, {}, {}]
        store.get_scheduler_snapshot.return_value = {
            "candidate_state": state,
            "queue_state": {"9": "queued"},
            "policy_state": _policy_state(),
            "updated_at": "earlier",
        }
        agent = Mock()
        agent.rank.return_value = [SchedulerDecision(1, "defer", 40, "Not next")]

        schedule_once(store, scheduler_agent=agent)

        agent.rank.assert_called_once_with([1])
        evaluation = store.add_event.call_args_list[0]
        self.assertEqual(evaluation.kwargs["data"]["trigger"], "queue_needs_replenishment")

    @patch("orchestration.scheduler._open_pr_issue_numbers", return_value=set())
    @patch("orchestration.scheduler._issue_is_open", return_value=False)
    @patch("orchestration.scheduler._list_open_issues")
    def test_schedule_once_releases_scheduler_queue_blocked_by_safety_filter(
        self, list_issues, issue_is_open, open_pr_issues
    ):
        list_issues.return_value = [
            {
                "number": 1,
                "title": "Now blocked",
                "body": "",
                "labels": [{"name": "blocked"}],
            },
        ]
        queued = SimpleNamespace(run_id="scheduled-1", issue_number=1)
        store = Mock()
        store.list_scheduler_queued.return_value = [queued]
        store.active_issue_numbers.return_value = {1}
        store.scheduler_queue_state.return_value = {}
        store.get_scheduler_snapshot.return_value = None

        self.assertEqual(schedule_once(store, scheduler_agent=Mock()), [])

        store.release_scheduler_queued.assert_called_once_with(
            "scheduled-1",
            action="defer",
            reason="Scheduler safety filters no longer allow this issue",
        )
        saved = store.save_scheduler_snapshot.call_args.kwargs
        self.assertEqual(saved["policy_state"], _policy_state())

    @patch("orchestration.scheduler._open_pr_issue_numbers", return_value=None)
    @patch("orchestration.scheduler._issue_is_open", return_value=False)
    @patch("orchestration.scheduler._list_open_issues")
    def test_schedule_once_keeps_scheduler_queue_when_pr_snapshot_fails(
        self, list_issues, issue_is_open, open_pr_issues
    ):
        list_issues.return_value = [
            {"number": 1, "title": "Actionable", "body": "", "labels": []},
        ]
        queued = SimpleNamespace(run_id="scheduled-1", issue_number=1)
        store = Mock()
        store.list_scheduler_queued.return_value = [queued]
        store.active_issue_numbers.return_value = {1}
        store.scheduler_queue_state.return_value = {"1": "queued"}
        store.get_scheduler_snapshot.return_value = None

        self.assertEqual(schedule_once(store, scheduler_agent=Mock()), [])

        store.release_scheduler_queued.assert_not_called()

    @patch("orchestration.scheduler.run_cmd")
    def test_bulk_open_pr_query_extracts_exact_issue_references(self, run_cmd):
        run_cmd.return_value = SimpleNamespace(
            returncode=0,
            stdout="""[
                {"title": "Fix #12", "body": ""},
                {"title": "Fix #123", "body": "Issue: 9"}
            ]""",
            stderr="",
        )
        self.assertEqual(_open_pr_issue_numbers(), {9, 12, 123})
        self.assertEqual(run_cmd.call_count, 1)
        command = run_cmd.call_args.args[0]
        self.assertEqual(command[command.index("--state") + 1], "open")


if __name__ == "__main__":
    unittest.main()
