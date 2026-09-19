# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``notifications.channels`` skill — enumerate registered channel adapters."""

from __future__ import annotations

from typing import Any

from axiom.extensions.builtins.notifications.skills.send import _ctx
from axiom.infra.skills import SkillContext, SkillResult


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    providers = _ctx().registry.all()
    items = []
    for p in providers:
        caps = p.capabilities()
        items.append({
            "name": caps.name,
            "direction": caps.direction.value,
            "classification_ceiling": caps.classification_ceiling.value,
            "priority_levels": list(caps.priority_levels),
            "supports_threading": caps.supports_threading,
            "supports_acknowledge": caps.supports_acknowledge,
            "delivery_sla_p95_ms": caps.delivery_sla_p95_ms,
        })
    return SkillResult(
        ok=True,
        value={
            "resource": "channels",
            "count": len(items),
            "items": items,
        },
    )


__all__ = ["run"]
