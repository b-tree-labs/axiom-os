# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``data.ingest_freshness`` — which streams stopped advancing, and when.

There was no freshness view at all. `silence()` and :class:`CreditedGuard`
watch a **live producer's** stream and are excellent at it — but they run
inside the producer. They cannot say that a site's silver data stopped
advancing four months ago, because in that case the producer is not running to
notice. That is exactly the ACU state: 17.4M rows, last one 2026-05-20, and
nothing anywhere said so.

The distinction matters more with three partner sites than with one. A stream
that has stopped looks identical to a healthy one if you only count rows, and
row counts are what people look at.

**An expectation is required to call something stale.** Without a declared
cadence this reports age and refuses to grade it — a threshold invented here
would be a number nobody agreed to, and "STALE" carries more authority than a
guess deserves. Sites declare their expectation; streams that have not are
reported as ``ungraded`` rather than quietly assumed fine.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

#: Grades. ``ungraded`` is a real outcome, not a missing one.
FRESH, STALE, UNGRADED = "fresh", "stale", "ungraded"


def _age_hours(last_ts: str | None, now: datetime) -> float | None:
    if not last_ts:
        return None
    try:
        dt = datetime.fromisoformat(str(last_ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        # A naive timestamp cannot be aged against UTC without inventing an
        # offset, and an hour of invented offset is an hour of invented
        # freshness. Report it as unknown rather than guess.
        return None
    return (now - dt).total_seconds() / 3600.0


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """Report per-stream freshness for one site, or every site.

    Params: ``site`` (optional — all sites when absent), ``expect_hours``
    (optional dict ``{stream: hours}`` or a single number applied to every
    stream), ``now`` (optional ISO string, for tests).
    """
    from sqlalchemy import text

    from axiom.infra.db import engine_for

    now_param = params.get("now")
    now = (
        datetime.fromisoformat(str(now_param).replace("Z", "+00:00"))
        if now_param
        else datetime.now(UTC)
    )

    site = params.get("site")
    where = "site = :site" if site else "TRUE"
    sql = text(
        f"SELECT site, stream, source_class, count(*) AS n, max(ts) AS last_ts "  # noqa: S608
        f"FROM silver.signals WHERE {where} GROUP BY 1, 2, 3 ORDER BY 1, 2"
    )
    try:
        # engine_for is INSIDE the try: building the engine is where an
        # unreachable or misconfigured store fails, and it was outside — so a
        # refused connection raised out of a skill whose whole contract is to
        # report rather than raise. Caught by its own test.
        eng = engine_for("data_platform")
        eng = eng[0] if isinstance(eng, tuple) else eng
        with eng.connect() as conn:
            rows = conn.execute(sql, {"site": site} if site else {}).fetchall()
    except Exception as exc:  # noqa: BLE001 — an unreachable store is a report
        return SkillResult(
            ok=False, errors=[f"could not read silver.signals: {type(exc).__name__}: {exc}"]
        )

    expect = params.get("expect_hours")
    def _expected_for(stream: str) -> float | None:
        if isinstance(expect, dict):
            v = expect.get(stream)
            return float(v) if v is not None else None
        return float(expect) if expect is not None else None

    streams: list[dict[str, Any]] = []
    for r in rows:
        last = r.last_ts.isoformat() if r.last_ts else None
        age = _age_hours(last, now)
        limit = _expected_for(r.stream)
        if age is None or limit is None:
            grade = UNGRADED
        else:
            grade = STALE if age > limit else FRESH
        streams.append({
            "site": r.site,
            "stream": r.stream,
            "source_class": r.source_class,
            "rows": int(r.n),
            "last_ts": last,
            "age_hours": round(age, 2) if age is not None else None,
            "expect_hours": limit,
            "grade": grade,
        })

    stale = [s for s in streams if s["grade"] == STALE]
    ungraded = [s for s in streams if s["grade"] == UNGRADED]

    actions = [f"{len(streams)} stream(s) across {len({s['site'] for s in streams})} site(s)"]
    for s in streams:
        age = f"{s['age_hours']:.1f}h" if s["age_hours"] is not None else "age unknown"
        actions.append(
            f"   {s['site']:<16} {s['stream']:<20} {s['grade']:<9} "
            f"{s['rows']:>10,}  last {s['last_ts'] or '?'}  ({age})"
        )
    if ungraded:
        # Named, not silently folded into "fine". A stream nobody declared an
        # expectation for is a stream nobody can be alerted about.
        actions.append(
            f"{len(ungraded)} stream(s) are UNGRADED — no declared cadence, so "
            "age is reported and not judged. Nothing will alert on these."
        )

    # --- telling someone ----------------------------------------------------
    # Off by default: the skill is read-only and an operator inspecting
    # freshness should not page anyone by looking. The scheduled cadence turns
    # it on; a human at a terminal does not.
    alerted = None
    if params.get("alert") and stale:
        import hashlib

        from .. import _herald

        # The dedup key is the SET of stale streams, not the fact that
        # something is stale. An alert that repeats every run for four months
        # is one people learn to close without reading; a NEW stale stream
        # changes the set, changes the key, and gets through immediately.
        # Delivery dedup is a sliding window (fabric §6.1), so a long-standing
        # problem still resurfaces rather than disappearing forever.
        fingerprint = ",".join(sorted(f"{s['site']}/{s['stream']}" for s in stale))
        key = "ingest-stale-" + hashlib.sha256(fingerprint.encode()).hexdigest()[:16]
        worst = max(stale, key=lambda s: s["age_hours"] or 0)
        alerted = _herald.publish_event(
            "data.ingest.stale",
            f"{len(stale)} stream(s) stopped advancing — worst: "
            f"{worst['site']}/{worst['stream']} at {worst['age_hours']:.0f}h",
            body="\n".join(
                f"{s['site']}/{s['stream']}: last {s['last_ts']} "
                f"({s['age_hours']:.1f}h, expected within {s['expect_hours']}h)"
                for s in stale
            ),
            dedup_key=key,
            payload={"stale": stale, "checked_at": now.isoformat()},
        )
        actions.append(
            f"published HERALD event: data.ingest.stale ({len(stale)} stream(s))"
            if alerted
            else "HERALD publish was attempted and did not complete"
        )

    return SkillResult(
        ok=not stale,
        value={
            "streams": streams,
            "stale": len(stale),
            "ungraded": len(ungraded),
            "checked_at": now.isoformat(),
            "alert_receipt": alerted,
        },
        actions_taken=actions,
        errors=[
            f"{s['site']}/{s['stream']}: last row {s['last_ts']} "
            f"({s['age_hours']:.1f}h old, expected within {s['expect_hours']}h)"
            for s in stale
        ],
    )


__all__ = ["FRESH", "STALE", "UNGRADED", "run"]
