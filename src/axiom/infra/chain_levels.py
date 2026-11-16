# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Chain-readiness levels and their conformance rules (AEOS 0.2 section 4.11).

An extension claims a level in ``[extension] chain_level`` (absent means ``A``);
this module decides, from the declarations alone, whether the claim holds.

* **A** - baseline. Nothing is required; a declaration that is present must be
  well formed and must not contradict itself.
* **B** - chain-ready. Every skill declares ``risk``; every skill that is not a
  read declares ``requires`` (an explicit empty list is an answer); every
  precondition carries an exact ``fix``; every produced ref is consumed or says
  what to do with it; every ``next_steps`` entry resolves; every ``recipes`` name
  is a recipe that exists.
* **C** - recipe-publishing. B, and the extension ships at least one recipe, each
  recipe valid against the skill catalog, with an approval point on every
  destructive step, and membership declared in both directions.

Everything here is static: it reads the manifest's ``[[extension.provides]]``
tables and recipe data files and never imports extension code, so ``axi ext
lint`` can run it on an extension nobody has installed. A built-in extension
whose skills are declared in code (``SkillSpec``) is read through
:func:`descriptor_from_spec`, the same shape, and the two are compared by rule
AEOS130 so the code and any manifest copy cannot drift.

Every finding carries the rule id and the exact fix.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

LEVELS = ("A", "B", "C")
#: First AEOS version that defines chain levels.
MIN_AEOS_VERSION = (0, 2)

_BACKTICK = re.compile(r"`[^`]+`")
_QUALIFIED = re.compile(r"[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*")


@dataclass(frozen=True)
class Rule:
    id: str
    level: str  # the level at which it starts to bind: A (always), B, C, or "-" (advisory)
    severity: str  # error | warning | info
    title: str


#: The rule catalogue. Order is presentation order; the spec table must list
#: exactly these ids (a test holds the two together).
RULES: tuple[Rule, ...] = (
    Rule("AEOS100", "A", "error", "chain_level is not one of A, B, C"),
    Rule("AEOS101", "A", "error", "a chain_level claim above A needs aeos_version >= 0.2"),
    Rule("AEOS102", "A", "error", "the claimed chain level is not achieved"),
    Rule("AEOS103", "-", "info", "a higher chain level is achieved than claimed"),
    Rule(
        "AEOS104",
        "-",
        "info",
        "this extension claims Level A and some skills do not yet declare for Level B",
    ),
    Rule("AEOS110", "B", "error", "a skill declares no risk"),
    Rule("AEOS111", "B", "error", "a skill that is not a read says nothing about requires"),
    Rule("AEOS112", "A", "error", "risk contradicts side_effects"),
    Rule("AEOS113", "A", "error", "a requires entry has no fix"),
    Rule(
        "AEOS114", "B", "error", "a produced ref is consumed by nothing and says nothing about use"
    ),
    Rule("AEOS115", "B", "error", "a next_steps target does not resolve"),
    Rule(
        "AEOS116",
        "B",
        "error",
        "a next_steps entry feeds or waits on a ref the skill does not produce",
    ),
    Rule("AEOS118", "B", "error", "recipes names a recipe that exists nowhere"),
    Rule("AEOS119", "A", "error", "a chaining declaration is malformed"),
    Rule("AEOS120", "C", "error", "no recipe is shipped"),
    Rule("AEOS121", "C", "error", "a recipe file is missing or unparseable"),
    Rule("AEOS122", "C", "error", "a recipe is malformed"),
    Rule("AEOS123", "C", "error", "a recipe does not validate against the skill catalog"),
    Rule("AEOS124", "C", "error", "recipe membership is not declared in both directions"),
    Rule(
        "AEOS125", "-", "warning", "a reference to another extension cannot be verified statically"
    ),
    Rule("AEOS127", "C", "error", "a destructive recipe step has no approval point"),
    Rule("AEOS130", "A", "error", "the manifest copy of a declaration differs from the code"),
    Rule(
        "AEOS140",
        "runtime",
        "error",
        "plan/apply contract: a destructive skill ran without an approved plan",
    ),
    Rule(
        "AEOS141",
        "runtime",
        "error",
        "requires contract: an unmet requirement had an effect, or lacked a fix",
    ),
    Rule(
        "AEOS142",
        "runtime",
        "error",
        "next_steps contract: a follow-up was not pre-filled from the result",
    ),
    Rule(
        "AEOS150",
        "runtime",
        "error",
        "audit contract: a chain id was not recorded or the record is not tamper-evident",
    ),
)
RULE_IDS = tuple(r.id for r in RULES)
_RULE = {r.id: r for r in RULES}


@dataclass(frozen=True)
class ChainFinding:
    code: str
    level: str  # A, B, C, or "-"
    severity: str
    skill: str
    message: str
    fix: str


@dataclass
class ChainAssessment:
    extension: str
    claimed: str
    achieved: str
    findings: list[ChainFinding] = field(default_factory=list)
    skills: int = 0
    recipes: int = 0

    def blocking(self, target: str) -> list[ChainFinding]:
        """Findings that are errors for ``target`` (a level the extension claims or is tried against)."""
        want = LEVELS.index(target)
        out = []
        for f in self.findings:
            if f.severity != "error":
                continue
            if f.level == "A" or (f.level in LEVELS and LEVELS.index(f.level) <= want):
                out.append(f)
        return out


# ---------------------------------------------------------------- descriptors


def qualify(name: str, extension: str) -> str:
    """The capability name: dotted as written, else ``<extension>.<name>``."""
    return name if "." in name else f"{extension}.{name}"


def _clean(d: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if v not in ("", None)}


def normalise_requires(items: Any) -> list[dict[str, Any]] | None:
    if items is None:
        return None
    return [
        _clean({k: it.get(k) for k in ("id", "description", "fix", "consumes", "param")})
        for it in items
    ]


def normalise_produces(items: Any) -> list[dict[str, Any]] | None:
    if items is None:
        return None
    return [_clean({k: it.get(k) for k in ("kind", "how_to_use", "field")}) for it in items]


def normalise_next(items: Any) -> list[dict[str, Any]] | None:
    if items is None:
        return None
    out = []
    for it in items:
        d = _clean({k: it.get(k) for k in ("target", "when", "why")})
        d.setdefault("when", "ok")
        if it.get("feeds"):
            d["feeds"] = dict(it["feeds"])
        out.append(d)
    return out


def descriptor_from_block(block: Mapping[str, Any], extension: str) -> dict[str, Any]:
    """A skill descriptor from a manifest ``kind = "skill"`` table."""
    name = str(block.get("name", ""))
    recipes = block.get("recipes")
    return {
        "name": name,
        "qualified": qualify(name, extension),
        "description": str(block.get("description", "")),
        "risk": block.get("risk"),
        "side_effects": block.get("side_effects"),
        "idempotent": block.get("idempotent"),
        "requires": normalise_requires(block.get("requires")),
        "produces": normalise_produces(block.get("produces")),
        "next_steps": normalise_next(block.get("next_steps")),
        "recipes": list(recipes) if recipes is not None else None,
        "source": "manifest",
    }


def descriptor_from_spec(spec: Any) -> dict[str, Any]:
    """The same descriptor from a registered ``SkillSpec`` (code is the source of truth)."""

    def req(p: Any) -> dict[str, Any]:
        return _clean(
            {
                "id": p.id,
                "description": p.description,
                "fix": p.fix,
                "consumes": p.consumes,
                "param": p.param,
            }
        )

    def prod(r: Any) -> dict[str, Any]:
        return _clean({"kind": r.kind, "how_to_use": r.how_to_use, "field": r.field})

    def nxt(n: Any) -> dict[str, Any]:
        d = _clean({"target": n.target, "when": n.when, "why": n.why})
        if n.feeds:
            d["feeds"] = dict(n.feeds)
        return d

    return {
        "name": spec.name,
        "qualified": spec.name,
        "description": spec.description or "",
        "risk": spec.risk,
        "side_effects": spec.side_effects,
        "idempotent": spec.idempotent,
        "requires": None if spec.requires is None else [req(p) for p in spec.requires],
        "produces": None if spec.produces is None else [prod(r) for r in spec.produces],
        "next_steps": None if spec.next_steps is None else [nxt(n) for n in spec.next_steps],
        "recipes": list(spec.recipes) if spec.recipes else None,
        "source": "code",
    }


def manifest_descriptors(manifest: Mapping[str, Any]) -> list[dict[str, Any]]:
    ext = manifest.get("extension") or {}
    name = str(ext.get("name", ""))
    return [
        descriptor_from_block(p, name)
        for p in (ext.get("provides") or [])
        if isinstance(p, Mapping) and p.get("kind") == "skill"
    ]


def effective_risk(d: Mapping[str, Any]) -> str | None:
    if d.get("risk"):
        return d["risk"]
    if d.get("side_effects") is False:
        return "read"
    return None


def spec_from_descriptor(d: Mapping[str, Any]) -> Any:
    """A checkable ``SkillSpec`` stand-in (no body, no precondition checks)."""
    from axiom.infra.skill_declarations import NextStep, Precondition, Ref
    from axiom.infra.skills import SkillResult, SkillSpec

    def body(params: dict, ctx: Any) -> Any:  # pragma: no cover - never called
        return SkillResult()

    return SkillSpec(
        name=d["qualified"],
        fn=body,
        description=d.get("description", ""),
        side_effects=d.get("side_effects"),
        idempotent=d.get("idempotent"),
        risk=d.get("risk"),
        requires=None
        if d.get("requires") is None
        else tuple(Precondition(**r) for r in d["requires"]),
        produces=None if d.get("produces") is None else tuple(Ref(**r) for r in d["produces"]),
        next_steps=None
        if d.get("next_steps") is None
        else tuple(NextStep(**n) for n in d["next_steps"]),
        recipes=tuple(d.get("recipes") or ()),
    )


# ---------------------------------------------------------------- recipes (data files)


@dataclass
class RecipeSource:
    name: str  # manifest name
    path: str
    recipe: Any = None  # axiom.infra.recipes.Recipe
    problem: str = ""
    code: str = ""


def load_recipes(manifest: Mapping[str, Any], root: Path) -> list[RecipeSource]:
    from axiom.infra.recipes import Recipe

    ext = manifest.get("extension") or {}
    out: list[RecipeSource] = []
    for p in ext.get("provides") or []:
        if not isinstance(p, Mapping) or p.get("kind") != "recipe":
            continue
        src = RecipeSource(name=str(p.get("name", "")), path=str(p.get("path", "")))
        out.append(src)
        file = root / src.path
        if not src.path or not file.is_file():
            src.code, src.problem = "AEOS121", f"recipe file not found: {src.path or '(no path)'}"
            continue
        try:
            data = tomllib.loads(file.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 - reported, never raised
            src.code, src.problem = "AEOS121", f"recipe file is not valid TOML: {exc}"
            continue
        try:
            src.recipe = Recipe.from_mapping(data)
        except (ValueError, TypeError) as exc:
            src.code, src.problem = "AEOS122", str(exc)
            continue
        if src.recipe.id != src.name:
            src.code, src.problem = (
                "AEOS122",
                f"recipe id {src.recipe.id!r} does not match its manifest name {src.name!r}",
            )
    return out


# ---------------------------------------------------------------- the assessment


class Host:
    """What lint may know about skills and recipes outside the extension (optional)."""

    def __init__(
        self, registry: Any = None, recipe_ids: Callable[[], set[str]] | None = None
    ) -> None:
        self.registry = registry
        self._recipe_ids = recipe_ids

    def has(self, name: str) -> bool:
        return self.registry is not None and self.registry.has(name)

    def recipe_ids(self) -> set[str]:
        return set(self._recipe_ids()) if self._recipe_ids else set()

    @property
    def available(self) -> bool:
        return self.registry is not None


def _add(out: list[ChainFinding], code: str, skill: str, message: str, fix: str) -> None:
    r = _RULE[code]
    out.append(ChainFinding(code, r.level, r.severity, skill, message, fix))


def _where(d: Mapping[str, Any]) -> str:
    return (
        "its [[extension.provides]] skill table"
        if d.get("source") == "manifest"
        else "its SkillSpec"
    )


def _risk_fix(d: Mapping[str, Any]) -> str:
    guess = "read" if d.get("side_effects") is False else "reversible-write"
    if d.get("source") == "manifest":
        return (
            f'add `risk = "{guess}"` to the skill table (use "destructive" if a mistaken call cannot be undone; '
            '"read" only if it changes nothing)'
        )
    return f'pass risk="{guess}" to its SkillSpec (risk="destructive" if a mistaken call cannot be undone)'


def _requires_fix(d: Mapping[str, Any]) -> str:
    if d.get("source") == "manifest":
        return (
            "add `requires = []` if nothing must hold before it runs, or one table per precondition: "
            '`requires = [{ id = "<id>", description = "<what must be true>", fix = "<the exact command or setting>" }]`'
        )
    return "pass requires=() if nothing must hold, or requires=(Precondition(id, description, check=..., fix=...),)"


def assess(
    extension: str,
    descriptors: list[dict[str, Any]],
    recipes: list[RecipeSource],
    *,
    claimed: str = "A",
    aeos_version: str | None = None,
    host: Host | None = None,
    parity: Mapping[str, dict[str, Any]] | None = None,
) -> ChainAssessment:
    """Evaluate every rule for one extension. ``claimed`` is what the manifest says."""
    host = host or Host()
    findings: list[ChainFinding] = []
    by_q = {d["qualified"]: d for d in descriptors}

    # -- the claim itself ------------------------------------------------
    if claimed not in LEVELS:
        _add(
            findings,
            "AEOS100",
            "-",
            f"chain_level {claimed!r} is not one of A, B, C",
            'set `chain_level = "A"`, `"B"` or `"C"` in [extension], or remove the key (absent means A)',
        )
    elif claimed != "A" and not _version_at_least(aeos_version):
        _add(
            findings,
            "AEOS101",
            "-",
            f"chain_level {claimed!r} is claimed but aeos_version is {aeos_version!r}; chain levels are defined from AEOS 0.2",
            'set `aeos_version = "0.2.0"` in [extension], or claim Level A',
        )

    if not descriptors and not recipes:
        # Nothing to declare: chain readiness does not apply, so the claim is trivially met.
        level = claimed if claimed in LEVELS else "A"
        return ChainAssessment(extension, level, level, findings)

    # -- structural rules (always bind) ----------------------------------
    for d in descriptors:
        q = d["qualified"]
        risk, side = d.get("risk"), d.get("side_effects")
        if risk == "read" and side is True:
            _add(
                findings,
                "AEOS112",
                q,
                f"{q}: risk is read but side_effects is true",
                "make them agree: either side_effects = false (it only reads) or a risk of reversible-write or destructive",
            )
        if risk in ("reversible-write", "destructive") and side is False:
            _add(
                findings,
                "AEOS112",
                q,
                f"{q}: risk is {risk} but side_effects is false",
                'make them agree: either side_effects = true or `risk = "read"`',
            )
        for pre in d.get("requires") or []:
            if not (pre.get("fix") or "").strip():
                _add(
                    findings,
                    "AEOS113",
                    q,
                    f"{q}: requires entry {pre.get('id', '?')!r} has no fix",
                    f'add `fix = "<the exact command or setting that satisfies it>"` to requires entry {pre.get("id", "?")!r}: '
                    "a refusal that does not say how to proceed is only a wall",
                )
        try:
            spec_from_descriptor(d)
        except (ValueError, TypeError) as exc:
            _add(
                findings,
                "AEOS119",
                q,
                f"{q}: a chaining declaration is malformed: {exc}",
                "correct the declaration named in the message; ids are lowercase, a next step needs a why, "
                "when is ok, error, always or produced:<kind>",
            )

    # -- Level B ---------------------------------------------------------
    consumed = _consumed_kinds(descriptors, recipes)
    for d in descriptors:
        q = d["qualified"]
        if d.get("risk") is None:
            _add(findings, "AEOS110", q, f"{q}: declares no risk", _risk_fix(d))
        er = effective_risk(d)
        if er != "read" and d.get("requires") is None:
            _add(
                findings,
                "AEOS111",
                q,
                f"{q}: is {'not a read' if er else 'of unknown risk'} and says nothing about requires",
                _requires_fix(d),
            )
        own = {r["kind"] for r in (d.get("produces") or [])}
        for ref in d.get("produces") or []:
            how = ref.get("how_to_use", "")
            documented = bool(_BACKTICK.search(how)) or any(
                t in by_q or host.has(t) for t in _QUALIFIED.findall(how)
            )
            if ref["kind"] not in consumed and not documented:
                _add(
                    findings,
                    "AEOS114",
                    q,
                    f"{q}: produces {ref['kind']!r}, which nothing consumes, and its how_to_use names no skill or command",
                    f"rewrite how_to_use for {ref['kind']!r} to name what to do with it, e.g. 'pass as thing to ns.verb' "
                    "or a `command`, or declare the consumer's requires/next_steps",
                )
        for step in d.get("next_steps") or []:
            target = step.get("target", "")
            is_skill = bool(re.match(r"^[a-z][a-z0-9_-]*\.[a-z][a-z0-9_.-]*$", target.strip()))
            if is_skill and target not in by_q:
                if host.has(target):
                    pass
                elif host.available:
                    _add(
                        findings,
                        "AEOS115",
                        q,
                        f"{q}: next step {target!r} is not a skill of this extension or of the host",
                        f"correct the target to a registered skill, or make it a command line; this extension's skills are: "
                        f"{', '.join(sorted(by_q)) or '(none)'}",
                    )
                else:
                    _add(
                        findings,
                        "AEOS125",
                        q,
                        f"{q}: next step {target!r} names another extension's skill and cannot be verified here",
                        "nothing to fix if the target exists where this extension is installed; lint re-checks it inside an Axiom install",
                    )
            for kind in (step.get("feeds") or {}).values():
                if kind not in own:
                    _add(
                        findings,
                        "AEOS116",
                        q,
                        f"{q}: next step {target!r} feeds {kind!r}, which this skill does not produce",
                        f'add a produces entry with kind = "{kind}", or correct the kind in feeds',
                    )
            when = step.get("when", "ok")
            if when.startswith("produced:") and when.partition(":")[2] not in own:
                _add(
                    findings,
                    "AEOS116",
                    q,
                    f"{q}: next step {target!r} waits on {when!r}, which this skill does not produce",
                    f'add a produces entry with kind = "{when.partition(":")[2]}", or change when',
                )
        shipped = {r.recipe.id for r in recipes if r.recipe is not None} | {r.name for r in recipes}
        for rid in d.get("recipes") or []:
            if rid not in shipped and rid not in host.recipe_ids():
                _add(
                    findings,
                    "AEOS118",
                    q,
                    f"{q}: recipes lists {rid!r}, which is not a recipe this extension ships or the host publishes",
                    f'ship it (`kind = "recipe"`, name = "{rid}") or remove {rid!r} from this skill\'s recipes',
                )

    # -- Level C ---------------------------------------------------------
    if not recipes:
        _add(
            findings,
            "AEOS120",
            "-",
            "no recipe is shipped",
            'add `[[extension.provides]] kind = "recipe"`, `name = "<id>"`, `path = "recipes/<id>.toml"` and the recipe file; '
            "`axi ext init` writes a stub",
        )
    good = []
    for src in recipes:
        if src.problem:
            _add(
                findings,
                src.code,
                src.name,
                f"recipe {src.name!r}: {src.problem}",
                "fix the recipe file named in the message (see the stub `axi ext init` writes for the shape)",
            )
        else:
            good.append(src.recipe)
    if good:
        _check_recipes(findings, extension, good, descriptors, by_q, host)

    if parity:
        for q, other in parity.items():
            d = by_q.get(q)
            if d is None:
                continue
            for key in ("risk", "requires", "produces", "next_steps", "recipes"):
                if d.get(key) != other.get(key):
                    _add(
                        findings,
                        "AEOS130",
                        q,
                        f"{q}: manifest {key} differs from the code's",
                        f"regenerate the manifest table from the SkillSpec (the code is the source of truth) so {key} matches",
                    )

    achieved = _achieved(findings)
    a = ChainAssessment(
        extension,
        claimed if claimed in LEVELS else "A",
        achieved,
        findings,
        skills=len(descriptors),
        recipes=len(recipes),
    )
    if a.claimed in LEVELS and LEVELS.index(achieved) > LEVELS.index(a.claimed):
        _add(
            findings,
            "AEOS103",
            "-",
            f"{extension}: achieves Level {achieved} and claims Level {a.claimed}",
            f'set `chain_level = "{achieved}"` (and aeos_version >= 0.2) in [extension] to claim it',
        )
    if a.claimed == "A" and achieved == "A" and descriptors:
        owing = [f for f in findings if f.severity == "error" and f.level == "B"]
        if owing:
            skills = sorted({f.skill for f in owing})
            _add(
                findings,
                "AEOS104",
                "-",
                f"{extension}: Level A; {len(skills)} of {len(descriptors)} skill(s) would need declarations for Level B",
                "run `axi ext lint --chain-level B` for each skill's finding and exact fix; nothing is required to stay at Level A",
            )
    return a


def _version_at_least(v: str | None) -> bool:
    m = re.match(r"^(\d+)\.(\d+)", v or "")
    return bool(m) and (int(m.group(1)), int(m.group(2))) >= MIN_AEOS_VERSION


def _achieved(findings: list[ChainFinding]) -> str:
    if any(f.severity == "error" and f.level in ("A", "B") for f in findings):
        return "A"
    if any(f.severity == "error" and f.level == "C" for f in findings):
        return "B"
    return "C"


def _consumed_kinds(descriptors: list[dict[str, Any]], recipes: list[RecipeSource]) -> set[str]:
    out: set[str] = set()
    for d in descriptors:
        for r in d.get("requires") or []:
            if r.get("consumes"):
                out.add(r["consumes"])
        for n in d.get("next_steps") or []:
            out.update((n.get("feeds") or {}).values())
    for src in recipes:
        if src.recipe is None:
            continue
        for step in src.recipe.steps:
            for value in step.inputs.values():
                m = re.match(r"^\$[a-z][a-z0-9_-]*\.([a-z][a-z0-9_-]*)$", str(value))
                if m:
                    out.add(m.group(1))
    return out


def _check_recipes(
    findings: list[ChainFinding],
    extension: str,
    recipes: list[Any],
    descriptors: list[dict[str, Any]],
    by_q: dict[str, dict[str, Any]],
    host: Host,
) -> None:
    from axiom.infra.recipes import check
    from axiom.infra.skills import SkillRegistry

    reg = SkillRegistry()
    for d in descriptors:
        try:
            reg.register_skill(spec_from_descriptor(d))
        except (ValueError, TypeError):
            continue  # already reported as AEOS119
    if host.registry is not None:
        for name in host.registry.list():
            if not reg.has(name):
                spec = host.registry.spec(name)
                if spec is not None:
                    reg.register_skill(spec)
    for recipe in recipes:
        rep = check(recipe, reg)
        for step in recipe.steps:
            d = by_q.get(step.skill)
            spec = reg.spec(step.skill) if reg.has(step.skill) else None
            risk = effective_risk(d) if d else (spec.effective_risk if spec is not None else None)
            if risk == "destructive" and not step.approval:
                _add(
                    findings,
                    "AEOS127",
                    recipe.id,
                    f"recipe {recipe.id!r} step {step.id!r}: {step.skill!r} is destructive and the step has no approval point",
                    f'set `approval = true` on step "{step.id}" in the recipe file: a person approves every destructive step',
                )
        for problem in rep.problems:
            if "so the step must carry an approval point" in problem:
                continue  # AEOS127, reported with its own fix
            if "does not list recipe" in problem:
                skill = next((s.skill for s in recipe.steps if f"step {s.id}:" in problem), "")
                _add(
                    findings,
                    "AEOS124",
                    recipe.id,
                    f"recipe {recipe.id!r}: {problem}",
                    f'add "{recipe.id}" to `recipes` on skill {skill!r}',
                )
                continue
            foreign = "does not exist and is not a declared placeholder" in problem
            if foreign and not host.available:
                _add(
                    findings,
                    "AEOS125",
                    recipe.id,
                    f"recipe {recipe.id!r}: {problem} (it may belong to another extension)",
                    "nothing to fix if the skill exists where this extension is installed; lint re-checks it inside an Axiom install",
                )
                continue
            _add(
                findings,
                "AEOS123",
                recipe.id,
                f"recipe {recipe.id!r}: {problem}",
                "correct the step in the recipe file (the skill name, an input name, or the `$step.kind` wiring) so it matches the skills' declarations",
            )
        for rid_skill in {s.skill for s in recipe.steps}:
            d = by_q.get(rid_skill)
            if d is None:
                continue
        for d in descriptors:
            if recipe.id in (d.get("recipes") or []) and not any(
                s.skill == d["qualified"] for s in recipe.steps
            ):
                _add(
                    findings,
                    "AEOS124",
                    recipe.id,
                    f"recipe {recipe.id!r}: skill {d['qualified']!r} lists it in recipes but is not one of its steps",
                    f'remove "{recipe.id}" from `recipes` on {d["qualified"]!r}, or add a step that calls it',
                )


__all__ = [
    "LEVELS",
    "MIN_AEOS_VERSION",
    "RULES",
    "RULE_IDS",
    "ChainAssessment",
    "ChainFinding",
    "Host",
    "RecipeSource",
    "Rule",
    "assess",
    "descriptor_from_block",
    "descriptor_from_spec",
    "effective_risk",
    "load_recipes",
    "manifest_descriptors",
    "qualify",
    "spec_from_descriptor",
]
