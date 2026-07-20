# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Axi's orchestration tools — the no-hardcode seam the agent-mode loop calls
from natural language (no keyword routing)."""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.chat.tools_ext import agent_ops
from axiom.extensions.builtins.connect.agent_work import spawn_work


@pytest.fixture
def runner(tmp_path):
    from axiom.infra.tasks.runner import TaskRunner
    from axiom.infra.tasks.store import TaskStore

    return TaskRunner(TaskStore(base_dir=tmp_path / "tasks"))


def _ctx(runner, requester="@ben:example-org"):
    agent_ops.set_op_context(principal="@axi:bens", requester=requester, runner=runner)


def test_tools_are_discoverable_with_schemas():
    names = {t.name for t in agent_ops.TOOLS}
    assert {"agent_task_status", "agent_stop_task", "verify_prediction", "delegate_to_agent"} <= names


def test_status_lists_owner_tasks_with_progress(runner, tmp_path):
    _ctx(runner)
    spawn_work("@axi:bens", "longjob", ["sh", "-c", "echo step1; sleep 30"], cwd=tmp_path, runner=runner)
    out = agent_ops.execute("agent_task_status", {})
    assert out["tasks"] and out["tasks"][0]["name"] == "longjob"


def test_stop_cancels_and_is_ownership_scoped(runner, tmp_path):
    _ctx(runner)
    t = spawn_work("@axi:bens", "long", ["sleep", "30"], cwd=tmp_path, runner=runner)
    out = agent_ops.execute("agent_stop_task", {"task_id": t.task_id})
    assert out["status"] == "cancelled"


def test_verify_prediction_tolerance():
    _ctx(None)
    assert agent_ops.execute("verify_prediction", {"measured": 2.2, "predicted": 2.1, "tolerance": 0.3})["within_tolerance"] is True
    assert agent_ops.execute("verify_prediction", {"measured": 3.0, "predicted": 2.1, "tolerance": 0.3})["within_tolerance"] is False


def test_delegate_resolves_sme_agent():
    _ctx(None)
    out = agent_ops.execute("delegate_to_agent", {"agent": "tidy", "request": "prune the journal"})
    assert out["delegated_to"] == "TIDY"
    bad = agent_ops.execute("delegate_to_agent", {"agent": "tidey", "request": "x"})
    assert "error" in bad and "tidy" in bad["did_you_mean"]


def test_llm_cannot_assert_requester_via_params(runner, tmp_path):
    # requester comes from ambient context, never from tool params — so a model
    # can't claim to be the owner to get past the ownership gate. Owner is @ben;
    # the requester is someone else, and the param 'requester' is ignored.
    agent_ops.set_op_context(principal="@axi:bens", owner="@ben:example-org",
                             requester="@mallory:elsewhere", runner=runner)
    t = spawn_work("@axi:bens", "long", ["sleep", "30"], cwd=tmp_path, runner=runner)
    out = agent_ops.execute("agent_stop_task", {"task_id": t.task_id, "requester": "@ben:example-org"})
    assert "error" in out and "not authorized" in out["error"]  # param requester ignored
