# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``node.wait_for`` — the readiness gate, as a skill."""

from __future__ import annotations

from typing import Any

from axiom.infra import readiness
from axiom.infra.skills import SkillContext, SkillResult


def run(params: dict[str, Any], ctx: SkillContext | None = None) -> SkillResult:
    name = str(params.get("name") or "")
    if not name:
        return SkillResult(ok=False, errors=["which dependency? give its declared name"])
    try:
        declared = readiness.dependencies()
    except ValueError as exc:
        return SkillResult(
            ok=False, errors=[f"the node's dependency declarations are unreadable: {exc}"]
        )
    dep = declared.get(name)
    if dep is None:
        known = ", ".join(sorted(declared)) or "none"
        return SkillResult(
            ok=False,
            errors=[
                f"this node declares no dependency named {name!r} (declared: {known}); "
                "declare it with `node dependency declare`"
            ],
        )
    timeout = params.get("timeout")
    interval = params.get("interval")
    try:
        waited = readiness.wait_for(
            dep,
            timeout=None if timeout is None else float(timeout),
            interval=None if interval is None else float(interval),
        )
    except readiness.NotReady as exc:
        return SkillResult(
            ok=False,
            errors=[str(exc)],
            value={
                "name": name,
                "probe": dep.describe(),
                "ready": False,
                "waited_s": exc.waited,
                "last": exc.last,
            },
        )
    return SkillResult(
        value={"name": name, "probe": dep.describe(), "ready": True, "waited_s": waited},
        actions_taken=[f"{name} ready after {waited:.1f}s ({dep.describe()})"],
    )


__all__ = ["run"]
