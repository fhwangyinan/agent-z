import json
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from agents.runners.base import decode_subprocess_output
from agents.runners.claude import ClaudeRunner
from agents.runners.codex import CodexRunner
from agents.runners.opencode import OpenCodeRunner


def result(returncode=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


class ClaudeRunnerTests(unittest.TestCase):
    def test_success_returns_explicit_session(self):
        runner = ClaudeRunner(flags=["--flag"])
        payload = json.dumps({"result": "ok", "session_id": "session-1"})
        with patch.object(runner, "_run", return_value=result(stdout=payload)) as execute:
            response = runner.execute("prompt")
        self.assertEqual(response.output, "ok")
        self.assertEqual(response.session_id, "session-1")
        self.assertEqual(execute.call_count, 1)

    def test_resume_uses_requested_session(self):
        runner = ClaudeRunner(flags=["--flag"])
        payload = json.dumps({"result": "ok", "session_id": "session-1"})
        with patch.object(runner, "_run", return_value=result(stdout=payload)) as execute:
            runner.execute("prompt", session_id="session-1")
        self.assertIn("--resume", execute.call_args.args[0])
        self.assertIn("session-1", execute.call_args.args[0])
        self.assertEqual(execute.call_args.args[1], "prompt")

    def test_timeout_retries_explicit_session(self):
        runner = ClaudeRunner(flags=["--flag"])
        payload = json.dumps({"result": "retried", "session_id": "session-1"})
        with patch.object(
            runner,
            "_run",
            side_effect=[subprocess.TimeoutExpired("claude", 1), result(stdout=payload)],
        ) as execute:
            response = runner.execute("prompt")
        self.assertEqual(response.output, "retried")
        self.assertIn("--resume", execute.call_args_list[1].args[0])

    def test_nonzero_result_retries_once_and_reports_details(self):
        runner = ClaudeRunner()
        with patch.object(
            runner,
            "_run",
            side_effect=[result(1, stderr="first"), result(2, stderr="final failure")],
        ):
            with self.assertRaisesRegex(RuntimeError, "final failure"):
                runner.execute("prompt")

    def test_missing_resume_session_restarts_original_prompt_fresh(self):
        runner = ClaudeRunner(flags=["--flag"])
        payload = json.dumps({"result": "ok", "session_id": "fresh-session"})
        with patch.object(
            runner,
            "_run",
            side_effect=[
                result(1, stderr="No conversation found with session ID: stale-session"),
                result(stdout=payload),
            ],
        ) as execute:
            response = runner.execute("original prompt", session_id="stale-session")
        retry_args = execute.call_args_list[1].args[0]
        self.assertNotIn("--resume", retry_args)
        self.assertEqual(execute.call_args_list[1].args[1], "original prompt")
        self.assertEqual(response.session_id, "fresh-session")

    def test_missing_session_after_retry_restarts_original_prompt_fresh(self):
        runner = ClaudeRunner(flags=["--flag"])
        payload = json.dumps({"result": "ok", "session_id": "fresh-session"})
        with patch.object(
            runner,
            "_run",
            side_effect=[
                result(1, stderr="temporary failure"),
                result(1, stderr="No conversation found with session ID: generated-session"),
                result(stdout=payload),
            ],
        ) as execute:
            response = runner.execute("original prompt")
        final_args = execute.call_args_list[2].args[0]
        self.assertNotIn("--resume", final_args)
        self.assertEqual(execute.call_args_list[2].args[1], "original prompt")
        self.assertEqual(response.session_id, "fresh-session")

    def test_long_prompt_is_sent_via_stdin_not_command_line(self):
        runner = ClaudeRunner(flags=["--flag"])
        payload = json.dumps({"result": "ok", "session_id": "session-1"})
        prompt = "x" * 100000
        with patch.object(runner, "_run", return_value=result(stdout=payload)) as execute:
            runner.execute(prompt)
        self.assertNotIn(prompt, execute.call_args.args[0])
        self.assertEqual(execute.call_args.args[1], prompt)


class RunnerOutputDecodingTests(unittest.TestCase):
    @patch("agents.runners.base.locale.getpreferredencoding", return_value="UTF-8")
    def test_decodes_windows_gbk_error_output_on_utf8_host(self, _preferred_encoding):
        self.assertEqual(decode_subprocess_output("命令行太长".encode("gbk")), "命令行太长")


class CodexRunnerTests(unittest.TestCase):
    def test_jsonl_returns_thread_and_message(self):
        stdout = "\n".join([
            json.dumps({"type": "thread.started", "thread_id": "thread-1"}),
            json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "done"}}),
        ])
        runner = CodexRunner(flags=[])
        with patch.object(runner, "_run", return_value=result(stdout=stdout)):
            response = runner.execute("prompt")
        self.assertEqual(response.output, "done")
        self.assertEqual(response.session_id, "thread-1")

    def test_resume_uses_session(self):
        runner = CodexRunner(flags=[])
        with patch.object(runner, "_run", return_value=result(stdout="")) as execute:
            runner.execute("prompt", session_id="thread-1")
        self.assertIn("resume", execute.call_args.args[0])
        self.assertIn("thread-1", execute.call_args.args[0])


class OpenCodeRunnerTests(unittest.TestCase):
    def test_json_events_return_session_and_text(self):
        stdout = json.dumps({
            "type": "text",
            "sessionID": "ses_1",
            "part": {"text": "done"},
        })
        runner = OpenCodeRunner()
        with patch.object(runner, "_run", return_value=result(stdout=stdout)):
            response = runner.execute("prompt")
        self.assertEqual(response.output, "done")
        self.assertEqual(response.session_id, "ses_1")

    def test_resume_uses_session(self):
        runner = OpenCodeRunner()
        with patch.object(runner, "_run", return_value=result(stdout="")) as execute:
            runner.execute("prompt", session_id="ses_1")
        self.assertIn("--session", execute.call_args.args[0])
        self.assertIn("ses_1", execute.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
