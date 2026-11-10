# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Walk the steps: check each, and for each one that is not right, propose, ask, apply.

Three modes over one walk, so they cannot drift apart:

- ``apply`` (``setup``): asks before each change (``--yes`` answers for you).
- ``plan`` (``setup --dry-run``): says what each change would be and makes none.
- ``check`` (``doctor``): the same checks, no questions, no changes.

A step that needs another (sign-in needs the vault and the node) is not attempted
while that one is red; its line says what it is waiting for and repeats that line's
remedy, so the checklist always ends with one thing to do per red line.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from .environment import SetupEnv, resolve_env, save_state
from .steps import Line, Step, all_steps

#: Set when setup re-runs itself on freshly upgraded code, so it never loops on an
#: upgrade that did not take.
RESUMED_ENV = "AXI_SETUP_RESUMED"

Ask = Callable[[str], bool] | None


def run_steps(
    env: SetupEnv,
    steps: list[Step],
    *,
    mode: str = "apply",
    ask: Ask = None,
) -> dict[str, Any]:
    """Run *steps* in order. Returns ``{"lines": [Line], "resume": bool}``."""
    if mode not in ("apply", "plan", "check"):
        raise ValueError(f"mode must be apply, plan or check, not {mode!r}")
    done: dict[str, Line] = {}
    out: list[Line] = []
    resume = False
    for step in steps:
        blocker = next((done[n] for n in step.needs if n in done and not done[n].green), None)
        if blocker is not None:
            line = Line(step.name, step.title, False, f"waits for: {blocker.title}",
                        f"fix the {blocker.title} line first")
        else:
            line = _one(env, step, mode=mode, ask=ask)
        done[step.name] = line
        out.append(line)
        if mode == "apply" and env.seams.get("upgraded") and not os.environ.get(RESUMED_ENV):
            resume = True
            break
    if mode != "check":
        save_state(env.state_dir, node=env.node, site=env.site, lines=out)
    return {"lines": out, "resume": resume}


def _one(env: SetupEnv, step: Step, *, mode: str, ask: Ask) -> Line:
    try:
        line = step.check(env)
    except Exception as exc:  # noqa: BLE001 - one broken check costs its own line only
        return Line(step.name, step.title, False, f"could not be checked ({type(exc).__name__}: {exc})",
                    f"run `{env.cli} doctor` and report this line")
    if line.green or mode == "check":
        return line
    if step.name == "upgrade" and os.environ.get(RESUMED_ENV):
        return line  # already tried once this run; the line carries the manual command
    try:
        plan = step.plan(env)
    except Exception:  # noqa: BLE001 - no plan means the remedy is the person's
        plan = None
    if plan is None:
        return line
    if mode == "plan":
        env.say(f"  would {plan}")
        return replace(line, fix=f"would {plan} (run without --dry-run)")
    if ask is None:
        return replace(
            line,
            status=line.status + " (not changed: no terminal to ask)",
            fix=f"run `{env.cli} setup --yes` to allow it, or run setup in a terminal",
        )
    if not ask(f"{step.title}: {plan}?"):
        return replace(line, status=line.status + " (you said no)")
    env.say(f"  {step.title}: working ...")
    try:
        return step.apply(env)
    except Exception as exc:  # noqa: BLE001 - say what broke, keep going with the rest
        return Line(step.name, step.title, False, f"failed: {type(exc).__name__}: {exc}",
                    f"run `{env.cli} setup` again; if it repeats, run `{env.cli} doctor` and report it")


def render_checklist(lines: list[Line], *, cli: str) -> str:
    """Green lines for what works; for each red line, the one thing to do about it."""
    width = max((len(ln.title) for ln in lines), default=8) + 2
    rows = ["", "Checklist"]
    for ln in lines:
        mark = "✓" if ln.ok else ("·" if ln.skipped else "✗")
        rows.append(f"  {mark} {ln.title.ljust(width)}{ln.status}")
        if not ln.green and ln.fix:
            rows.append(f"    {' ' * width}→ {ln.fix}")
    red = [ln for ln in lines if not ln.green]
    rows.append("")
    if not red:
        rows.append(f"All set. `{cli} doctor` re-checks any time without changing anything.")
    else:
        n = len(red)
        rows.append(
            f"{n} thing{'s' if n != 1 else ''} to fix (the arrows above). "
            f"Run `{cli} setup` again afterwards: it repairs what it can and skips what is done."
        )
    return "\n".join(rows)


def doctor_rows(params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """The setup checks as ``doctor`` rows. Read-only; never raises."""
    try:
        env = resolve_env(dict(params or {}))
        env.say = lambda _m: None
        result = run_steps(env, all_steps(), mode="check")
    except Exception:  # noqa: BLE001 - doctor must survive a broken setup module
        return []
    return [ln.as_doctor_row() for ln in result["lines"]]


__all__ = ["RESUMED_ENV", "doctor_rows", "render_checklist", "run_steps"]
