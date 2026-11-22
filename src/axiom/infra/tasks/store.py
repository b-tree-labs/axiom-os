# Copyright (c) 2026 B-Tree Ventures, LLC
# SPDX-License-Identifier: Apache-2.0

"""Persistent task store, on Postgres (ADR-174).

The rows live in the ``tasks`` schema. A task's output is a log file on the machine that ran
it, so the store keeps ``base_dir`` for those files and records which machine (``node_id``) owns
each row: a process id, a working directory and a log path mean nothing on another host.
"""

from __future__ import annotations

import socket
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from sqlalchemy import delete, select

from axiom.infra.schema_seam import SchemaSeam
from axiom.infra.tasks.models import TaskRow

TaskStatus = Literal["pending", "running", "done", "failed", "cancelled"]
_TERMINAL_STATUSES: frozenset[str] = frozenset({"done", "failed", "cancelled"})

#: The session seam for the ``tasks`` schema. Tests bind SQLite through ``seam.set_provider``.
seam = SchemaSeam("tasks", what="the background task store")


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
    node_id: str = ""

    @classmethod
    def from_row(cls, row: TaskRow) -> Task:
        return cls(
            task_id=row.task_id,
            name=row.name,
            command=list(row.command or []),
            cwd=Path(row.cwd),
            spawner_principal=row.spawner_principal,
            status=row.status,  # type: ignore[arg-type]
            output_path=Path(row.output_path),
            pid=row.pid,
            started_at=row.started_at,
            ended_at=row.ended_at,
            exit_code=row.exit_code,
            created_at=row.created_at or "",
            node_id=row.node_id,
        )


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


class TaskStore:
    """Persistent task store. Output logs default to ``$AXI_STATE_DIR/tasks/output``.

    Federation-aware in the data model: every task carries a Matrix-style
    ``spawner_principal``. Empty principals are rejected at create time so
    peer-introspection can rely on the field once that CLI lands.
    """

    def __init__(self, base_dir: Path | None = None, *, node_id: str | None = None):
        if base_dir is None:
            from axiom.infra.paths import get_user_state_dir

            base_dir = get_user_state_dir() / "tasks"
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        (self.base_dir / "output").mkdir(exist_ok=True)
        self.node_id = node_id or socket.gethostname()
        seam.ensure_provisioned()

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
            node_id=self.node_id,
        )
        with seam.session_scope() as s:
            s.add(
                TaskRow(
                    task_id=task.task_id,
                    node_id=task.node_id,
                    name=task.name,
                    command=task.command,
                    cwd=str(task.cwd),
                    spawner_principal=task.spawner_principal,
                    status=task.status,
                    output_path=str(task.output_path),
                    pid=task.pid,
                    started_at=task.started_at,
                    ended_at=task.ended_at,
                    exit_code=task.exit_code,
                    created_at=task.created_at,
                )
            )
            s.commit()
        return task

    def get(self, task_id: str) -> Task | None:
        with seam.session_scope() as s:
            row = s.get(TaskRow, task_id)
            return Task.from_row(row) if row is not None else None

    def update(self, task_id: str, **fields) -> Task:
        with seam.session_scope() as s:
            row = s.get(TaskRow, task_id)
            if row is None:
                raise KeyError(f"unknown task_id: {task_id}")
            existing = Task.from_row(row)
            # Auto-stamp ended_at on first transition into a terminal status.
            if (
                "status" in fields
                and fields["status"] in _TERMINAL_STATUSES
                and not existing.ended_at
                and "ended_at" not in fields
            ):
                fields["ended_at"] = _now_iso()
            updated = replace(existing, **fields)
            row.name = updated.name
            row.command = list(updated.command)
            row.cwd = str(updated.cwd)
            row.spawner_principal = updated.spawner_principal
            row.status = updated.status
            row.output_path = str(updated.output_path)
            row.pid = updated.pid
            row.started_at = updated.started_at
            row.ended_at = updated.ended_at
            row.exit_code = updated.exit_code
            s.commit()
            return updated

    def list(self, status: str | None = None, *, all_nodes: bool = False) -> list[Task]:
        """Tasks, newest first. By default only this machine's: another host's pid and log path
        are meaningless here."""
        stmt = select(TaskRow).order_by(TaskRow.created_at.desc())
        if not all_nodes:
            stmt = stmt.where(TaskRow.node_id == self.node_id)
        if status:
            stmt = stmt.where(TaskRow.status == status)
        with seam.session_scope() as s:
            return [Task.from_row(r) for r in s.execute(stmt).scalars()]

    def clear(self) -> int:
        """Remove this machine's done/failed/cancelled tasks. Returns count removed."""
        with seam.session_scope() as s:
            result = s.execute(
                delete(TaskRow).where(
                    TaskRow.node_id == self.node_id,
                    TaskRow.status.in_(("done", "failed", "cancelled")),
                )
            )
            s.commit()
            return int(result.rowcount or 0)


__all__ = ["Task", "TaskStatus", "TaskStore", "seam"]
