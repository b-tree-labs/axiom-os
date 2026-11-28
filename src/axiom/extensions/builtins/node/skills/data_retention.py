# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``node.data_retention`` and ``node.data_apply``: KEEL keeps tables to what they declare.

``node.data_retention`` reads PLINTH's rules (through the registry) and KEEL's own
measurements, and plans, per table that declares a bound, what keeping to it
takes: a retention policy, a batched delete past its retain, a trim to max_rows.
It reads only.

``node.data_apply`` is the one KEEL capability that changes the database. It is
destructive, so the platform's plan gate runs it only with a ``plan_hash`` a
person approved for exactly these ``{table, sql}``; and it runs only statements
from a short allowlist (:data:`..data_growth.ALLOWED_STATEMENTS`), approved or
not. After it runs, the CLERK follow-up for that plan is closed with what ran.
"""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from .. import data_growth as dg
from .. import proposals
from ._handoff import PLINTH_RULES, handoff


def retention(params: dict[str, Any], ctx: SkillContext | None = None) -> SkillResult:
    from . import data_growth as growth_skill

    rules = params.get("_rules")
    if rules is None:
        rec = handoff(ctx, PLINTH_RULES, {})
        if rec["status"] != "ok":
            return SkillResult(ok=False, errors=[
                f"PLINTH's storage rules are not readable here ({rec['status']})"])
        rules = (rec.get("value") or {}).get("rules") or []
    measured = growth_skill.run({**{k: v for k, v in params.items() if k != "_rules"},
                                 "alert": False, "explain": False}, ctx)
    if not measured.ok:
        return measured
    plans = dg.retention_plans(rules, measured.value.get("tables") or [])
    need = [p for p in plans if p["fix"]]
    return SkillResult(
        ok=True,
        value={"plans": plans,
               "applies": "only with a person's approval of each plan_hash; KEEL never acts on its own"},
        actions_taken=[f"{len(plans)} table(s) declare a bound; {len(need)} need action"],
    )


def _execute(params: dict[str, Any], statements: list[str]) -> None:
    from .data_growth import _connect

    conn = params.get("_conn")
    if conn is not None:
        for s in statements:
            conn.execute(s)
        return
    conn = _connect(params)
    try:
        conn.execute("SET default_transaction_read_only = off")
        for s in statements:
            conn.execute(s)
    finally:
        conn.close()


def apply(params: dict[str, Any], ctx: SkillContext | None = None) -> SkillResult:
    from axiom.infra.skill_chain import plan_digest

    table = str(params.get("table") or "")
    sql = [str(s) for s in params.get("sql") or []]
    if not table or not sql:
        return SkillResult(ok=False, errors=["pass table and sql exactly as the plan states them"])
    refused = [s for s in sql if not dg.allowed(s)]
    if refused:
        return SkillResult(ok=False, errors=[
            "these statements are not ones KEEL runs, approved or not: " + "; ".join(refused)])
    try:
        _execute(params, sql)
    except Exception as exc:  # noqa: BLE001
        return SkillResult(ok=False, errors=[f"the plan did not apply: {exc}"])
    h = plan_digest(dg.APPLY, {"table": table, "sql": sql})
    state = getattr(ctx, "state_dir", None)
    closed = {"status": "untracked"}
    if state:
        closed = proposals.close(ctx, proposals.Ledger.under(state), h,
                                 f"applied to {table}: " + "; ".join(sql))
    return SkillResult(
        ok=True, value={"table": table, "ran": sql, "plan_hash": h, "followup": closed},
        actions_taken=[f"ran on {table}: {s}" for s in sql],
    )


__all__ = ["apply", "retention"]
