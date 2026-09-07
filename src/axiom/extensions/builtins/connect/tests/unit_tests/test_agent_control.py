# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""C3 — phone control plane: status / stop / redirect, ownership-gated."""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.connect.agent_control import (
    InterruptInbox,
    NotAuthorized,
    redirect_work,
    status_for,
    stop_work,
)
from axiom.extensions.builtins.connect.agent_work import spawn_work, work_status
from axiom.memory.ownership import Right, delegate, new_ownership

OWNER = "@ben:example-org"
STRANGER = "@mallory:elsewhere"


@pytest.fixture
def runner(tmp_path):
    from axiom.infra.tasks.runner import TaskRunner
    from axiom.infra.tasks.store import TaskStore

    return TaskRunner(TaskStore(base_dir=tmp_path / "tasks"))


@pytest.fixture
def ownership():
    return new_ownership(OWNER)


def test_status_lists_owner_work(runner, ownership, tmp_path):
    spawn_work("@axi:bens", "burnup-calc", ["true"], cwd=tmp_path, runner=runner)
    out = status_for("@axi:bens", OWNER, ownership=ownership, runner=runner)
    assert out["tasks"] and out["tasks"][0]["name"] == "burnup-calc"


def test_owner_can_stop_running_task(runner, ownership, tmp_path):
    t = spawn_work("@axi:bens", "long", ["sleep", "30"], cwd=tmp_path, runner=runner)
    res = stop_work(t.task_id, OWNER, ownership=ownership, runner=runner)
    assert res["status"] == "cancelled"
    # confirm it really stopped
    assert work_status(t.task_id, runner=runner)["status"] == "cancelled"


def test_stranger_cannot_stop(runner, ownership, tmp_path):
    t = spawn_work("@axi:bens", "long", ["sleep", "30"], cwd=tmp_path, runner=runner)
    with pytest.raises(NotAuthorized):
        stop_work(t.task_id, STRANGER, ownership=ownership, runner=runner)


def test_redirect_delivers_instruction_via_inbox(ownership):
    inbox = InterruptInbox()
    res = redirect_work("run-1", OWNER, "rerun at 2x histories", ownership=ownership, inbox=inbox)
    assert res["instruction"] == "rerun at 2x histories"
    drained = inbox.drain("run-1")
    assert drained and drained[0].kind == "redirect" and "2x" in drained[0].payload
    assert inbox.drain("run-1") == []  # drained


def test_delegate_with_effort_can_redirect_but_not_stop(ownership):
    # grant a colleague EFFORT (direct cycles) but not CONTROL
    deleg = delegate(ownership, STRANGER, {Right.EFFORT},
                     expires_at="2999-01-01T00:00:00+00:00")
    inbox = InterruptInbox()
    # EFFORT → redirect allowed
    redirect_work("run-9", STRANGER, "focus on tail latency", ownership=deleg, inbox=inbox)
    assert inbox.peek("run-9")
    # but no CONTROL → stop refused
    with pytest.raises(NotAuthorized):
        stop_work("t-1", STRANGER, ownership=deleg)


def test_stranger_cannot_view_status(runner, ownership):
    with pytest.raises(NotAuthorized):
        status_for("@axi:bens", STRANGER, ownership=ownership, runner=runner)
