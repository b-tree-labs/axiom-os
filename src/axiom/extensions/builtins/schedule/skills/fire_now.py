# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``schedule.fire-now`` — manual fire of a registered schedule.

Subject to authz per spec-axiom-schedule §8. PULSE-1 stance per
spec §12 Q-6: respects ``not_before`` / ``not_after``.
"""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    sid = params.get("schedule_id")
    if not sid:
        return SkillResult(ok=False, errors=["schedule.fire-now requires --schedule-id"])
    return SkillResult(
        ok=False,
        errors=[
            "schedule.fire-now: PULSE-1 wiring in progress; driven by test_engine_tick."
        ],
    )
