# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``video.verify``: one frame, recorded; the source is active only while its last verify passed."""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from ..connector import verify
from ._common import need_name, source_name, state_dir


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    name = source_name(params)
    if not name:
        return need_name("verify")
    record = verify(name, state_dir=state_dir(ctx))
    value = {"name": name, **record.as_dict()}
    if record.ok:
        report = record.report or {}
        return SkillResult(
            ok=True,
            value=value,
            actions_taken=[
                f"recorded a passing verify of {name}: {report.get('codec')} {report.get('width')}x{report.get('height')}"
            ],
        )
    return SkillResult(
        ok=False,
        value=value,
        errors=[f"{name} did not verify: {record.error}. Fix: {record.fix}"],
        actions_taken=[f"recorded the failed verify of {name}"]
        if record.code != "not-declared"
        else [],
    )


__all__ = ["run"]
