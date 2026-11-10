# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Recipes (ADR-185): validated against the live registry, with controls that can fail."""

from __future__ import annotations

import logging

import pytest

from axiom.extensions.builtins.chat import skills as chat_skills
from axiom.extensions.builtins.process import skills as process_skills
from axiom.extensions.builtins.recipe import skills as recipe_skills
from axiom.extensions.builtins.secrets import skills as secrets_skills
from axiom.extensions.builtins.state import skills as state_skills
from axiom.extensions.builtins.vault import skills as vault_skills
from axiom.infra import recipes
from axiom.infra.recipes import Recipe, RecipeRegistry, RecipeStep
from axiom.infra.skill_declarations import Precondition, Ref
from axiom.infra.skill_dispatch import invoke_capability
from axiom.infra.skills import SkillContext, SkillRegistry, SkillResult, SkillSpec


def _fn(p, c):
    return SkillResult()


@pytest.fixture(scope="module")
def real():
    reg = SkillRegistry()
    recipe_skills.bind(reg)
    secrets_skills.bind(reg)
    vault_skills.bind(reg)
    state_skills.bind(reg)
    process_skills.bind(reg)
    chat_skills.bind(reg)
    return reg


# ---------------------------------------------------------------- the shipped catalogue


def test_the_shipped_recipes_have_no_defects_only_declared_placeholders(real):
    shipped = {r.id: recipes.check(r, real) for r in recipes.default_recipes().all()}
    assert {rid: rep.problems for rid, rep in shipped.items()} == {rid: [] for rid in shipped}
    assert shipped["credential-health-review"].ok
    assert shipped["credential-finding-clearance"].ok
    # every step of these is a real skill on this base
    for rid in ("incident-response", "credential-leak-response", "memory-verify"):
        assert shipped[rid].ok, (rid, shipped[rid].as_dict())
    # the chat commands that change a session have no skill form yet; they are the only placeholders left
    assert sorted(shipped["session-handoff"].missing) == ["chat.compact", "chat.renew"]


def test_the_four_requested_recipes_exist():
    assert {"incident-response", "credential-leak-response", "session-handoff", "memory-verify"} <= set(
        recipes.default_recipes().ids())


def test_destructive_placeholder_steps_carry_an_approval_point():
    inc = recipes.default_recipes().get("incident-response")
    assert [s.id for s in inc.steps if s.approval] == ["stop", "restore"]


# ---------------------------------------------------------------- check controls


def _two(produce=True, risk="read", extra_req=()):
    reg = SkillRegistry()
    reg.register_skill(SkillSpec(
        name="x.a", fn=_fn, risk="read", side_effects=False, recipes=("r",),
        produces=(Ref("thing_id", "pass to x.b"),) if produce else ()))
    reg.register_skill(SkillSpec(
        name="x.b", fn=_fn, risk=risk, side_effects=(risk != "read"), recipes=("r",), inputs={"thing": "str"},
        requires=(Precondition("thing", "needs a thing", consumes="thing_id", param="thing", fix="run x.a"),
                  *extra_req)))
    return reg


def _r(approval=False, thing="$a.thing_id", **kw):
    return Recipe("r", "do a thing", (
        RecipeStep("a", "x.a", "find it"),
        RecipeStep("b", "x.b", "use it", inputs={"thing": thing} if thing else {}, approval=approval),
    ), **kw)


def test_control_a_sound_recipe_passes():
    assert recipes.check(_r(), _two()).ok


def test_control_an_unknown_skill_that_is_not_a_placeholder_is_a_defect():
    rec = Recipe("r", "d", (RecipeStep("a", "x.ghost", "p"),))
    assert any("not a declared placeholder" in p for p in recipes.check(rec, _two()).problems)


def test_control_a_placeholder_that_now_exists_must_be_removed():
    rec = Recipe("r", "d", (RecipeStep("a", "x.a", "p"),), placeholders=frozenset({"x.a"}))
    assert any("now exists" in p for p in recipes.check(rec, _two()).problems)


def test_control_a_missing_placeholder_is_reported_missing_not_defective():
    rec = Recipe("r", "d", (RecipeStep("a", "x.ghost", "p"),), placeholders=frozenset({"x.ghost"}))
    rep = recipes.check(rec, _two())
    assert rep.missing == ["x.ghost"] and rep.problems == [] and not rep.ok and rep.structurally_sound


def test_control_a_destructive_step_without_approval_is_a_defect():
    reg = _two(risk="destructive")
    assert any("approval point" in p for p in recipes.check(_r(approval=False), reg).problems)
    assert recipes.check(_r(approval=True), reg).ok


def test_control_a_ref_the_source_does_not_produce_is_a_defect():
    probs = recipes.check(_r(), _two(produce=False)).problems
    assert any("does not produce it" in p for p in probs)


def test_control_a_required_ref_the_step_never_supplies_is_a_defect():
    probs = recipes.check(_r(thing=None), _two()).problems
    assert any("requires 'thing_id' as 'thing'" in p for p in probs)


def test_control_a_ref_of_the_wrong_kind_is_a_defect():
    reg = SkillRegistry()
    reg.register_skill(SkillSpec(name="x.a", fn=_fn, risk="read", side_effects=False, recipes=("r",),
                                 produces=(Ref("other_id", "use it"),)))
    reg.register_skill(SkillSpec(name="x.b", fn=_fn, risk="read", side_effects=False, recipes=("r",),
                                 requires=(Precondition("t", "needs", consumes="thing_id", param="thing", fix="f"),)))
    rec = Recipe("r", "d", (RecipeStep("a", "x.a", "p"),
                            RecipeStep("b", "x.b", "q", inputs={"thing": "$a.other_id"})))
    assert any("needs a 'thing_id' ref" in p for p in recipes.check(rec, reg).problems)


def test_control_an_input_the_skill_does_not_take_is_a_defect():
    rec = Recipe("r", "d", (RecipeStep("a", "x.a", "p"), RecipeStep("b", "x.b", "q", inputs={"bogus": "1", "thing": "$a.thing_id"})))
    assert any("takes no input 'bogus'" in p for p in recipes.check(rec, _two()).problems)


def test_control_a_skill_with_no_risk_or_membership_is_a_defect():
    reg = SkillRegistry()
    reg.register_skill(SkillSpec(name="x.a", fn=_fn))
    rec = Recipe("r", "d", (RecipeStep("a", "x.a", "p"),))
    probs = recipes.check(rec, reg).problems
    assert any("declares no risk" in p for p in probs) and any("does not list recipe" in p for p in probs)


def test_construction_rejects_a_forward_reference_and_an_undeclared_param():
    with pytest.raises(ValueError):
        Recipe("r", "d", (RecipeStep("a", "x.a", "p", inputs={"i": "$b.k"}), RecipeStep("b", "x.b", "q")))
    with pytest.raises(ValueError):
        Recipe("r", "d", (RecipeStep("a", "x.a", "p", inputs={"i": "$nope"}),))


def test_a_registry_refuses_a_duplicate():
    reg = RecipeRegistry()
    reg.register(_r())
    with pytest.raises(ValueError):
        reg.register(_r())


# ---------------------------------------------------------------- plan


def test_plan_lists_steps_unmet_preconditions_and_approval_points():
    reg = _two(risk="destructive", extra_req=(Precondition(
        "ready", "must be ready", check=lambda p, c: (False, "not ready"), fix="make it ready"),))
    out = recipes.plan(_r(approval=True), {}, reg)
    assert out["approval_points"] == ["b"] and out["steps"][1]["approval"] is True
    # step b takes a ref from step a, so nothing about it can be evaluated yet
    assert out["steps"][1]["deferred"] is True and out["steps"][1]["unmet"] == []


def test_plan_evaluates_a_step_whose_inputs_are_all_known():
    reg = SkillRegistry()
    reg.register_skill(SkillSpec(
        name="x.a", fn=_fn, risk="reversible-write", side_effects=True, recipes=("r",),
        requires=(Precondition("ready", "must be ready", check=lambda p, c: (False, "not ready"), fix="make {what} ready"),)))
    rec = Recipe("r", "d", (RecipeStep("a", "x.a", "p", inputs={"what": "$what"}),), params={"what": "the thing"})
    out = recipes.plan(rec, {"what": "cache"}, reg)
    assert out["steps"][0]["unmet"] == [{"id": "ready", "why": "not ready", "fix": "make cache ready"}]
    assert out["missing_params"] == []
    assert recipes.plan(rec, {}, reg)["missing_params"] == ["what"]


def test_plan_marks_a_placeholder_step_missing(real):
    out = recipes.plan(recipes.default_recipes().get("session-handoff"), {"session": "s"}, real)
    assert out["steps"][0]["available"] is False and out["steps"][0]["note"] == "missing on this base"
    assert out["steps"][1]["available"] is True  # chat.where is real
    assert out["check"]["missing"] == ["chat.renew", "chat.compact"]


def test_plan_of_a_real_recipe_marks_each_destructive_step_for_approval(real):
    out = recipes.plan(recipes.default_recipes().get("incident-response"), {"match": "x"}, real)
    assert out["check"]["ok"] is True
    assert out["approval_points"] == ["stop", "restore"]
    assert [s["risk"] for s in out["steps"]] == ["read", "destructive", "reversible-write", "destructive"]


# ---------------------------------------------------------------- the skills


def test_recipe_skills_run_through_the_chokepoint(real, tmp_path, monkeypatch):
    from axiom.infra import authority

    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("AXIOM_AUTHORITY_RECEIPTS", "off")
    monkeypatch.setattr(authority, "_default_ctx", authority.build_tool_context(session_factory=None))
    ctx = SkillContext(registry=real, state_dir=tmp_path, logger=logging.getLogger("t"))
    listed = invoke_capability(real, "recipe.list", {}, ctx, surface="mcp")
    assert listed.ok and {r["id"] for r in listed.value["recipes"]} == set(recipes.default_recipes().ids())
    ready = {r["id"]: r["ready"] for r in listed.value["recipes"]}
    assert ready["credential-health-review"] is True and ready["incident-response"] is True
    assert ready["session-handoff"] is False  # two of its steps have no skill yet
    # the next step it offers is real, and carries the ref it produced
    assert listed.next and listed.next[0]["skill"] == "recipe.check"
    checked = invoke_capability(real, "recipe.check", {"id": "session-handoff"}, ctx, surface="mcp")
    assert checked.ok and checked.value["missing"]  # sound, but not yet fillable
    plan = invoke_capability(real, "recipe.plan", {"id": "memory-verify", "params": {"query": "q"}}, ctx, surface="mcp")
    assert plan.ok and plan.value["steps"][0]["inputs"] == {"query": "q"}
    # an unknown id refuses with the exact fix, before the body runs
    bad = invoke_capability(real, "recipe.check", {"id": "nope"}, ctx, surface="mcp")
    assert not bad.ok and bad.unmet[0]["id"] == "recipe-exists" and "recipe.list" in bad.unmet[0]["fix"]


# ---------------------------------------------------------------- publication


def test_the_prompt_body_names_the_tools_the_wiring_and_the_approval_protocol():
    text = recipes.render_markdown(recipes.default_recipes().get("incident-response"))
    assert "`axiom_process__stop`" in text and "A person approves this step first" in text
    assert "plan_hash" in text and "_chain" in text
    assert "the `process_id` returned by step `list`" in text


def test_prompt_descriptions_disambiguate_from_harness_commands():
    d = recipes.prompt_description(recipes.default_recipes().get("memory-verify"))
    assert d.startswith("Axiom recipe:") and "not a harness built-in command" in d
    assert "Not the harness's own /memory, /verify." in d
    assert recipes.default_recipes().get("memory-verify").prompt_name == "axiom_recipe__memory_verify"


# ---------------------------------------------------------------- Arazzo projection (AEOS 0.2 section 4.11.6)


def test_a_recipe_projects_to_an_arazzo_workflow_with_the_aeos_fields_as_extensions():
    r = Recipe(
        id="tidy",
        intent="make a thing then drop it",
        params={"name": "what to call it"},
        rollback="drop it",
        steps=(
            RecipeStep("make", "t.make", "make it", inputs={"q": "$name"}, expects=("thing_id",)),
            RecipeStep("drop", "t.drop", "drop it", inputs={"thing": "$make.thing_id"}, approval=True, undo="recreate"),
        ),
    )
    wf = recipes.to_arazzo(r, tool_name=lambda s: "axiom_" + s.replace(".", "__"))
    assert wf["workflowId"] == "tidy" and wf["summary"] == "make a thing then drop it"
    make, drop = wf["steps"]
    assert make["operationId"] == "axiom_t__make"
    assert make["parameters"] == [{"name": "q", "in": "body", "value": "$inputs.name"}]
    assert make["outputs"] == {"thing_id": "$response.body#/value/thing_id"}
    assert drop["parameters"][0]["value"] == "$steps.make.outputs.thing_id"
    assert drop["dependsOn"] == ["make"]
    assert drop["x-aeos-approval"] is True and make["x-aeos-approval"] is False
    assert wf["x-aeos-rollback"] == "drop it"
    assert wf["inputs"]["properties"]["name"]["description"] == "what to call it"


def test_a_recipe_round_trips_from_its_data_form():
    data = {
        "id": "tidy", "intent": "x", "rollback": "y", "params": {"name": "n"},
        "step": [{"id": "make", "skill": "t.make", "purpose": "p", "inputs": {"q": "$name"}, "approval": False}],
    }
    r = Recipe.from_mapping(data)
    assert r.id == "tidy" and r.steps[0].inputs == {"q": "$name"}
    with pytest.raises(ValueError):
        Recipe.from_mapping({"id": "BAD ID", "intent": "x", "step": [{"id": "a", "skill": "t.a", "purpose": "p"}]})
