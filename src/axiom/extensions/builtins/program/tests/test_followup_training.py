# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Training and surfacing for follow-ups (ADR-196 D6).

The agent is taught by data the platform already renders: CLERK's persona, the
skill descriptions, and the declarations that become ``needs`` / ``makes`` hints
in the capabilities block. Nothing here is hand-kept prose that can drift from
the skills: the tests read the real manifest, the real registry and the real
renderer.
"""

from __future__ import annotations

import tomllib
from datetime import timedelta
from pathlib import Path

from axiom.extensions.builtins.program import followup_store as store
from axiom.extensions.builtins.program import skills
from axiom.infra.skills import SkillRegistry
from axiom.memory.rendering import capabilities_for_block, render_capability_block

from .conftest import ME, NOW, OTHER

ROOT = Path(skills.__file__).parent.parent
PERSONA = (ROOT / "agents" / "clerk" / "persona.md").read_text(encoding="utf-8")
VERBS = ("add", "list", "show", "owed", "snooze", "ack", "done")


def test_the_persona_teaches_when_to_propose_how_to_ask_and_when_to_stop():
    text = PERSONA.lower()
    assert "approved but deferred" in text and "table swap" in text
    assert "deploy with a rollback window" in text and "key rotation" in text
    assert "propose, do not create" in text and "only on a yes" in text
    assert "declines twice" in text and "never a secret" in text
    for skill in ("followup_add", "followup_owed", "followup_done"):
        assert skill in text
    assert "adr-196" in text


def test_clerk_declares_every_followup_skill_it_uses_and_each_exists_in_the_manifest():
    manifest = tomllib.loads((ROOT / "axiom-extension.toml").read_text(encoding="utf-8"))
    provides = manifest["extension"]["provides"]
    declared = {p["name"] for p in provides if p["kind"] == "skill"}
    [clerk] = [p for p in provides if p["kind"] == "agent"]
    wanted = {f"program.followup_{v}" for v in VERBS} | {"program.followup_fire", "program.followup_sweep"}
    assert wanted <= set(clerk["uses_skills"]) and wanted <= declared


def test_the_add_skill_tells_the_agent_to_propose_and_ask():
    registry = SkillRegistry()
    skills.bind(registry)
    d = registry.spec("program.followup_add").description.lower()
    assert "propose it" in d and "ask before creating" in d and "do not create one silently" in d
    assert "table swap" in d and "rollback window" in d and "key rotation" in d


def test_the_capabilities_block_carries_needs_and_makes_hints_for_the_followup_skills():
    registry = SkillRegistry()
    skills.bind(registry)
    specs = [registry.spec(f"program.followup_{v}") for v in VERBS]
    caps = {c["name"]: c for c in capabilities_for_block(specs, used=set())}
    assert caps["program.followup_add"]["makes"] == ["followup_id"]
    for verb in ("show", "snooze", "ack", "done"):
        assert caps[f"program.followup_{verb}"]["needs"] == ["followup-id"]
    block = render_capability_block(list(caps.values()), budget_lines=20)
    assert "(makes followup_id)" in block and "(needs followup-id)" in block
    assert "remember to come back to a deferred risky step" in block


def test_the_owed_tool_is_the_what_do_i_owe_answer(world, monkeypatch):
    assert world.call("followup_owed").value["summary"] == "nothing owed"
    world.call("followup_add", what="approve the drop-old", due="1h", next_command="axi data drop-old orders_old")
    out = world.call("followup_owed").value
    assert out["total"] == 1 and out["open"][0]["next_command"] == "axi data drop-old orders_old"


# ---- the status read leads with the caller's follow-ups ----------------------


def test_status_leads_with_the_callers_followups_and_is_quiet_when_none(world):
    r = world.call("status", scope="priorities")
    assert list(r.value)[0] == "followups" and r.value["followups"]["summary"] == "nothing owed"


def test_status_lists_the_due_and_missed_first_and_only_the_callers(world, monkeypatch):
    a = world.call("followup_add", what="upcoming", due="48h").value["id"]
    b = world.call("followup_add", what="overdue", due="1h").value["id"]
    world.clock["now"] = NOW + timedelta(hours=3)
    block = world.call("status", scope="priorities").value["followups"]
    assert [x["id"] for x in block["missed"]] == [b] and [x["id"] for x in block["open"]] == [a]
    assert list(block)[:3] == ["principal", "total", "missed"]
    monkeypatch.setenv("AXIOM_MCP_CLIENT_PRINCIPAL", OTHER)
    other = world.call("status", surface="mcp", scope="priorities").value["followups"]
    assert other["total"] == 0, "another principal's status does not carry them"
    monkeypatch.setenv("AXIOM_MCP_CLIENT_PRINCIPAL", ME)
    assert world.call("status", surface="mcp", scope="priorities").value["followups"]["total"] == 2


def test_status_still_answers_when_the_follow_up_store_is_down(world):
    def down():
        raise ConnectionError("no database")

    store.set_provider(down)
    r = world.call("status", scope="priorities")
    assert r.ok is not None
    block = r.value["followups"] if isinstance(r.value, dict) else None
    assert block["available"] is False and "ConnectionError" in block["reason"]


def test_the_web_surface_never_carries_a_principals_followups(world):
    world.call("followup_add", what="mine", due="1h")
    r = world.call("status", surface="web", scope="priorities")
    assert "followups" not in (r.value or {})


def test_the_freshness_read_is_untouched(world):
    r = world.call("status", scope="freshness")
    assert "followups" not in (r.value or {})
