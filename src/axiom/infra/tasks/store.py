# Copyright (c) 2026 B-Tree Ventures, LLC
# SPDX-License-Identifier: Apache-2.0

"""SQLite-backed persistent task store."""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

TaskStatus = Literal["pending", "running", "done", "failed", "cancelled"]
_TERMINAL_STATUSES: frozenset[str] = frozenset({"done", "failed", "cancelled"})


@dataclass(frozen=True)
class Task:
    task_id: str
    name: str
    command: list[str]
    cwd: Path
    spawner_principal: str
    status: TaskStatus
    output_path: Path
    pid: int | None = None
    started_at: str | None = None
    ended_at: str | None = None
    exit_code: int | None = None
    created_at: str = ""

    def to_row(self) -> tuple:
        return (
            self.task_id,
            self.name,
            "\x00".join(self.command),
            str(self.cwd),
            self.spawner_principal,
            self.status,
            str(self.output_path),
            self.pid,
            self.started_at,
            self.ended_at,
            self.exit_code,
            self.created_at,
        )

    @classmethod
    def from_row(cls, row: tuple) -> Task:
        (
            task_id,
            name,
            command,
            cwd,
            principal,
            status,
            output_path,
            pid,
            started_at,
            ended_at,
            exit_code,
            created_at,
        ) = row
        return cls(
            task_id=task_id,
            name=name,
            command=command.split("\x00") if command else [],
            cwd=Path(cwd),
            spawner_principal=principal,
            status=status,
            output_path=Path(output_path),
            pid=pid,
            started_at=started_at,
            ended_at=ended_at,
            exit_code=exit_code,
            created_at=created_at or "",
        )


_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id           TEXT PRIMARY KEY,
    name              TEXT NOT NULL,
    command           TEXT NOT NULL,
    cwd               TEXT NOT NULL,
    spawner_principal TEXT NOT NULL,
    status            TEXT NOT NULL,
    output_path       TEXT NOT NULL,
    pid               INTEGER,
    started_at        TEXT,
    ended_at          TEXT,
    exit_code         INTEGER,
    created_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_status      ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_created     ON tasks(created_at);
CREATE INDEX IF NOT EXISTS idx_tasks_principal   ON tasks(spawner_principal);
"""


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


class TaskStore:
    """Persistent task store. Default location: ``$AXI_STATE_DIR/tasks/``.

    Federation-aware in the data model — every task carries a Matrix-style
    ``spawner_principal``. Empty principals are rejected at create time so
    peer-introspection can rely on the field once that CLI lands.
    """

    def __init__(self, base_dir: Path | None = None):
        if base_dir is None:
            from axiom.infra.paths import get_user_state_dir

            base_dir = get_user_state_dir() / "tasks"
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        (self.base_dir / "output").mkdir(exist_ok=True)
        self._db_path = self.base_dir / "tasks.db"
        with self._conn() as c:
            c.executescript(_SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self._db_path)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=ON")
        return c

    def create(
        self,
        *,
        name: str,
        command: list[str],
        cwd: Path,
        principal: str,
    ) -> Task:
        if not principal:
            raise ValueError(
                "principal is required (Matrix-style @name:context); "
                "anonymous tasks would break federation peer-query"
            )
        task_id = uuid.uuid4().hex[:12]
        output_path = self.base_dir / "output" / f"{task_id}.log"
        # Touch the file so tail() works even before the runner writes anything.
        output_path.touch()
        task = Task(
            task_id=task_id,
            name=name,
            command=list(command),
            cwd=Path(cwd),
            spawner_principal=principal,
            status="pending",
            output_path=output_path,
            created_at=_now_iso(),
        )
        with self._conn() as c:
            c.execute(
                "INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                task.to_row(),
            )
        return task

    def get(self, task_id: str) -> Task | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
        return Task.from_row(row) if row else None

    def update(self, task_id: str, **fields) -> Task:
        existing = self.get(task_id)
        if existing is None:
            raise KeyError(f"unknown task_id: {task_id}")
        # Auto-stamp ended_at on first transition into a terminal status.
        if (
            "status" in fields
            and fields["status"] in _TERMINAL_STATUSES
            and not existing.ended_at
            and "ended_at" not in fields
        ):
            fields["ended_at"] = _now_iso()
        updated = replace(existing, **fields)
        with self._conn() as c:
            c.execute(
                "UPDATE tasks SET name=?, command=?, cwd=?, spawner_principal=?, "
                "status=?, output_path=?, pid=?, started_at=?, ended_at=?, "
                "exit_code=? WHERE task_id=?",
                (
                    updated.name,
                    "\x00".join(updated.command),
                    str(updated.cwd),
                    updated.spawner_principal,
                    updated.status,
                    str(updated.output_path),
                    updated.pid,
                    updated.started_at,
                    updated.ended_at,
                    updated.exit_code,
                    task_id,
                ),
            )
        return updated

    def list(self, status: str | None = None) -> list[Task]:
        sql = "SELECT * FROM tasks"
        params: tuple = ()
        if status:
            sql += " WHERE status = ?"
            params = (status,)
        sql += " ORDER BY created_at DESC"
        with self._conn() as c:
            rows = c.execute(sql, params).fetchall()
        return [Task.from_row(r) for r in rows]

    def clear(self) -> int:
        """Remove all done/failed/cancelled tasks. Returns count removed."""
        with self._conn() as c:
            cur = c.execute(
                "DELETE FROM tasks WHERE status IN ('done', 'failed', 'cancelled')"
            )
            return cur.rowcount


__all__ = ["Task", "TaskStatus", "TaskStore"]
