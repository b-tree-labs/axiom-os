# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Agents that *do* work: long-running tasks owned by an agent principal
(ADR-074, C2).

A presence isn't just a chatbot — it runs work (in Expman, the long digital-twin
calculations). This is the thin seam an agent (in ``agent`` mode) uses to spawn,
inspect, and list that work, built on the existing ``infra/tasks`` substrate
(`TaskStore`/`TaskRunner`: detached subprocess, status, `tail` = progress).
Every task carries the agent's ``spawner_principal`` so the owner can later
query/stop/redirect it from any channel (C3) and federation peers can scope by
principal.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

_DEFAULT_RUNNER: Any | None = None


def _runner(runner: Any | None = None) -> Any:
    """Shared in-process TaskRunner (keeps the live-process handle map) unless a
    runner is injected (tests)."""
    global _DEFAULT_RUNNER
    if runner is not None:
        return runner
    if _DEFAULT_RUNNER is None:
        from axiom.infra.tasks.runner import TaskRunner
        from axiom.infra.tasks.store import TaskStore

        _DEFAULT_RUNNER = TaskRunner(TaskStore())
    return _DEFAULT_RUNNER


def spawn_work(
    principal: str,
    name: str,
    command: list[str],
    *,
    cwd: Path | str | None = None,
    runner: Any | None = None,
) -> Any:
    """Spawn a long-running task owned by ``principal`` (e.g. a twin calc)."""
    r = _runner(runner)
    return r.spawn(
        name=name,
        command=list(command),
        cwd=Path(cwd or Path.cwd()),
        principal=principal,
    )


def work_status(task_id: str, *, runner: Any | None = None, tail_lines: int = 20) -> dict:
    """Reconciled status + recent output (progress) for one task."""
    r = _runner(runner)
    t = r.status(task_id)  # reconciles running→done/failed against the real proc
    return {
        "task_id": t.task_id,
        "name": t.name,
        "status": t.status,
        "exit_code": t.exit_code,
        "principal": t.spawner_principal,
        "tail": r.tail(task_id, tail_lines),
    }


def list_work(principal: str, *, runner: Any | None = None, reconcile: bool = True) -> list[dict]:
    """The owner's tasks (most recent first), statuses reconciled."""
    r = _runner(runner)
    out: list[dict] = []
    for t in r.store.list():
        if t.spawner_principal != principal:
            continue
        if reconcile and t.status == "running":
            t = r.status(t.task_id)
        out.append({"task_id": t.task_id, "name": t.name, "status": t.status})
    return out


__all__ = ["spawn_work", "work_status", "list_work"]
