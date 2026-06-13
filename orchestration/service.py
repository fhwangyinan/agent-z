from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from rich.live import Live

from config import (
    MAX_PARALLEL_TASKS,
    SERVICE_LOG_BACKUPS,
    SERVICE_LOG_MAX_BYTES,
    SERVICE_RESTART_MAX_ATTEMPTS,
    SERVICE_RESTART_MAX_DELAY,
    SERVICE_RESTART_DELAY,
    SERVICE_RESTART_RESET_SECONDS,
    STATE_DB,
)
from orchestration.github_ops import validate_environment
from orchestration.store import RunStore
from orchestration.tui import console, render_service_dashboard

MAX_SERVICE_NOTICES = 200


@dataclass
class ServiceProcess:
    name: str
    args: list[str]
    process: subprocess.Popen | None = None
    restarts: int = 0
    log_path: str | None = None
    log_handle: BinaryIO | None = None
    started_at: float | None = None
    consecutive_failures: int = 0
    circuit_open: bool = False


def service_specs(
    workers: int,
    *,
    force: bool = False,
    keep_worktree: bool = False,
) -> list[ServiceProcess]:
    worker_args = ["--worker"]
    if force:
        worker_args.append("--force")
    if keep_worktree:
        worker_args.append("--keep-worktree")
    return [
        ServiceProcess("scheduler", ["--scheduler"]),
        ServiceProcess("planner", ["--planner"]),
        *[
            ServiceProcess(f"worker-{index + 1}", list(worker_args))
            for index in range(workers)
        ],
        ServiceProcess("reconciler", ["--reconciler"]),
    ]


def _spawn(service: ServiceProcess, *, max_parallel: int) -> subprocess.Popen:
    entrypoint = str(Path(__file__).resolve().parents[1] / "run.py")
    environment = os.environ.copy()
    environment["MAX_PARALLEL_TASKS"] = str(max_parallel)
    environment["AGENT_Z_QUIET_IDLE"] = "1"
    environment["AGENT_Z_QUIET_LIVE"] = "1"
    logs = Path(STATE_DB).resolve().parent / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    if service.log_handle is not None:
        service.log_handle.close()
    service.log_path = str(logs / f"{service.name}.log")
    log_path = Path(service.log_path)
    if log_path.exists() and log_path.stat().st_size >= SERVICE_LOG_MAX_BYTES:
        oldest = log_path.with_name(f"{log_path.name}.{SERVICE_LOG_BACKUPS}")
        oldest.unlink(missing_ok=True)
        for index in range(SERVICE_LOG_BACKUPS - 1, 0, -1):
            source = log_path.with_name(f"{log_path.name}.{index}")
            if source.exists():
                source.replace(log_path.with_name(f"{log_path.name}.{index + 1}"))
        log_path.replace(log_path.with_name(f"{log_path.name}.1"))
    service.log_handle = open(service.log_path, "ab", buffering=0)
    kwargs = {
        "env": environment,
        "stdout": service.log_handle,
        "stderr": subprocess.STDOUT,
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    process = subprocess.Popen([sys.executable, entrypoint, *service.args], **kwargs)
    service.process = process
    service.started_at = time.monotonic()
    service.circuit_open = False
    return process


def _register_service_failure(service: ServiceProcess) -> int | None:
    service.restarts += 1
    service.consecutive_failures += 1
    if service.consecutive_failures > SERVICE_RESTART_MAX_ATTEMPTS:
        service.circuit_open = True
        return None
    return min(
        SERVICE_RESTART_MAX_DELAY,
        SERVICE_RESTART_DELAY * (2 ** (service.consecutive_failures - 1)),
    )


def _stop(service: ServiceProcess):
    process = service.process
    if process is not None and process.poll() is None:
        try:
            if os.name == "nt":
                process.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=10)
        except Exception:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
    if service.log_handle is not None:
        service.log_handle.close()
        service.log_handle = None


def _read_dashboard_key() -> str | None:
    if os.name == "nt":
        import msvcrt

        if not msvcrt.kbhit():
            return None
        key = msvcrt.getwch()
        if key in {"\x00", "\xe0"}:
            arrow = msvcrt.getwch()
            return {"H": "up", "P": "down"}.get(arrow)
        return {"\r": "enter", " ": "enter", "\t": "tab"}.get(key, key.lower())
    try:
        import select

        if select.select([sys.stdin], [], [], 0)[0]:
            key = sys.stdin.read(1)
            if key == "\x1b" and select.select([sys.stdin], [], [], 0.02)[0]:
                key += sys.stdin.read(2)
            return {
                "\n": "enter",
                " ": "enter",
                "\t": "tab",
                "\x1b[A": "up",
                "\x1b[B": "down",
            }.get(key, key.lower())
    except (OSError, ValueError):
        return None
    return None


@contextmanager
def _dashboard_keyboard():
    if os.name == "nt" or not sys.stdin.isatty():
        yield
        return
    import termios
    import tty

    settings = termios.tcgetattr(sys.stdin)
    try:
        tty.setcbreak(sys.stdin.fileno())
        yield
    finally:
        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, settings)


def run_service(
    *,
    workers: int = MAX_PARALLEL_TASKS,
    force: bool = False,
    keep_worktree: bool = False,
    key_reader=None,
    sleep=None,
) -> int:
    key_reader = key_reader or _read_dashboard_key
    sleep = sleep or time.sleep
    started = time.monotonic()
    validate_environment()
    store = RunStore(STATE_DB)
    services = service_specs(
        workers,
        force=force,
        keep_worktree=keep_worktree,
    )
    notices = deque(
        [f"Starting Scheduler, Planner, {workers} Worker(s), and Reconciler"],
        maxlen=MAX_SERVICE_NOTICES,
    )
    selected = 0
    selected_run_id = None
    selected_service = 0
    focus = "tasks"
    expanded = False
    try:
        for service in services:
            process = _spawn(service, max_parallel=workers)
            notices.append(f"Started {service.name} | PID {process.pid} | log {service.log_path}")

        with _dashboard_keyboard():
            with Live(
                console=console,
                screen=console.is_terminal,
                refresh_per_second=4,
            ) as live:
                while True:
                    records = store.list(limit=20)
                    if focus == "tasks":
                        if selected_run_id:
                            matches = [
                                index
                                for index, record in enumerate(records)
                                if record.run_id == selected_run_id
                            ]
                            if matches:
                                selected = matches[0]
                        selected = max(0, min(selected, max(0, len(records) - 1)))
                        selected_run_id = records[selected].run_id if records else None
                    dashboard, run_count = render_service_dashboard(
                        store,
                        services,
                        selected=selected,
                        selected_run_id=selected_run_id,
                        selected_service=selected_service,
                        focus=focus,
                        expanded=expanded,
                        uptime=time.monotonic() - started,
                        notices=list(notices),
                    )
                    live.update(dashboard)
                    key = key_reader()
                    if key == "q":
                        notices.append("Stopping service by user request")
                        break
                    if key == "tab":
                        focus = "processes" if focus == "tasks" else "tasks"
                        expanded = False
                    elif key in {"up", "k"}:
                        if focus == "processes":
                            selected_service = max(0, selected_service - 1)
                        else:
                            selected = max(0, selected - 1)
                            selected_run_id = None
                    elif key in {"down", "j"}:
                        if focus == "processes":
                            selected_service = min(
                                max(0, len(services) - 1), selected_service + 1
                            )
                        else:
                            selected = min(max(0, run_count - 1), selected + 1)
                            selected_run_id = None
                    elif key == "enter":
                        expanded = not expanded
                    sleep(0.25)
                    for service in services:
                        process = service.process
                        if process is None:
                            continue
                        returncode = process.poll()
                        if returncode is None:
                            if (
                                service.consecutive_failures
                                and service.started_at is not None
                                and time.monotonic() - service.started_at
                                >= SERVICE_RESTART_RESET_SECONDS
                            ):
                                service.consecutive_failures = 0
                            continue
                        if service.circuit_open:
                            continue
                        restart_delay = _register_service_failure(service)
                        if restart_delay is None:
                            notices.append(
                                f"{service.name} circuit open after "
                                f"{SERVICE_RESTART_MAX_ATTEMPTS} restart attempts"
                            )
                            continue
                        notices.append(
                            f"{service.name} exited with code {returncode}; "
                            f"restart attempt {service.consecutive_failures}/"
                            f"{SERVICE_RESTART_MAX_ATTEMPTS}"
                        )
                        deadline = time.monotonic() + restart_delay
                        while time.monotonic() < deadline:
                            dashboard, _ = render_service_dashboard(
                                store,
                                services,
                                selected=selected,
                                selected_run_id=selected_run_id,
                                selected_service=selected_service,
                                focus=focus,
                                expanded=expanded,
                                uptime=time.monotonic() - started,
                                notices=list(notices) + [
                                    f"Restarting {service.name} in "
                                    f"{max(0, int(deadline - time.monotonic() + 0.999))}s"
                                ],
                            )
                            live.update(dashboard)
                            sleep(min(0.25, max(0, deadline - time.monotonic())))
                        process = _spawn(service, max_parallel=workers)
                        notices.append(
                            f"Restarted {service.name} | PID {process.pid} | "
                            f"restarts {service.restarts}"
                        )
    except KeyboardInterrupt:
        notices.append("Stopping service after keyboard interrupt")
    finally:
        for service in reversed(services):
            _stop(service)
    return 0
