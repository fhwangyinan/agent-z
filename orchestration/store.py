from __future__ import annotations

import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path


ACTIVE_STATUSES = {"planning", "running", "waiting_checks"}
FINAL_STATUSES = {"completed", "failed", "skipped", "needs_human", "cancelled"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _lease_deadline(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def _pid_alive(pid: int) -> bool:
    if os.name == "nt":
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _module_resources(files: set[str]) -> set[str]:
    resources = set()
    for path in files:
        normalized = path.replace("\\", "/").strip("/")
        parts = [part for part in normalized.split("/") if part]
        if not parts:
            continue
        module = "/".join(parts[:2]) if len(parts) > 2 else normalized
        resources.add(module)
    return resources


@dataclass
class RunRecord:
    run_id: str
    repo: str
    issue_number: int
    status: str
    stage: str
    worktree_path: str | None
    branch: str | None
    pr_url: str | None
    risk: str | None
    plan: dict
    lease_role: str | None
    lease_expires_at: str | None
    sessions: dict[str, str]
    touched_files: list[str]
    owner_pid: int | None
    error: str | None
    created_at: str
    updated_at: str


@dataclass
class RunEvent:
    event_id: int
    run_id: str | None
    event_type: str
    stage: str | None
    status: str | None
    message: str | None
    data: dict
    created_at: str


class RunStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def _initialize(self):
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    repo TEXT NOT NULL,
                    issue_number INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    worktree_path TEXT,
                    branch TEXT,
                    pr_url TEXT,
                    risk TEXT,
                    plan TEXT NOT NULL DEFAULT '{}',
                    lease_role TEXT,
                    lease_expires_at TEXT,
                    sessions TEXT NOT NULL DEFAULT '{}',
                    touched_files TEXT NOT NULL DEFAULT '[]',
                    owner_pid INTEGER,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS run_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT,
                    event_type TEXT NOT NULL,
                    stage TEXT,
                    status TEXT,
                    message TEXT,
                    data TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS scheduler_snapshots (
                    repo TEXT PRIMARY KEY,
                    candidate_state TEXT NOT NULL DEFAULT '{}',
                    queue_state TEXT NOT NULL DEFAULT '{}',
                    policy_state TEXT NOT NULL DEFAULT '{}',
                    decision_state TEXT NOT NULL DEFAULT '{}',
                    agent_evaluated_at TEXT,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS run_events_run_id_created_at
                ON run_events(run_id, created_at)
                """
            )
            connection.execute("DROP INDEX IF EXISTS active_issue_lock")
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS active_issue_lock
                ON runs(repo, issue_number)
                WHERE status IN ('queued', 'planning', 'ready', 'running', 'waiting_checks')
                """
            )
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(runs)").fetchall()
            }
            if "touched_files" not in columns:
                connection.execute(
                    "ALTER TABLE runs ADD COLUMN touched_files TEXT NOT NULL DEFAULT '[]'"
                )
            if "owner_pid" not in columns:
                connection.execute("ALTER TABLE runs ADD COLUMN owner_pid INTEGER")
            if "plan" not in columns:
                connection.execute(
                    "ALTER TABLE runs ADD COLUMN plan TEXT NOT NULL DEFAULT '{}'"
                )
            if "lease_role" not in columns:
                connection.execute("ALTER TABLE runs ADD COLUMN lease_role TEXT")
            if "lease_expires_at" not in columns:
                connection.execute("ALTER TABLE runs ADD COLUMN lease_expires_at TEXT")
            snapshot_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(scheduler_snapshots)"
                ).fetchall()
            }
            if "policy_state" not in snapshot_columns:
                connection.execute(
                    "ALTER TABLE scheduler_snapshots "
                    "ADD COLUMN policy_state TEXT NOT NULL DEFAULT '{}'"
                )
            if "agent_evaluated_at" not in snapshot_columns:
                connection.execute(
                    "ALTER TABLE scheduler_snapshots ADD COLUMN agent_evaluated_at TEXT"
                )
            if "decision_state" not in snapshot_columns:
                connection.execute(
                    "ALTER TABLE scheduler_snapshots "
                    "ADD COLUMN decision_state TEXT NOT NULL DEFAULT '{}'"
                )

    @staticmethod
    def _record(row: sqlite3.Row) -> RunRecord:
        data = dict(row)
        data["sessions"] = json.loads(data["sessions"] or "{}")
        data["touched_files"] = json.loads(data["touched_files"] or "[]")
        data["plan"] = json.loads(data["plan"] or "{}")
        return RunRecord(**data)

    @staticmethod
    def _event(row: sqlite3.Row) -> RunEvent:
        data = dict(row)
        data["data"] = json.loads(data["data"] or "{}")
        return RunEvent(**data)

    def _insert_event(
        self,
        connection: sqlite3.Connection,
        *,
        run_id: str | None,
        event_type: str,
        stage: str | None = None,
        status: str | None = None,
        message: str | None = None,
        data: dict | None = None,
    ):
        connection.execute(
            """
            INSERT INTO run_events (
                run_id, event_type, stage, status, message, data, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                event_type,
                stage,
                status,
                message,
                json.dumps(data or {}, sort_keys=True),
                _now(),
            ),
        )

    def add_event(
        self,
        run_id: str | None,
        event_type: str,
        *,
        stage: str | None = None,
        status: str | None = None,
        message: str | None = None,
        data: dict | None = None,
    ) -> RunEvent:
        with self._connect() as connection:
            self._insert_event(
                connection,
                run_id=run_id,
                event_type=event_type,
                stage=stage,
                status=status,
                message=message,
                data=data,
            )
            event_id = connection.execute(
                "SELECT last_insert_rowid()"
            ).fetchone()[0]
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM run_events WHERE event_id = ?", (event_id,)
            ).fetchone()
        return self._event(row)

    def list_events(self, run_id: str, limit: int = 100) -> list[RunEvent]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM run_events
                WHERE run_id = ?
                ORDER BY event_id ASC
                LIMIT ?
                """,
                (run_id, limit),
            ).fetchall()
        return [self._event(row) for row in rows]

    def list_global_events(self, limit: int = 20) -> list[RunEvent]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM run_events
                WHERE run_id IS NULL
                ORDER BY event_id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._event(row) for row in reversed(rows)]

    def count_events(self, run_id: str, event_type: str) -> int:
        with self._connect() as connection:
            return int(connection.execute(
                """
                SELECT COUNT(*) FROM run_events
                WHERE run_id = ? AND event_type = ?
                """,
                (run_id, event_type),
            ).fetchone()[0])

    def create(self, repo: str, issue_number: int, max_parallel: int) -> RunRecord:
        run_id = uuid.uuid4().hex[:12]
        timestamp = _now()
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                active = connection.execute(
                    "SELECT COUNT(*) FROM runs WHERE status IN ('running', 'waiting_checks')"
                ).fetchone()[0]
                if active >= max_parallel:
                    raise RuntimeError(
                        f"parallel run limit reached ({active}/{max_parallel})"
                    )
                connection.execute(
                    """
                    INSERT INTO runs (
                        run_id, repo, issue_number, status, stage, sessions, owner_pid,
                        lease_role, lease_expires_at, created_at, updated_at
                    ) VALUES (?, ?, ?, 'running', 'created', '{}', ?, 'worker', ?, ?, ?)
                    """,
                    (
                        run_id, repo, issue_number, os.getpid(),
                        _lease_deadline(21600), timestamp, timestamp,
                    ),
                )
                self._insert_event(
                    connection,
                    run_id=run_id,
                    event_type="run_created",
                    stage="created",
                    status="running",
                    message=f"Created run for issue #{issue_number}",
                    data={"repo": repo, "issue_number": issue_number},
                )
        except sqlite3.IntegrityError as exc:
            raise RuntimeError(
                f"issue #{issue_number} already has an active run"
            ) from exc
        return self.get(run_id)

    def enqueue(self, repo: str, issue_number: int) -> RunRecord:
        run_id = uuid.uuid4().hex[:12]
        timestamp = _now()
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO runs (
                        run_id, repo, issue_number, status, stage, sessions,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, 'queued', 'queued', '{}', ?, ?)
                    """,
                    (run_id, repo, issue_number, timestamp, timestamp),
                )
                self._insert_event(
                    connection,
                    run_id=run_id,
                    event_type="run_enqueued",
                    stage="queued",
                    status="queued",
                    message=f"Enqueued issue #{issue_number}",
                    data={"repo": repo, "issue_number": issue_number},
                )
        except sqlite3.IntegrityError as exc:
            raise RuntimeError(
                f"issue #{issue_number} already has a queued or active run"
            ) from exc
        return self.get(run_id)

    def _claim(
        self,
        *,
        from_status: str,
        to_status: str,
        stage: str | None,
        role: str,
        lease_seconds: int,
        max_parallel: int | None = None,
        run_id: str | None = None,
    ) -> RunRecord | None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if max_parallel is not None:
                active = connection.execute(
                    "SELECT COUNT(*) FROM runs WHERE status IN ('running', 'waiting_checks')"
                ).fetchone()[0]
                if active >= max_parallel:
                    return None
            if run_id:
                row = connection.execute(
                    "SELECT run_id, stage FROM runs WHERE status = ? AND run_id = ?",
                    (from_status, run_id),
                ).fetchone()
            else:
                order_column = "updated_at" if from_status == "ready" else "created_at"
                row = connection.execute(
                    f"""
                    SELECT run_id, stage FROM runs
                    WHERE status = ?
                    ORDER BY {order_column}
                    LIMIT 1
                    """,
                    (from_status,),
                ).fetchone()
            if row is None:
                return None
            claimed_stage = stage or row["stage"]
            connection.execute(
                """
                UPDATE runs
                SET status = ?, stage = ?, owner_pid = ?, lease_role = ?,
                    lease_expires_at = ?, updated_at = ?
                WHERE run_id = ?
                """,
                (
                    to_status, claimed_stage, os.getpid(), role,
                    _lease_deadline(lease_seconds), _now(), row["run_id"],
                ),
            )
            run_id = row["run_id"]
            self._insert_event(
                connection,
                run_id=run_id,
                event_type=f"{role}_claimed",
                stage=claimed_stage,
                status=to_status,
                message=f"{role.title()} claimed run",
                data={"owner_pid": os.getpid(), "lease_seconds": lease_seconds},
            )
        return self.get(run_id)

    def claim_for_planning(
        self, lease_seconds: int, run_id: str | None = None
    ) -> RunRecord | None:
        return self._claim(
            from_status="queued",
            to_status="planning",
            stage="analyzing",
            role="planner",
            lease_seconds=lease_seconds,
            run_id=run_id,
        )

    def claim_ready(
        self, max_parallel: int, lease_seconds: int, run_id: str | None = None
    ) -> RunRecord | None:
        return self._claim(
            from_status="ready",
            to_status="running",
            stage=None,
            role="worker",
            lease_seconds=lease_seconds,
            max_parallel=max_parallel,
            run_id=run_id,
        )

    def claim_next(self, max_parallel: int) -> RunRecord | None:
        """Backward-compatible alias for development workers."""
        return self.claim_ready(max_parallel, 21600)

    def finish_planning(
        self,
        run_id: str,
        *,
        plan: dict,
        risk: str,
        sessions: dict[str, str] | None = None,
    ) -> RunRecord:
        return self.update(
            run_id,
            status="ready",
            stage="ready",
            plan=plan,
            risk=risk,
            sessions=sessions or {},
            owner_pid=None,
            lease_role=None,
            lease_expires_at=None,
            error=None,
        )

    def heartbeat(self, run_id: str, role: str, lease_seconds: int) -> RunRecord:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE runs
                SET lease_expires_at = ?, updated_at = ?
                WHERE run_id = ?
                  AND lease_role = ?
                  AND owner_pid = ?
                  AND status IN ('planning', 'running', 'waiting_checks')
                """,
                (
                    _lease_deadline(lease_seconds),
                    _now(),
                    run_id,
                    role,
                    os.getpid(),
                ),
            )
        if cursor.rowcount != 1:
            raise RuntimeError(f"run {run_id} is not leased by this {role}")
        return self.get(run_id)

    def reconcile_expired(self) -> list[RunRecord]:
        """Recover abandoned planning leases and quarantine abandoned development."""
        reconciled: list[str] = []
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT * FROM runs
                WHERE lease_expires_at IS NOT NULL
                  AND lease_expires_at < ?
                  AND status IN ('planning', 'running', 'waiting_checks')
                """,
                (now,),
            ).fetchall()
            for row in rows:
                current = self._record(row)
                if current.status == "planning":
                    status, stage, error = "queued", "queued", "planner lease expired"
                else:
                    status, stage = "needs_human", current.stage
                    error = f"{current.lease_role or 'worker'} lease expired"
                connection.execute(
                    """
                    UPDATE runs
                    SET status = ?, stage = ?, owner_pid = NULL, lease_role = NULL,
                        lease_expires_at = NULL, error = ?, updated_at = ?
                    WHERE run_id = ?
                    """,
                    (status, stage, error, now, current.run_id),
                )
                self._insert_event(
                    connection,
                    run_id=current.run_id,
                    event_type="lease_reconciled",
                    stage=stage,
                    status=status,
                    message=error,
                    data={"previous_status": current.status},
                )
                reconciled.append(current.run_id)
        return [self.get(run_id) for run_id in reconciled]

    def reconcile_dead_owners(self) -> list[RunRecord]:
        """Immediately release active runs whose recorded owner process is gone."""
        recovered: list[str] = []
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT * FROM runs
                WHERE owner_pid IS NOT NULL
                  AND status IN ('planning', 'running', 'waiting_checks')
                ORDER BY updated_at
                """
            ).fetchall()
            for row in rows:
                current = self._record(row)
                if _pid_alive(current.owner_pid):
                    continue
                if current.status == "planning":
                    status, stage = "queued", "queued"
                    message = f"planner owner process {current.owner_pid} exited; requeued"
                else:
                    status, stage = "ready", current.stage
                    message = (
                        f"{current.lease_role or 'worker'} owner process "
                        f"{current.owner_pid} exited; released for checkpoint resume"
                    )
                connection.execute(
                    """
                    UPDATE runs
                    SET status = ?, stage = ?, owner_pid = NULL, lease_role = NULL,
                        lease_expires_at = NULL, error = NULL, updated_at = ?
                    WHERE run_id = ?
                    """,
                    (status, stage, now, current.run_id),
                )
                self._insert_event(
                    connection,
                    run_id=current.run_id,
                    event_type="dead_owner_recovered",
                    stage=stage,
                    status=status,
                    message=message,
                    data={
                        "previous_status": current.status,
                        "previous_owner_pid": current.owner_pid,
                        "resume_stage": stage,
                    },
                )
                recovered.append(current.run_id)
        return [self.get(run_id) for run_id in recovered]

    def get(self, run_id: str) -> RunRecord:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        if row is None:
            raise RuntimeError(f"run not found: {run_id}")
        return self._record(row)

    def list(self, limit: int = 20) -> list[RunRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM runs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._record(row) for row in rows]

    def active_issue_numbers(self, repo: str) -> set[int]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT issue_number FROM runs
                WHERE repo = ?
                  AND status IN ('queued', 'planning', 'ready', 'running', 'waiting_checks')
                """,
                (repo,),
            ).fetchall()
        return {int(row["issue_number"]) for row in rows}

    def scheduler_queue_state(self, repo: str) -> dict[str, str]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT issue_number, status FROM runs
                WHERE repo = ?
                  AND status IN ('queued', 'planning', 'ready', 'running', 'waiting_checks')
                ORDER BY issue_number
                """,
                (repo,),
            ).fetchall()
        return {str(row["issue_number"]): str(row["status"]) for row in rows}

    def get_scheduler_snapshot(self, repo: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT candidate_state, queue_state, policy_state, decision_state,
                       agent_evaluated_at, updated_at
                FROM scheduler_snapshots WHERE repo = ?
                """,
                (repo,),
            ).fetchone()
        if row is None:
            return None
        return {
            "candidate_state": json.loads(row["candidate_state"] or "{}"),
            "queue_state": json.loads(row["queue_state"] or "{}"),
            "policy_state": json.loads(row["policy_state"] or "{}"),
            "decision_state": json.loads(row["decision_state"] or "{}"),
            "agent_evaluated_at": row["agent_evaluated_at"],
            "updated_at": row["updated_at"],
        }

    def save_scheduler_snapshot(
        self,
        repo: str,
        *,
        candidate_state: dict,
        queue_state: dict,
        policy_state: dict | None = None,
        decision_state: dict | None = None,
        agent_evaluated: bool = False,
    ):
        agent_evaluated_at = _now() if agent_evaluated else None
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO scheduler_snapshots (
                    repo, candidate_state, queue_state, policy_state, decision_state,
                    agent_evaluated_at, updated_at
                )
                VALUES (?, ?, ?, ?, COALESCE(?, '{}'), ?, ?)
                ON CONFLICT(repo) DO UPDATE SET
                    candidate_state = excluded.candidate_state,
                    queue_state = excluded.queue_state,
                    policy_state = excluded.policy_state,
                    decision_state = COALESCE(?, scheduler_snapshots.decision_state),
                    agent_evaluated_at = COALESCE(
                        excluded.agent_evaluated_at,
                        scheduler_snapshots.agent_evaluated_at
                    ),
                    updated_at = excluded.updated_at
                """,
                (
                    repo,
                    json.dumps(candidate_state, sort_keys=True),
                    json.dumps(queue_state, sort_keys=True),
                    json.dumps(policy_state or {}, sort_keys=True),
                    (
                        json.dumps(decision_state, sort_keys=True)
                        if decision_state is not None else None
                    ),
                    agent_evaluated_at,
                    _now(),
                    (
                        json.dumps(decision_state, sort_keys=True)
                        if decision_state is not None else None
                    ),
                ),
            )

    def list_scheduler_queued(self, repo: str) -> list[RunRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT runs.* FROM runs
                WHERE repo = ? AND status = 'queued'
                  AND EXISTS (
                    SELECT 1 FROM run_events
                    WHERE run_events.run_id = runs.run_id
                      AND run_events.event_type = 'scheduler_enqueued'
                  )
                ORDER BY created_at
                """,
                (repo,),
            ).fetchall()
        return [self._record(row) for row in rows]

    def release_scheduler_queued(
        self,
        run_id: str,
        *,
        action: str,
        reason: str,
    ) -> bool:
        if action not in {"defer", "reject"}:
            raise ValueError(f"unsupported scheduler queue action: {action}")
        status = "cancelled" if action == "defer" else "skipped"
        event_type = "scheduler_queue_deferred" if action == "defer" else "scheduler_queue_rejected"
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT runs.* FROM runs
                WHERE run_id = ? AND status = 'queued'
                  AND EXISTS (
                    SELECT 1 FROM run_events
                    WHERE run_events.run_id = runs.run_id
                      AND run_events.event_type = 'scheduler_enqueued'
                  )
                """,
                (run_id,),
            ).fetchone()
            if row is None:
                return False
            connection.execute(
                """
                UPDATE runs
                SET status = ?, stage = ?, error = ?, updated_at = ?
                WHERE run_id = ? AND status = 'queued'
                """,
                (status, status, reason, _now(), run_id),
            )
            self._insert_event(
                connection,
                run_id=run_id,
                event_type=event_type,
                stage=status,
                status=status,
                message=reason,
                data={"action": action, "previous_status": "queued"},
            )
        return True

    def find_completed_issue(
        self, repo: str, issue_number: int, *, exclude_run_id: str | None = None
    ) -> RunRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM runs
                WHERE repo = ? AND issue_number = ? AND status = 'completed'
                  AND (? IS NULL OR run_id != ?)
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (repo, issue_number, exclude_run_id, exclude_run_id),
            ).fetchone()
        return self._record(row) if row else None

    def list_submission_recovery_candidates(self) -> list[RunRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM runs
                WHERE status IN ('failed', 'needs_human')
                  AND stage = 'submitting'
                  AND worktree_path IS NOT NULL
                  AND branch IS NOT NULL
                ORDER BY updated_at
                """
            ).fetchall()
        return [self._record(row) for row in rows]

    def list_pr_recovery_candidates(self) -> list[RunRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM runs
                WHERE status IN ('ready', 'failed', 'needs_human')
                  AND pr_url IS NOT NULL
                ORDER BY updated_at
                """
            ).fetchall()
        return [self._record(row) for row in rows]

    def update(self, run_id: str, **fields) -> RunRecord:
        allowed = {
            "status", "stage", "worktree_path", "branch", "pr_url",
            "risk", "plan", "sessions", "error",
            "touched_files",
            "owner_pid",
            "lease_role", "lease_expires_at",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"unsupported run fields: {', '.join(sorted(unknown))}")
        if "sessions" in fields:
            fields["sessions"] = json.dumps(fields["sessions"], sort_keys=True)
        if "plan" in fields:
            fields["plan"] = json.dumps(fields["plan"], sort_keys=True)
        if "touched_files" in fields:
            fields["touched_files"] = json.dumps(sorted(set(fields["touched_files"])))
        if fields.get("status") in FINAL_STATUSES:
            fields["owner_pid"] = None
            fields["lease_role"] = None
            fields["lease_expires_at"] = None
        fields["updated_at"] = _now()
        assignments = ", ".join(f"{key} = ?" for key in fields)
        values = [*fields.values(), run_id]
        with self._connect() as connection:
            previous = connection.execute(
                "SELECT * FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if previous is None:
                raise RuntimeError(f"run not found: {run_id}")
            connection.execute(
                f"UPDATE runs SET {assignments} WHERE run_id = ?", values
            )
            status = fields.get("status", previous["status"])
            stage = fields.get("stage", previous["stage"])
            visible_fields = {
                key: value
                for key, value in fields.items()
                if key not in {"sessions", "updated_at"}
            }
            if visible_fields:
                event_type = "run_updated"
                if "stage" in fields and fields["stage"] != previous["stage"]:
                    event_type = "stage_changed"
                if "status" in fields and fields["status"] != previous["status"]:
                    event_type = "status_changed"
                self._insert_event(
                    connection,
                    run_id=run_id,
                    event_type=event_type,
                    stage=stage,
                    status=status,
                    message=None,
                    data=visible_fields,
                )
        return self.get(run_id)

    def claim_files(self, run_id: str, files: list[str]) -> RunRecord:
        requested = set(files)
        if not requested:
            return self.get(run_id)
        record = self.get(run_id)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT run_id, touched_files FROM runs
                WHERE repo = ? AND run_id != ?
                  AND status IN ('running', 'waiting_checks')
                """,
                (record.repo, run_id),
            ).fetchall()
            conflicts: dict[str, list[str]] = {}
            requested_resources = _module_resources(requested)
            for row in rows:
                other_files = set(json.loads(row["touched_files"] or "[]"))
                file_overlap = requested & other_files
                module_overlap = requested_resources & _module_resources(other_files)
                if file_overlap or module_overlap:
                    details = [
                        *(f"file:{path}" for path in sorted(file_overlap)),
                        *(f"module:{path}" for path in sorted(module_overlap)),
                    ]
                    conflicts[row["run_id"]] = details
            if conflicts:
                details = "; ".join(
                    f"{other}: {', '.join(paths)}"
                    for other, paths in conflicts.items()
                )
                raise RuntimeError(f"file lock conflict with active run(s): {details}")
            connection.execute(
                "UPDATE runs SET touched_files = ?, updated_at = ? WHERE run_id = ?",
                (json.dumps(sorted(requested)), _now(), run_id),
            )
            self._insert_event(
                connection,
                run_id=run_id,
                event_type="files_claimed",
                stage=record.stage,
                status=record.status,
                message=f"Claimed {len(requested)} changed file(s)",
                data={"files": sorted(requested)},
            )
        return self.get(run_id)

    def resume(self, run_id: str, max_parallel: int) -> RunRecord:
        record = self.get(run_id)
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                current = self._record(
                    connection.execute(
                        "SELECT * FROM runs WHERE run_id = ?", (run_id,)
                    ).fetchone()
                )
                if current.status in {"completed", "cancelled", "skipped"}:
                    raise RuntimeError(
                        f"run {run_id} cannot be resumed from status {current.status}"
                    )
                if (
                    current.owner_pid
                    and current.owner_pid != os.getpid()
                    and _pid_alive(current.owner_pid)
                ):
                    raise RuntimeError(
                        f"run {run_id} is still owned by active process "
                        f"{current.owner_pid}"
                    )
                active = connection.execute(
                    """
                    SELECT COUNT(*) FROM runs
                    WHERE status IN ('running', 'waiting_checks') AND run_id != ?
                    """,
                    (run_id,),
                ).fetchone()[0]
                if active >= max_parallel:
                    raise RuntimeError(
                        f"parallel run limit reached ({active}/{max_parallel})"
                    )
                connection.execute(
                    """
                    UPDATE runs SET status = 'running', owner_pid = ?, lease_role = 'worker',
                        lease_expires_at = ?, error = NULL, updated_at = ?
                    WHERE run_id = ?
                    """,
                    (os.getpid(), _lease_deadline(21600), _now(), run_id),
                )
                self._insert_event(
                    connection,
                    run_id=run_id,
                    event_type="run_resumed",
                    stage=current.stage,
                    status="running",
                    message="Resumed run",
                    data={"owner_pid": os.getpid(), "previous_status": current.status},
                )
        except sqlite3.IntegrityError as exc:
            raise RuntimeError(
                f"issue #{record.issue_number} already has another active run"
            ) from exc
        return self.get(run_id)

    def cancel(self, run_id: str) -> RunRecord:
        self.get(run_id)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self._record(
                connection.execute(
                    "SELECT * FROM runs WHERE run_id = ?", (run_id,)
                ).fetchone()
            )
            if current.status == "completed":
                raise RuntimeError(f"run {run_id} is already completed")
            if current.owner_pid and _pid_alive(current.owner_pid):
                raise RuntimeError(
                    f"run {run_id} is still owned by active process "
                    f"{current.owner_pid}"
                )
            connection.execute(
                """
                UPDATE runs
                SET status = 'cancelled', stage = 'cancelled', owner_pid = NULL,
                    lease_role = NULL, lease_expires_at = NULL,
                    error = 'cancelled by user', updated_at = ?
                WHERE run_id = ?
                """,
                (_now(), run_id),
            )
            self._insert_event(
                connection,
                run_id=run_id,
                event_type="run_cancelled",
                stage="cancelled",
                status="cancelled",
                message="Cancelled run",
                data={"previous_status": current.status},
            )
        return self.get(run_id)
