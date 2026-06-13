import unittest
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

from orchestration.service import (
    ServiceProcess,
    _register_service_failure,
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
        self.assertEqual(run_service(workers=1, key_reader=lambda: "q"), 0)
        self.assertEqual(stop.call_count, 4)


if __name__ == "__main__":
    unittest.main()
