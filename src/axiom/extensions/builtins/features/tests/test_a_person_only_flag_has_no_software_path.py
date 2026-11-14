# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A flag only a person may set (#1811): ``set_by = "person"``. No MCP tool, agent tool or
in-process caller changes it; the command line asks the person at the prompt; the web route
needs a sign-in within five minutes. Every write to the flag is held to it: an override, a
rollout and the kill switch."""

from __future__ import annotations

import logging
import time

import pytest

from axiom.extensions.builtins.features import flag_registry
from axiom.extensions.builtins.features.flag_registry import FlagDef
from axiom.extensions.builtins.features.tests.conftest import FLAGS, LOOP_FLAGS, mcp, svc, web

PERSON_FLAG = FlagDef.from_mapping(
    {"key": "site_log", "description": "turns a site's log on", "set_by": "person"}, owner="core_ext"
)


@pytest.fixture
def person_flag(loop):
    flag_registry.set_registry(FLAGS + LOOP_FLAGS + (PERSON_FLAG,))
    return loop


def _invoke(capability, params, state_dir, *, surface, prompt=None):
    from axiom.extensions.builtins.features.skills import bind
    from axiom.infra.skill_dispatch import invoke_capability
    from axiom.infra.skills import SkillContext, SkillRegistry

    reg = SkillRegistry()
    bind(reg)
    ctx = SkillContext(registry=reg, state_dir=state_dir, logger=logging.getLogger("t"),
                       user_prompt=prompt)  # fmt: skip
    return invoke_capability(reg, capability, params, ctx, surface=surface)


@pytest.mark.acceptance("OL-109")
def test_the_declaration_reads_set_by_and_refuses_anything_else():
    assert PERSON_FLAG.set_by == "person"
    with pytest.raises(ValueError, match="set_by"):
        FlagDef.from_mapping({"key": "x", "set_by": "agent"}, owner="core_ext")


@pytest.mark.acceptance("OL-109")
def test_no_mcp_tool_or_agent_tool_sets_it(person_flag):
    state = person_flag["state"]
    got = mcp("axiom_flags__set", {"key": "site_log", "value": "on", "scope": "user", "scope_id": "cy"},
              state)  # fmt: skip
    assert not got["ok"] and "person" in " ".join(got["errors"])
    for capability, params in (
        ("flags.set", {"key": "site_log", "value": "on", "scope": "user", "scope_id": "cy"}),
        ("flags.kill", {"key": "site_log", "reason": "x", "engaged": True}),
        ("flags.rollout_set", {"key": "site_log", "rules": "[]"}),
    ):
        r = _invoke(capability, params, state, surface="agent_tool")
        assert not r.ok and "person" in r.errors[0], (capability, r.errors)
    assert svc().explain("site_log", user="cy")["value"] == "off"


@pytest.mark.acceptance("OL-109")
def test_the_command_line_asks_the_person_at_the_prompt(person_flag):
    state = person_flag["state"]
    params = {"key": "site_log", "value": "on", "scope": "user", "scope_id": "ana"}
    assert not _invoke("flags.set", params, state, surface="cli").ok  # nobody at the prompt
    assert not _invoke("flags.set", params, state, surface="cli", prompt=lambda q: "no").ok
    asked: list[str] = []
    ok = _invoke("flags.set", params, state, surface="cli", prompt=lambda q: asked.append(q) or "yes")
    assert ok.ok, ok.errors
    assert "site_log" in asked[0]
    assert svc().explain("site_log", user="ana")["value"] == "on"


@pytest.mark.acceptance("OL-109")
def test_the_web_needs_a_fresh_sign_in(person_flag, monkeypatch):
    client = web(monkeypatch, sub="dee")
    from axiom.extensions.builtins.features import api

    stale = {"sub": "dee", "roles": [], "site": "acct", "auth_time": int(time.time()) - 3600}
    monkeypatch.setattr(api, "_claims", lambda request: stale)
    r = client.patch("/api/v1/features/site_log", json={"value": "on"})
    assert r.status_code == 403 and "sign in again" in r.json()["detail"]
    fresh = {**stale, "auth_time": int(time.time()) - 30}
    monkeypatch.setattr(api, "_claims", lambda request: fresh)
    r = client.patch("/api/v1/features/site_log", json={"value": "on"})
    assert r.status_code == 200 and r.json()["flags"]["site_log"] == "on"


@pytest.mark.acceptance("OL-109")
def test_any_other_flag_is_unchanged(person_flag):
    got = mcp("axiom_flags__set", {"key": "beta_editor", "value": "on", "scope": "user", "scope_id": "cy"},
              person_flag["state"])  # fmt: skip
    assert got["ok"]
