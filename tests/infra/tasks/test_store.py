# Copyright (c) 2026 B-Tree Ventures, LLC
# SPDX-License-Identifier: Apache-2.0

"""Tests for the persistent task store.

Closes the parity-doc gap 'Background tasks (runs while user works
elsewhere)' — and goes beyond Claude Code's session-bound version by
persisting tasks across CLI restarts in a SQLite store under
``$AXI_STATE_DIR/tasks/``. Federation-aware in the data model
(`spawner_principal` is a Matrix-style ``@name:context``); peer-query
CLI is a future increment.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def store(tmp_path):
    from axiom.infra.tasks.store import TaskStore

    return TaskStore(base_dir=tmp_path / "tasks")


def test_create_returns_task_with_generated_id(store):
    t = store.create(
        name="long-eigendecomp",
        command=["sleep", "30"],
        cwd=Path("/tmp"),
        principal="@ben:laptop",
    )
    assert t.task_id  # auto-generated
    assert len(t.task_id) >= 8  # at least 8 hex chars
    assert t.name == "long-eigendecomp"
    assert t.status == "pending"
    assert t.spawner_principal == "@ben:laptop"


def test_create_persists_to_disk(store, tmp_path):
    """Restart the store and the task is still there."""
    from axiom.infra.tasks.store import TaskStore

    t = store.create(
        name="t1", command=["true"], cwd=Path("/tmp"), principal="@ben:laptop",
    )
    saved_id = t.task_id

    # Fresh store instance — same backing dir
    store2 = TaskStore(base_dir=tmp_path / "tasks")
    found = store2.get(saved_id)
    assert found is not None
    assert found.name == "t1"
    assert found.spawner_principal == "@ben:laptop"


def test_get_unknown_returns_none(store):
    assert store.get("does-not-exist") is None


def test_update_changes_fields(store):
    t = store.create(
        name="t", command=["true"], cwd=Path("/tmp"), principal="@a:b",
    )
    updated = store.update(t.task_id, status="running", pid=12345)
    assert updated.status == "running"
    assert updated.pid == 12345
    # Roundtrip via get
    fresh = store.get(t.task_id)
    assert fresh.status == "running"
    assert fresh.pid == 12345


def test_update_unknown_raises(store):
    with pytest.raises(KeyError):
        store.update("unknown", status="done")


def test_list_returns_all(store):
    for i in range(3):
        store.create(
            name=f"t{i}", command=["true"], cwd=Path("/tmp"), principal="@a:b",
        )
    assert len(store.list()) == 3


def test_list_filters_by_status(store):
    a = store.create(name="a", command=["true"], cwd=Path("/tmp"), principal="@p:c")
    b = store.create(name="b", command=["true"], cwd=Path("/tmp"), principal="@p:c")
    store.update(a.task_id, status="running")
    store.update(b.task_id, status="done")

    running = store.list(status="running")
    assert len(running) == 1
    assert running[0].name == "a"

    done = store.list(status="done")
    assert len(done) == 1
    assert done[0].name == "b"


def test_list_orders_by_created_at_desc(store):
    """Newest first — matches the user expectation when reading /tasks."""
    import time

    first = store.create(name="oldest", command=["true"], cwd=Path("/tmp"), principal="@a:b")
    time.sleep(0.01)
    second = store.create(name="newest", command=["true"], cwd=Path("/tmp"), principal="@a:b")

    listed = store.list()
    assert listed[0].task_id == second.task_id
    assert listed[1].task_id == first.task_id


def test_clear_removes_terminal_tasks(store):
    """clear() drops done/failed/cancelled but keeps running/pending."""
    a = store.create(name="a", command=["true"], cwd=Path("/tmp"), principal="@p:c")
    b = store.create(name="b", command=["true"], cwd=Path("/tmp"), principal="@p:c")
    c = store.create(name="c", command=["true"], cwd=Path("/tmp"), principal="@p:c")
    store.update(a.task_id, status="done")
    store.update(b.task_id, status="failed")
    store.update(c.task_id, status="running")

    n = store.clear()
    assert n == 2  # a + b removed
    remaining = store.list()
    assert len(remaining) == 1
    assert remaining[0].task_id == c.task_id


def test_output_path_is_set_on_create(store):
    """Each task has a designated output file path under base_dir."""
    t = store.create(
        name="t", command=["true"], cwd=Path("/tmp"), principal="@a:b",
    )
    assert t.output_path is not None
    assert isinstance(t.output_path, Path)
    # Parent dir exists so the runner can write immediately.
    assert t.output_path.parent.exists()


def test_principal_is_required(store):
    """Federation-aware from day 1: spawner_principal is a hard requirement.
    No anonymous tasks (would break peer-introspection later)."""
    with pytest.raises((ValueError, TypeError)):
        store.create(
            name="t", command=["true"], cwd=Path("/tmp"), principal="",
        )
