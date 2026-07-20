# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``data.list`` — list connectors / source-kinds / db-kinds / vector-kinds."""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from ..agents.plinth.connectors import list_connectors
from ..sources import default_source_kind_registry


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    resource = params.get("resource", "connectors")

    if resource == "connectors":
        rows = list_connectors(state_dir=ctx.state_dir)
        return SkillResult(
            ok=True,
            value={
                "resource": "connectors",
                "items": [
                    {"name": c.name, "kind": c.kind, "default_tier": c.default_tier,
                     "params_keys": sorted(c.params)}
                    for c in rows
                ],
            },
        )

    if resource == "kinds":
        registry = default_source_kind_registry()
        return SkillResult(
            ok=True,
            value={
                "resource": "kinds",
                "items": [
                    {"kind": k, "description": registry.get(k).description}
                    for k in registry.kinds()
                ],
            },
        )

    if resource == "db-kinds":
        from ..database import default_database_kind_registry

        registry = default_database_kind_registry()
        return SkillResult(
            ok=True,
            value={
                "resource": "db-kinds",
                "items": [
                    {"kind": k, "description": registry.get(k).description}
                    for k in registry.kinds()
                ],
            },
        )

    if resource == "vector-kinds":
        from ..vectorstore import default_vector_store_registry

        registry = default_vector_store_registry()
        return SkillResult(
            ok=True,
            value={
                "resource": "vector-kinds",
                "items": [
                    {"kind": k, "description": registry.get(k).description}
                    for k in registry.kinds()
                ],
            },
        )

    return SkillResult(
        ok=False,
        errors=[f"unknown resource {resource!r}; "
                "supported: connectors, kinds, db-kinds, vector-kinds"],
    )
