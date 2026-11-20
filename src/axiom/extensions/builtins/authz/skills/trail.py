# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``audit.trail`` — the action chain beneath the change log, and its verification.

Every write the platform makes through a skill (and every write refused at the
dispatch chokepoint) leaves one HMAC-chained record (``axiom.infra.audit_trail``).
This lists them newest first and, with ``verify``, recomputes every link: a
removed, reordered or edited record breaks the chain at a named ``seq``
(MVP-13-A2). Read-only.

CLI shape::

    axi audit trail [--limit 50] [--verify] [--json]
"""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    from axiom.extensions.builtins.settings.changes import audit_rows, verify

    try:
        limit = max(1, min(int(params.get("limit") or 50), 500))
    except (TypeError, ValueError):
        return SkillResult(ok=False, errors=["--limit is a whole number from 1 to 500"])
    if params.get("verify"):
        report = verify(ctx.state_dir)
        return SkillResult(
            ok=bool(report["ok"]),
            value={"resource": "trail_verify", **report},
            errors=[] if report["ok"] else [
                f"audit chain broken at record {report['broken_at']}: {report['reason']}"],
        )
    rows = audit_rows(ctx.state_dir, limit=limit)
    return SkillResult(value={"resource": "trail", "count": len(rows), "items": rows})
