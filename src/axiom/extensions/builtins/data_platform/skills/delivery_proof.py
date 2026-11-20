# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``data.delivery_proof``: is what the source holds what gold serves, per site.

Reads only. ``ok`` only when every site is proven, so a timer, a deploy gate or an
agent can act on the exit status. See :mod:`..delivery_proof` for the verdicts.
"""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from .. import _herald


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """Params: ``dsn``, ``site``, ``connector``, ``window_days``, ``alert``."""
    from .._dsn import resolve_dsn
    from ..agents.plinth.connectors import list_connectors
    from ..delivery_proof import WINDOW_DAYS, prove

    dsn = resolve_dsn(params)
    if not dsn:
        return SkillResult(ok=False, errors=["no DSN: pass dsn= or set DP1_RAG_DSN / DATABASE_URL"])
    try:
        import psycopg
    except ImportError:  # pragma: no cover
        return SkillResult(ok=False, errors=["psycopg is not installed"])

    connectors = list_connectors()
    site, only = params.get("site"), params.get("connector")
    if site:
        connectors = [c for c in connectors if c.site == site]
    if only:
        connectors = [c for c in connectors if c.name == only]
    if not connectors:
        return SkillResult(ok=False, errors=["no configured connector matches"])
    try:
        with (
            psycopg.connect(dsn, autocommit=True, connect_timeout=15) as conn,
            conn.cursor() as cur,
        ):
            cur.execute("SET default_transaction_read_only = on")
            # Longer than a serving read: counting one day of readings in compressed
            # chunks is real work, and a count that gives up proves nothing.
            cur.execute("SET statement_timeout = '300s'")
            report = prove(
                cur, connectors, window_days=int(params.get("window_days") or WINDOW_DAYS)
            )
    except Exception as exc:  # noqa: BLE001
        return SkillResult(ok=False, errors=[f"could not read the delivery state: {exc}"])

    lines = [f"{name}: {verdict}" for name, verdict in report["sites"].items()]
    problems = []
    for proof in report["connectors"]:
        lines.append(
            f"  {proof['connector']}: {proof['verdict']}; {proof['files']} files, "
            f"{proof['landed']} landed, {proof['declined']} declined, {proof['failed']} failed, "
            f"{proof['unread']} unread; {proof['readings']} readings; "
            f"{proof['gold_checked']} checked against gold, "
            f"{proof['history_unchecked']} older not rechecked"
        )
        lines.extend(f"    {reason}" for reason in proof["reasons"])
        if proof["verdict"] != "PROVEN":
            problems.append(f"{proof['connector']}: {proof['verdict']}")

    if params.get("alert") and not report["proven"]:
        report["alert_receipt"] = _herald.publish_event(
            "data.delivery.unproven",
            "data delivery not proven: " + "; ".join(lines[: len(report["sites"])]),
            body="\n".join(lines),
            dedup_key="data.delivery.unproven",
            payload={"sites": report["sites"]},
        )
    return SkillResult(ok=report["proven"], value=report, actions_taken=lines, errors=problems)
