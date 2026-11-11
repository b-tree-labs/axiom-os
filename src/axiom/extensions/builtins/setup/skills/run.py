# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``setup.run``, ``setup.check`` and ``setup.detect``: the whole walk, and what it found.

``setup.run`` is what the ``setup`` verb calls. It asks through ``ctx.user_prompt``
before each change, so a chat surface that can ask gets the same questions; with no
way to ask and no ``yes``, it changes nothing and says so on each line.
"""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from ..environment import resolve_env
from ..runner import run_steps
from ..steps import all_steps


def _steps(params: dict[str, Any]):
    only = [s for s in (params.get("only") or []) if s]
    steps = all_steps()
    if only:
        unknown = sorted(set(only) - {s.name for s in steps})
        if unknown:
            raise ValueError(
                f"no setup step named {', '.join(unknown)}; the steps are "
                + ", ".join(s.name for s in steps)
            )
        steps = [s for s in steps if s.name in only]
    return steps


def _ask(params: dict[str, Any], ctx: SkillContext | None):
    if params.get("yes"):
        return lambda _q: True
    prompt = getattr(ctx, "user_prompt", None) if ctx is not None else None
    if prompt is None:
        return None
    return lambda q: prompt(f"{q} [Y/n] ").strip().lower() not in ("n", "no")


def run(params: dict[str, Any], ctx: SkillContext | None = None) -> SkillResult:
    """Detect, then for each step: check, propose, ask, apply, verify."""
    try:
        steps = _steps(params)
    except ValueError as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    env = resolve_env(params)
    mode = "plan" if params.get("dry_run") else "apply"
    result = run_steps(env, steps, mode=mode, ask=_ask(params, ctx))
    lines = result["lines"]
    return SkillResult(
        ok=all(ln.green for ln in lines),
        value={
            "lines": [ln.as_dict() for ln in lines],
            "resume": result["resume"],
            "node": env.node,
            "site": env.site,
        },
    )


def check(params: dict[str, Any], ctx: SkillContext | None = None) -> SkillResult:
    """Every setup check, read-only: what ``doctor`` shows."""
    try:
        steps = _steps(params)
    except ValueError as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    env = resolve_env(params)
    lines = run_steps(env, steps, mode="check")["lines"]
    return SkillResult(ok=all(ln.green for ln in lines),
                       value={"lines": [ln.as_dict() for ln in lines], "node": env.node})


def detect(params: dict[str, Any], ctx: SkillContext | None = None) -> SkillResult:
    """What this computer is: OS, shell and its start-up file, installer, Docker, AI apps."""
    from ..detect import describe

    env = resolve_env(params)
    return SkillResult(ok=True, value=describe(env))


__all__ = ["check", "detect", "run"]
