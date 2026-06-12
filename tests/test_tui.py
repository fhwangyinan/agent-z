import os
import unittest
from unittest.mock import Mock, patch

from orchestration.tui import wait_with_status


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


if __name__ == "__main__":
    unittest.main()
