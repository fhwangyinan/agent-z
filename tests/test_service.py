import unittest
from unittest.mock import Mock, patch

from orchestration.service import ServiceProcess, _spawn, run_service, service_specs


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

    @patch("orchestration.service.subprocess.Popen")
    def test_spawn_passes_service_concurrency_to_children(self, popen):
        _spawn(ServiceProcess("worker-1", ["--worker"]), max_parallel=4)
        self.assertEqual(popen.call_args.kwargs["env"]["MAX_PARALLEL_TASKS"], "4")
        self.assertEqual(popen.call_args.kwargs["env"]["AGENT_Z_QUIET_LIVE"], "1")

    @patch("orchestration.service.validate_environment")
    @patch("orchestration.service.show_pool_status")
    @patch("orchestration.service.warn")
    @patch("orchestration.service.done")
    @patch("orchestration.service._stop")
    @patch("orchestration.service._spawn")
    @patch("orchestration.service.time.sleep", side_effect=KeyboardInterrupt)
    def test_keyboard_interrupt_stops_all_children(
        self, sleep, spawn, stop, done, warn, show_pool_status, validate_environment
    ):
        processes = []

        def start(service, *, max_parallel):
            process = Mock(pid=len(processes) + 1)
            service.process = process
            processes.append(process)
            return process

        spawn.side_effect = start
        self.assertEqual(run_service(workers=2), 0)
        self.assertEqual(spawn.call_count, 5)
        self.assertEqual(stop.call_count, 5)

    @patch("orchestration.service.validate_environment")
    @patch("orchestration.service.show_pool_status")
    @patch("orchestration.service.done")
    @patch("orchestration.service._stop")
    @patch("orchestration.service._spawn")
    def test_partial_startup_failure_stops_started_children(
        self, spawn, stop, done, show_pool_status, validate_environment
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


if __name__ == "__main__":
    unittest.main()
