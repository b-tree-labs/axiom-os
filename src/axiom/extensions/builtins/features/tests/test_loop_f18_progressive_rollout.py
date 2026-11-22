# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""LOOP-F18: a rollout may be a plan of stages (percent steps or rings of cohorts), each
with a minimum soak and health gates. The platform scheduler advances a stage only when
its gates hold, and halts or rolls back when one fails. Every automatic advance or halt is
audited and raises one notification."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from axiom.extensions.builtins.features import progressive
from axiom.extensions.builtins.features import targeting as t
from axiom.extensions.builtins.features.tests.conftest import mcp, run, svc
from axiom.extensions.builtins.notifications import incidents
from axiom.extensions.builtins.notifications.incidents.model import Cause
from axiom.extensions.builtins.notifications.incidents.stage import OPEN_STATES
from axiom.infra import audit_trail
from axiom.infra.skills import SkillRegistry, SkillResult, SkillSpec

PERCENT_PLAN = {"stages": [{"percentage": 1}, {"percentage": 5}, {"percentage": 25}, {"percentage": 100}],
                "soak": "1h", "value": "on", "gates": [{"kind": "incidents"}], "on_fail": "halt"}


def _set(modes, plan, key="beta_editor", **extra):
    out = run("flags.stage_plan_set", {"key": key, "plan": plan, "reason": "staged", **extra}, modes["state"])
    assert out.ok, out.errors
    return out


def _tick(modes, after: timedelta):
    out = run("flags.rollout_tick", {"now": (datetime.now(UTC) + after).isoformat()}, modes["state"],
              actor="@pulse")
    assert out.ok, out.errors
    return out.value


def _stage(modes, key="beta_editor"):
    return modes["targeting"].stage_plan(key, "development")


def _audited(modes, skill):
    return [r for r in audit_trail.read_chain(audit_trail.audit_path(modes["state"])) if r["skill"] == skill]


def _incident(subject="flag:beta_editor"):
    incidents.default_stage().record(
        Cause(area="app", kind="errors", action="app.request", subject=subject, error_class="ServerError"),
        summary="errors are up")


@pytest.mark.acceptance("LOOP-F18")
def test_a_percent_plan_advances_only_after_its_soak_while_gates_hold(modes):
    _set(modes, PERCENT_PLAN)
    assert _stage(modes)["stage"] == 0 and _stage(modes)["status"] == "running"
    s = svc()
    users = [f"user-{i}" for i in range(400)]
    assert {u for u in users if s.resolve_flag("beta_editor", user=u) == "on"} == {
        u for u in users if t.bucket("beta_editor", u) < 1}

    assert _tick(modes, timedelta(minutes=30))["advanced"] == []
    assert _stage(modes)["stage"] == 0
    assert _tick(modes, timedelta(minutes=61))["advanced"] == ["beta_editor (development)"]
    assert _stage(modes)["stage"] == 1
    assert {u for u in users if s.resolve_flag("beta_editor", user=u) == "on"} == {
        u for u in users if t.bucket("beta_editor", u) < 5}
    out = s.explain("beta_editor", user=users[0])
    assert out["stage"]["index"] == 1 and out["stage"]["of"] == 4 and out["stage"]["status"] == "running"

    # One notification and one audit record for the one advance.
    assert len(modes["inbox"].all()) == 1
    recs = _audited(modes, "features.stage_advance")
    assert len(recs) == 1 and recs[0]["change"]["old"]["stage"] == 0 and recs[0]["change"]["new"]["stage"] == 1


@pytest.mark.acceptance("LOOP-F18")
def test_a_plan_reaches_its_last_stage_and_is_done(modes):
    _set(modes, {**PERCENT_PLAN, "soak": "1m"})
    for i in range(1, 6):
        _tick(modes, timedelta(minutes=2 * i))
    assert _stage(modes)["status"] == "done" and _stage(modes)["stage"] == 3
    assert svc().resolve_flag("beta_editor", user="anyone") == "on"


@pytest.mark.acceptance("LOOP-F18")
def test_a_ring_plan_widens_cohort_by_cohort(modes):
    ts = modes["targeting"]
    ts.put_cohort("internal", principals=["ana"], by="@t")
    ts.put_cohort("canary", principals=["bob"], by="@t")
    _set(modes, {"stages": [{"cohort": "internal"}, {"cohort": "canary"}, {"everyone": True}],
                 "soak": "1h", "value": "on", "gates": [], "on_fail": "halt"})
    s = svc()
    assert [s.resolve_flag("beta_editor", user=u) for u in ("ana", "bob", "cy")] == ["on", "off", "off"]
    _tick(modes, timedelta(hours=2))
    assert [s.resolve_flag("beta_editor", user=u) for u in ("ana", "bob", "cy")] == ["on", "on", "off"]
    _tick(modes, timedelta(hours=4))
    assert [s.resolve_flag("beta_editor", user=u) for u in ("ana", "bob", "cy")] == ["on", "on", "on"]


@pytest.mark.acceptance("LOOP-F18")
def test_a_failing_gate_halts_raises_one_incident_and_holds_there(modes):
    _set(modes, PERCENT_PLAN)
    _incident()
    for minutes in (5, 70, 140):
        value = _tick(modes, timedelta(minutes=minutes))
    assert _stage(modes)["status"] == "halted" and _stage(modes)["stage"] == 0
    assert "incidents" in _stage(modes)["halted_reason"]
    assert value["halted"] == []  # halted once, not again on every tick
    halts = [i for i in modes["incidents"].in_states(OPEN_STATES) if i.kind == "rollout_halted"]
    assert len(halts) == 1 and halts[0].occurrences == 1
    assert len(_audited(modes, "features.stage_halt")) == 1


@pytest.mark.acceptance("LOOP-F18")
def test_a_plan_that_rolls_back_on_failure_returns_the_flag_to_its_default(modes):
    _set(modes, {**PERCENT_PLAN, "stages": [{"percentage": 100}], "on_fail": "rollback"})
    assert svc().resolve_flag("beta_editor", user="ana") == "on"
    _incident()
    _tick(modes, timedelta(minutes=1))
    assert _stage(modes)["status"] == "rolled_back"
    out = svc().explain("beta_editor", user="ana")
    assert out["value"] == "off" and out["decided_by"] == "default"


def _metric_registry(value):
    reg = SkillRegistry()
    reg.register_skill(SkillSpec(name="probe.error_rate", fn=lambda p, c: SkillResult(value={"rate": value}),
                                 description="error rate", inputs={}, side_effects=False, idempotent=True,
                                 surfaces=("cli",), risk="read"))
    reg.register_skill(SkillSpec(name="probe.reset", fn=lambda p, c: SkillResult(value={}),
                                 description="a write", inputs={}, side_effects=True, idempotent=True,
                                 surfaces=("cli",), risk="reversible-write"))
    reg.register_skill(SkillSpec(name="probe.broken", fn=lambda p, c: SkillResult(ok=False, errors=["down"]),
                                 description="broken", inputs={}, side_effects=False, idempotent=True,
                                 surfaces=("cli",), risk="read"))
    return reg


@pytest.mark.acceptance("LOOP-F18")
def test_a_metric_gate_reads_a_declared_read_and_halts_above_its_threshold(modes):
    progressive.set_skill_registry(_metric_registry(0.2))
    gate = {"kind": "metric", "capability": "probe.error_rate", "path": "rate", "op": "lte", "value": 0.05}
    _set(modes, {**PERCENT_PLAN, "gates": [gate]})
    _tick(modes, timedelta(minutes=61))
    assert _stage(modes)["status"] == "halted" and "probe.error_rate" in _stage(modes)["halted_reason"]


@pytest.mark.acceptance("LOOP-F18")
def test_a_metric_that_cannot_be_read_fails_its_gate(modes):
    progressive.set_skill_registry(_metric_registry(0.0))
    gate = {"kind": "metric", "capability": "probe.broken", "path": "rate", "op": "lte", "value": 0.05}
    _set(modes, {**PERCENT_PLAN, "gates": [gate]})
    _tick(modes, timedelta(minutes=61))
    assert _stage(modes)["status"] == "halted" and "could not" in _stage(modes)["halted_reason"]


@pytest.mark.acceptance("LOOP-F18")
def test_a_gate_must_be_a_read_and_feedback_waits_on_a_source(modes):
    progressive.set_skill_registry(_metric_registry(0.0))
    out = run("flags.stage_plan_set", {"key": "beta_editor", "reason": "r", "plan": {**PERCENT_PLAN, "gates": [
        {"kind": "metric", "capability": "probe.reset", "path": "x", "op": "lte", "value": 1}]}}, modes["state"])
    assert not out.ok and "read" in out.errors[0]
    out = run("flags.stage_plan_set", {"key": "beta_editor", "reason": "r",
                                          "plan": {**PERCENT_PLAN, "gates": [{"kind": "feedback"}]}}, modes["state"])
    assert not out.ok and "feedback" in out.errors[0]
    progressive.register_gate_kind("feedback", lambda gate, key, env, now: (True, "no bad reports"))
    _set(modes, {**PERCENT_PLAN, "gates": [{"kind": "feedback"}]})
    _tick(modes, timedelta(minutes=61))
    assert _stage(modes)["stage"] == 1


@pytest.mark.acceptance("LOOP-F18")
def test_a_plan_is_previewed_planned_from_an_ai_client_and_halted_fast(modes):
    out = run("flags.stage_plan_preview", {"key": "beta_editor", "plan": PERCENT_PLAN,
                                              "subjects": [f"u{i}" for i in range(200)]}, modes["state"])
    assert out.ok and [s["percentage"] for s in out.value["stages"]] == [1, 5, 25, 100]
    assert out.value["stages"][-1]["admitted"] == out.value["population"]["size"]
    assert _stage(modes) is None
    held = mcp("axiom_flags__stage_plan_set", {"key": "beta_editor", "plan": PERCENT_PLAN, "reason": "r"},
               modes["state"])
    assert not held["ok"] and held["value"]["plan_hash"]
    _set(modes, PERCENT_PLAN)
    out = mcp("axiom_flags__stage_plan_halt", {"key": "beta_editor", "reason": "looks wrong"}, modes["state"])
    assert out["ok"], out
    assert _stage(modes)["status"] == "halted" and "looks wrong" in _stage(modes)["halted_reason"]
