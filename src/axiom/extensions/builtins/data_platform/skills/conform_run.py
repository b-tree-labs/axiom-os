# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``data.conform_run`` — the real bronze→silver pass, as a skill.

The peer of the Dagster conform asset: both call
:func:`...conformance.runner.run_conform`. This is the door a systemd timer (or
a person, or an agent) uses when the Dagster runtime is not running the node —
which today it is not. It resolves the DSN the same way every other data skill
does, runs one pass, and reports the funnel **loudly**: a run that skipped a
connector or dropped an unknown schema exits non-zero, so a green timer can
never hide a silent drop.
"""

from __future__ import annotations

import os
from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from ..conformance.runner import conform_verdict, run_conform


def _resolve_dsn(params: dict[str, Any]) -> str | None:
    from .._dsn import resolve_dsn

    return resolve_dsn(params)


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """Run one bronze→silver conform pass.

    Params: ``bronze_root`` (default ``~/.axi/bronze``), ``dsn`` (else
    ``DP1_RAG_DSN`` / ``DATABASE_URL``), ``strict`` (default true — unmapped or
    unknown-schema makes the run fail).
    """
    bronze_root = params.get("bronze_root") or os.path.expanduser("~/.axi/bronze")
    dsn = _resolve_dsn(params)
    if not dsn:
        return SkillResult(
            ok=False,
            errors=["no DSN: pass dsn= or set DP1_RAG_DSN / DATABASE_URL"],
        )
    strict = params.get("strict", True)

    try:
        stats = run_conform(bronze_root=bronze_root, dsn=dsn, state_dir=ctx.state_dir)
    except Exception as exc:  # noqa: BLE001 — surface the failure, never a half-green
        return SkillResult(ok=False, errors=[f"conform pass failed: {exc}"])

    verdict = conform_verdict(stats, strict=bool(strict))
    actions = [
        f"conformed {stats.get('rows_out', 0)}/{stats.get('rows_in', 0)} rows "
        f"(loaded normalizers from: {', '.join(stats.get('distributions_loaded') or ['<none>'])})",
        *verdict.messages,
    ]
    return SkillResult(
        ok=verdict.ok,
        value={
            "rows_in": stats.get("rows_in", 0),
            "rows_out": stats.get("rows_out", 0),
            "errored": stats.get("errored", 0),
            "unknown_schema": stats.get("unknown_schema", {}),
            "unmapped_connectors": stats.get("unmapped_connectors", []),
            "registered_without_site": stats.get("registered_without_site", []),
            "distributions_loaded": stats.get("distributions_loaded", []),
        },
        actions_taken=actions,
    )
