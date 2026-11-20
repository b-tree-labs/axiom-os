# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``node.window``: open, extend, close or list maintenance windows on this node."""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from .. import windows
from ._handoff import next_step
from .inventory import _state_dir

ACTIONS = ("status", "open", "extend", "close", "check")


def _who(ctx: SkillContext | None, params: dict[str, Any]) -> str:
    if params.get("owner"):
        return str(params["owner"])
    actor = getattr(ctx, "actor", None) if ctx is not None else None
    if actor:
        return str(actor)
    p = getattr(ctx, "principal", None)
    return str(getattr(p, "handle", "") or getattr(p, "principal_id", "") or "")


def run(params: dict[str, Any], ctx: SkillContext | None = None) -> SkillResult:
    action = str(params.get("action") or "status")
    if action not in ACTIONS:
        return SkillResult(
            ok=False,
            errors=[f"not a window action: {action!r}; the actions are {', '.join(ACTIONS)}"],
        )
    sd = _state_dir(ctx)
    scope = params.get("scope") or []
    if isinstance(scope, str):
        scope = [s.strip() for s in scope.split(",")]
    try:
        if action == "open":
            w = windows.open_window(
                sd,
                owner=_who(ctx, params),
                scope=scope,
                expect=str(params.get("expect") or ""),
                note=str(params.get("note") or ""),
            )
            return SkillResult(
                ok=True,
                value={"window": w.view()},
                actions_taken=[
                    f"opened window {w.window_id} on {', '.join(w.scope)} until {w.view()['expected_end_at']}"
                ],
                next=[
                    next_step(
                        "node.window", f"close it when done: node window close --id {w.window_id}"
                    )
                ],
            )
        if action == "extend":
            w = windows.extend(
                sd,
                str(params.get("id") or ""),
                by=_who(ctx, params),
                expect=str(params.get("expect") or ""),
            )
            return SkillResult(
                ok=True,
                value={"window": w.view()},
                actions_taken=[f"extended window {w.window_id} to {w.view()['expected_end_at']}"],
            )
        if action == "close":
            w = windows.close_window(
                sd,
                str(params.get("id") or ""),
                by=_who(ctx, params),
                outcome=str(params.get("outcome") or ""),
            )
            return SkillResult(
                ok=True, value={"window": w.view()}, actions_taken=[f"closed window {w.window_id}"]
            )
    except ValueError as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    if action == "check":
        blocking = windows.blocking(sd, scope)
        return SkillResult(
            ok=True,
            value={"clear": not blocking, "blocking": [w.view() for w in blocking]},
            actions_taken=["checked for windows on " + (", ".join(scope) or "node")],
        )
    views = [w.view() for w in windows.load(sd)]
    return SkillResult(
        ok=True,
        value={
            "windows": views,
            "open": sum(1 for v in views if v["state"] in ("open", "overdue")),
        },
        actions_taken=["listed maintenance windows"],
    )


__all__ = ["ACTIONS", "run"]
