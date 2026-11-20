# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""LOOP-F23: who must approve a change to a cohort, rollout or stage plan is configurable
per environment and per flag; production can require two people. The approver's roles come
from the platform's one permission model (the directory's groups mapped to roles)."""

from __future__ import annotations

import json

import pytest

from axiom.extensions.builtins.features import approvals
from axiom.extensions.builtins.features.tests.conftest import mcp, run

PROD = {"environment": "production"}
CHANGE = {"key": "beta_editor", "rules": [{"value": "on"}], "reason": "launch", **PROD}


def _approve(modes, change_id, actor):
    return run("flags.approve", {"id": change_id, "reason": "looked at it"}, modes["state"], actor=actor)


@pytest.mark.acceptance("LOOP-F23")
def test_production_can_require_a_second_person(modes):
    approvals.set_environment_policies({"production": {"two_person": True}})
    first = run("flags.rollout_set", CHANGE, modes["state"], actor="@ana")
    assert not first.ok and first.unmet and first.unmet[0]["id"] == "approvals"
    change_id = first.value["change"]["id"]
    assert modes["targeting"].rollout("beta_editor", "production") is None

    own = _approve(modes, change_id, "@ana")
    assert not own.ok and "author" in own.errors[0]
    assert _approve(modes, change_id, "@bob").ok

    applied = run("flags.rollout_set", CHANGE, modes["state"], actor="@ana")
    assert applied.ok, applied.errors
    assert modes["targeting"].rollout("beta_editor", "production")["rules"] == [{"value": "on"}]
    listed = run("flags.changes", {}, modes["state"]).value["changes"]
    assert listed[0]["status"] == "applied" and [a["by"] for a in listed[0]["approvals"]] == ["@bob"]

    # An approval is for one change, once.
    again = run("flags.rollout_set", CHANGE, modes["state"], actor="@ana")
    assert not again.ok and again.value["change"]["id"] != change_id


@pytest.mark.acceptance("LOOP-F23")
def test_the_policy_is_per_environment(modes):
    approvals.set_environment_policies({"production": {"approvals": 1}})
    assert run("flags.rollout_set", {**CHANGE, "environment": "staging"}, modes["state"]).ok
    assert not run("flags.rollout_set", CHANGE, modes["state"]).ok


@pytest.mark.acceptance("LOOP-F23")
def test_a_flag_declares_who_approves_it_and_roles_come_from_the_permission_model(modes, tmp_path, monkeypatch):
    directory = tmp_path / "directory.json"
    directory.write_text(json.dumps({"groups": {"g-release": {"members": ["@cy"]}}}))
    role_map = tmp_path / "roles.json"
    role_map.write_text(json.dumps({"rules": [{"group": "g-release", "role": "release_manager"}]}))
    monkeypatch.setenv("AXIOM_DIRECTORY_PROVIDER", "local")
    monkeypatch.setenv("AXIOM_DIRECTORY_LOCAL_FILE", str(directory))
    monkeypatch.setenv("AXIOM_DIRECTORY_ROLE_MAP", str(role_map))

    params = {"key": "prod_guard", "rules": [{"value": "on"}], "reason": "r", **PROD}
    first = run("flags.rollout_set", params, modes["state"], actor="@ana")
    assert not first.ok and "release_manager" in first.unmet[0]["why"]
    change_id = first.value["change"]["id"]
    refused = _approve(modes, change_id, "@bob")
    assert not refused.ok and "release_manager" in refused.errors[0]
    assert _approve(modes, change_id, "@cy").ok
    assert run("flags.rollout_set", params, modes["state"], actor="@ana").ok
    # The flag's own policy says production only; elsewhere nobody else is asked.
    assert run("flags.rollout_set", {**params, "environment": "staging"}, modes["state"]).ok


@pytest.mark.acceptance("LOOP-F23")
def test_an_ai_client_or_an_agent_cannot_approve(modes):
    approvals.set_environment_policies({"production": {"approvals": 1}})
    change_id = run("flags.rollout_set", CHANGE, modes["state"]).value["change"]["id"]
    assert "axiom_features__approve" not in _mcp_tools(modes)
    out = _approve(modes, change_id, "@rivet")
    assert not out.ok and "person" in out.errors[0]


def _mcp_tools(modes):
    from axiom.extensions.builtins.features.skills import bind
    from axiom.extensions.builtins.mcp.skill_tools import skill_tool_contribution
    from axiom.infra.skills import SkillRegistry

    reg = SkillRegistry()
    bind(reg)
    return set(skill_tool_contribution(reg, ctx_factory=lambda: None).dispatch)


@pytest.mark.acceptance("LOOP-F23")
def test_cohort_and_stage_plan_changes_are_held_by_the_strictest_policy_that_reads_them(modes):
    approvals.set_environment_policies({"production": {"approvals": 1}})
    ts = modes["targeting"]
    ts.put_cohort("pilots", principals=["ana"], by="@t")
    ts.put_rollout("beta_editor", "production", rules=[{"cohort": "pilots", "value": "on"}], reason="r", by="@t")
    out = run("flags.cohort_set", {"key": "pilots", "principals": ["ana", "bob"], "reason": "more"},
              modes["state"])
    assert not out.ok and out.unmet[0]["id"] == "approvals" and "production" in out.unmet[0]["why"]
    plan = {"stages": [{"percentage": 10}], "soak": "1h", "value": "on", "gates": [], "on_fail": "halt"}
    out = run("flags.stage_plan_set", {"key": "beta_editor", "plan": plan, "reason": "r", **PROD},
              modes["state"])
    assert not out.ok and out.unmet[0]["id"] == "approvals"
    # A cohort nothing in production reads is not held by production's policy.
    assert run("flags.cohort_set", {"key": "loose", "principals": ["x"], "reason": "r"}, modes["state"]).ok


@pytest.mark.acceptance("LOOP-F23")
def test_the_environment_policy_is_a_setting(modes, monkeypatch):
    approvals.set_environment_policies(None)
    values = {"features.approvals.production.approvals": 2, "features.approvals.production.roles": ["site-admin"]}
    monkeypatch.setattr(approvals, "_setting", lambda key: values.get(key))
    policy = approvals.environment_policy("production")
    assert policy.approvals == 2 and policy.roles == ("site-admin",)
    assert approvals.environment_policy("staging").approvals == 0


@pytest.mark.acceptance("LOOP-F23")
def test_the_kill_switch_is_never_held_for_approval(modes):
    approvals.set_environment_policies({"production": {"approvals": 2}})
    out = mcp("axiom_flags__kill", {"key": "beta_editor", "reason": "incident", **PROD}, modes["state"])
    assert out["ok"], out
