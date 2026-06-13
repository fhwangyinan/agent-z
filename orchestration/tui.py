import json
import locale
import os
import time
from datetime import datetime, timezone
from typing import Callable

from rich import box
from rich.align import Align
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.markup import escape
from rich.spinner import Spinner
from rich.panel import Panel
from rich.prompt import IntPrompt, Prompt
from rich.table import Table
from rich.text import Text

from agents import __version__
from agents.base import format_duration, run_cmd, warn
from config import GITHUB_REPO, MAX_PARALLEL_TASKS, REVIEWER_BACKEND, TASK_LEAD_BACKEND
from orchestration.store import FINAL_STATUSES, RunRecord, RunStore
from orchestration.runtime import runtime

console = Console()

STATUS_STYLES = {
    "queued": "dim cyan",
    "planning": "cyan",
    "ready": "blue",
    "running": "yellow",
    "waiting_checks": "magenta",
    "completed": "green",
    "skipped": "dim",
    "failed": "red",
    "needs_human": "bold red",
    "cancelled": "dim red",
}

STAGE_LABELS = {
    "queued": "Queued",
    "analyzing": "Planning",
    "created": "Analyzed",
    "assessing": "Impact assessment",
    "assessed": "Assessed",
    "ready": "Ready",
    "developing": "Development",
    "reviewing": "Local review",
    "submitting": "Submission",
    "waiting_checks": "PR checks",
    "handling_feedback": "PR feedback",
    "completed": "Completed",
    "skipped": "Skipped",
    "cancelled": "Cancelled",
}

def show_banner():
    console.print()
    table = Table.grid(padding=(0, 2))
    table.add_column(style="dim", justify="right")
    table.add_column()
    table.add_row("Repository", GITHUB_REPO)
    table.add_row("Task Lead", TASK_LEAD_BACKEND)
    table.add_row("Reviewer", REVIEWER_BACKEND)
    table.add_row("Concurrency", str(MAX_PARALLEL_TASKS))
    table.add_row("Process", str(os.getpid()))
    console.print(Align.center(
        Panel.fit(
            table,
            title=f"[bold cyan]Agent-Z v{__version__}[/bold cyan]",
            subtitle="[dim]Scheduler | Planner Pool | Worker Pool | Reconciler[/dim]",
            border_style="cyan",
            padding=(0, 4),
        )
    ))
    console.print(Align.center(
        "[dim]Task Lead -> Independent Reviewer -> Deterministic Coordinator -> PR Checks[/dim]"
    ))

def _safe_text(value, default: str = "-") -> str:
    if isinstance(value, str):
        return value or default
    if isinstance(value, (int, float)):
        return str(value)
    return default

def _parse_iso(value: str | None) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None

def _record_age(record: RunRecord) -> str:
    created = _parse_iso(getattr(record, "created_at", None))
    if created is None:
        return "-"
    if getattr(record, "status", None) in FINAL_STATUSES:
        end = _parse_iso(getattr(record, "updated_at", None))
        if end is None:
            end = datetime.now(timezone.utc)
    else:
        end = datetime.now(timezone.utc)
    return format_duration((end - created).total_seconds())

def _status_text(status: str) -> str:
    status = _safe_text(status, "unknown")
    style = STATUS_STYLES.get(status, "white")
    return f"[{style}]{status.replace('_', ' ').upper()}[/]"

def _run_context_line(record: RunRecord, *, elapsed: float | None = None) -> str:
    run_id = _safe_text(getattr(record, "run_id", None))
    issue_number = _safe_text(getattr(record, "issue_number", None))
    status = _safe_text(getattr(record, "status", None), "unknown")
    stage = _safe_text(getattr(record, "stage", None), "unknown")
    parts = [
        f"[bold cyan]RUN {run_id}[/bold cyan]",
        f"[bold]Issue #{issue_number}[/bold]",
        _status_text(status),
        f"[dim]{STAGE_LABELS.get(stage, stage)}[/dim]",
    ]
    if getattr(record, "lease_role", None):
        parts.append(f"[dim]lease:{record.lease_role}[/dim]")
    if elapsed is not None:
        parts.append(f"[dim]elapsed:{format_duration(elapsed)}[/dim]")
    return "  |  ".join(parts)

def run_step(record: RunRecord, title: str, *, started: float | None = None):
    elapsed = time.monotonic() - started if started is not None else None
    console.print()
    console.rule(f"[bold white]{title}[/bold white]", style="cyan")
    console.print(f"  {_run_context_line(record, elapsed=elapsed)}")

def show_run_summary(
    record: RunRecord,
    *,
    title: str,
    elapsed: float | None = None,
    message: str | None = None,
    border_style: str | None = None,
):
    run_id = _safe_text(getattr(record, "run_id", None))
    issue_number = _safe_text(getattr(record, "issue_number", None))
    status = _safe_text(getattr(record, "status", None), "unknown")
    stage = _safe_text(getattr(record, "stage", None), "unknown")
    table = Table.grid(padding=(0, 2))
    table.add_column(style="dim", justify="right")
    table.add_column()
    table.add_row("Run ID", f"[bold cyan]{run_id}[/bold cyan]")
    table.add_row("Issue", f"#{issue_number}")
    table.add_row("Status", _status_text(status))
    table.add_row("Stage", STAGE_LABELS.get(stage, stage))
    table.add_row("Elapsed", format_duration(elapsed) if elapsed is not None else _record_age(record))
    if getattr(record, "risk", None):
        table.add_row("Risk", str(record.risk))
    if getattr(record, "branch", None):
        table.add_row("Branch", str(record.branch))
    if getattr(record, "pr_url", None):
        table.add_row("PR", f"[link={record.pr_url}]{record.pr_url}[/link]")
    if message:
        table.add_row("Details", message)
    style = border_style or STATUS_STYLES.get(status, "cyan").split()[-1]
    console.print(Panel(table, title=title, border_style=style))

def show_pool_status(role: str, state: str, details: str, *, style: str = "cyan"):
    console.print(
        Panel(
            f"[bold]{state}[/bold]\n[dim]{details}[/dim]",
            title=f"[bold {style}]{role}[/bold {style}]",
            border_style=style,
            padding=(0, 2),
        )
    )

def wait_with_status(
    role: str,
    details: str | Callable[[], str],
    seconds: int,
    *,
    style: str = "cyan",
    sleep=time.sleep,
):
    if os.environ.get("AGENT_Z_QUIET_IDLE") == "1":
        sleep(seconds)
        return
    deadline = time.monotonic() + seconds
    spinner = Spinner("dots", style=style)
    with Live(console=console, refresh_per_second=4, transient=True) as live:
        while True:
            remaining = max(0, int(deadline - time.monotonic() + 0.999))
            current_details = details() if callable(details) else details
            spinner.update(
                text=f"[bold {style}]{role} idle[/bold {style}] "
                f"[dim]| {current_details} | next scan:{remaining}s[/dim]"
            )
            live.update(spinner)
            if remaining <= 0:
                break
            sleep(min(1, remaining))

def _show_pr_checks(checks: list[dict], elapsed: float):
    table = Table(
        title=f"PR checks | {format_duration(elapsed)}",
        box=box.SIMPLE,
        show_edge=False,
    )
    table.add_column("Check")
    table.add_column("Result", justify="right")
    for check in checks:
        bucket = str(check.get("bucket", "unknown"))
        style = {"pass": "green", "fail": "red", "pending": "yellow"}.get(bucket, "dim")
        table.add_row(str(check.get("name", "(unnamed)")), f"[{style}]{bucket.upper()}[/{style}]")
    console.print(table)

def choose_issue() -> int | None:
    console.print()
    if runtime.auto_mode:
        loop_info = (
            f" (round {runtime.current_loop}/{runtime.total_loops})"
            if runtime.total_loops > 1 else ""
        )
        console.print(Panel(
            f"[bold]Autonomous mode: Agent selects an issue{loop_info}[/bold]",
            border_style="cyan",
        ))
        return None

    table = Table(show_header=False, box=None, padding=(0, 4))
    table.add_column(style="cyan")
    table.add_column()
    table.add_row("[bold][1][/bold]", "Let the Agent recommend the highest-priority issue")
    table.add_row("[bold][2][/bold]", "Enter an issue number")
    console.print(Panel(table, title="[bold]Select mode[/bold]", border_style="cyan"))
    choice = Prompt.ask("", choices=["1", "2"], default="1")
    if choice == "1":
        return None
    else:
        return IntPrompt.ask("Issue number")


def _get_issue_title(issue_number: int) -> str:
    try:
        result = run_cmd(
            ["gh", "issue", "view", str(issue_number), "--json", "title,labels,state"],
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            data = json.loads(result.stdout)
            title = data.get("title", "(untitled)")
            labels = "  ".join(f"[{label['name']}]" for label in data.get("labels", []))
            return f"{title}\n[dim]{labels}[/dim]" if labels else title
    except Exception:
        pass
    return "(unable to fetch title)"

def confirm_issue(issue_number: int) -> int | None:
    title = _get_issue_title(issue_number)
    console.print(Panel(
        title,
        title=f"[bold green]#{issue_number}[/bold green]",
        border_style="green",
        subtitle="Confirm? (y/n or another issue number)",
        subtitle_align="left",
    ))

    if runtime.auto_mode:
        return issue_number

    while True:
        user_input = Prompt.ask("", default="y").strip()
        if user_input.lower() in ("y", "yes", ""):
            return issue_number
        elif user_input.lower() in ("n", "no"):
            return None
        elif user_input.isdigit():
            new_num = int(user_input)
            title = _get_issue_title(new_num)
            console.print(f"  -> Switched to [bold]#{new_num}[/bold]")
            console.print(Panel(title, title=f"[bold green]#{new_num}[/bold green]", border_style="green"))
            return new_num
        else:
            warn("Invalid input")

def show_analysis(issue_number: int, analysis: str):
    """Display the analysis in a panel."""
    display = analysis[:2000] + ("..." if len(analysis) > 2000 else "")
    console.print(Panel(display, title=f"[bold cyan]Task Lead -> Issue #{issue_number}[/bold cyan]", border_style="blue"))

def show_runs(store: RunStore):
    records = store.list()
    counts: dict[str, int] = {}
    for record in records:
        counts[record.status] = counts.get(record.status, 0) + 1
    summary = "  ".join(
        f"{_status_text(status)} {count}"
        for status, count in sorted(counts.items())
    ) or "[dim]No runs[/dim]"
    console.print(Panel(summary, title="Run status summary", border_style="cyan"))

    table = Table(title="Recent Agent-Z runs", box=box.ROUNDED)
    table.add_column("Run ID", style="bold cyan", no_wrap=True)
    table.add_column("Issue", justify="right")
    table.add_column("Status")
    table.add_column("Stage")
    table.add_column("Age", justify="right")
    table.add_column("Lease")
    table.add_column("Result / Error", overflow="fold")
    for record in records:
        result = record.pr_url or record.error or ""
        table.add_row(
            record.run_id,
            f"#{record.issue_number}",
            _status_text(record.status),
            STAGE_LABELS.get(record.stage, record.stage),
            _record_age(record),
            getattr(record, "lease_role", None) or "-",
            result,
        )
    console.print(table)

def show_run_detail(store: RunStore, run_id: str):
    record = store.get(run_id)
    show_run_summary(
        record,
        title=f"Run {run_id}",
        message=record.error or None,
    )
    details = Table(title="Runtime details", show_header=False, box=box.SIMPLE)
    details.add_column("Field", style="cyan")
    details.add_column("Value")
    rows = [
        ("Issue", f"#{record.issue_number}"),
        ("Repo", record.repo),
        ("Status", record.status),
        ("Stage", record.stage),
        ("Risk", record.risk or ""),
        ("Branch", record.branch or ""),
        ("PR", record.pr_url or ""),
        ("Worktree", record.worktree_path or ""),
        ("Owner PID", str(record.owner_pid or "")),
        ("Lease role", getattr(record, "lease_role", None) or ""),
        ("Lease expires", getattr(record, "lease_expires_at", None) or ""),
        ("Touched files", str(len(getattr(record, "touched_files", []) or []))),
        ("Sessions", ", ".join(sorted((getattr(record, "sessions", {}) or {}).keys())) or ""),
        ("Error", record.error or ""),
        ("Created", record.created_at.replace("T", " ")[:19]),
        ("Updated", record.updated_at.replace("T", " ")[:19]),
    ]
    for key, value in rows:
        details.add_row(key, value)
    console.print(details)
    if getattr(record, "plan", None):
        console.print(Panel(
            Text(json.dumps(record.plan, ensure_ascii=True, indent=2, sort_keys=True)),
            title="Execution plan",
        ))

    events = store.list_events(run_id)
    timeline = Table(title="Event timeline", box=box.ROUNDED, show_lines=False)
    timeline.add_column("#", justify="right", style="dim")
    timeline.add_column("+Time", justify="right", style="dim")
    timeline.add_column("Event", style="cyan")
    timeline.add_column("Status / Stage")
    timeline.add_column("Details", overflow="fold")
    first_at = _parse_iso(events[0].created_at) if events else None
    for event in events:
        created = _parse_iso(event.created_at)
        delta = (
            format_duration((created - first_at).total_seconds())
            if created is not None and first_at is not None
            else "-"
        )
        data = json.dumps(event.data, ensure_ascii=True, sort_keys=True)
        details_text = event.message or ""
        if data != "{}":
            details_text = f"{details_text} {data}".strip()
        status = _status_text(event.status) if event.status else "-"
        timeline.add_row(
            str(event.event_id),
            delta,
            event.event_type,
            f"{status}\n[dim]{event.stage or '-'}[/dim]",
            details_text,
        )
    console.print(timeline)


def _service_activity(service, records: list[RunRecord], global_events) -> str:
    process = getattr(service, "process", None)
    pid = getattr(process, "pid", None)
    for record in records:
        if pid is not None and getattr(record, "owner_pid", None) == pid:
            stage = STAGE_LABELS.get(record.stage, record.stage)
            return f"#{record.issue_number} · {stage}"

    name = str(getattr(service, "name", ""))
    if name == "scheduler":
        scheduler_events = [
            event for event in global_events if event.event_type.startswith("scheduler_")
        ]
        if scheduler_events:
            return scheduler_events[-1].message or scheduler_events[-1].event_type
        return "waiting for first scan"
    if name == "planner":
        return "waiting for queued task"
    if name.startswith("worker-"):
        return "waiting for ready task"
    if name == "reconciler":
        return "waiting for expired lease"
    return ""


def _service_table(
    services,
    records: list[RunRecord],
    global_events,
    selected_service: int | None = None,
) -> Table:
    table = Table(box=box.SIMPLE, expand=True, show_edge=False)
    table.add_column("", width=2)
    table.add_column("Process", width=12, no_wrap=True)
    table.add_column("PID", justify="right", width=7)
    table.add_column("State", width=12, no_wrap=True)
    table.add_column("Activity", overflow="ellipsis", no_wrap=True)
    table.add_column("R", justify="right", width=3)
    for index, service in enumerate(services):
        process = getattr(service, "process", None)
        returncode = process.poll() if process is not None else None
        alive = process is not None and returncode is None
        if getattr(service, "circuit_open", False):
            state = "[bold red]CIRCUIT OPEN[/bold red]"
        else:
            state = "[green]RUNNING[/green]" if alive else f"[red]EXIT {returncode}[/red]"
        table.add_row(
            ">" if selected_service == index else "",
            str(getattr(service, "name", "unknown")),
            str(getattr(process, "pid", "-")),
            state,
            _service_activity(service, records, global_events),
            str(getattr(service, "restarts", 0)),
        )
    return table


def _dashboard_runs_table(records: list[RunRecord], selected: int) -> Table:
    table = Table(box=box.SIMPLE, expand=True, show_edge=False)
    table.add_column("", width=2)
    table.add_column("Issue", width=8)
    table.add_column("Status", width=16)
    table.add_column("Stage", width=18)
    table.add_column("Age", justify="right", width=10)
    table.add_column("Result / Error", overflow="ellipsis")
    for index, record in enumerate(records):
        table.add_row(
            ">" if index == selected else "",
            f"#{record.issue_number}",
            _status_text(record.status),
            STAGE_LABELS.get(record.stage, record.stage),
            _record_age(record),
            record.pr_url or record.error or "",
        )
    if not records:
        table.add_row("", "-", "[dim]NO RUNS[/dim]", "-", "-", "")
    return table


def _dashboard_run_detail(
    store: RunStore,
    record: RunRecord | None,
    expanded: bool,
    *,
    empty_message: str | None = None,
):
    if record is None:
        content = "[dim]No persisted tasks yet[/dim]"
        if empty_message:
            content += f"\n\n[bold]Scheduler[/bold]\n{escape(empty_message)}"
        return Panel(content, title="Task detail", border_style="dim")
    lines = [
        f"[bold cyan]{record.run_id}[/bold cyan]  |  Issue #{record.issue_number}",
        f"{_status_text(record.status)}  |  {STAGE_LABELS.get(record.stage, record.stage)}",
    ]
    if record.pr_url:
        lines.append(f"PR: {escape(record.pr_url)}")
    if record.error:
        lines.append(f"[red]{escape(record.error)}[/red]")
    if expanded:
        lines.extend([
            f"[dim]Branch:[/dim] {record.branch or '-'}",
            f"[dim]Lease:[/dim] {record.lease_role or '-'} / {record.lease_expires_at or '-'}",
            f"[dim]Worktree:[/dim] {record.worktree_path or '-'}",
        ])
        events = store.list_events(record.run_id)[-8:]
        if events:
            lines.append("")
            lines.append("[bold]Recent events[/bold]")
            for event in events:
                message = (event.message or "").replace("\n", " ")
                lines.append(
                    f"[dim]{escape(event.event_type)}[/dim] {escape(message[:160])}"
                )
    return Panel(
        "\n".join(lines),
        title=f"Task detail {'[expanded]' if expanded else '[collapsed]'}",
        border_style="cyan",
    )

def _tail_service_log(service, *, expanded: bool) -> Panel:
    log_path = getattr(service, "log_path", None)
    lines = []
    if log_path:
        try:
            with open(log_path, "rb") as handle:
                handle.seek(0, 2)
                size = handle.tell()
                handle.seek(max(0, size - 65536))
                data = handle.read()
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                text = data.decode(locale.getpreferredencoding(False), errors="replace")
            lines = text.splitlines()[-(30 if expanded else 8):]
        except OSError as exc:
            lines = [f"Could not read log: {exc}"]
    content = "\n".join(escape(line) for line in lines) or "[dim]No log output yet[/dim]"
    return Panel(
        content,
        title=f"Process log {'[expanded]' if expanded else '[collapsed]'}",
        border_style="blue",
    )


def render_service_dashboard(
    store: RunStore,
    services,
    *,
    selected: int = 0,
    selected_run_id: str | None = None,
    selected_service: int = 0,
    focus: str = "tasks",
    expanded: bool = False,
    uptime: float = 0,
    notices: list[str] | None = None,
):
    records = store.list(limit=20)
    global_events = [
        event
        for event in store.list_global_events(limit=50)
        if not event.event_type.endswith("_idle")
    ]
    if selected_run_id:
        for index, record in enumerate(records):
            if record.run_id == selected_run_id:
                selected = index
                break
    selected = max(0, min(selected, max(0, len(records) - 1)))
    selected_service = max(0, min(selected_service, max(0, len(services) - 1)))
    selected_record = records[selected] if records else None
    selected_process = services[selected_service] if services else None
    alive = sum(
        1
        for service in services
        if getattr(service, "process", None) is not None
        and service.process.poll() is None
    )
    header = Panel(
        f"[bold cyan]Agent-Z Service[/bold cyan]  "
        f"[dim]alive:{alive}/{len(services)} | uptime:{format_duration(uptime)}[/dim]\n"
        "[dim]Tab switch pane  |  ↑/↓ or j/k select  |  Enter/Space expand  |  q stop[/dim]",
        border_style="cyan",
    )
    service_panel = Panel(
        _service_table(
            services,
            records,
            global_events,
            selected_service if focus == "processes" else None,
        ),
        title=f"Processes {'[focused]' if focus == 'processes' else ''}",
        border_style="bold blue" if focus == "processes" else "blue",
    )
    run_panel = Panel(
        _dashboard_runs_table(records, selected if focus == "tasks" else -1),
        title=f"Tasks {'[focused]' if focus == 'tasks' else ''}",
        border_style="bold green" if focus == "tasks" else "green",
    )
    detail_panel = (
        _tail_service_log(selected_process, expanded=expanded)
        if focus == "processes" and selected_process is not None
        else _dashboard_run_detail(
            store,
            selected_record,
            expanded,
            empty_message=next(
                (
                    event.message or event.event_type
                    for event in reversed(global_events)
                    if event.event_type.startswith("scheduler_")
                ),
                None,
            ),
        )
    )
    event_lines = [
        f"{event.event_type}: {event.message or ''}".rstrip()
        for event in global_events[-4:]
    ]
    notice_lines = [*(notices or []), *event_lines]
    notice_text = (
        "\n".join(escape(line) for line in notice_lines[-4:])
        or "[dim]No recent service events[/dim]"
    )
    notice_panel = Panel(notice_text, title="Service events", border_style="yellow")

    layout = Layout()
    layout.split_column(
        Layout(header, size=4),
        Layout(name="main", ratio=1),
        Layout(notice_panel, size=7),
    )
    layout["main"].split_row(
        Layout(name="left", ratio=2),
        Layout(detail_panel, ratio=3),
    )
    layout["main"]["left"].split_column(
        Layout(service_panel, size=max(8, len(services) + 4)),
        Layout(run_panel, ratio=1),
    )
    return layout, len(records)
