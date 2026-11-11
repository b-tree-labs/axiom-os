# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Recipes: vetted chains of skills, published so an agent can follow them (ADR-185).

A tool that works alone is the baseline. A *recipe* is what makes the whole
bigger than the parts: an ordered, named sequence of skills with how each step's
output feeds the next, where a person must approve, and how to undo it. It is
data, validated against the live registry, and published three ways: as an MCP
prompt (``axiom_recipe__<id>``), as a line in the capabilities block ("to do X,
follow recipe R"), and through ``recipe.check`` / ``recipe.plan``.

There is no runner. The agent follows the recipe, calling each skill through the
ordinary chokepoint, so every step keeps its own authorization, audit and
refusal. What a recipe adds is the *order* and the *wiring*, and the guarantee
(``check``) that both are real.

Wiring syntax in ``RecipeStep.inputs``: a plain string is a literal;
``$name`` is a recipe parameter; ``$step.kind`` is the ref of that kind produced
by an earlier step.

Placeholders. A recipe may name a skill that does not exist on this base yet
(it is declared in ``Recipe.placeholders``, with the ref kinds it is expected to
produce in ``RecipeStep.expects``). ``check`` reports each as *missing* so the
sweep can fill them, and fails if a placeholder has since appeared (the list
only shrinks) or if a missing skill is not declared as a placeholder.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from axiom.infra.skill_declarations import APPROVAL_PARAM, check_precondition, fill_fix

_ID = re.compile(r"^[a-z][a-z0-9-]*$")
_REF = re.compile(r"^\$([a-z][a-z0-9_-]*)\.([a-z][a-z0-9_-]*)$")
_PARAM = re.compile(r"^\$([a-z][a-z0-9_-]*)$")

#: Words a harness commonly reserves as a slash command. A recipe whose id
#: contains one says, in its description, that it is not that command (the
#: disambiguation rule in the MCP spec, "Name collisions with a harness"). A
#: short list on purpose: the authoritative per-harness data lives in the MCP
#: extension's harness profiles, and this only guards names this module mints.
HARNESS_RESERVED_WORDS = frozenset(
    {"verify", "init", "compact", "resume", "review", "plan", "status", "memory", "context",
     "model", "clear", "help", "config", "doctor", "cost", "login", "logout", "export", "rewind"}
)

#: Prompt / tool name prefix for a recipe projected over MCP.
RECIPE_PREFIX = "axiom_recipe__"


@dataclass(frozen=True)
class RecipeStep:
    id: str
    skill: str
    purpose: str
    inputs: Mapping[str, str] = field(default_factory=dict)
    #: A person approves this step before it runs (required for a destructive skill).
    approval: bool = False
    #: How to reverse it, in words or as the skill that does.
    undo: str = ""
    #: For a placeholder skill only: the ref kinds it is expected to produce.
    expects: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not _ID.match(self.id.replace("_", "-")):
            raise ValueError(f"step id {self.id!r} must be lowercase kebab")
        if "." not in self.skill:
            raise ValueError(f"step {self.id!r}: skill {self.skill!r} must be qualified (namespace.verb)")
        if not self.purpose.strip():
            raise ValueError(f"step {self.id!r} needs a purpose")


@dataclass(frozen=True)
class Recipe:
    id: str
    intent: str
    steps: tuple[RecipeStep, ...]
    params: Mapping[str, str] = field(default_factory=dict)
    #: One sentence on how to back out of the whole recipe.
    rollback: str = ""
    #: Skills this recipe names that do not exist on this base yet (the sweep's TODO).
    placeholders: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not _ID.match(self.id):
            raise ValueError(f"recipe id {self.id!r} must be lowercase kebab")
        if not self.intent.strip():
            raise ValueError(f"recipe {self.id!r} needs an intent sentence")
        if not self.steps:
            raise ValueError(f"recipe {self.id!r} has no steps")
        ids = [s.id for s in self.steps]
        if len(ids) != len(set(ids)):
            raise ValueError(f"recipe {self.id!r}: duplicate step ids")
        seen: set[str] = set()
        for step in self.steps:
            for name, value in step.inputs.items():
                ref = _REF.match(str(value))
                if ref and ref.group(1) not in seen:
                    raise ValueError(
                        f"recipe {self.id!r}: step {step.id!r} input {name!r} refers to "
                        f"{ref.group(1)!r}, which is not an earlier step"
                    )
                par = _PARAM.match(str(value))
                if par and par.group(1) not in self.params:
                    raise ValueError(
                        f"recipe {self.id!r}: step {step.id!r} input {name!r} uses "
                        f"undeclared recipe parameter {par.group(1)!r}"
                    )
            seen.add(step.id)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> Recipe:
        """Build a recipe from the data form a manifest ships (a TOML recipe file).

        Keys: ``id``, ``intent``, ``rollback``, optional ``params`` table,
        ``placeholders`` list, and ``step`` array of tables (``id``, ``skill``,
        ``purpose``, ``inputs`` table, ``approval``, ``undo``, ``expects``).
        Raises ``ValueError`` on anything malformed, so lint can report it.
        """
        steps = []
        for raw in data.get("step") or data.get("steps") or []:
            steps.append(
                RecipeStep(
                    id=str(raw.get("id", "")),
                    skill=str(raw.get("skill", "")),
                    purpose=str(raw.get("purpose", "")),
                    inputs=dict(raw.get("inputs") or {}),
                    approval=bool(raw.get("approval", False)),
                    undo=str(raw.get("undo", "")),
                    expects=tuple(raw.get("expects") or ()),
                )
            )
        return cls(
            id=str(data.get("id", "")),
            intent=str(data.get("intent", "")),
            steps=tuple(steps),
            params=dict(data.get("params") or {}),
            rollback=str(data.get("rollback", "")),
            placeholders=frozenset(data.get("placeholders") or ()),
        )

    @property
    def prompt_name(self) -> str:
        return RECIPE_PREFIX + self.id.replace("-", "_")


class RecipeRegistry:
    """The set of recipes this install publishes."""

    def __init__(self) -> None:
        self._recipes: dict[str, Recipe] = {}

    def register(self, recipe: Recipe) -> None:
        if recipe.id in self._recipes:
            raise ValueError(f"recipe {recipe.id!r} is already registered")
        self._recipes[recipe.id] = recipe

    def get(self, recipe_id: str) -> Recipe | None:
        return self._recipes.get(recipe_id)

    def all(self) -> list[Recipe]:
        return [self._recipes[k] for k in sorted(self._recipes)]

    def ids(self) -> list[str]:
        return sorted(self._recipes)

    def for_skill(self, skill: str) -> list[str]:
        return sorted(r.id for r in self._recipes.values() if any(s.skill == skill for s in r.steps))


_default: RecipeRegistry | None = None


def default_recipes() -> RecipeRegistry:
    """The shipped catalogue (``recipe_catalog``), built once per process."""
    global _default
    if _default is None:
        from axiom.infra.recipe_catalog import RECIPES

        reg = RecipeRegistry()
        for recipe in RECIPES:
            reg.register(recipe)
        _default = reg
    return _default


# ---------------------------------------------------------------- Arazzo projection


def to_arazzo(recipe: Recipe, *, tool_name=lambda skill: skill) -> dict[str, Any]:
    """The recipe as one workflow in the Arazzo 1.1 shape (AEOS 0.2 §4.11.6).

    A projection, not a second source of truth: the recipe stays the authority and
    this is regenerated from it. Arazzo fields used: ``workflowId``, ``summary``,
    ``inputs`` (a JSON Schema), ``steps[].stepId`` / ``operationId`` /
    ``parameters`` / ``outputs`` / ``dependsOn``. Everything Arazzo has no word
    for rides in ``x-aeos-*`` extension fields, which Arazzo permits: the
    approval point, the undo hint, the rollback sentence. ``operationId`` names
    the MCP tool (``tool_name`` maps a skill to it), because the Arazzo
    ``sourceDescriptions`` for an MCP server are the server's own tool list.

    Wiring uses Arazzo runtime expressions: ``$inputs.<param>`` for a recipe
    parameter and ``$steps.<id>.outputs.<kind>`` for a ref an earlier step made.
    """
    steps: list[dict[str, Any]] = []
    for i, step in enumerate(recipe.steps):
        params = []
        depends: list[str] = []
        for name, value in step.inputs.items():
            text = str(value)
            ref = _REF.match(text)
            par = _PARAM.match(text)
            if ref:
                expr = f"$steps.{ref.group(1)}.outputs.{ref.group(2)}"
                if ref.group(1) not in depends:
                    depends.append(ref.group(1))
            elif par:
                expr = f"$inputs.{par.group(1)}"
            else:
                expr = text
            params.append({"name": name, "in": "body", "value": expr})
        entry: dict[str, Any] = {
            "stepId": step.id,
            "description": step.purpose,
            "operationId": tool_name(step.skill),
        }
        if params:
            entry["parameters"] = params
        if depends:
            entry["dependsOn"] = depends
        if step.expects:
            entry["outputs"] = {k: f"$response.body#/value/{k}" for k in step.expects}
        entry["x-aeos-approval"] = bool(step.approval)
        if step.undo:
            entry["x-aeos-undo"] = step.undo
        steps.append(entry)
    return {
        "workflowId": recipe.id,
        "summary": recipe.intent,
        "inputs": {
            "type": "object",
            "properties": {k: {"type": "string", "description": v} for k, v in recipe.params.items()},
        },
        "steps": steps,
        "x-aeos-rollback": recipe.rollback,
    }


# ---------------------------------------------------------------- check


@dataclass
class RecipeReport:
    recipe: str
    problems: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems and not self.missing

    @property
    def structurally_sound(self) -> bool:
        """No real defect; only placeholders are outstanding."""
        return not self.problems

    def as_dict(self) -> dict[str, Any]:
        return {"recipe": self.recipe, "ok": self.ok, "problems": list(self.problems), "missing": list(self.missing)}


def check(recipe: Recipe, registry: Any) -> RecipeReport:
    """Validate ``recipe`` against the live ``registry``."""
    rep = RecipeReport(recipe=recipe.id)
    produced_by: dict[str, set[str]] = {}
    for step in recipe.steps:
        spec = registry.spec(step.skill) if registry.has(step.skill) else None
        exists = registry.has(step.skill)
        if not exists:
            if step.skill in recipe.placeholders:
                rep.missing.append(step.skill)
                produced_by[step.id] = set(step.expects)
            else:
                rep.problems.append(f"step {step.id}: skill {step.skill!r} does not exist and is not a declared placeholder")
                produced_by[step.id] = set()
            _check_wiring(rep, recipe, step, None, produced_by)
            continue
        if step.skill in recipe.placeholders:
            rep.problems.append(
                f"step {step.id}: {step.skill!r} now exists; remove it from the recipe's placeholders"
            )
        if spec is None:
            rep.problems.append(f"step {step.id}: {step.skill!r} is registered without a SkillSpec, so it cannot be checked")
            produced_by[step.id] = set()
            continue
        produced_by[step.id] = {r.kind for r in (spec.produces or ())}
        risk = spec.effective_risk
        if risk is None:
            rep.problems.append(f"step {step.id}: {step.skill!r} declares no risk")
        if risk == "destructive" and not step.approval:
            rep.problems.append(
                f"step {step.id}: {step.skill!r} is destructive, so the step must carry an approval point"
            )
        if recipe.id not in spec.recipes:
            rep.problems.append(
                f"step {step.id}: {step.skill!r} does not list recipe {recipe.id!r} in its recipes"
            )
        _check_wiring(rep, recipe, step, spec, produced_by)
    return rep


def _check_wiring(
    rep: RecipeReport, recipe: Recipe, step: RecipeStep, spec: Any, produced_by: dict[str, set[str]]
) -> None:
    declared_inputs = set(spec.inputs) if spec is not None and spec.inputs else None
    for name, value in step.inputs.items():
        if declared_inputs is not None and name not in declared_inputs and name != APPROVAL_PARAM:
            rep.problems.append(f"step {step.id}: {step.skill!r} takes no input {name!r}")
        ref = _REF.match(str(value))
        if ref:
            src, kind = ref.groups()
            if kind not in produced_by.get(src, set()):
                rep.problems.append(
                    f"step {step.id}: input {name!r} wants {kind!r} from step {src!r}, which does not produce it"
                )
    if spec is None:
        return
    for pre in spec.requires or ():
        if not pre.consumes:
            continue
        wired = step.inputs.get(pre.input_name)
        if wired is None:
            rep.problems.append(
                f"step {step.id}: {step.skill!r} requires {pre.consumes!r} as {pre.input_name!r}, but the step supplies none"
            )
            continue
        ref = _REF.match(str(wired))
        if ref and ref.group(2) != pre.consumes:
            rep.problems.append(
                f"step {step.id}: {pre.input_name!r} needs a {pre.consumes!r} ref, not {ref.group(2)!r}"
            )


# ---------------------------------------------------------------- plan


def plan(recipe: Recipe, supplied: Mapping[str, Any], registry: Any, ctx: Any = None) -> dict[str, Any]:
    """A dry run: the steps, what each needs, what is unmet now, and where a person approves.

    Nothing is executed. A step whose inputs come from an earlier step's output
    cannot be evaluated yet, so it is marked ``deferred`` instead of guessed at.
    """
    missing_params = [p for p in recipe.params if p not in supplied]
    rows: list[dict[str, Any]] = []
    approval_points: list[str] = []
    for step in recipe.steps:
        spec = registry.spec(step.skill) if registry.has(step.skill) else None
        resolved: dict[str, Any] = {}
        deferred = False
        for name, value in step.inputs.items():
            if _REF.match(str(value)):
                deferred = True
                resolved[name] = f"<{value[1:]}>"
                continue
            par = _PARAM.match(str(value))
            if par:
                if par.group(1) in supplied:
                    resolved[name] = supplied[par.group(1)]
                else:
                    resolved[name] = f"<{value}>"
                    deferred = True
            else:
                resolved[name] = value
        row: dict[str, Any] = {
            "step": step.id,
            "skill": step.skill,
            "purpose": step.purpose,
            "inputs": resolved,
            "available": spec is not None,
            "approval": bool(step.approval),
            "undo": step.undo,
            "unmet": [],
            "needs": [],
            "deferred": deferred,
        }
        if spec is None:
            row["note"] = "missing on this base" if step.skill in recipe.placeholders else "unknown skill"
        else:
            row["risk"] = spec.effective_risk
            for pre in spec.requires or ():
                row["needs"].append(pre.id)
                if deferred or (pre.check is None and not pre.consumes):
                    continue
                met, why = check_precondition(pre, {k: v for k, v in resolved.items()}, ctx)
                if not met:
                    row["unmet"].append({"id": pre.id, "why": why or pre.description, "fix": fill_fix(pre.fix, resolved)})
        if step.approval:
            approval_points.append(step.id)
        rows.append(row)
    report = check(recipe, registry)
    return {
        "recipe": recipe.id,
        "intent": recipe.intent,
        "missing_params": missing_params,
        "steps": rows,
        "approval_points": approval_points,
        "rollback": recipe.rollback,
        "check": report.as_dict(),
    }


# ---------------------------------------------------------------- publication


def render_markdown(recipe: Recipe, *, cli: str = "axi") -> str:
    """The body of the recipe's MCP prompt: what to do, in order, with the wiring spelled out."""
    lines = [f"# Recipe {recipe.id}", "", recipe.intent, ""]
    if recipe.params:
        lines.append("Parameters: " + ", ".join(f"{k} ({v})" for k, v in recipe.params.items()))
        lines.append("")
    lines.append(
        "Follow the steps in order. Pass `_chain` = {\"chain_id\": \"<one id for this run>\", "
        "\"parent_step\": \"<step_id from the previous result>\"} on each call so the run can be reconstructed."
    )
    lines.append("")
    for i, step in enumerate(recipe.steps, 1):
        tool = "axiom_" + step.skill.replace(".", "__")
        gate = " **A person approves this step first.**" if step.approval else ""
        lines.append(f"{i}. `{tool}` - {step.purpose}.{gate}")
        for name, value in step.inputs.items():
            lines.append(f"   - `{name}` = {_describe_input(value)}")
        if step.approval:
            lines.append(
                f"   - Call it without `{APPROVAL_PARAM}` to open the plan, show the person the plan, "
                f"and call it again with the returned `{APPROVAL_PARAM}` once they approve."
            )
        if step.undo:
            lines.append(f"   - Undo: {step.undo}")
    if recipe.rollback:
        lines += ["", "Rollback: " + recipe.rollback]
    lines += ["", f"If a step refuses, read its `chain.unmet` list: each entry carries the exact fix. Dry run: `{cli} recipe plan {recipe.id}`."]
    return "\n".join(lines) + "\n"


def _describe_input(value: str) -> str:
    ref = _REF.match(str(value))
    if ref:
        return f"the `{ref.group(2)}` returned by step `{ref.group(1)}`"
    par = _PARAM.match(str(value))
    if par:
        return f"the recipe parameter `{par.group(1)}`"
    return f"`{value}`"


def prompt_description(recipe: Recipe) -> str:
    """A prompt description that cannot be mistaken for a harness built-in.

    Harnesses own short slash commands and some surface MCP prompts as slash
    commands. The name is already prefixed (``axiom_recipe__<id>``); the
    description says what the thing is and is not, in the form the collision
    rules ask for.
    """
    text = (
        f"Axiom recipe: {recipe.intent.rstrip('.')}. "
        "A checklist of Axiom tools to follow in order, not a harness built-in command."
    )
    clashes = sorted({w for w in re.split(r"[-_]", recipe.id) if w in HARNESS_RESERVED_WORDS})
    if clashes:
        text += " Not the harness's own " + ", ".join(f"/{w}" for w in clashes) + "."
    return text


__all__ = [
    "RECIPE_PREFIX",
    "Recipe",
    "RecipeRegistry",
    "RecipeReport",
    "RecipeStep",
    "check",
    "default_recipes",
    "to_arazzo",
    "plan",
    "prompt_description",
    "render_markdown",
]
