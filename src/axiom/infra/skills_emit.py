# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""ADR-063 — generate SKILL.md + AEOS ``[[extension.provides]]`` blocks
from registered :class:`~axiom.infra.skills.SkillSpec` instances.

Single source of truth: the Python ``SkillSpec`` call in each
extension's ``skills/__init__.py``. The generator walks extensions,
imports their ``skills`` package (calling ``bind_default`` to populate
the registry), and emits one SKILL.md per spec plus a delimited block
inside the ext's ``axiom-extension.toml``.

The skill registered at the end of this module is ``skills.emit_md`` —
ADR-056 says CLI verbs are thin wrappers, so ``axi skills emit-md`` is
exactly that.
"""

from __future__ import annotations

import difflib
import importlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from axiom.infra.skills import (
    SkillContext,
    SkillRegistry,
    SkillResult,
    SkillSpec,
)


# ---------- generator primitives ------------------------------------------


_GENERATED_BEGIN = "# BEGIN axi-skills-emit-md (generated — do not edit)"
_GENERATED_END = "# END axi-skills-emit-md"


def _short_name(spec_name: str) -> str:
    """``press.draft`` -> ``draft``."""
    _, _, leaf = spec_name.partition(".")
    return leaf or spec_name


def _render_inputs_yaml(inputs: dict[str, str]) -> str:
    if not inputs:
        return "inputs: []"
    lines = ["inputs:"]
    for k, v in inputs.items():
        lines.append(f"  - name: {k}")
        lines.append(f"    type: {v}")
    return "\n".join(lines)


def _render_allowed_tools_yaml(tools: Iterable[str]) -> str:
    tools = list(tools)
    if not tools:
        return "allowed-tools: []"
    return "allowed-tools: [" + ", ".join(tools) + "]"


def _render_skill_md(spec: SkillSpec, ext_version: str) -> str:
    body = spec.long_description.strip() or (
        f"Skill ``{spec.name}`` — see registration in the providing "
        f"extension's ``skills/__init__.py``. This SKILL.md is generated "
        f"by ``axi skills emit-md`` (ADR-063); edit the Python "
        f"``SkillSpec`` to change it."
    )
    frontmatter = (
        "---\n"
        f"name: {spec.name}\n"
        f"description: {spec.description}\n"
        f"version: {ext_version}\n"
        f"{_render_inputs_yaml(spec.inputs)}\n"
        "outputs:\n"
        "  - kind: SkillResult\n"
        f"{_render_allowed_tools_yaml(spec.allowed_tools)}\n"
        "---\n"
    )
    return frontmatter + "\n" + body + "\n"


def emit_md_for_spec(spec: SkillSpec, out_dir: Path, ext_version: str) -> Path:
    """Write SKILL.md for ``spec`` under ``out_dir``. Returns the path."""
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / "SKILL.md"
    target.write_text(_render_skill_md(spec, ext_version))
    return target


def _module_entry_for(spec: SkillSpec) -> str:
    """Return ``module.path:function`` for AEOS ``entry``.

    Falls back to ``<unknown>:?`` if the fn doesn't expose its module
    (lambdas in tests). Real call sites are top-level functions and
    will resolve cleanly.
    """
    mod = getattr(spec.fn, "__module__", None) or "<unknown>"
    fn_name = getattr(spec.fn, "__name__", None) or "?"
    return f"{mod}:{fn_name}"


def _render_provides_block(spec: SkillSpec) -> str:
    leaf = _short_name(spec.name)
    return (
        "[[extension.provides]]\n"
        "kind = \"skill\"\n"
        f"name = \"{spec.name}\"\n"
        f"entry = \"{_module_entry_for(spec)}\"\n"
        f"path = \"skills/{leaf}\"\n"
        f"description = \"{spec.description}\"\n"
    )


def _render_generated_section(specs: list[SkillSpec]) -> str:
    blocks = [_render_provides_block(s) for s in sorted(specs, key=lambda s: s.name)]
    return _GENERATED_BEGIN + "\n" + "\n".join(blocks) + _GENERATED_END + "\n"


def _splice_generated_section(toml_text: str, section: str) -> str:
    """Replace the delimited block in ``toml_text`` with ``section``.

    Append (with a leading blank line separator) if no block exists yet.
    Other content is byte-identical preserved — hand edits survive.
    """
    if _GENERATED_BEGIN in toml_text and _GENERATED_END in toml_text:
        begin = toml_text.index(_GENERATED_BEGIN)
        end = toml_text.index(_GENERATED_END) + len(_GENERATED_END) + 1
        return toml_text[:begin] + section + toml_text[end:]
    sep = "" if toml_text.endswith("\n\n") else ("\n" if toml_text.endswith("\n") else "\n\n")
    return toml_text + sep + section


# ---------- ext discovery -------------------------------------------------


@dataclass
class _ExtTarget:
    name: str
    root: Path
    version: str


def _builtins_root() -> Path:
    return Path(__file__).resolve().parent.parent / "extensions" / "builtins"


def _read_version(toml_path: Path) -> str:
    if not toml_path.exists():
        return "0.0.0"
    for line in toml_path.read_text().splitlines():
        s = line.strip()
        if s.startswith("version") and "=" in s:
            return s.split("=", 1)[1].strip().strip('"').strip("'")
    return "0.0.0"


def _discover_ext_targets(only: str | None, root_override: Path | None) -> list[_ExtTarget]:
    base = root_override or _builtins_root()
    targets: list[_ExtTarget] = []
    if root_override:
        # Single-tree override (used in tests).
        name = only or base.name
        targets.append(_ExtTarget(
            name=name,
            root=base,
            version=_read_version(base / "axiom-extension.toml"),
        ))
        return targets
    if not base.is_dir():
        return targets
    for child in sorted(base.iterdir()):
        if not child.is_dir():
            continue
        if only and child.name != only:
            continue
        if not (child / "skills").is_dir():
            continue
        targets.append(_ExtTarget(
            name=child.name,
            root=child,
            version=_read_version(child / "axiom-extension.toml"),
        ))
    return targets


def _load_specs_for_ext(ext: _ExtTarget, root_override: Path | None) -> list[SkillSpec]:
    """Import the ext's skills package + call ``bind_default``; return its specs.

    When ``root_override`` is set (tests), we expect the caller to have
    pre-populated the registry — we just return any specs whose name's
    namespace matches one of the ext's expected prefixes. Here, simpler:
    just return all specs registered in the default registry that start
    with no particular prefix (tests work with disposable registries
    passed via ``registry`` in params).
    """
    from axiom.infra.skills import default_registry

    if root_override is None:
        mod_path = f"axiom.extensions.builtins.{ext.name}.skills"
        try:
            mod = importlib.import_module(mod_path)
        except Exception:
            return []
        if hasattr(mod, "bind_default"):
            try:
                mod.bind_default()
            except Exception:
                pass
        reg = default_registry()
        return list(reg.specs().values())
    return []


# ---------- the skill -----------------------------------------------------


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """``skills.emit_md`` — emit SKILL.md + AEOS provides for every spec.

    Params:
      - ``ext`` (str, optional): restrict to a single extension name.
      - ``check`` (bool): if True, don't write — diff and fail on drift.
      - ``only`` (str, optional): comma-separated skill-name allowlist
        (used by CLI smoke tests on partial trees).
      - ``ext_root`` (str, internal/test): override builtins root.
      - ``ext_name`` (str, internal/test): name for the override target.
    """
    only_ext = params.get("ext")
    check = bool(params.get("check"))
    only_skills = params.get("only")
    only_skill_set: set[str] | None = (
        {s.strip() for s in only_skills.split(",") if s.strip()}
        if isinstance(only_skills, str) and only_skills
        else None
    )

    root_override = None
    if params.get("ext_root"):
        root_override = Path(params["ext_root"])
        only_ext = params.get("ext_name") or only_ext

    targets = _discover_ext_targets(only_ext, root_override)
    if not targets:
        return SkillResult(
            ok=False,
            errors=[f"no extension matched (ext={only_ext!r})"],
        )

    actions: list[str] = []
    drift_errors: list[str] = []

    for ext in targets:
        # For real ext discovery, load via import; for tests with an
        # explicit registry on the context, take that registry's specs.
        if root_override is not None:
            specs = list(ctx.registry.specs().values())
        else:
            specs = _load_specs_for_ext(ext, root_override)

        # Filter by ext-name prefix-match-ish: each spec's namespace
        # might not equal the ext name (press lives in publishing). So
        # we don't filter by ext here — every spec gets emitted under
        # ITS ext (the one we're iterating). For PR-1 with a single ext
        # target this is correct; PR-2 may add per-ext attribution.
        if only_skill_set is not None:
            specs = [s for s in specs if s.name in only_skill_set]

        # ---- SKILL.md files
        for spec in specs:
            leaf = _short_name(spec.name)
            out_dir = ext.root / "skills" / leaf
            target_md = out_dir / "SKILL.md"
            new_text = _render_skill_md(spec, ext.version)

            if check:
                if not target_md.exists():
                    drift_errors.append(
                        f"missing SKILL.md for {spec.name} at {target_md}"
                    )
                    continue
                cur = target_md.read_text()
                if cur != new_text:
                    diff = "\n".join(difflib.unified_diff(
                        cur.splitlines(),
                        new_text.splitlines(),
                        fromfile=str(target_md),
                        tofile=f"generated/{spec.name}",
                        lineterm="",
                    ))
                    drift_errors.append(
                        f"drift: {spec.name} SKILL.md differs from generated\n{diff}"
                    )
            else:
                out_dir.mkdir(parents=True, exist_ok=True)
                target_md.write_text(new_text)
                actions.append(f"wrote {target_md}")

        # ---- AEOS provides block
        toml_path = ext.root / "axiom-extension.toml"
        if not toml_path.exists():
            continue
        cur_toml = toml_path.read_text()
        section = _render_generated_section(specs) if specs else ""
        if specs:
            new_toml = _splice_generated_section(cur_toml, section)
        else:
            new_toml = cur_toml

        if check:
            if specs and new_toml != cur_toml:
                drift_errors.append(
                    f"drift: {toml_path} provides-block differs from generated"
                )
        else:
            if new_toml != cur_toml:
                toml_path.write_text(new_toml)
                actions.append(f"updated {toml_path}")

    if drift_errors:
        return SkillResult(ok=False, errors=drift_errors, actions_taken=actions)
    return SkillResult(ok=True, value={"actions": actions}, actions_taken=actions)


# ---------- registration --------------------------------------------------


_SPEC = SkillSpec(
    name="skills.emit_md",
    fn=run,
    description="Generate SKILL.md + AEOS provides blocks from SkillSpec metadata.",
    long_description=(
        "Walks every extension's skills package, imports the SkillSpec "
        "registrations, and emits one SKILL.md per skill plus a delimited "
        "[[extension.provides]] section in each axiom-extension.toml. "
        "Single source of truth is the Python SkillSpec; this generator + "
        "matching --check lint replaces three-way drift between code, "
        "manifest, and SKILL.md."
    ),
    inputs={
        "ext": "str | None",
        "check": "bool = False",
        "only": "str | None",
    },
    allowed_tools=(),
)


def bind(registry: SkillRegistry) -> None:
    """Register ``skills.emit_md`` into ``registry``."""
    if not registry.has(_SPEC.name):
        registry.register_skill(_SPEC)


def bind_default() -> SkillRegistry:
    from axiom.infra.skills import default_registry

    reg = default_registry()
    bind(reg)
    return reg


__all__ = ["bind", "bind_default", "emit_md_for_spec", "run"]
