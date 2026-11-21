# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``principal.route`` — explain which channel a topic would take, and why.

The explanation is the feature. Routing that cannot be interrogated after an
incident is routing nobody trusts during one.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from ..routing import route as route_decision
from ..store import load


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    principal = load(config_dir=params.get("config_dir"))
    if principal is None:
        return SkillResult(ok=False, errors=["no principal configured; run `axi principal setup`"])

    topic = params.get("topic") or "*"
    urgency = int(params.get("urgency", 5))
    at = params.get("at")
    now = datetime.fromisoformat(at) if at else datetime.now()

    decision = route_decision(principal, topic=topic, urgency=urgency, now=now)
    return SkillResult(
        ok=True,
        value={
            "topic": topic,
            "urgency": urgency,
            "chosen": decision.endpoint.kind if decision.endpoint else None,
            "escalation": [e.kind for e in decision.escalation],
            "rejected": decision.rejected_reasons,
            "deferred_until": decision.deferred_until.isoformat()
            if decision.deferred_until
            else None,
            "unreachable": decision.unreachable,
            "explain": decision.explain,
        },
        actions_taken=[decision.explain],
    )
