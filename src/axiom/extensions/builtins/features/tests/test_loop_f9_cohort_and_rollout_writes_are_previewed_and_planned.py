# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""LOOP-F9: a cohort or rollout write returns what would change and who would be affected,
applies only through the plan a person approved, and is audited with its before and after."""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.flags import cli
from axiom.extensions.builtins.features.tests.conftest import mcp, svc
from axiom.infra import audit_trail
from axiom.infra.orchestrator.approval import ApprovalGate
from axiom.infra.orchestrator.approval_store import FileActionStore


def _gate(state):
    return ApprovalGate(FileActionStore(state / "orchestrator" / "approvals.json"))


def _population(loop):
    s = svc()
    for user in ("ana", "bob", "cy", "dee"):
        s.set_override("user", user, "fast_search", "off", by="@t")
    loop["targeting"].put_cohort("listed", principals=["ana", "bob"], by="@t")


@pytest.mark.acceptance("LOOP-F9")
def test_a_cohort_preview_names_the_change_and_its_members_and_writes_nothing(loop):
    _population(loop)
    out = mcp("axiom_flags__cohort_preview",
              {"key": "pilots", "principals": ["ana", "cy"], "description": "first users"}, loop["state"])
    assert out["ok"], out
    v = out["value"]
    assert v["applied"] is False and v["before"] is None and v["after"]["principals"] == ["ana", "cy"]
    assert v["members"]["count"] == 2 and set(v["members"]["sample"]) == {"ana", "cy"}
    assert v["population"]["size"] >= 4 and v["population"]["source"]
    assert loop["targeting"].cohort("pilots") is None


@pytest.mark.acceptance("LOOP-F9")
def test_a_rollout_preview_names_who_changes(loop):
    _population(loop)
    out = mcp("axiom_flags__rollout_preview",
              {"key": "beta_editor", "rules": [{"cohort": "listed", "value": "on"}]}, loop["state"])
    assert out["ok"], out
    v = out["value"]
    assert v["applied"] is False and v["environment"] == "development"
    assert v["changes"]["count"] == 2 and set(v["changes"]["sample"]) == {"ana", "bob"}
    assert v["per_rule"] == [{"rule_index": 0, "admitted": 2}]
    assert loop["targeting"].rollout("beta_editor", "development") is None


@pytest.mark.acceptance("LOOP-F9")
def test_a_rollout_write_applies_only_through_an_approved_plan(loop):
    state = loop["state"]
    params = {"key": "beta_editor", "rules": [{"value": "on", "percentage": 10}], "reason": "start 10%"}
    first = mcp("axiom_flags__rollout_set", params, state)
    assert not first["ok"] and loop["targeting"].rollout("beta_editor", "development") is None
    plan_hash = first["value"]["plan_hash"]
    # A changed call cannot ride the plan.
    swapped = mcp("axiom_flags__rollout_set", {**params, "rules": [{"value": "on"}], "plan_hash": plan_hash}, state)
    assert not swapped["ok"]
    _gate(state).approve(first["value"]["held_action_id"], decided_by="@person:test")
    applied = mcp("axiom_flags__rollout_set", {**params, "plan_hash": plan_hash}, state)
    assert applied["ok"], applied
    assert loop["targeting"].rollout("beta_editor", "development")["rules"] == [{"value": "on", "percentage": 10}]
    assert applied["value"]["before"] is None and applied["value"]["after"]["rules"][0]["percentage"] == 10
    assert not mcp("axiom_flags__rollout_set", {**params, "plan_hash": plan_hash}, state)["ok"]  # spent


@pytest.mark.acceptance("LOOP-F9")
def test_a_cohort_write_applies_only_through_an_approved_plan(loop):
    state = loop["state"]
    params = {"key": "pilots", "principals": ["ana"], "reason": "first pilot"}
    first = mcp("axiom_flags__cohort_set", params, state)
    assert not first["ok"] and loop["targeting"].cohort("pilots") is None
    _gate(state).approve(first["value"]["held_action_id"], decided_by="@person:test")
    applied = mcp("axiom_flags__cohort_set", {**params, "plan_hash": first["value"]["plan_hash"]}, state)
    assert applied["ok"], applied
    assert loop["targeting"].cohort("pilots")["principals"] == ["ana"]


@pytest.mark.acceptance("LOOP-F9")
def test_every_write_is_audited_with_before_and_after(loop, capsys):
    state = loop["state"]
    assert cli.main(["cohort", "set", "pilots", "--principal", "ana", "--reason", "pilot", "--apply"]) == 0
    assert cli.main(["cohort", "set", "pilots", "--principal", "ana", "--principal", "bob",
                     "--reason", "more", "--apply"]) == 0
    assert cli.main(["rollout", "set", "beta_editor", "--rules", '[{"cohort": "pilots", "value": "on"}]',
                     "--reason", "go", "--apply"]) == 0
    assert cli.main(["kill", "beta_editor", "--reason", "incident"]) == 0
    recs = [r for r in audit_trail.read_chain(audit_trail.audit_path(state))
            if r["skill"] in ("flags.cohort_set", "flags.rollout_set", "flags.kill")]
    changes = [r["change"] for r in recs]
    assert changes[0]["kind"] == "cohort" and changes[0]["old"] is None
    assert changes[0]["new"]["principals"] == ["ana"]
    assert changes[1]["old"]["principals"] == ["ana"] and changes[1]["new"]["principals"] == ["ana", "bob"]
    assert changes[2]["kind"] == "rollout" and changes[2]["subject"] == "beta_editor (development)"
    assert changes[2]["new"]["rules"] == [{"cohort": "pilots", "value": "on"}]
    assert changes[3] == {"kind": "kill-switch", "subject": "beta_editor (development)", "old": "off", "new": "on"}
    assert audit_trail.verify_chain(audit_trail.audit_path(state))


@pytest.mark.acceptance("LOOP-F9")
def test_at_the_command_line_set_previews_until_apply(loop, capsys):
    assert cli.main(["cohort", "set", "pilots", "--principal", "ana", "--reason", "r"]) == 0
    out = capsys.readouterr().out
    assert "preview" in out and "--apply" in out
    assert loop["targeting"].cohort("pilots") is None


@pytest.mark.acceptance("LOOP-F9")
def test_a_write_needs_a_reason(loop):
    out = mcp("axiom_flags__rollout_preview", {"key": "beta_editor", "rules": [{"value": "on"}]}, loop["state"])
    assert out["ok"]
    assert cli.main(["rollout", "set", "beta_editor", "--rules", '[{"value": "on"}]', "--apply"]) != 0
    assert loop["targeting"].rollout("beta_editor", "development") is None


@pytest.mark.acceptance("LOOP-F9")
def test_a_cohort_in_use_cannot_be_deleted(loop, capsys):
    loop["targeting"].put_cohort("pilots", principals=["ana"], by="@t")
    loop["targeting"].put_rollout("beta_editor", "development", rules=[{"cohort": "pilots", "value": "on"}],
                                  reason="r", by="@t")
    assert cli.main(["cohort", "delete", "pilots", "--reason", "done", "--apply"]) != 0
    assert "beta_editor (development)" in capsys.readouterr().err
    assert loop["targeting"].cohort("pilots") is not None
