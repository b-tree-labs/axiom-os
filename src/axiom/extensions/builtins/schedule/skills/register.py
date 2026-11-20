# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``schedule.register`` — register a new schedule from CLI / agent.

Params:
    cadence:     "interval:1h" | "cron:0 */6 * * *" | "one_shot"
    action:      dotted callable ref
    description: human description (optional)
"""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    cadence = params.get("cadence")
    action = params.get("action")
    if not cadence or not action:
        return SkillResult(
            ok=False,
            errors=["schedule.register requires both --cadence and --action"],
        )
    # PULSE-1 stub: full registration wiring lands as test_api drives it.
    return SkillResult(
        ok=False,
        errors=[
            "schedule.register: PULSE-1 wiring in progress; "
            "see spec-axiom-schedule §3 + test_api."
        ],
    )
