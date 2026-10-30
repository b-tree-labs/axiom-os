# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``data.pipeline_proof`` — is data flowing bronze → silver → gold, and if not, whose problem is it.

Reads only. See :mod:`..pipeline_proof` for the verdicts and why they are
separate. The result is ``ok`` only when no connector is in a pipeline fault, so
a scheduled caller (a deploy gate, a monitor, an agent) can act on the exit
status without parsing.
"""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """Params: ``dsn``, ``observe_s`` (sample the conform watermarks twice, that far apart),
    ``stuck_after_s`` (default 1800), ``connector`` (restrict to one)."""
    from .._dsn import resolve_dsn
    from ..agents.plinth.connectors import list_connectors
    from ..pipeline_proof import STUCK_AFTER_S, prove

    dsn = resolve_dsn(params)
    if not dsn:
        return SkillResult(ok=False, errors=["no DSN: pass dsn= or set DP1_RAG_DSN / DATABASE_URL"])
    try:
        import psycopg
    except ImportError:  # pragma: no cover
        return SkillResult(ok=False, errors=["psycopg is not installed"])

    connectors = list_connectors()
    only = params.get("connector")
    if only:
        connectors = [c for c in connectors if c.name == only]
        if not connectors:
            return SkillResult(ok=False, errors=[f"no connector named {only!r} is configured"])
    try:
        with psycopg.connect(dsn, autocommit=True, connect_timeout=15) as conn, conn.cursor() as cur:
            cur.execute("SET default_transaction_read_only = on")
            cur.execute("SET statement_timeout = '60s'")
            report = prove(
                cur,
                connectors,
                stuck_after_s=float(params.get("stuck_after_s", STUCK_AFTER_S)),
                observe_s=float(params.get("observe_s", 0) or 0),
            )
    except Exception as exc:  # noqa: BLE001
        return SkillResult(ok=False, errors=[f"could not read the pipeline's state: {exc}"])

    faults = [f for f in report["feeds"] if f["verdict"] in ("PIPELINE_STUCK", "NEVER_FLOWED", "GOLD_DISAGREES")]
    from axiom.infra.cli_format import kv_line

    lines = [
        kv_line(f"{f['connector']} / {f['feed'] or '-'}", f"{f['verdict']}  {'; '.join(f['reasons'])}", 34)
        for f in report["feeds"]
    ]
    lines.append(f"schema current: {report['schema_current']}; "
                 f"sessions idle in a transaction on silver.signals: {len(report['idle_in_transaction'])}")
    return SkillResult(
        ok=not faults and report["overall"] != "NO_CONNECTORS",
        value=report,
        actions_taken=lines,
        errors=[f"{f['connector']}: {f['verdict']}" for f in faults],
    )
