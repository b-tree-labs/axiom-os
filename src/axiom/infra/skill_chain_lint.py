# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Which skills still owe chaining declarations, per extension (ADR-185).

The sweep's work list and its enforcement are the same computation. This module
walks the real registry (every builtin extension's ``skills`` package, bound the
way the MCP server binds them) and reports, per skill, what it still owes:

``spec``       registered without a ``SkillSpec``, so it can declare nothing
``risk``       no explicit ``risk`` (``side_effects=False`` implies ``read`` at
               runtime, but the declaration itself is what is owed)
``requires``   not read, and silent about ``requires`` (declare it, or ``()``)
``next``       a ``next_steps`` entry that does not resolve (unregistered target,
               a ``feeds`` kind the skill does not produce, a ``produced:`` kind
               it does not declare)
``produces``   a produced ref kind nothing consumes and that does not say what
               to do with it
``recipes``    a ``recipes`` entry naming no recipe, or a recipe step the skill
               does not list

The debt list (``tests/infra/skill_chaining_debt.txt``) names each skill still
owing. The test fails when a listed skill is already clean (delete its line), and
when any other skill owes anything (a new skill is clean by default), so the list
only shrinks and says which skills, not how many.
"""

from __future__ import annotations

import importlib
import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from axiom.infra.skills import SkillRegistry

_EXT_RE = re.compile(r"axiom\.extensions\.builtins\.([a-z0-9_]+)\.")


@dataclass
class SkillAudit:
    skill: str
    extension: str
    path: str
    owes: list[str] = field(default_factory=list)  # codes
    detail: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.owes


@dataclass
class LintResult:
    skills: dict[str, SkillAudit]
    unbound_extensions: list[str]
    unloadable_extensions: dict[str, str]
    recipe_problems: dict[str, list[str]]

    def owing(self) -> dict[str, SkillAudit]:
        return {k: v for k, v in sorted(self.skills.items()) if not v.clean}

    def by_extension(self) -> dict[str, list[SkillAudit]]:
        out: dict[str, list[SkillAudit]] = defaultdict(list)
        for audit in self.owing().values():
            out[audit.extension].append(audit)
        return dict(sorted(out.items()))


def builtins_root() -> Path:
    import axiom.extensions.builtins as b

    return Path(b.__file__).parent


def load_registry() -> tuple[SkillRegistry, list[str], dict[str, str]]:
    """Every builtin extension's skills, bound into a fresh registry.

    Returns ``(registry, unbound, unloadable)``: extensions whose ``skills``
    package has no ``bind`` (their skill functions are not registered anywhere
    a surface can reach), and extensions whose package would not import.
    """
    registry = SkillRegistry()
    unbound: list[str] = []
    unloadable: dict[str, str] = {}
    root = builtins_root()
    for d in sorted(root.iterdir()):
        if not d.is_dir() or d.name.startswith("_"):
            continue
        if not ((d / "skills").exists() or (d / "skills.py").exists()):
            continue
        try:
            mod = importlib.import_module(f"axiom.extensions.builtins.{d.name}.skills")
        except Exception as exc:  # noqa: BLE001
            unloadable[d.name] = f"{type(exc).__name__}"
            continue
        binder = getattr(mod, "bind", None)
        if not callable(binder):
            unbound.append(d.name)
            continue
        try:
            binder(registry)
        except Exception as exc:  # noqa: BLE001
            unloadable[d.name] = f"bind: {type(exc).__name__}"
    return registry, unbound, unloadable


def claimed_levels() -> dict[str, str]:
    """``{extension: chain_level}`` as each builtin manifest claims it (absent means ``A``).

    The debt list below is the skill-level ratchet; this is the extension-level
    one (AEOS 0.2 section 4.11): an extension whose skills are all clean claims
    Level B, and ``axi ext lint`` verifies the claim.
    """
    import tomllib

    out: dict[str, str] = {}
    for d in sorted(builtins_root().iterdir()):
        manifest = d / "axiom-extension.toml"
        if not d.is_dir() or not manifest.exists():
            continue
        try:
            ext = tomllib.loads(manifest.read_text(encoding="utf-8")).get("extension", {})
        except Exception:  # noqa: BLE001
            continue
        out[d.name] = str(ext.get("chain_level", "A"))
    return out


def _fn_of(registry: SkillRegistry, name: str) -> Any:
    fn = registry._skills.get(name)  # noqa: SLF001 - the lint reads what the registry holds
    return getattr(fn, "_entry", None) or fn


def _location(registry: SkillRegistry, name: str) -> tuple[str, str]:
    """``(extension, source path relative to the repo's src)`` for a registered skill."""
    fn = _fn_of(registry, name)
    module = ""
    if isinstance(fn, str):
        module = fn.partition(":")[0]
    else:
        module = getattr(fn, "__module__", "") or ""
    m = _EXT_RE.search(module + ".")
    ext = m.group(1) if m else name.partition(".")[0]
    path = ""
    if module:
        try:
            spec = importlib.util.find_spec(module)  # type: ignore[attr-defined]
            if spec and spec.origin:
                path = _relative(spec.origin)
        except Exception:  # noqa: BLE001
            path = ""
    return ext, path


def _relative(origin: str) -> str:
    p = Path(origin)
    parts = p.parts
    if "src" in parts:
        return "/".join(parts[parts.index("src") :])
    return p.name


def lint(registry: SkillRegistry | None = None) -> LintResult:
    from axiom.infra.recipes import check, default_recipes

    unbound: list[str] = []
    unloadable: dict[str, str] = {}
    if registry is None:
        registry, unbound, unloadable = load_registry()
    recipes = default_recipes()

    # Which ref kinds are consumed anywhere.
    consumed: set[str] = set()
    for spec in registry.specs().values():
        for pre in spec.requires or ():
            if pre.consumes:
                consumed.add(pre.consumes)
        for step in spec.next_steps or ():
            consumed.update(step.feeds.values())
    for recipe in recipes.all():
        for step in recipe.steps:
            for value in step.inputs.values():
                m = re.match(r"^\$[a-z][a-z0-9_-]*\.([a-z][a-z0-9_-]*)$", str(value))
                if m:
                    consumed.add(m.group(1))

    audits: dict[str, SkillAudit] = {}
    for name in registry.list():
        ext, path = _location(registry, name)
        audit = SkillAudit(skill=name, extension=ext, path=path)
        audits[name] = audit
        spec = registry.spec(name)
        if spec is None:
            audit.owes.append("spec")
            audit.detail.append("registered with register(name, fn): no SkillSpec to declare on")
            continue
        # Explicit, not implied: ``side_effects=False`` lets the runtime treat an
        # undeclared skill as read, but the sweep's point is one vocabulary on
        # every skill, so the declaration itself is what is owed.
        risk = spec.effective_risk
        if spec.risk is None:
            audit.owes.append("risk")
            audit.detail.append("declare risk=read|reversible-write|destructive")
        if risk != "read" and spec.requires is None:
            audit.owes.append("requires")
            audit.detail.append("declare requires=(Precondition(...),) or an explicit requires=()")
        own = {r.kind for r in (spec.produces or ())}
        for step in spec.next_steps or ():
            if step.is_skill and not registry.has(step.target):
                audit.owes.append("next")
                audit.detail.append(f"next step {step.target!r} is not a registered skill")
            for kind in step.feeds.values():
                if kind not in own:
                    audit.owes.append("next")
                    audit.detail.append(f"next step {step.target!r} feeds {kind!r}, which this skill does not produce")
            if step.when.startswith("produced:") and step.when.partition(":")[2] not in own:
                audit.owes.append("next")
                audit.detail.append(f"next step {step.target!r} waits on {step.when!r}, which this skill does not produce")
        for ref in spec.produces or ():
            documented = bool(re.search(r"`[^`]+`", ref.how_to_use)) or any(
                registry.has(tok) for tok in re.findall(r"[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*", ref.how_to_use)
            )
            if ref.kind not in consumed and not documented:
                audit.owes.append("produces")
                audit.detail.append(
                    f"produced ref {ref.kind!r} is consumed by nothing and its how_to_use names no skill or command"
                )
        for rid in spec.recipes:
            if recipes.get(rid) is None:
                audit.owes.append("recipes")
                audit.detail.append(f"recipes lists {rid!r}, which is not a published recipe")
        for rid in recipes.ids():
            recipe = recipes.get(rid)
            if recipe and any(s.skill == name for s in recipe.steps) and rid not in spec.recipes:
                audit.owes.append("recipes")
                audit.detail.append(f"is a step of recipe {rid!r} but does not list it in recipes")
        audit.owes = sorted(set(audit.owes))

    recipe_problems = {r.id: check(r, registry).problems for r in recipes.all()}
    return LintResult(
        skills=audits,
        unbound_extensions=unbound,
        unloadable_extensions=unloadable,
        recipe_problems={k: v for k, v in recipe_problems.items() if v},
    )


# ---------------------------------------------------------------- debt list + report


DEBT_FILE = "tests/infra/skill_chaining_debt.txt"


def debt_lines(result: LintResult) -> list[str]:
    """One line per skill still owing: ``skill<TAB>path``. Sorted, so a diff is a review."""
    return [f"{a.skill}\t{a.path}" for a in result.owing().values()]


def unbound_lines(result: LintResult) -> list[str]:
    return [f"unbound:{e}" for e in sorted(result.unbound_extensions)]


def render_report(result: LintResult) -> str:
    owing = result.by_extension()
    total = sum(len(v) for v in owing.values())
    lines = [
        "# Skill chaining debt",
        "",
        "Generated by `python scripts/skill_chaining_debt.py`. Do not edit by hand.",
        "",
        "Every skill owes chaining declarations (ADR-185): `risk`, `requires` (or an explicit `()`),",
        "resolvable `next_steps`, consumed or documented `produces`, and recipe membership. This is the",
        "sweep's work list, grouped so one agent can take one extension at a time. The enforcing list is",
        "`tests/infra/skill_chaining_debt.txt`; delete a skill's line when it is clean.",
        "",
        f"**{total} skills owe declarations across {len(owing)} extensions** "
        f"({len(result.skills)} registered in total).",
        "",
        "Codes: `spec` registered without a SkillSpec; `risk` no risk declared; `requires` not read and silent",
        "about requires; `next` an unresolved follow-up; `produces` a ref nothing consumes; `recipes` membership.",
        "",
        "## Claimed chain level per extension",
        "",
        "An extension whose registered skills are all clean claims Level B in its manifest",
        "(`chain_level`, AEOS 0.2 section 4.11), and `axi ext lint` verifies the claim. An",
        "extension still owing declarations stays at Level A. The two ratchets agree: a skill",
        "on the list belongs to a Level A extension; a Level B extension has no skill on it.",
        "",
        "| Extension | Claimed level | Skills owing | Registered |",
        "|---|---|---|---|",
    ]
    registered: dict[str, int] = defaultdict(int)
    for a in result.skills.values():
        registered[a.extension] += 1
    claims = claimed_levels()
    for ext in sorted(registered):
        lines.append(f"| {ext} | {claims.get(ext, 'A')} | {len(owing.get(ext, []))} | {registered[ext]} |")
    lines += ["", "## By extension", "", "| Extension | Skills owing | Registered |", "|---|---|---|"]
    for ext, rows in owing.items():
        lines.append(f"| {ext} | {len(rows)} | {registered[ext]} |")
    lines.append("")
    for ext, rows in owing.items():
        lines += [f"### {ext}", "", "| Skill | Owes | File |", "|---|---|---|"]
        for a in sorted(rows, key=lambda r: r.skill):
            lines.append(f"| `{a.skill}` | {', '.join(a.owes)} | `{a.path}` |")
        lines.append("")
    if result.unbound_extensions:
        lines += [
            "## Extensions whose skill functions are not registered",
            "",
            "Their `skills` package has no `bind(registry)`, so no surface can reach the functions and the lint",
            "cannot see them. Registering them (with a `SkillSpec` each) is part of their sweep.",
            "",
        ]
        lines += [f"- `{e}`" for e in sorted(result.unbound_extensions)]
        lines.append("")
    if result.unloadable_extensions:
        lines += ["## Extensions whose skills would not import", ""]
        lines += [f"- `{e}`: {why}" for e, why in sorted(result.unloadable_extensions.items())]
        lines.append("")
    if result.recipe_problems:
        lines += ["## Recipe problems", ""]
        for rid, probs in sorted(result.recipe_problems.items()):
            lines += [f"- `{rid}`: {p}" for p in probs]
        lines.append("")
    machine = {
        "total_registered": len(result.skills),
        "owing": {ext: {a.skill: {"owes": a.owes, "file": a.path} for a in rows} for ext, rows in owing.items()},
        "unbound_extensions": sorted(result.unbound_extensions),
    }
    lines += ["## Machine-readable", "", "```json", json.dumps(machine, indent=1, sort_keys=True), "```", ""]
    return "\n".join(lines)


__all__ = [
    "DEBT_FILE",
    "LintResult",
    "SkillAudit",
    "claimed_levels",
    "debt_lines",
    "lint",
    "load_registry",
    "render_report",
    "unbound_lines",
]
