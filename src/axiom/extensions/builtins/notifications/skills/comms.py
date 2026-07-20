# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0
"""comms.configure skill — AXI's guided communications-config (ADR-074).

Called with no answers → returns the questions to ask (+ available channels).
Called with answers → assembles and returns a ChannelPolicy summary. NL (typed
or verbalized) maps the user's words onto the answers; AXI drives the loop.
"""
from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillResult

from ..comms_config import config_questions, policy_from_answers


def _available(params: dict) -> set[str]:
    av = params.get("available")
    if av:
        return set(av)
    # default to enabled connectors of kind channel_adapter
    try:
        from axiom.extensions.builtins.connect.connectors import register_builtin_connectors
        from axiom.infra.connector_fabric import default_fabric, default_state
        register_builtin_connectors(default_fabric())
        st = default_state()
        return {
            d.name.rsplit(".", 1)[-1]
            for d in default_fabric().catalog(kind="channel_adapter")
            if st.is_enabled(d.name)
        } or {"inbox"}
    except Exception:  # noqa: BLE001
        return {"inbox"}


def configure(params: dict[str, Any], ctx: Any = None) -> SkillResult:
    available = _available(params)
    answers = params.get("answers")
    if not answers:
        return SkillResult(ok=True, value={
            "available": sorted(available),
            "questions": config_questions(available),
        })
    policy = policy_from_answers(answers, available)
    return SkillResult(ok=True, value={
        "default_channels": list(policy.default_channels),
        "rules": [
            {"channels": list(r.channels), "status": [s.value for s in r.status] if r.status else None,
             "topics": list(r.topics) if r.topics else None, "min_priority": r.min_priority}
            for r in policy.rules
        ],
    }, actions_taken=["assembled communications policy from answers"])


__all__ = ["configure"]
