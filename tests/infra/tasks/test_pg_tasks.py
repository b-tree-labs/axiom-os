# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The task store against a real Postgres (ADR-174): the migration and the node scoping."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from axiom.infra.tasks.store import TaskStore, seam


@pytest.fixture
def pg(real_postgres):
    seam.reset_provider()
    yield real_postgres
    seam.reset_provider()


def _node() -> str:
    """A node id no other test shares: the session database persists between tests."""
    return f"node-{uuid.uuid4().hex[:8]}"


def test_the_migration_builds_the_task_table_in_the_tasks_schema_never_public(pg):
    TaskStore(base_dir=Path("/tmp/axiom-pg-tasks-a"))
    with create_engine(pg).connect() as c:
        rows = c.execute(
            text("select table_schema from information_schema.tables where table_name = 'task'")
        ).fetchall()
    schemas = {r[0] for r in rows}
    from axiom.infra.db import normalize_extension_name

    expected = normalize_extension_name("tasks")  # carries the per-worker test suffix, if any
    assert expected in schemas and "public" not in schemas, schemas


def test_roundtrip_stamps_the_end_time_and_keeps_the_command_exactly(pg, tmp_path):
    store = TaskStore(base_dir=tmp_path, node_id=_node())
    task = store.create(
        name="build",
        command=["echo", "with space", "and\ttab"],
        cwd=tmp_path,
        principal="@ben:home",
    )
    assert store.get(task.task_id).command == ["echo", "with space", "and\ttab"]
    done = store.update(task.task_id, status="done", exit_code=0)
    assert done.ended_at and store.get(task.task_id).status == "done"
    with pytest.raises(KeyError):
        store.update("nope", status="done")


def test_a_node_sees_only_its_own_tasks_unless_it_asks_for_all(pg, tmp_path):
    a = TaskStore(base_dir=tmp_path / "a", node_id=_node())
    b = TaskStore(base_dir=tmp_path / "b", node_id=_node())
    ta = a.create(name="a", command=["x"], cwd=tmp_path, principal="@ben:home")
    tb = b.create(name="b", command=["y"], cwd=tmp_path, principal="@ben:home")
    assert [t.task_id for t in a.list()] == [ta.task_id]
    assert {ta.task_id, tb.task_id} <= {
        t.task_id for t in a.list(all_nodes=True)
    }  # the session database holds other tests' rows too
    # clear() is also this node's only: it must not delete another host's finished tasks.
    a.update(ta.task_id, status="done")
    b.update(tb.task_id, status="done")
    assert a.clear() == 1
    assert b.get(tb.task_id) is not None


def test_an_empty_principal_is_still_refused(pg, tmp_path):
    with pytest.raises(ValueError, match="principal is required"):
        TaskStore(base_dir=tmp_path, node_id="n").create(
            name="x", command=["x"], cwd=tmp_path, principal=""
        )
