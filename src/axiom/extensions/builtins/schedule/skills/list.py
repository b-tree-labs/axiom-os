# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``schedule.list`` — enumerate registered schedules."""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    state = params.get("state")
    # PULSE-1 stub: returns empty list so the CLI exits 0 in smoke tests
    # against an uninitialized DB. Full query lands as test_api drives it.
    return SkillResult(
        ok=True,
        value={"schedules": [], "state_filter": state},
        actions_taken=["schedule.list: returned empty inventory (PULSE-1 stub)"],
    )
