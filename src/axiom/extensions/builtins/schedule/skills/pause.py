# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``schedule.pause`` — pause an active schedule."""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    sid = params.get("schedule_id")
    reason = params.get("reason", "")
    if not sid:
        return SkillResult(ok=False, errors=["schedule.pause requires --schedule-id"])
    if not reason:
        return SkillResult(ok=False, errors=["schedule.pause requires --reason"])
    return SkillResult(
        ok=False,
        errors=["schedule.pause: PULSE-1 wiring in progress; driven by test_api."],
    )
