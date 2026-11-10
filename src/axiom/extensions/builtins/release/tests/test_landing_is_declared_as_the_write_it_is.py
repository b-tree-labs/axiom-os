# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``release.land`` reports; ``release.land_act`` merges (ADR-237).

One skill did both and declared ``risk = "read"``, so the baton, the MCP read-only hint and
chat's auto-approval all counted a merge as a read. The command a person types is unchanged.
"""

from __future__ import annotations

import logging

import pytest

from axiom.extensions.builtins.release import cli
from axiom.extensions.builtins.release import skills as release_skills
from axiom.extensions.builtins.release.lander import skill as lander_skill
from axiom.extensions.builtins.mcp.skill_tools import tool_annotations
from axiom.infra.skills import SkillContext, SkillRegistry, SkillResult


@pytest.fixture
def registry() -> SkillRegistry:
    reg = SkillRegistry()
    release_skills.bind(reg)
    return reg


def _ctx(registry, tmp_path) -> SkillContext:
    return SkillContext(registry=registry, state_dir=tmp_path, logger=logging.getLogger("t"))


def test_the_report_stays_a_read_on_every_surface_it_had(registry):
    spec = registry.spec("release.land")
    assert spec.effective_risk == "read" and spec.side_effects is False
    assert set(spec.surfaces) == {"cli", "mcp", "agent_tool"}
    assert "act" not in spec.inputs


def test_merging_is_declared_as_a_write_and_stays_off_mcp(registry):
    spec = registry.spec("release.land_act")
    assert spec.effective_risk == "reversible-write"
    assert spec.side_effects is True and spec.idempotent is False
    assert set(spec.surfaces) == {"cli"}
    assert tool_annotations(spec).read_only_hint is False


def test_the_read_refuses_to_merge(registry, tmp_path, monkeypatch):
    monkeypatch.setattr(
        lander_skill, "land", lambda *a: pytest.fail("the read reached the lander")
    )
    r = release_skills.land({"repo": "o/r", "act": True}, _ctx(registry, tmp_path))
    assert not r.ok and "release.land_act" in r.errors[0]


def test_the_write_reaches_the_lander_with_act_set(registry, tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(
        lander_skill,
        "land",
        lambda params, ctx: seen.update(params) or SkillResult(ok=True, value={}),
    )
    assert release_skills.land_act({"repo": "o/r"}, _ctx(registry, tmp_path)).ok
    assert seen == {"repo": "o/r", "act": True}


@pytest.mark.parametrize(
    "argv,expected",
    [
        (["land", "--repo", "o/r"], "release.land"),
        (["land", "--repo", "o/r", "--propose"], "release.land"),
        (["land", "--repo", "o/r", "--act"], "release.land_act"),
        # --propose has always won over --act: it records and never merges
        (["land", "--repo", "o/r", "--propose", "--act"], "release.land"),
    ],
)
def test_the_command_a_person_types_is_unchanged(argv, expected, monkeypatch):
    called = []

    def fake_invoke(registry, name, params, ctx, surface):
        called.append(name)
        return SkillResult(ok=True, value={})

    monkeypatch.setattr("axiom.infra.skill_dispatch.invoke_capability", fake_invoke)
    assert cli.main(argv) == 0
    assert called == [expected]


def test_propose_alongside_act_still_only_records(registry, tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(
        lander_skill,
        "land",
        lambda params, ctx: seen.update(params) or SkillResult(ok=True, value={}),
    )
    assert release_skills.land(
        {"repo": "o/r", "propose": True, "act": True}, _ctx(registry, tmp_path)
    ).ok
    assert seen["propose"] is True
