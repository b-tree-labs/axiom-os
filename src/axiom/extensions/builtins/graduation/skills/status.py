# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``graduation.status`` — report the shadow outcome log.

The skill function ``(params, ctx) -> SkillResult`` per ADR-056. The CLI
verb ``axi graduation status`` is a thin wrapper that dispatches here; an
agent persona reaches the same surface. Read-only: it never writes.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from .. import shadow


def run(params: dict[str, Any], ctx: SkillContext | None = None) -> SkillResult:
    records = shadow.load_outcome_records()
    outcomes = Counter(str(r.get("outcome", "unknown")) for r in records)
    labels = Counter(str(r.get("label", "?")) for r in records)
    timestamps = [
        r["timestamp"]
        for r in records
        if isinstance(r.get("timestamp"), (int, float))
    ]
    backend = "postrule" if shadow._import_postrule() is not None else "jsonl"
    value = {
        "switch": shadow.SWITCH_NAME,
        "phase": shadow.PHASE,
        "enabled": shadow.shadow_enabled(),
        "backend": backend,
        "log_path": str(shadow.outcome_log_path()),
        "records": len(records),
        "outcomes": dict(outcomes),
        "labels": dict(labels),
        "first_timestamp": min(timestamps) if timestamps else None,
        "last_timestamp": max(timestamps) if timestamps else None,
    }
    return SkillResult(
        ok=True,
        value=value,
        actions_taken=[f"read {len(records)} outcome record(s) for {shadow.SWITCH_NAME}"],
    )


__all__ = ["run"]
