# Copyright (c) 2026 B-Tree Ventures, LLC
# SPDX-License-Identifier: Apache-2.0

"""Subprocess-backed task runner.

Spawns each task as a detached subprocess with stdout+stderr redirected
to a per-task file under ``$AXI_STATE_DIR/tasks/output/<task_id>.log``.
Status checks consult ``os.kill(pid, 0)`` to detect crashed/orphaned
PIDs so a CLI restart does not leave tasks stuck in ``running`` forever.
"""

from __future__ import annotations

import os
import signal
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from .store import Task, TaskStore


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _pid_alive(pid: int | None) -> bool:
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but is owned by another user — counts as alive.
        return True


class TaskRunner:
    """Spawn / status / cancel / tail subprocesses tracked in a TaskStore.

    The store is the single source of truth — a fresh ``TaskRunner``
    constructed against the same store sees all prior tasks. This is
    what gives us the "tasks survive CLI restart" guarantee.

    For tasks this runner spawned, we keep the ``Popen`` handle in
    ``_procs`` so ``poll()`` can reap zombies and give the real exit
    code. For tasks spawned by a previous CLI invocation (no handle),
    we fall back to ``os.kill(pid, 0)`` — exit code is unknown but the
    status transitions correctly.
    """

    def __init__(self, store: TaskStore):
        self.store = store
        self._procs: dict[str, subprocess.Popen] = {}

    def spawn(
        self,
        *,
        name: str,
        command: list[str],
        cwd: Path,
        principal: str,
    ) -> Task:
        task = self.store.create(
            name=name, command=command, cwd=cwd, principal=principal,
        )
        # Open the output file for the subprocess. Append-mode in case
        # the file already exists (it does — store.create touched it).
        out_fh = open(task.output_path, "ab", buffering=0)
        try:
            proc = subprocess.Popen(
                command,
                cwd=str(cwd),
                stdout=out_fh,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                # Detach into a new process group so CLI exit doesn't
                # propagate SIGHUP to the task.
                start_new_session=True,
            )
        except Exception:
            out_fh.close()
            self.store.update(
                task.task_id,
                status="failed",
                ended_at=_now_iso(),
                exit_code=-1,
            )
            raise
        finally:
            out_fh.close()
        self._procs[task.task_id] = proc
        return self.store.update(
            task.task_id,
            status="running",
            pid=proc.pid,
            started_at=_now_iso(),
        )

    def status(self, task_id: str) -> Task:
        """Reconcile stored status with reality."""
        task = self.store.get(task_id)
        if task is None:
            raise KeyError(f"unknown task_id: {task_id}")
        if task.status != "running":
            return task

        # Preferred path: we have the Popen handle from this runner's
        # spawn(). poll() reaps the zombie and yields the real exit code.
        proc = self._procs.get(task_id)
        if proc is not None:
            ret = proc.poll()
            if ret is None:
                return task  # still running
            new_status = "done" if ret == 0 else "failed"
            updated = self.store.update(task_id, status=new_status, exit_code=ret)
            self._procs.pop(task_id, None)
            return updated

        # Fallback: cross-CLI-restart path. Best-effort — exit code unknown.
        if _pid_alive(task.pid):
            return task
        return self.store.update(task_id, status="done", exit_code=None)

    def cancel(self, task_id: str) -> Task:
        task = self.store.get(task_id)
        if task is None:
            raise KeyError(f"unknown task_id: {task_id}")
        if task.status != "running":
            return task
        if task.pid is not None and _pid_alive(task.pid):
            try:
                # Kill the process group (we set start_new_session=True).
                os.killpg(os.getpgid(task.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
        # Wait briefly for the signal to take effect; SIGKILL if it doesn't.
        proc = self._procs.get(task_id)
        if proc is not None:
            try:
                proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(task.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError, AttributeError):
                    pass
                try:
                    proc.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    pass
            self._procs.pop(task_id, None)
        return self.store.update(task_id, status="cancelled")

    def tail(self, task_id: str, n: int = 50) -> str:
        task = self.store.get(task_id)
        if task is None:
            raise KeyError(f"unknown task_id: {task_id}")
        if not task.output_path.exists():
            return ""
        # Naive tail — fine for typical task output sizes. Big-output
        # streaming via seek-from-end is a future optimization.
        try:
            text = task.output_path.read_text(errors="replace")
        except OSError:
            return ""
        lines = text.splitlines()
        return "\n".join(lines[-n:])


__all__ = ["TaskRunner"]
