from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from agents.base import done, format_duration, warn
from config import MAX_PARALLEL_TASKS, SERVICE_RESTART_DELAY
from orchestration.github_ops import validate_environment
from orchestration.tui import console, show_pool_status


@dataclass
class ServiceProcess:
    name: str
    args: list[str]
    process: subprocess.Popen | None = None
    restarts: int = 0


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
    kwargs = {"env": environment}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    process = subprocess.Popen([sys.executable, entrypoint, *service.args], **kwargs)
    service.process = process
    return process


def _stop(service: ServiceProcess):
    process = service.process
    if process is None or process.poll() is not None:
        return
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


def run_service(
    *,
    workers: int = MAX_PARALLEL_TASKS,
    force: bool = False,
    keep_worktree: bool = False,
) -> int:
    started = time.monotonic()
    validate_environment()
    services = service_specs(
        workers,
        force=force,
        keep_worktree=keep_worktree,
    )
    show_pool_status(
        "Service",
        "STARTING",
        f"Scheduler 1 | Planner 1 | Worker {workers} | Reconciler 1 | "
        f"force: {'yes' if force else 'no'}",
        style="cyan",
    )
    try:
        for service in services:
            process = _spawn(service, max_parallel=workers)
            done(f"Started {service.name} [dim]| PID {process.pid}[/dim]")

        with console.status("[dim]Service running[/dim]", spinner="dots") as status:
            while True:
                alive = sum(
                    1
                    for service in services
                    if service.process is not None and service.process.poll() is None
                )
                status.update(
                    f"[bold cyan]Service running[/bold cyan] [dim]| "
                    f"alive:{alive}/{len(services)} | "
                    f"uptime:{format_duration(time.monotonic() - started)}[/dim]"
                )
                time.sleep(1)
                for service in services:
                    process = service.process
                    if process is None:
                        continue
                    returncode = process.poll()
                    if returncode is None:
                        continue
                    service.restarts += 1
                    warn(
                        f"{service.name} exited with code {returncode}; "
                        f"restarting in {SERVICE_RESTART_DELAY}s"
                    )
                    deadline = time.monotonic() + SERVICE_RESTART_DELAY
                    while time.monotonic() < deadline:
                        status.update(
                            f"[bold yellow]Restarting {service.name}[/bold yellow] "
                            f"[dim]| in:{format_duration(deadline - time.monotonic())} | "
                            f"alive:{alive}/{len(services)} | "
                            f"uptime:{format_duration(time.monotonic() - started)}[/dim]"
                        )
                        time.sleep(min(1, max(0, deadline - time.monotonic())))
                    process = _spawn(service, max_parallel=workers)
                    done(
                        f"Restarted {service.name} [dim]| PID {process.pid} | "
                        f"restarts {service.restarts}[/dim]"
                    )
    except KeyboardInterrupt:
        warn("Stopping Agent-Z service")
    finally:
        for service in reversed(services):
            _stop(service)
        show_pool_status(
            "Service",
            "STOPPED",
            f"uptime {format_duration(time.monotonic() - started)}",
            style="yellow",
        )
    return 0
