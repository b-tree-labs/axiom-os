# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``schedule.resume`` — resume a paused schedule."""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    sid = params.get("schedule_id")
    if not sid:
        return SkillResult(ok=False, errors=["schedule.resume requires --schedule-id"])
    return SkillResult(
        ok=False,
        errors=["schedule.resume: PULSE-1 wiring in progress; driven by test_api."],
    )
