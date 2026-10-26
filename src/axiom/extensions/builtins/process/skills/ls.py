# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""`process.ls`: what the harness has started, and what is wrong with it."""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from ._common import next_steps, snapshot

_MATCH_FIELDS = ("id", "kind", "agent", "extension", "session", "purpose", "command")


def _matches(row: dict, needle: str) -> bool:
    return any(needle in str(row.get(f) or "").lower() for f in _MATCH_FIELDS)


def ls(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    _reg, rows, report = snapshot(ctx)
    needle = str(params.get("match") or "").strip().lower()
    if needle:
        rows = [r for r in rows if _matches(r, needle)]
    flagged: dict[str, list[str]] = {}
    for f in report.findings:
        flagged.setdefault(f.record_id, []).append(f.kind)
    for r in rows:
        r["findings"] = flagged.get(r["id"], [])
    unregistered = [f.to_dict() for f in report.findings if f.kind == "unregistered"]
    steps = ["{cli} process show <id>        full record and findings"]
    if report.findings or report.damaged:
        steps.append("{cli} process sweep            report every finding (changes nothing)")
    if rows:
        steps.append("{cli} process stop <id>       dry-run; add --kill to actually stop it")
    steps.append("{cli} process panic            dry-run emergency stop")
    value = {
        "processes": rows, "unregistered": unregistered, "damaged": report.damaged,
        "next": next_steps(*steps),
    }
    if len(rows) == 1:
        # Exactly one candidate is a reference a later step can use. Several are not: choosing among
        # them is the caller's decision, so none is offered and `match` is how to narrow.
        value["process_id"] = rows[0]["id"]
    return SkillResult(ok=True, value=value)
