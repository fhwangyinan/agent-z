import unittest
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

from orchestration.service import (
    PendingAction,
    ServiceProcess,
    _confirm_action,
    _open_task,
    _register_service_failure,
    _restart_service,
    _spawn,
    run_service,
    service_specs,
)


class ServiceTests(unittest.TestCase):
    def test_specs_include_complete_pipeline(self):
        specs = service_specs(2, force=True, keep_worktree=True)
        self.assertEqual(
            [spec.name for spec in specs],
            ["scheduler", "planner", "worker-1", "worker-2", "reconciler"],
        )
        self.assertEqual(specs[0].args, ["--scheduler"])
        self.assertEqual(specs[2].args, ["--worker", "--force", "--keep-worktree"])
        self.assertEqual(specs[-1].args, ["--reconciler"])

    def test_restart_backoff_opens_circuit_after_limit(self):
        service = ServiceProcess("worker-1", ["--worker"])
        with patch("orchestration.service.SERVICE_RESTART_DELAY", 2), patch(
            "orchestration.service.SERVICE_RESTART_MAX_DELAY", 5
        ), patch("orchestration.service.SERVICE_RESTART_MAX_ATTEMPTS", 3):
            self.assertEqual(_register_service_failure(service), 2)
            self.assertEqual(_register_service_failure(service), 4)
            self.assertEqual(_register_service_failure(service), 5)
            self.assertIsNone(_register_service_failure(service))
        self.assertTrue(service.circuit_open)

    def test_dangerous_action_requires_matching_second_key_before_timeout(self):
        pending, confirmed = _confirm_action(
            None,
            key="r",
            action="restart scheduler",
            target="scheduler",
            prompt="confirm",
            now=10,
        )
        self.assertIsInstance(pending, PendingAction)
        self.assertFalse(confirmed)

        pending, confirmed = _confirm_action(
            pending,
            key="r",
            action="restart scheduler",
            target="scheduler",
            prompt="confirm",
            now=11,
        )
        self.assertIsNone(pending)
        self.assertTrue(confirmed)

    @patch("orchestration.service._spawn")
    @patch("orchestration.service._stop")
    def test_manual_restart_resets_circuit(self, stop, spawn):
        service = ServiceProcess(
            "scheduler",
            ["--scheduler"],
            consecutive_failures=4,
            circuit_open=True,
        )
        process = Mock(pid=42)
        spawn.return_value = process

        self.assertIs(_restart_service(service, max_parallel=2), process)

        stop.assert_called_once_with(service)
        spawn.assert_called_once_with(service, max_parallel=2)
        self.assertEqual(service.consecutive_failures, 0)
        self.assertFalse(service.circuit_open)
        self.assertEqual(service.restarts, 1)

    @patch("orchestration.service.webbrowser.open")
    def test_open_task_prefers_pr_url(self, open_browser):
        record = Mock(
            pr_url="https://github.com/example/repo/pull/7",
            repo="example/repo",
            issue_number=42,
        )

        url = _open_task(record)

        self.assertEqual(url, record.pr_url)
        open_browser.assert_called_once_with(record.pr_url)

    @patch("orchestration.service.subprocess.Popen")
    def test_spawn_passes_service_concurrency_to_children(self, popen):
        service = ServiceProcess("worker-1", ["--worker"])
        self.addCleanup(lambda: service.log_handle and service.log_handle.close())
        _spawn(service, max_parallel=4)
        self.assertEqual(popen.call_args.kwargs["env"]["MAX_PARALLEL_TASKS"], "4")
        self.assertEqual(popen.call_args.kwargs["env"]["AGENT_Z_QUIET_LIVE"], "1")
        self.assertEqual(popen.call_args.kwargs["env"]["AGENT_Z_LOG_AGENT_STATUS"], "1")
        self.assertEqual(popen.call_args.kwargs["env"]["PYTHONIOENCODING"], "utf-8")
        self.assertEqual(popen.call_args.kwargs["env"]["PYTHONUNBUFFERED"], "1")
        self.assertIs(popen.call_args.kwargs["stderr"], __import__("subprocess").STDOUT)
        self.assertTrue(service.log_path.endswith("worker-1.log"))

    @patch("orchestration.service.subprocess.Popen")
    def test_spawn_rotates_large_service_log(self, popen):
        with tempfile.TemporaryDirectory() as temp:
            state_db = Path(temp) / "state.db"
            log_dir = state_db.parent / "logs"
            log_dir.mkdir()
            log_path = log_dir / "worker-1.log"
            log_path.write_bytes(b"old log")
            service = ServiceProcess("worker-1", ["--worker"])
            try:
                with patch("orchestration.service.STATE_DB", str(state_db)), patch(
                    "orchestration.service.SERVICE_LOG_MAX_BYTES", 1
                ), patch("orchestration.service.SERVICE_LOG_BACKUPS", 2):
                    _spawn(service, max_parallel=1)
                self.assertEqual((log_dir / "worker-1.log.1").read_bytes(), b"old log")
            finally:
                if service.log_handle:
                    service.log_handle.close()

    @patch("orchestration.service.validate_environment")
    @patch("orchestration.service.render_service_dashboard", return_value=(Mock(), 0))
    @patch("orchestration.service.Live")
    @patch("orchestration.service._stop")
    @patch("orchestration.service._spawn")
    @patch("orchestration.service.time.sleep", side_effect=KeyboardInterrupt)
    def test_keyboard_interrupt_stops_all_children(
        self, sleep, spawn, stop, live, render, validate_environment
    ):
        processes = []

        def start(service, *, max_parallel):
            process = Mock(pid=len(processes) + 1)
            service.process = process
            processes.append(process)
            return process

        spawn.side_effect = start
        self.assertEqual(run_service(workers=2, key_reader=lambda: None), 0)
        self.assertEqual(spawn.call_count, 5)
        self.assertEqual(stop.call_count, 5)

    @patch("orchestration.service.validate_environment")
    @patch("orchestration.service._stop")
    @patch("orchestration.service._spawn")
    def test_partial_startup_failure_stops_started_children(
        self, spawn, stop, validate_environment
    ):
        first = Mock(pid=1)

        def start(service, *, max_parallel):
            if service.name == "scheduler":
                service.process = first
                return first
            raise RuntimeError("start failed")

        spawn.side_effect = start
        with self.assertRaisesRegex(RuntimeError, "start failed"):
            run_service(workers=1)
        self.assertEqual(stop.call_count, 4)

    @patch("orchestration.service.validate_environment")
    @patch("orchestration.service.render_service_dashboard", return_value=(Mock(), 0))
    @patch("orchestration.service.Live")
    @patch("orchestration.service._stop")
    @patch("orchestration.service._spawn")
    def test_q_stops_dashboard_and_children(
        self, spawn, stop, live, render, validate_environment
    ):
        spawn.side_effect = lambda service, *, max_parallel: Mock(pid=1)
        keys = iter(["q", "q"])
        self.assertEqual(run_service(workers=1, key_reader=lambda: next(keys)), 0)
        self.assertEqual(stop.call_count, 4)

    @patch("orchestration.service.validate_environment")
    @patch("orchestration.service.render_service_dashboard", return_value=(Mock(), 1))
    @patch("orchestration.service.Live")
    @patch("orchestration.service._stop")
    @patch("orchestration.service._spawn")
    @patch("orchestration.service.RunStore")
    def test_c_cancels_selected_task_after_confirmation(
        self, store_class, spawn, stop, live, render, validate_environment
    ):
        record = Mock(run_id="run-1", issue_number=42)
        store = store_class.return_value
        store.list.return_value = [record]
        spawn.side_effect = lambda service, *, max_parallel: Mock(pid=1)
        keys = iter(["c", "c", "q", "q"])

        self.assertEqual(run_service(workers=1, key_reader=lambda: next(keys)), 0)

        store.cancel.assert_called_once_with("run-1")

    @patch("orchestration.service.validate_environment")
    @patch("orchestration.service.render_service_dashboard", return_value=(Mock(), 0))
    @patch("orchestration.service.Live")
    @patch("orchestration.service._stop")
    @patch("orchestration.service._restart_service")
    @patch("orchestration.service._spawn")
    def test_r_restarts_selected_process_after_confirmation(
        self, spawn, restart, stop, live, render, validate_environment
    ):
        spawn.side_effect = lambda service, *, max_parallel: Mock(pid=1)
        restart.return_value = Mock(pid=99)
        keys = iter(["tab", "r", "r", "q", "q"])

        self.assertEqual(run_service(workers=1, key_reader=lambda: next(keys)), 0)

        restart.assert_called_once()
        self.assertEqual(restart.call_args.args[0].name, "scheduler")


if __name__ == "__main__":
    unittest.main()
