# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``data.adopt_watch``: PLINTH's check on an unattended adoption copy.

Run by the orchestrator every few minutes while an adoption job exists. Quiet
when the copy is healthy or none is in progress; otherwise it raises a fault,
attributed to us (ADR-182), through HERALD and the node's health and heartbeat.
See :mod:`..timeseries.adopt_watch` for the four faults.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from axiom.infra.skills import SkillContext, SkillResult


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    import os

    from ..timeseries import adopt_watch as w
    from ..timeseries.contract import parse_duration

    if params.get("acknowledge_mismatch"):
        table = str(params["acknowledge_mismatch"])
        cleared = w.acknowledge(ctx.state_dir, table)
        return SkillResult(ok=True, value={"acknowledged": cleared},
                           actions_taken=[f"{table}: mismatch " + ("acknowledged" if cleared else "was not raised")])

    stall = params.get("stall_after") or os.environ.get(w.STALL_ENV)
    stall_after = (parse_duration(stall) if stall and not str(stall).isdigit()
                   else timedelta(seconds=int(stall)) if stall else w.DEFAULT_STALL_AFTER)
    report = w.watch(ctx.state_dir, params.get("source") or w.PgSource(), now=params.get("now"),
                     stall_after=stall_after)
    value = {"faults": [f.__dict__ for f in report.faults], "raised": report.raised, "cleared": report.cleared}
    # Quiet when there is nothing to say.
    actions = [f.message for f in report.faults]
    return SkillResult(ok=True, value=value, actions_taken=actions)


__all__ = ["run"]
