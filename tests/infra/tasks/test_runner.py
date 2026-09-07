# Copyright (c) 2026 B-Tree Ventures, LLC
# SPDX-License-Identifier: Apache-2.0

"""Tests for TaskRunner subprocess management."""

from __future__ import annotations

import os
import time

import pytest


@pytest.fixture
def store(tmp_path):
    from axiom.infra.tasks.store import TaskStore

    return TaskStore(base_dir=tmp_path / "tasks")


@pytest.fixture
def runner(store):
    from axiom.infra.tasks.runner import TaskRunner

    return TaskRunner(store)


def _wait_for_status(runner, task_id, target_statuses, timeout=5.0):
    """Poll runner.status() until it reaches one of target_statuses."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        t = runner.status(task_id)
        if t.status in target_statuses:
            return t
        time.sleep(0.05)
    raise AssertionError(
        f"task {task_id} did not reach {target_statuses} within {timeout}s "
        f"(last status: {t.status})"
    )


def test_spawn_returns_running_task(runner, tmp_path):
    t = runner.spawn(
        name="quick-true",
        command=["true"],
        cwd=tmp_path,
        principal="@ben:laptop",
    )
    assert t.status == "running"
    assert t.pid is not None
    assert t.started_at is not None


def test_status_detects_completed_process(runner, tmp_path):
    t = runner.spawn(
        name="quick", command=["true"], cwd=tmp_path, principal="@a:b",
    )
    final = _wait_for_status(runner, t.task_id, {"done", "failed"})
    assert final.status == "done"
    assert final.exit_code == 0
    assert final.ended_at is not None


def test_status_records_nonzero_exit_as_failed(runner, tmp_path):
    t = runner.spawn(
        name="boom", command=["false"], cwd=tmp_path, principal="@a:b",
    )
    final = _wait_for_status(runner, t.task_id, {"done", "failed"})
    assert final.status == "failed"
    assert final.exit_code != 0


def test_subprocess_writes_stdout_to_output_file(runner, tmp_path):
    t = runner.spawn(
        name="echoer",
        command=["sh", "-c", "echo hello && echo world"],
        cwd=tmp_path,
        principal="@a:b",
    )
    final = _wait_for_status(runner, t.task_id, {"done"})
    output = final.output_path.read_text()
    assert "hello" in output
    assert "world" in output


def test_tail_returns_recent_output(runner, tmp_path):
    t = runner.spawn(
        name="counter",
        command=["sh", "-c", "for i in 1 2 3 4 5; do echo line$i; done"],
        cwd=tmp_path,
        principal="@a:b",
    )
    _wait_for_status(runner, t.task_id, {"done"})
    tail = runner.tail(t.task_id, n=3)
    lines = tail.strip().splitlines()
    # Last 3 lines.
    assert lines == ["line3", "line4", "line5"]


def test_cancel_terminates_running_task(runner, tmp_path):
    t = runner.spawn(
        name="long",
        command=["sleep", "30"],
        cwd=tmp_path,
        principal="@a:b",
    )
    assert t.status == "running"
    cancelled = runner.cancel(t.task_id)
    assert cancelled.status == "cancelled"
    # Process actually died.
    final = _wait_for_status(runner, t.task_id, {"cancelled", "failed"})
    assert final.status == "cancelled"
    # PID should not be alive.
    if final.pid is not None:
        try:
            os.kill(final.pid, 0)
            still_alive = True
        except ProcessLookupError:
            still_alive = False
        assert not still_alive


def test_cancel_no_op_on_terminal_task(runner, tmp_path):
    t = runner.spawn(
        name="quick", command=["true"], cwd=tmp_path, principal="@a:b",
    )
    _wait_for_status(runner, t.task_id, {"done"})
    # Cancel after the task already finished — should be a no-op, not raise.
    result = runner.cancel(t.task_id)
    assert result.status == "done"


def test_status_checks_dead_pid_for_orphaned_task(runner, store, tmp_path):
    """Regression: if the CLI was killed and the recorded pid no longer
    exists, status() should detect that and mark the task failed (not
    leave it stuck at 'running' forever).
    """
    # Create a task that finishes immediately. Mutate the store record
    # to simulate a crashed CLI that left status='running' behind.
    t = runner.spawn(
        name="ghost", command=["true"], cwd=tmp_path, principal="@a:b",
    )
    _wait_for_status(runner, t.task_id, {"done"})
    # Now simulate orphan state
    store.update(t.task_id, status="running", ended_at=None, exit_code=None)

    refreshed = runner.status(t.task_id)
    assert refreshed.status in {"done", "failed"}, (
        f"runner.status should detect dead pid; got {refreshed.status}"
    )


def test_persistent_runner_sees_other_runner_tasks(store, tmp_path):
    """Two TaskRunner instances against the same store see each other's
    spawned tasks — the cross-process / cross-CLI-restart guarantee."""
    from axiom.infra.tasks.runner import TaskRunner

    r1 = TaskRunner(store)
    r2 = TaskRunner(store)
    t = r1.spawn(name="x", command=["true"], cwd=tmp_path, principal="@a:b")
    seen_by_r2 = r2.status(t.task_id)
    assert seen_by_r2.task_id == t.task_id
