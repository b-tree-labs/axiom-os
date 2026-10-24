# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``recipe.list``, ``recipe.check`` and ``recipe.plan`` (ADR-185).

All three read. ``check`` validates a recipe against the registry the caller is
actually running; ``plan`` is the dry run. Neither executes a step.
"""

from __future__ import annotations

from typing import Any

from axiom.infra import recipes as recipe_lib
from axiom.infra.skill_declarations import NextStep, Precondition, Ref
from axiom.infra.skills import SkillContext, SkillRegistry, SkillResult, SkillSpec, ensure_context

_SURFACES = ("cli", "mcp", "agent_tool")


def _recipe(params: dict[str, Any]):
    rid = str(params.get("id") or "").strip()
    reg = recipe_lib.default_recipes()
    recipe = reg.get(rid) if rid else None
    return rid, recipe, reg


def _unknown(rid: str, reg: recipe_lib.RecipeRegistry) -> SkillResult:
    return SkillResult(
        ok=False,
        errors=[f"no recipe {rid!r}; the recipes are: {', '.join(reg.ids())}" if rid else
                f"id is required; the recipes are: {', '.join(reg.ids())}"],
    )


def list_recipes(params: dict[str, Any], ctx: SkillContext | None = None) -> SkillResult:
    ctx = ensure_context(ctx)
    reg = recipe_lib.default_recipes()
    rows = []
    for r in reg.all():
        rep = recipe_lib.check(r, ctx.registry)
        rows.append(
            {
                "id": r.id,
                "intent": r.intent,
                "steps": len(r.steps),
                "ready": rep.ok,
                "missing": rep.missing,
                "prompt": r.prompt_name,
            }
        )
    return SkillResult(ok=True, value={"recipes": rows, "recipe_id": rows[0]["id"] if rows else ""})


def check_recipe(params: dict[str, Any], ctx: SkillContext | None = None) -> SkillResult:
    ctx = ensure_context(ctx)
    rid, recipe, reg = _recipe(params)
    if recipe is None:
        return _unknown(rid, reg)
    report = recipe_lib.check(recipe, ctx.registry)
    return SkillResult(ok=report.structurally_sound, value=report.as_dict(), errors=list(report.problems))


def plan_recipe(params: dict[str, Any], ctx: SkillContext | None = None) -> SkillResult:
    ctx = ensure_context(ctx)
    rid, recipe, reg = _recipe(params)
    if recipe is None:
        return _unknown(rid, reg)
    supplied = params.get("params") or {}
    if not isinstance(supplied, dict):
        return SkillResult(ok=False, errors=["params must be a mapping of recipe parameter to value"])
    return SkillResult(ok=True, value=recipe_lib.plan(recipe, supplied, ctx.registry, ctx))


_ID_PRE = (
    Precondition(
        "recipe-exists",
        "the recipe id must name a published recipe",
        check=lambda p, c: str(p.get("id") or "") in recipe_lib.default_recipes().ids(),
        fix="call recipe.list and pass one of its ids as id",
    ),
)

_SPECS = (
    SkillSpec(
        name="recipe.list",
        fn=list_recipes,
        description="List the published recipes: vetted chains of tools for a task, and whether each is ready to follow on this install.",
        inputs={},
        side_effects=False,
        idempotent=True,
        surfaces=_SURFACES,
        risk="read",
        requires=(),
        produces=(Ref("recipe_id", "pass as id to recipe.check or recipe.plan", field="recipe_id"),),
        next_steps=(NextStep("recipe.check", "produced:recipe_id", "confirm the recipe is sound here", {"id": "recipe_id"}),),
    ),
    SkillSpec(
        name="recipe.check",
        fn=check_recipe,
        description="Validate one recipe against the tools actually installed: every step real, outputs wired to inputs, approval on destructive steps.",
        inputs={"id": "str!"},
        side_effects=False,
        idempotent=True,
        surfaces=_SURFACES,
        risk="read",
        requires=_ID_PRE,
        produces=(),
        next_steps=(NextStep("recipe.plan", "ok", "dry-run it with your parameters"),),
    ),
    SkillSpec(
        name="recipe.plan",
        fn=plan_recipe,
        description="Dry-run a recipe: its steps in order, what each needs, which preconditions are unmet now, and where a person approves. Executes nothing.",
        inputs={"id": "str!", "params": "dict"},
        side_effects=False,
        idempotent=True,
        surfaces=_SURFACES,
        risk="read",
        requires=_ID_PRE,
        produces=(),
        next_steps=(),
    ),
)


def bind(registry: SkillRegistry) -> list[str]:
    names = []
    for spec in _SPECS:
        names.append(spec.name)
        if not registry.has(spec.name):
            registry.register_skill(spec, mutating=False)
    return names


__all__ = ["bind", "check_recipe", "list_recipes", "plan_recipe"]
