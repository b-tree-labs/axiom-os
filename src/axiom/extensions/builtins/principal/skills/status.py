# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``principal.status`` — who this harness works for, and what actually delivers."""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from ..store import load


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    principal = load(config_dir=params.get("config_dir"))
    if principal is None:
        return SkillResult(
            ok=True,
            value={"status": "absent", "endpoints": []},
            actions_taken=[
                "No principal is configured for this harness. "
                "Run `axi principal setup` — until then nothing knows who to tell."
            ],
        )

    endpoints = [
        {
            "kind": e.kind,
            "address": e.address,
            "verified": e.is_verified,
            "health": e.health.value,
            "reason": e.health_reason,
        }
        for e in principal.endpoints
    ]
    verified = [e["kind"] for e in endpoints if e["verified"]]
    unproven = [e["kind"] for e in endpoints if not e["verified"]]

    summary = f"{principal.display_name} ({principal.refresh_status().value})"
    if verified:
        summary += f"; delivering on {', '.join(verified)}"
    if unproven:
        summary += f"; unproven (never used for routing): {', '.join(unproven)}"

    return SkillResult(
        ok=True,
        value={
            "handle": principal.handle,
            "directory_ref": principal.directory_ref,
            "display_name": principal.display_name,
            "status": principal.status.value,
            "quiet_hours": list(principal.quiet_hours) if principal.quiet_hours else None,
            "endpoints": endpoints,
            "preferences": [
                {
                    "topic_class": p.topic_class,
                    "ranked_kinds": p.ranked_kinds,
                    "urgency_floor": p.urgency_floor,
                }
                for p in principal.preferences
            ],
        },
        actions_taken=[summary],
    )
