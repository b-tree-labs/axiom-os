# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``data.ingest_freshness`` — which feeds stopped advancing, and when.

`silence()` and :class:`CreditedGuard` watch a **live producer's** feed and
are excellent at it — but they run inside the producer. They cannot say that
a site's data stopped advancing four months ago, because in that case the
producer is not running to notice. That is a state seen on a live node: 17.4M rows,
last one 2026-05-20, and nothing anywhere said so.

A feed that has stopped looks identical to a healthy one if you only count
rows, and row counts are what people look at.

**This reads `gold.ingest_freshness`.** Silver is the working tier and gold
is what is served; a status report is a serving path, and this one used to
reach past the published surface into the intermediate one — then re-derive,
in Python, an answer the gold view already gave.

It gave a better one. `gold.ingest_freshness` carries ``typical_gap``: the
feed's OWN observed cadence, self-calibrated from its arrivals. So
staleness needs no declaration from anybody — ``gold.ingest_stale`` flags a
lag beyond four times it, and a 1/min feed trips after ~4 min while a daily
one trips after ~4 days, with no per-feed threshold to maintain.

This skill had argued the opposite: that without a declared cadence it must
refuse to grade, because "a threshold invented here would be a number nobody
agreed to". That is right about an INVENTED threshold and wrong about a
MEASURED one — and it is why a site sat four months stale under a report that
was working as designed, because nobody had declared a cadence for it.

A declared expectation now overrides the observed cadence rather than being
a precondition for an answer. ``ungraded`` remains for a feed with neither.
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
    """Report per-feed freshness for one site, or every site.

    Params: ``site`` (optional — all sites when absent), ``expect_hours``
    (optional dict ``{feed: hours}`` or a single number applied to every
    feed), ``now`` (optional ISO string, for tests).
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
    # A site that changed name keeps its history under the old id, so a
    # question about the new one would report a feed as never having
    # arrived while 28 million of its rows sat under the former name. The
    # ids are widened on the way in and the answer is labelled with the
    # canonical one on the way out — otherwise one site reports twice and
    # both halves look stale.
    from axiom.infra.site_identity import identities

    registry = identities()
    if site:
        wanted = list(registry.aliases_of(registry.resolve(str(site)))) or [str(site)]
        where = "site = ANY(:sites)"
        binds: dict[str, Any] = {"sites": wanted}
    else:
        where = "TRUE"
        binds = {}
    # Read the GOLD view, which already computes this — and computes it
    # better. `gold.ingest_freshness` carries `typical_gap`, the feed's
    # OWN observed cadence, so staleness needs no declaration from anyone:
    # `gold.ingest_stale` flags lag beyond four times it. This skill was
    # re-deriving a cruder version in Python against silver and refusing
    # to grade anything without a declared `expect_hours`.
    #
    # Two implementations of one rule drift, and the one in SQL was the
    # better of the two. A declared expectation is now an OVERRIDE of the
    # self-calibrated cadence rather than a precondition for an answer.
    sql = text(
        f"SELECT site, feed, points AS n, last_ts, "  # noqa: S608
        f"EXTRACT(EPOCH FROM typical_gap) AS typical_gap_s, model_ref "
        f"FROM gold.ingest_freshness WHERE {where} ORDER BY 1, 2"
    )
    try:
        # engine_for is INSIDE the try: building the engine is where an
        # unreachable or misconfigured store fails, and it was outside — so a
        # refused connection raised out of a skill whose whole contract is to
        # report rather than raise. Caught by its own test.
        eng = engine_for("data_platform")
        eng = eng[0] if isinstance(eng, tuple) else eng
        with eng.connect() as conn:
            rows = conn.execute(sql, binds).fetchall()
    except Exception as exc:  # noqa: BLE001 — an unreachable store is a report
        return SkillResult(
            ok=False, errors=[f"could not read gold.ingest_freshness: {type(exc).__name__}: {exc}"]
        )

    expect = params.get("expect_hours")

    def _declared_for(feed: str) -> float | None:
        if isinstance(expect, dict):
            v = expect.get(feed)
            return float(v) if v is not None else None
        return float(expect) if expect is not None else None

    feeds: list[dict[str, Any]] = []
    for r in rows:
        last = r.last_ts.isoformat() if r.last_ts else None
        age = _age_hours(last, now)

        # A declared cadence overrides; otherwise the feed's OWN observed
        # cadence, four times over, which is `gold.ingest_stale`'s rule
        # stated in the same terms. This used to refuse to grade anything
        # undeclared — which is how a site sat four months stale with nothing
        # said, because nobody had declared a cadence for it.
        declared = _declared_for(r.feed)
        observed = getattr(r, "typical_gap_s", None)
        if declared is not None:
            limit, basis = declared, "declared"
        elif observed:
            limit, basis = float(observed) / 3600.0 * 4, "observed cadence"
        else:
            limit, basis = None, "no cadence"

        if age is None or limit is None:
            grade = UNGRADED
        else:
            grade = STALE if age > limit else FRESH
        feeds.append({
            "basis": basis,
            # The CANONICAL id, not the one the row is stored under. A site
            # reported under both its names reads as two sites, and the
            # count in the summary line below would say so.
            "site": registry.resolve(r.site),
            "feed": r.feed,
            "rows": int(r.n),
            "last_ts": last,
            "age_hours": round(age, 2) if age is not None else None,
            "expect_hours": limit,
            "grade": grade,
        })

    stale = [s for s in feeds if s["grade"] == STALE]
    ungraded = [s for s in feeds if s["grade"] == UNGRADED]

    actions = [f"{len(feeds)} feed(s) across {len({s['site'] for s in feeds})} site(s)"]
    for s in feeds:
        age = f"{s['age_hours']:.1f}h" if s["age_hours"] is not None else "age unknown"
        actions.append(
            f"   {s['site']:<16} {s['feed']:<20} {s['grade']:<9} "
            f"{s['rows']:>10,}  last {s['last_ts'] or '?'}  ({age})"
        )
    if ungraded:
        # Named, not silently folded into "fine". A feed nobody declared an
        # expectation for is a feed nobody can be alerted about.
        actions.append(
            f"{len(ungraded)} feed(s) are UNGRADED — no declared cadence, so "
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

        # The dedup key is the SET of stale feeds, not the fact that
        # something is stale. An alert that repeats every run for four months
        # is one people learn to close without reading; a NEW stale feed
        # changes the set, changes the key, and gets through immediately.
        # Delivery dedup is a sliding window (fabric §6.1), so a long-standing
        # problem still resurfaces rather than disappearing forever.
        fingerprint = ",".join(sorted(f"{s['site']}/{s['feed']}" for s in stale))
        key = "ingest-stale-" + hashlib.sha256(fingerprint.encode()).hexdigest()[:16]
        worst = max(stale, key=lambda s: s["age_hours"] or 0)
        alerted = _herald.publish_event(
            "data.ingest.stale",
            f"{len(stale)} feed(s) stopped advancing — worst: "
            f"{worst['site']}/{worst['feed']} at {worst['age_hours']:.0f}h",
            body="\n".join(
                f"{s['site']}/{s['feed']}: last {s['last_ts']} "
                f"({s['age_hours']:.1f}h, expected within {s['expect_hours']}h)"
                for s in stale
            ),
            dedup_key=key,
            payload={"stale": stale, "checked_at": now.isoformat()},
        )
        actions.append(
            f"published HERALD event: data.ingest.stale ({len(stale)} feed(s))"
            if alerted
            else "HERALD publish was attempted and did not complete"
        )

    return SkillResult(
        ok=not stale,
        value={
            "feeds": feeds,
            "stale": len(stale),
            "ungraded": len(ungraded),
            "checked_at": now.isoformat(),
            "alert_receipt": alerted,
        },
        actions_taken=actions,
        errors=[
            f"{s['site']}/{s['feed']}: last row {s['last_ts']} "
            f"({s['age_hours']:.1f}h old, expected within {s['expect_hours']}h)"
            for s in stale
        ],
    )


__all__ = ["FRESH", "STALE", "UNGRADED", "run"]
