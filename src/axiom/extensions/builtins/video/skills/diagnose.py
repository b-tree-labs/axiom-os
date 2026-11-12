# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``video.diagnose``: the ladder, whose first failing rung names its fix. Changes nothing."""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from ..diagnose import diagnose
from ._common import need_name, source_name, state_dir


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    name = source_name(params)
    if not name:
        return need_name("diagnose")
    ladder = diagnose(name, state_dir=state_dir(ctx))
    if ladder["ok"]:
        return SkillResult(value=ladder)
    first = next(s for s in ladder["steps"] if s["ok"] is False)
    return SkillResult(
        ok=False, value=ladder, errors=[f"{first['step']}: {first['detail']}. Fix: {first['fix']}"]
    )


__all__ = ["run"]
