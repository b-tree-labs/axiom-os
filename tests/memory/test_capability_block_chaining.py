# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The capabilities block carries needs/makes hints and recipes, inside a byte budget (ADR-185).

The block is sent to every client on every connect. Hints and recipes are worth
their bytes only while the whole stays bounded, so the budget is a test, not a
hope: the hints go first, then the lowest-priority capability lines, and the
recipe lines and frame are never what gives way.
"""

from __future__ import annotations

import pytest

from axiom.infra.recipes import Recipe, RecipeStep, default_recipes
from axiom.infra.skill_declarations import Precondition, Ref
from axiom.infra.skills import SkillSpec
from axiom.memory.rendering import (
    CAPABILITY_BLOCK_MAX_BYTES,
    capabilities_for_block,
    chain_hints,
    recipes_for_block,
    render_capability_block,
)


def _spec(name, desc="Compute a total over stored data.", **kw):
    return SkillSpec(name=name, fn=lambda p, c: None, description=desc, side_effects=False, **kw)


def _cap(i, **extra):
    return {"name": f"x.verb{i:03d}", "when": f"someone asks about topic number {i} in some detail", **extra}


def test_hints_render_compactly_and_only_when_declared():
    plain = capabilities_for_block([_spec("x.a")], used=set())
    assert "needs" not in plain[0] and "makes" not in plain[0]
    declared = capabilities_for_block(
        [_spec("x.b", requires=(Precondition("p1", "d", fix="f"),), produces=(Ref("snap_id", "pass to x.c"),))],
        used=set())
    assert declared[0]["needs"] == ["p1"] and declared[0]["makes"] == ["snap_id"]
    out = render_capability_block(plain + declared)
    assert "use `x.a`." in out  # byte-identical to the pre-chaining line
    assert "use `x.b` (needs p1; makes snap_id)." in out


def test_an_mcp_tool_carries_its_hints_in_meta():
    class Tool:
        name = "axiom_x__b"
        description = "Compute a total."
        meta = {"axiom": {"requires": ["p1"], "produces": ["snap_id"]}}

    assert chain_hints(Tool()) == (["p1"], ["snap_id"])
    assert chain_hints(object()) == ([], [])


def test_an_undeclared_block_is_byte_identical_to_the_old_renderer():
    caps = [{"name": "x.a", "when": "someone wants to do a"}, {"name": "x.b", "when": "someone wants to do b"}]
    out = render_capability_block(caps)
    assert out.count("\n- When") == 2 and "(needs" not in out and "recipe" not in out


def test_recipes_come_first_and_extend_the_line_budget():
    caps = [_cap(i) for i in range(30)]
    recs = [{"id": "r1", "intent": "Close a leak", "prompt": "axiom_recipe__r1"}]
    without = render_capability_block(caps)
    with_r = render_capability_block(caps, recipes=recs)
    lines = with_r.splitlines()
    first = next(i for i, ln in enumerate(lines) if ln.startswith("- "))
    assert lines[first] == "- To close a leak, follow recipe `axiom_recipe__r1`."
    assert with_r.count("\n- When") == without.count("\n- When")  # capabilities were not starved


def test_a_recipe_with_placeholders_or_unavailable_tools_is_withheld():
    ready = Recipe("ok", "Do a thing", (RecipeStep("a", "x.a", "p"), RecipeStep("b", "x.b", "q")))
    partial = Recipe("part", "Do half", (RecipeStep("a", "x.a", "p"),), placeholders=frozenset({"x.a"}))
    rows = recipes_for_block([ready, partial], available={"axiom_x__a", "axiom_x__b"})
    assert [r["id"] for r in rows] == ["ok"]
    assert recipes_for_block([ready], available={"axiom_x__a"}) == []  # x.b is not a tool here
    assert recipes_for_block([ready], available=None)[0]["prompt"] == "axiom_recipe__ok"


def test_recipes_with_more_unused_steps_come_first():
    a = Recipe("aaa", "A", (RecipeStep("a", "x.a", "p"),))
    b = Recipe("bbb", "B", (RecipeStep("a", "x.b", "p"), RecipeStep("b", "x.c", "q")))
    rows = recipes_for_block([a, b], used={"x.a"})
    assert [r["id"] for r in rows] == ["bbb", "aaa"]


def test_nothing_to_say_renders_nothing():
    assert render_capability_block([], recipes=[]) == ""


# ---------------------------------------------------------------- the byte budget


def test_the_block_never_exceeds_its_byte_budget_however_much_is_declared():
    caps = [
        _cap(i, needs=[f"a-rather-long-precondition-id-{i}-{j}" for j in range(3)], makes=["a_long_ref_kind"] * 2)
        for i in range(200)
    ]
    recs = [{"id": f"r{i}", "intent": "Do a long and elaborate thing " * 3, "prompt": f"axiom_recipe__r{i}"}
            for i in range(10)]
    out = render_capability_block(caps, recipes=recs, budget_lines=60)
    assert len(out.encode()) <= CAPABILITY_BLOCK_MAX_BYTES
    assert "follow recipe" in out  # the recipe lines survive the trimming
    assert out.startswith("<!-- axiom:capabilities:begin -->") and out.rstrip().endswith("<!-- axiom:capabilities:end -->")


def test_hints_are_dropped_before_capability_lines():
    caps = [_cap(i, needs=["p1", "p2", "p3"], makes=["a", "b"]) for i in range(6)]
    out = render_capability_block(caps, max_bytes=len(render_capability_block(caps, max_bytes=10**6).encode()) - 80)
    assert "(needs" not in out and out.count("\n- When") == 6


def test_a_budget_too_small_for_everything_trims_lines_last():
    caps = [_cap(i) for i in range(6)]
    full = render_capability_block(caps)
    small = render_capability_block(caps, max_bytes=len(full.encode()) - 250)
    assert len(small.encode()) <= len(full.encode()) - 250
    assert small.count("\n- When") < full.count("\n- When") and "more; ask" in small


def test_the_default_budget_is_a_real_ceiling():
    # The numbers the report quotes: the frame costs ~250 bytes, a line ~150-190.
    assert 1500 <= CAPABILITY_BLOCK_MAX_BYTES <= 4096


def test_the_real_install_block_fits(tmp_path):
    """Against the real registry's declared skills and the real catalog."""
    from axiom.extensions.builtins.recipe import skills as recipe_skills
    from axiom.extensions.builtins.secrets import skills as secrets_skills
    from axiom.extensions.builtins.vault import skills as vault_skills
    from axiom.infra.skills import SkillRegistry

    reg = SkillRegistry()
    for mod in (recipe_skills, secrets_skills, vault_skills):
        mod.bind(reg)
    caps = capabilities_for_block(list(reg.specs().values()), used=set())
    recs = recipes_for_block(default_recipes().all(), available=set(reg.list()))
    out = render_capability_block(caps, recipes=recs)
    assert len(out.encode()) <= CAPABILITY_BLOCK_MAX_BYTES
    assert any("credential-health-review" in r["id"] for r in recs)


@pytest.mark.parametrize("n", [0, 1, 5, 50])
def test_budget_holds_at_every_size(n):
    out = render_capability_block([_cap(i, needs=["x" * 40] * 3) for i in range(n)])
    assert len(out.encode()) <= CAPABILITY_BLOCK_MAX_BYTES
