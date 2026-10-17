# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""`process.show`: one record, its live state and its findings."""

from __future__ import annotations

from typing import Any

from axiom.infra.process_containment import cli_name
from axiom.infra.skills import SkillContext, SkillResult

from ._common import describe, next_steps, snapshot


def show(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    ident = str(params.get("id", "")).strip()
    reg, _rows, report = snapshot(ctx)
    rec = reg.get(ident) if ident else None
    if rec is None:
        return SkillResult(ok=False, errors=[
            f"no unique registered process matches {ident!r}; run '{cli_name()} process ls'"])
    from axiom.infra import process_registry as pr

    row = describe(rec, pr.probe(rec.pid))
    findings = [f.to_dict() for f in report.findings if f.record_id == rec.id]
    return SkillResult(ok=True, value={
        "process": row, "record": rec.to_dict(), "findings": findings,
        "next": next_steps(
            f"{{cli}} process stop {rec.id}          dry-run the stop",
            f"{{cli}} process stop {rec.id} --kill   TERM the group, wait, then KILL",
            "{cli} process ls                list everything",
        ),
    })
