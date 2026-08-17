# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``press.do_standard`` — execute one named standard bundle.

Per ADR-058: resolves the name to a sequence of skill invocations,
threads params through each step, collects per-step results into a
composed ``SkillResult``."""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    name = params.get("name")
    if not name:
        return SkillResult(ok=False, errors=["missing required param: name"])

    from axiom.extensions.builtins.publishing.standards import get_standard

    std = get_standard(name)
    if std is None:
        from axiom.extensions.builtins.publishing.standards import list_standards
        known = [s.name for s in list_standards()]
        return SkillResult(
            ok=False,
            errors=[f"unknown standard {name!r}; known: {known}"],
        )

    # Reach the live registry. Tests inject one through ctx.registry.
    registry = ctx.registry if ctx else None
    if registry is None:
        from axiom.extensions.builtins.publishing.skills import bind_default
        registry = bind_default()

    # Thread params through each skill in order. Per-skill overlays
    # merge over the caller's payload.
    caller_params = {k: v for k, v in params.items() if k != "name"}
    step_results: list[dict[str, Any]] = []
    last_value: dict[str, Any] = {}

    for skill_name, overlay in std.skills:
        merged = {**caller_params, **last_value, **overlay}
        try:
            result = registry.invoke(skill_name, merged, ctx)
        except KeyError as exc:
            return SkillResult(
                ok=False,
                errors=[
                    f"standard {std.name!r} references unknown skill "
                    f"{skill_name!r}: {exc}"
                ],
                value={"standard": std.name, "steps": step_results},
            )
        step_results.append({
            "skill": skill_name,
            "ok": bool(result.ok),
            "value": result.value,
            "errors": list(result.errors),
        })
        if not result.ok:
            return SkillResult(
                ok=False,
                errors=[
                    f"step {skill_name!r} failed in standard "
                    f"{std.name!r}: {result.errors}"
                ],
                value={"standard": std.name, "steps": step_results},
            )
        # Propagate the step's value forward as additional params for the
        # next skill (e.g. ``detect_version`` feeds the version into the
        # next ``draft`` invocation).
        if isinstance(result.value, dict):
            last_value = result.value

    return SkillResult(
        ok=True,
        value={
            "standard":  std.name,
            "description": std.description,
            "version":   std.version,
            "steps":     step_results,
        },
        actions_taken=[f"ran standard {std.name!r} ({len(step_results)} step(s))"],
    )


__all__ = ["run"]
