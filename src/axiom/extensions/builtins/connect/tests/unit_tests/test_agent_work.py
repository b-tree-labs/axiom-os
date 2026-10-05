# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""C2 — agents run long work as principal-owned Tasks."""

from __future__ import annotations

import time

import pytest

from axiom.extensions.builtins.connect.agent_work import list_work, spawn_work, work_status


@pytest.fixture
def runner(tmp_path):
    from axiom.infra.tasks.runner import TaskRunner
    from axiom.infra.tasks.store import TaskStore

    return TaskRunner(TaskStore(base_dir=tmp_path / "tasks"))


def _wait_done(task_id, runner, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        st = work_status(task_id, runner=runner)
        if st["status"] in ("done", "failed", "cancelled"):
            return st
        time.sleep(0.05)
    return work_status(task_id, runner=runner)


def test_spawn_runs_and_completes_with_progress(runner, tmp_path):
    t = spawn_work("@axi:bens", "echo-job", ["sh", "-c", "echo twin-calc-output"],
                   cwd=tmp_path, runner=runner)
    assert t.spawner_principal == "@axi:bens"
    st = _wait_done(t.task_id, runner)
    assert st["status"] == "done"
    assert "twin-calc-output" in st["tail"]  # tail() is progress/output


def test_list_work_is_scoped_to_principal(runner, tmp_path):
    spawn_work("@axi:bens", "mine", ["true"], cwd=tmp_path, runner=runner)
    spawn_work("@axi:alice", "hers", ["true"], cwd=tmp_path, runner=runner)
    mine = list_work("@axi:bens", runner=runner)
    assert [w["name"] for w in mine] == ["mine"]


def test_anonymous_principal_rejected(runner, tmp_path):
    with pytest.raises(ValueError):
        spawn_work("", "x", ["true"], cwd=tmp_path, runner=runner)
