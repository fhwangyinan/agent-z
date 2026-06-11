from __future__ import annotations

import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


ACTIVE_STATUSES = {"running", "waiting_checks"}
FINAL_STATUSES = {"completed", "failed", "skipped", "needs_human", "cancelled"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
    sessions: dict[str, str]
    touched_files: list[str]
    owner_pid: int | None
    error: str | None
    created_at: str
    updated_at: str


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
                    sessions TEXT NOT NULL DEFAULT '{}',
                    touched_files TEXT NOT NULL DEFAULT '[]',
                    owner_pid INTEGER,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute("DROP INDEX IF EXISTS active_issue_lock")
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS active_issue_lock
                ON runs(repo, issue_number)
                WHERE status IN ('queued', 'running', 'waiting_checks')
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

    @staticmethod
    def _record(row: sqlite3.Row) -> RunRecord:
        data = dict(row)
        data["sessions"] = json.loads(data["sessions"] or "{}")
        data["touched_files"] = json.loads(data["touched_files"] or "[]")
        return RunRecord(**data)

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
                        created_at, updated_at
                    ) VALUES (?, ?, ?, 'running', 'created', '{}', ?, ?, ?)
                    """,
                    (run_id, repo, issue_number, os.getpid(), timestamp, timestamp),
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
        except sqlite3.IntegrityError as exc:
            raise RuntimeError(
                f"issue #{issue_number} already has a queued or active run"
            ) from exc
        return self.get(run_id)

    def claim_next(self, max_parallel: int) -> RunRecord | None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            active = connection.execute(
                "SELECT COUNT(*) FROM runs WHERE status IN ('running', 'waiting_checks')"
            ).fetchone()[0]
            if active >= max_parallel:
                return None
            row = connection.execute(
                """
                SELECT run_id FROM runs
                WHERE status = 'queued'
                ORDER BY created_at
                LIMIT 1
                """
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                "UPDATE runs SET status = 'running', owner_pid = ?, updated_at = ? WHERE run_id = ?",
                (os.getpid(), _now(), row["run_id"]),
            )
            run_id = row["run_id"]
        return self.get(run_id)

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

    def update(self, run_id: str, **fields) -> RunRecord:
        allowed = {
            "status", "stage", "worktree_path", "branch", "pr_url",
            "risk", "sessions", "error",
            "touched_files",
            "owner_pid",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"unsupported run fields: {', '.join(sorted(unknown))}")
        if "sessions" in fields:
            fields["sessions"] = json.dumps(fields["sessions"], sort_keys=True)
        if "touched_files" in fields:
            fields["touched_files"] = json.dumps(sorted(set(fields["touched_files"])))
        if fields.get("status") in FINAL_STATUSES:
            fields["owner_pid"] = None
        fields["updated_at"] = _now()
        assignments = ", ".join(f"{key} = ?" for key in fields)
        values = [*fields.values(), run_id]
        with self._connect() as connection:
            connection.execute(
                f"UPDATE runs SET {assignments} WHERE run_id = ?", values
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
            for row in rows:
                overlap = requested & set(json.loads(row["touched_files"] or "[]"))
                if overlap:
                    conflicts[row["run_id"]] = sorted(overlap)
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
                    UPDATE runs SET status = 'running', owner_pid = ?, error = NULL, updated_at = ?
                    WHERE run_id = ?
                    """,
                    (os.getpid(), _now(), run_id),
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
                    error = 'cancelled by user', updated_at = ?
                WHERE run_id = ?
                """,
                (_now(), run_id),
            )
        return self.get(run_id)
