# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The skills that arrived with the combined branch declare what they do (ADR-185).

The lint proves each owes nothing. This pins the *judgements*: which are destructive and which are not,
so a change of mind is a reviewed diff, and the small behaviours the declarations rely on (a reference
lifted into a result, a precondition that refuses with the exact fix).
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from axiom.extensions.builtins.chat import skills as chat_skills
from axiom.extensions.builtins.chat.skills import read as chat_read
from axiom.extensions.builtins.maintenance import skills as maintenance_skills
from axiom.extensions.builtins.process import skills as process_skills
from axiom.extensions.builtins.program import skills as program_skills
from axiom.extensions.builtins.state import skills as state_skills
from axiom.extensions.builtins.vault import skills as vault_skills
from axiom.infra.skill_dispatch import invoke_capability
from axiom.infra.skills import SkillContext, SkillRegistry, SkillResult


@pytest.fixture(scope="module")
def reg():
    r = SkillRegistry()
    for module in (
        state_skills,
        process_skills,
        vault_skills,
        chat_skills,
        maintenance_skills,
        program_skills,
    ):
        module.bind(r)
    return r


def _by_risk(reg, prefixes):
    out: dict[str, set[str]] = {"read": set(), "reversible-write": set(), "destructive": set()}
    for name in reg.list():
        if name.startswith(prefixes):
            out[reg.spec(name).effective_risk].add(name)
    return out


def test_state_risks(reg):
    risks = _by_risk(reg, ("state.",))
    assert risks["destructive"] == {"state.restore", "state.prune"}
    assert risks["reversible-write"] == {
        "state.snapshot",
        "state.snapshot_tick",
        "state.pin",
        "state.unpin",
    }
    assert risks["read"] == {"state.ls", "state.show", "state.diff", "state.verify"}


def test_process_risks(reg):
    risks = _by_risk(reg, ("process.",))
    assert risks["destructive"] == {"process.stop", "process.sweep", "process.panic"}
    assert risks["reversible-write"] == {"process.resume"}
    assert risks["read"] == {"process.ls", "process.show"}


def test_vault_backup_risks(reg):
    risks = _by_risk(reg, ("vault.backup", "vault.key_"))
    assert risks["destructive"] == {
        "vault.backup_restore",
        "vault.backup_prune",
        "vault.backup_scrub",
    }
    assert risks["reversible-write"] == {
        "vault.backup",
        "vault.backup_tick",
        "vault.backup_pin",
        "vault.backup_unpin",
        "vault.key_init",
    }
    assert risks["read"] == {
        "vault.backup_ls",
        "vault.backup_show",
        "vault.backup_verify",
        "vault.key_info",
    }


def test_every_chat_skill_is_a_read(reg):
    assert {reg.spec(n).effective_risk for n in reg.list() if n.startswith("chat.")} == {"read"}


def test_every_destructive_skill_here_is_cli_only(reg):
    # the plan/apply gate is there for a protocol surface; none of these opted into one, so widening
    # a destructive skill to MCP or the chat agent is a deliberate, reviewed change
    for name in reg.list():
        spec = reg.spec(name)
        if spec.effective_risk == "destructive":
            assert not ({"mcp", "agent_tool"} & set(spec.surfaces or ())), name


# ---------------------------------------------------------------- references lifted into results


def test_a_memory_search_result_carries_the_first_id_as_a_reference():
    data = {"items": [{"id": "frag-1", "text": "t"}, {"id": "frag-2", "text": "u"}], "served": 2}
    agent_for = chat_read.agent_for
    chat_read.agent_for = lambda params: (object(), None)
    try:
        res = chat_read._with_memory({}, lambda a: data)
    finally:
        chat_read.agent_for = agent_for
    assert res.ok and res.value["fragment_id"] == "frag-1"


def test_nothing_is_lifted_when_nothing_was_served():
    agent_for = chat_read.agent_for
    chat_read.agent_for = lambda params: (object(), None)
    try:
        res = chat_read._with_memory({}, lambda a: {"items": [], "served": 0})
    finally:
        chat_read.agent_for = agent_for
    assert res.ok and "fragment_id" not in res.value


def test_a_single_waiting_maintenance_request_is_a_reference(monkeypatch):
    row = {
        "id": "req-1",
        "action": "restart-service",
        "params": {},
        "operator": "@op:x",
        "expires_at": "t",
    }
    node = SimpleNamespace(pending=lambda: [row])
    monkeypatch.setattr(maintenance_skills, "_node", lambda ctx: node)
    monkeypatch.setattr(
        maintenance_skills.m, "ACTIONS", {"restart-service": SimpleNamespace(risk="low")}
    )
    res = maintenance_skills.pending({}, SimpleNamespace())
    assert res.value["request_id"] == "req-1"
    node.pending = lambda: [row, {**row, "id": "req-2"}]
    assert "request_id" not in maintenance_skills.pending({}, SimpleNamespace()).value


# ---------------------------------------------------------------- refusals with the exact fix


def _ctx(reg, tmp_path):
    return SkillContext(
        registry=reg, state_dir=tmp_path, logger=logging.getLogger("t"), user_prompt=None
    )


def test_a_program_restore_with_no_backups_refuses_with_the_fix(reg, tmp_path):
    data = tmp_path / "data.json"
    data.write_text("{}")
    out = invoke_capability(
        reg,
        "program.restore",
        {"data": str(data), "latest": True},
        _ctx(reg, tmp_path),
        surface="cli",
    )
    assert not out.ok and out.unmet[0]["id"] == "backup-exists"
    assert "axi program snapshots" in out.unmet[0]["fix"]


def test_resuming_an_agent_that_was_never_paused_refuses_with_the_fix(reg, tmp_path):
    out = invoke_capability(
        reg, "process.resume", {"agent": "scan", "yes": True}, _ctx(reg, tmp_path), surface="cli"
    )
    assert not out.ok and out.unmet[0]["id"] == "agent-paused"
    assert "axi agents status" in out.unmet[0]["fix"]


def test_pinning_a_snapshot_that_does_not_exist_refuses_with_the_fix(reg, tmp_path):
    out = invoke_capability(reg, "state.pin", {"id": "nope"}, _ctx(reg, tmp_path), surface="cli")
    assert not out.ok and out.unmet[0]["id"] == "snapshot-exists"
    assert "axi state ls" in out.unmet[0]["fix"]


def test_key_init_refuses_when_there_is_nowhere_to_show_the_code(reg, tmp_path):
    out = invoke_capability(reg, "vault.key_init", {}, _ctx(reg, tmp_path), surface="cli")
    assert not out.ok and out.unmet[0]["id"] == "somewhere-to-show-the-code"
    assert isinstance(out, SkillResult)
