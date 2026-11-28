# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``node.dependency_*`` — declare, list and remove a node's dependencies."""

from __future__ import annotations

from typing import Any

from axiom.infra import readiness
from axiom.infra.skills import SkillContext, SkillResult


def declare(params: dict[str, Any], ctx: SkillContext | None = None) -> SkillResult:
    try:
        dep = readiness.declare(
            str(params.get("name") or ""),
            http=params.get("http"),
            tcp=params.get("tcp"),
            command=params.get("command"),
            file=params.get("file"),
            timeout=params.get("timeout"),
            interval=params.get("interval"),
        )
    except ValueError as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    return SkillResult(
        value={"name": dep.name, "probe": dep.describe(), "timeout": dep.timeout},
        actions_taken=[f"declared {dep.name}: ready when {dep.describe()}"],
    )


def list_declared(params: dict[str, Any], ctx: SkillContext | None = None) -> SkillResult:
    try:
        deps = readiness.dependencies()
    except ValueError as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    return SkillResult(
        value=[
            {"name": d.name, "probe": d.describe(), "timeout": d.timeout, "interval": d.interval}
            for d in deps.values()
        ]
    )


def remove(params: dict[str, Any], ctx: SkillContext | None = None) -> SkillResult:
    name = str(params.get("name") or "")
    if not readiness.undeclare(name):
        return SkillResult(ok=False, errors=[f"this node declares no dependency named {name!r}"])
    return SkillResult(value={"name": name}, actions_taken=[f"removed {name}"])


__all__ = ["declare", "list_declared", "remove"]
