import os
import io
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from rich.console import Console

from orchestration.tui import render_service_dashboard, wait_with_status


class WaitStatusTests(unittest.TestCase):
    @patch("orchestration.tui.Live")
    @patch("orchestration.tui.time.monotonic", side_effect=[0, 0, 1, 2])
    def test_wait_status_refreshes_one_live_line(self, monotonic, live_class):
        live = Mock()
        live_class.return_value.__enter__.return_value = live
        sleep = Mock()
        details = Mock(side_effect=["claimed:0", "claimed:0", "claimed:0"])
        wait_with_status("Worker", details, 2, sleep=sleep)
        self.assertGreaterEqual(live.update.call_count, 2)
        self.assertGreaterEqual(details.call_count, 2)
        self.assertEqual(sleep.call_count, 2)

    @patch.dict(os.environ, {"AGENT_Z_QUIET_IDLE": "1"})
    @patch("orchestration.tui.Live")
    def test_quiet_idle_only_sleeps(self, live_class):
        sleep = Mock()
        wait_with_status("Worker", "claimed:0", 2, sleep=sleep)
        sleep.assert_called_once_with(2)
        live_class.assert_not_called()


class ServiceDashboardTests(unittest.TestCase):
    def test_dashboard_renders_selectable_expanded_task(self):
        record = SimpleNamespace(
            run_id="run-1",
            issue_number=42,
            status="running",
            stage="developing",
            created_at="2026-01-01T00:00:00+00:00",
            pr_url=None,
            error=None,
            branch="agent-z/42",
            lease_role="worker",
            lease_expires_at="later",
            worktree_path="worktree",
        )
        store = Mock()
        store.list.return_value = [record]
        store.list_global_events.return_value = [
            SimpleNamespace(
                event_type="scheduler_scan_failed",
                message="temporary GitHub failure",
            ),
        ]
        store.list_events.return_value = [
            SimpleNamespace(event_type="worker_claimed", message="Worker claimed run"),
        ]
        process = Mock(pid=123)
        process.poll.return_value = None
        service = SimpleNamespace(name="worker-1", process=process, restarts=0)

        dashboard, count = render_service_dashboard(
            store,
            [service],
            selected=0,
            expanded=True,
            uptime=10,
        )
        output = Console(file=io.StringIO(), record=True, width=160)
        output.print(dashboard)
        text = output.export_text()
        self.assertEqual(count, 1)
        self.assertIn("Agent-Z Service", text)
        self.assertIn("worker-1", text)
        self.assertIn("Issue #42", text)
        self.assertIn("Recent events", text)
        self.assertIn("scheduler_scan_failed", text)

    def test_dashboard_keeps_task_selected_by_run_id(self):
        older = SimpleNamespace(
            run_id="older", issue_number=1, status="ready", stage="ready",
            created_at="2026-01-01T00:00:00+00:00", pr_url=None, error=None,
            branch=None, lease_role=None, lease_expires_at=None, worktree_path=None,
        )
        newer = SimpleNamespace(
            run_id="newer", issue_number=2, status="ready", stage="ready",
            created_at="2026-01-02T00:00:00+00:00", pr_url=None, error=None,
            branch=None, lease_role=None, lease_expires_at=None, worktree_path=None,
        )
        store = Mock()
        store.list.return_value = [newer, older]
        store.list_global_events.return_value = []
        process = Mock(pid=123)
        process.poll.return_value = None
        service = SimpleNamespace(name="worker-1", process=process, restarts=0)
        dashboard, _ = render_service_dashboard(
            store, [service], selected=0, selected_run_id="older",
        )
        output = Console(file=io.StringIO(), record=True, width=120)
        output.print(dashboard)
        self.assertIn("Issue #1", output.export_text())

    def test_dashboard_can_expand_selected_process_log(self):
        with __import__("tempfile").TemporaryDirectory() as temp:
            log_path = os.path.join(temp, "worker.log")
            with open(log_path, "w", encoding="utf-8") as handle:
                handle.write("first line\nlatest line\n")
            store = Mock()
            store.list.return_value = []
            store.list_global_events.return_value = []
            process = Mock(pid=123)
            process.poll.return_value = None
            service = SimpleNamespace(
                name="worker-1", process=process, restarts=0,
                log_path=log_path, circuit_open=False,
            )
            dashboard, _ = render_service_dashboard(
                store, [service], focus="processes", selected_service=0, expanded=True,
            )
            output = Console(file=io.StringIO(), record=True, width=120)
            output.print(dashboard)
            text = output.export_text()
            self.assertIn("Process log", text)
            self.assertIn("latest line", text)

    def test_dashboard_shows_process_current_issue_and_scheduler_activity(self):
        planner_record = SimpleNamespace(
            run_id="run-1", issue_number=42, status="planning", stage="analyzing",
            created_at="2026-01-01T00:00:00+00:00", pr_url=None, error=None,
            branch=None, lease_role="planner", lease_expires_at=None,
            worktree_path=None, owner_pid=101,
        )
        store = Mock()
        store.list.return_value = [planner_record]
        store.list_global_events.return_value = [
            SimpleNamespace(
                event_type="scheduler_agent_started",
                message="Scheduler Agent evaluating 3 candidate(s): #1, #2, #3",
            ),
        ]
        scheduler_process = Mock(pid=100)
        scheduler_process.poll.return_value = None
        planner_process = Mock(pid=101)
        planner_process.poll.return_value = None
        services = [
            SimpleNamespace(name="scheduler", process=scheduler_process, restarts=0),
            SimpleNamespace(name="planner", process=planner_process, restarts=0),
        ]

        dashboard, _ = render_service_dashboard(store, services)

        output = Console(file=io.StringIO(), record=True, width=160)
        output.print(dashboard)
        text = output.export_text()
        self.assertIn("Activity", text)
        self.assertIn("#42", text)
        self.assertIn("evaluating 3", text)

    def test_dashboard_shows_scheduler_activity_before_first_task(self):
        store = Mock()
        store.list.return_value = []
        store.list_global_events.return_value = [
            SimpleNamespace(
                event_type="scheduler_scan_started",
                message="Scheduler scan started",
            ),
        ]
        process = Mock(pid=100)
        process.poll.return_value = None
        service = SimpleNamespace(name="scheduler", process=process, restarts=0)

        dashboard, _ = render_service_dashboard(store, [service])

        output = Console(file=io.StringIO(), record=True, width=120)
        output.print(dashboard)
        text = output.export_text()
        self.assertIn("No persisted tasks yet", text)
        self.assertIn("Scheduler scan started", text)


if __name__ == "__main__":
    unittest.main()
