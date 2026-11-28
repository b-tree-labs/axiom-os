# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``node.data_growth``: which tables on this node's database grow without a bound.

Reads the catalogue only (see :mod:`..data_growth`). With ``alert=true`` (the
daily schedule) each broken rule is handed to HERALD once, whatever the number
of tables behind it. It never changes the database: fixes come back as plans.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from axiom.infra.human_layer import explain
from axiom.infra.skills import SkillContext, SkillResult

from .. import data_growth as dg
from ._handoff import HERALD_ALERT, PLINTH_RULES, handoff, next_step


def _declarations(params: dict[str, Any], ctx: SkillContext | None, notes: list[str]):
    """PLINTH's storage rules and backup posture, through the registry."""
    if params.get("_limits") is not None:
        return params["_limits"], params.get("_backup")
    rec = handoff(ctx, PLINTH_RULES, {})
    if rec["status"] != "ok" or not isinstance(rec.get("value"), dict):
        notes.append(f"PLINTH's storage rules were not readable ({rec['status']}: "
                     f"{rec.get('detail', '')}); every table is judged as undeclared")
        return {}, None
    v = rec["value"]
    return dg.limits_from_rules(v.get("rules") or []), v.get("backup")


def _thresholds(params: dict[str, Any]) -> dg.Thresholds:
    th = dg.Thresholds()
    if params.get("min_gb") is not None:
        th.min_bytes = int(float(params["min_gb"]) * dg.GB)
    if params.get("churn_ratio") is not None:
        th.churn_ratio = float(params["churn_ratio"])
    if params.get("runway_days") is not None:
        th.runway_days = float(params["runway_days"])
    return th


def _connect(params: dict[str, Any]):
    """A read-only session on the node's database (the data platform's DSN resolution)."""
    import psycopg

    from axiom.infra.db import libpq_url, platform_db_url

    dsn = params.get("dsn") or next(
        (os.environ[n] for n in ("DP1_RAG_DSN", "DATABASE_URL", "AXIOM_DB_URL") if os.environ.get(n)),
        None,
    ) or platform_db_url()
    conn = psycopg.connect(libpq_url(str(dsn)), autocommit=True, connect_timeout=15)
    conn.execute("SET default_transaction_read_only = on")
    conn.execute("SET statement_timeout = 120000")
    return conn


def _state_dir(ctx: SkillContext | None) -> Path:
    sd = getattr(ctx, "state_dir", None)
    if sd:
        return Path(sd)
    from axiom.infra.paths import get_user_state_dir

    return Path(get_user_state_dir())


def run(params: dict[str, Any], ctx: SkillContext | None = None) -> SkillResult:
    catalog = params.get("_catalog")  # tests inject a fake catalogue
    conn = None
    try:
        if catalog is None:
            conn = _connect(params)
            catalog = dg.PostgresCatalog(conn.cursor())
        notes: list[str] = []
        limits, backup = _declarations(params, ctx, notes)
        value = dg.report(
            catalog,
            dg.SampleStore.under(_state_dir(ctx)),
            limits,
            backup=backup,
            th=_thresholds(params),
            disk_path=params.get("disk_path"),
            **({"disk_usage": params["_disk_usage"]} if params.get("_disk_usage") else {}),
        )
    except Exception as exc:  # noqa: BLE001 - a report that cannot read says so
        return SkillResult(ok=False, errors=[f"could not read the database catalogue: {exc}"])
    finally:
        if conn is not None:
            conn.close()

    value["notes"] = notes + value["notes"]
    actions = ["read table, index and volume metadata (no table rows)"]
    steps = []
    if value["findings"]:
        steps.append(next_step("data.storage", "the data platform's storage view of the same tables"))

    # The human layer: the configured model words what was decided above, nothing more.
    model = {"gateway": params["_gateway"]} if "_gateway" in params else {}
    if params.get("explain") is False:
        model = {"gateway": None}
    fallback = dg.why(value) + "\n\n" + _deterministic_findings(value["findings"])
    value["explanation"] = explain(
        {"why": dg.why(value), "findings": value["findings"], "volume": value["volume"]},
        agent="KEEL",
        ask=("Answer 'why is the database growing' from these facts, then explain each "
             "finding and its proposed fix and the fix's risks, in plain words."),
        fallback=fallback, **model,
    ).as_dict()

    if params.get("alert"):
        from .. import proposals

        ledger = proposals.Ledger.under(_state_dir(ctx))
        value["approved_since_last_run"] = proposals.sync_approvals(
            ctx, ledger, params.get("_find_plan"))
        tracked = proposals.track(ctx, ledger, value["findings"])
        value["followups"] = tracked
        new = set(tracked["new"])
        fresh = [f for f in value["findings"]
                 if (f.get("fix") or {}).get("plan_hash") in new or f["severity"] == "critical"]
        sent = []
        for a in dg.alerts(fresh):
            words = explain(a, agent="KEEL",
                            ask="Write a short alert (two or three sentences) for this cause.",
                            fallback=a["summary"], max_tokens=200, **model)
            rec = handoff(ctx, HERALD_ALERT, {
                "recipient": params.get("recipient") or "@operator:local",
                "summary": words.text,
                "priority": a["priority"],
                "actor": "@keel:local",
                "dedup_key": a["dedup_key"],
            })
            sent.append({"code": a["code"], "status": rec["status"], "detail": rec.get("detail", ""),
                         "worded_by": words.by})
        value["alerts"] = sent
        actions.append(f"raised {sum(s['status'] == 'ok' for s in sent)} alert(s) through HERALD; "
                       f"{len(tracked['new'])} new proposal(s) filed with CLERK, "
                       f"{len(tracked['already_tracked'])} already tracked")
    return SkillResult(ok=True, value=value, actions_taken=actions, next=steps)


def open_proposals(params: dict[str, Any], ctx: SkillContext | None = None) -> SkillResult:
    """``node.data_growth_open``: the data-growth proposals still waiting on a person (open or
    approved, not yet applied), after recording any approval. What CLERK's digest lists."""
    from .. import proposals

    ledger = proposals.Ledger.under(_state_dir(ctx))
    marked = proposals.sync_approvals(ctx, ledger, params.get("_find_plan"))
    rows = [{"plan_hash": p["plan_hash"], "rule": p["code"], "table": p["relation"],
             "message": p["message"], "capability": p["capability"], "state": p["state"],
             "followup_id": p["followup_id"]} for p in ledger.tracked()]
    return SkillResult(ok=True, value={"proposals": rows, "approved_now": marked},
                       actions_taken=[f"{len(rows)} data-growth proposal(s) open"])


def _deterministic_findings(findings: list[dict[str, Any]]) -> str:
    if not findings:
        return "Every table keeps within its storage rule."
    lines = []
    for f in findings:
        lines.append(f"- {f['message']}")
        fix = f.get("fix") or {}
        lines.extend(f"  fix: {s}" for s in fix.get("steps", []))
        if fix.get("capability"):
            lines.append(f"  runs only when a person approves plan {fix['plan_hash']} "
                         f"for {fix['capability']}")
    return "\n".join(lines)


__all__ = ["run"]
