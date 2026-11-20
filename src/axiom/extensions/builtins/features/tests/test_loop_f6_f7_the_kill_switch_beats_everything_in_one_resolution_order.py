# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""LOOP-F6 (a kill switch per flag and environment beats everything, at once, with a lower
permission than a rollout edit) and LOOP-F7 (one resolution order, reported with every answer)."""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.features import resolver
from axiom.extensions.builtins.features.skills import bind
from axiom.extensions.builtins.features.tests.conftest import mcp, svc, web
from axiom.infra.skills import SkillRegistry

RISK_ORDER = {"read": 0, "reversible-write": 1, "destructive": 2}


@pytest.mark.acceptance("LOOP-F6")
def test_the_kill_switch_beats_user_and_account_overrides_and_rollouts(loop):
    s = svc()
    s.set_override("user", "ana", "beta_editor", "on", by="@t")
    s.set_override("account", "acme", "beta_editor", "on", by="@t")
    loop["targeting"].put_rollout("beta_editor", "development", rules=[{"value": "on"}], reason="r", by="@t")
    loop["targeting"].set_kill_switch("beta_editor", "development", True, reason="incident 12", by="@t")
    out = s.explain("beta_editor", user="ana", account="acme")
    assert out["value"] == "off" and out["decided_by"] == "kill-switch"
    assert out["detail"]["reason"] == "incident 12"
    assert s.resolve(user="ana", account="acme").sources["beta_editor"] == "kill-switch"


@pytest.mark.acceptance("LOOP-F6")
def test_the_kill_switch_takes_effect_on_the_next_request_with_no_cache(loop):
    s = svc()  # one long-lived service, as a server holds it
    loop["targeting"].put_rollout("beta_editor", "development", rules=[{"value": "on"}], reason="r", by="@t")
    assert s.resolve_flag("beta_editor", user="ana") == "on"
    loop["targeting"].set_kill_switch("beta_editor", "development", True, reason="stop", by="@t")
    assert s.resolve_flag("beta_editor", user="ana") == "off"
    loop["targeting"].set_kill_switch("beta_editor", "development", False, reason="", by="@t")
    assert s.resolve_flag("beta_editor", user="ana") == "on"


@pytest.mark.acceptance("LOOP-F6")
def test_the_kill_switch_is_per_environment(loop, monkeypatch):
    loop["targeting"].set_kill_switch("timeline", "production", True, reason="prod only", by="@t")
    svc().set_override("user", "ana", "timeline", "on", by="@t")
    assert svc().resolve_flag("timeline", user="ana") == "on"
    monkeypatch.setenv("AXIOM_ENVIRONMENT", "production")
    assert svc().resolve_flag("timeline", user="ana") == "off"


@pytest.mark.acceptance("LOOP-F6")
def test_setting_the_kill_switch_needs_a_lower_permission_than_editing_a_rollout(loop):
    reg = SkillRegistry()
    bind(reg)
    kill = reg.spec("flags.kill").effective_risk
    for write in ("flags.rollout_set", "flags.cohort_set", "flags.cohort_delete"):
        assert RISK_ORDER[kill] < RISK_ORDER[reg.spec(write).effective_risk]
    # From an AI client: the kill switch applies at once; a rollout edit waits on a person's plan.
    out = mcp("axiom_flags__kill", {"key": "beta_editor", "reason": "incident"}, loop["state"])
    assert out["ok"], out
    assert svc().explain("beta_editor", user="ana")["decided_by"] == "kill-switch"
    out = mcp("axiom_flags__rollout_set", {"key": "beta_editor", "rules": [{"value": "on"}], "reason": "go"},
              loop["state"])
    assert not out["ok"] and out["value"]["plan_hash"]
    assert loop["targeting"].rollout("beta_editor", "development")["rules"] is None


@pytest.mark.acceptance("LOOP-F6")
def test_on_the_web_an_operator_sets_the_kill_switch_and_nobody_else(loop, monkeypatch):
    client = web(monkeypatch, roles=("viewer",))
    assert client.post("/api/v1/features/beta_editor/kill", json={"reason": "x"}).status_code == 403
    client = web(monkeypatch, roles=("site-admin",))
    r = client.post("/api/v1/features/beta_editor/kill", json={"reason": "incident"})
    assert r.status_code == 200, r.text
    assert r.json()["flags"]["beta_editor"] == "off" and r.json()["sources"]["beta_editor"] == "kill-switch"
    r = client.post("/api/v1/features/beta_editor/kill", json={"engaged": False})
    assert r.status_code == 200 and r.json()["sources"]["beta_editor"] != "kill-switch"


@pytest.mark.acceptance("LOOP-F6")
def test_engaging_the_kill_switch_needs_a_reason(loop):
    out = mcp("axiom_flags__kill", {"key": "beta_editor"}, loop["state"])
    assert not out["ok"] and "reason" in out["errors"][0]


@pytest.mark.acceptance("LOOP-F7")
def test_one_resolution_order_and_each_answer_says_what_decided_it(loop, monkeypatch):
    """Peel the layers off one at a time, most decisive first."""
    ts, s = loop["targeting"], svc()
    monkeypatch.setenv("AXIOM_FLAG_BETA_EDITOR", "on")                       # environment
    s.set_override("node", "node-a", "beta_editor", "soon", by="@t")          # node
    ts.put_rollout("beta_editor", "development", rules=[{"value": "off"}], reason="r", by="@t")  # rollout
    s.set_override("account", "acme", "beta_editor", "on", by="@t")           # account
    s.set_override("user", "ana", "beta_editor", "soon", by="@t")             # user
    ts.set_kill_switch("beta_editor", "development", True, reason="k", by="@t")  # kill switch

    def decided():
        out = s.explain("beta_editor", node="node-a", account="acme", user="ana")
        return out["value"], out["decided_by"]

    assert decided() == ("off", "kill-switch")
    ts.set_kill_switch("beta_editor", "development", False, reason="", by="@t")
    assert decided() == ("soon", "user")
    s.set_override("user", "ana", "beta_editor", None, by="@t")
    assert decided() == ("on", "account")
    s.set_override("account", "acme", "beta_editor", None, by="@t")
    assert decided() == ("off", "rollout")
    ts.put_rollout("beta_editor", "development", rules=[], reason="r", by="@t")
    assert decided() == ("soon", "node")
    s.set_override("node", "node-a", "beta_editor", None, by="@t")
    assert decided() == ("on", "env")
    monkeypatch.delenv("AXIOM_FLAG_BETA_EDITOR")
    assert decided() == ("off", "default")


@pytest.mark.acceptance("LOOP-F7")
def test_the_extension_off_rule_sits_above_the_kill_switch(loop):
    resolver.set_switched_off_owners({"widgets"})
    loop["targeting"].set_kill_switch("panel", "development", True, reason="k", by="@t")
    out = svc().explain("panel", user="ana")
    assert out["decided_by"] == "extension-off" and out["waiting_on"] == "widgets"
    assert out["staged"] == {"value": "off", "source": "kill-switch"}


@pytest.mark.acceptance("LOOP-F7")
def test_the_variant_sits_between_node_and_environment(loop, monkeypatch):
    from types import SimpleNamespace

    variant = SimpleNamespace(name="v", flags={"beta_editor": "soon"}, override_namespace=None)
    s = resolver.FlagService(variant=variant)
    monkeypatch.setenv("AXIOM_FLAG_BETA_EDITOR", "on")
    assert s.explain("beta_editor", user="ana")["decided_by"] == "variant"
    s.set_override("node", "node-a", "beta_editor", "off", by="@t")
    assert s.explain("beta_editor", node="node-a", user="ana")["decided_by"] == "node"
