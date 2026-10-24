# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``GET /ingest/summary`` — "is my data actually there?"

The face is write-only. A producer posts rows, gets a 2xx, and that is the end
of its feedback: **accepted is not landed**. A batch can be accepted, written to
bronze, and still be absent from silver because no normalizer claimed its
``schema_ref`` — and nothing tells the producer, whose only view is the HTTP
status it got hours ago.

For a partner on the tenant posture this is the whole problem. They have a face
URL and a site-scoped key and no database, so "is my data there?" can only be
answered by someone on our side running SQL. That is not an onboarding
experience; it is a support ticket.

**The site comes from the credential, never the query string.** That is the
same rule the write path follows (`tenancy` §5.2) and it is what makes this
endpoint safe to expose: a partner cannot ask about a site they cannot write
to, because there is no parameter with which to ask.

A grant with no site — a loopback, un-gated mount — gets 403 rather than a view
of everything. An endpoint that answers "all sites" to an unauthenticated
caller is a different endpoint than the one this is meant to be.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from .tenancy import TenancyPolicy

#: Bounded so a partner cannot turn the endpoint into a table scan. Silver is
#: tens of millions of rows; a summary is a summary.
MAX_STREAMS = 200

#: Per-channel counts are per window: a run, or a day of a live feed. A window
#: this long is a reconciliation of a whole history, which is a job for the
#: platform side, not a question a producer asks on every check.
MAX_CHANNEL_WINDOW_DAYS = 31

#: A feed with more channels than this is not one a producer reconciles in
#: one answer; the response says it was truncated.
MAX_CHANNELS = 2000


def _default_summarize(site: str, since_hours: float | None) -> dict[str, Any]:
    """Count the site's rows in GOLD, grouped by feed and class.

    Silver is the intermediate tier and gold is what is served. This read
    is a serving path — it answers a partner asking "did my data land?" —
    and it was reaching past the published surface into the working one.
    ``gold.signals`` is the published view over it, so the query is the
    same and the tier boundary is no longer crossed for a report.

    Filtered by ``site`` first: it is the leading column of the only usable
    index underneath, and a query that reaches for ``schema_ref`` instead
    is a sequential scan over tens of millions of rows.
    """
    from sqlalchemy import text

    from axiom.infra.db import engine_for

    eng = engine_for("data_platform")
    eng = eng[0] if isinstance(eng, tuple) else eng

    where = "site = :site"
    params: dict[str, Any] = {"site": site, "lim": MAX_STREAMS}
    if since_hours is not None:
        where += " AND ts >= now() - (:hours || ' hours')::interval"
        params["hours"] = str(float(since_hours))

    sql = text(
        f"SELECT feed, schema_ref, source_class, count(*) AS n, "  # noqa: S608 - fixed clauses
        f"min(ts) AS first_ts, max(ts) AS last_ts "
        f"FROM gold.signals WHERE {where} "
        f"GROUP BY 1, 2, 3 ORDER BY n DESC LIMIT :lim"
    )
    with eng.connect() as conn:
        rows = conn.execute(sql, params).fetchall()

    feeds = [
        {
            "feed": r.feed,
            "schema_ref": r.schema_ref,
            "source_class": r.source_class,
            "rows": int(r.n),
            "first_ts": r.first_ts.isoformat() if r.first_ts else None,
            "last_ts": r.last_ts.isoformat() if r.last_ts else None,
        }
        for r in rows
    ]
    return {
        "site": site,
        "feeds": feeds,
        "total_rows": sum(s["rows"] for s in feeds),
        "truncated": len(feeds) >= MAX_STREAMS,
    }


def _default_count_channels(
    site: str, feed: str, start: datetime, end: datetime
) -> list[dict[str, Any]]:
    """Rows per channel of one feed in ``[start, end]``, from gold.

    Filtered on site, feed and the window, the leading columns of the
    ``(site, feed, channel, ts)`` index, so the cost is the rows in the window.
    Each channel also carries the distinct units its rows hold.
    """
    from sqlalchemy import text

    from axiom.infra.db import engine_for

    eng = engine_for("data_platform")
    eng = eng[0] if isinstance(eng, tuple) else eng
    sql = text(
        "SELECT channel, count(*) AS n, min(ts) AS first_ts, max(ts) AS last_ts, "
        "array_agg(DISTINCT unit) FILTER (WHERE unit IS NOT NULL) AS units "
        "FROM gold.signals WHERE site = :site AND feed = :feed "
        "AND ts >= :start AND ts <= :end "
        "GROUP BY 1 ORDER BY 1 LIMIT :lim"
    )
    with eng.connect() as conn:
        rows = conn.execute(
            sql, {"site": site, "feed": feed, "start": start, "end": end, "lim": MAX_CHANNELS}
        ).fetchall()
    return [
        {
            "channel": r.channel,
            "rows": int(r.n),
            "first_ts": r.first_ts.isoformat() if r.first_ts else None,
            "last_ts": r.last_ts.isoformat() if r.last_ts else None,
            # Every unit the window's rows carry, so a producer can tell
            # whether its own declaration continues the series or changes
            # it. More than one is itself a finding.
            "units": sorted(r.units or []),
        }
        for r in rows
    ]


def _instant(raw: str | None, name: str) -> datetime:
    if not raw:
        raise HTTPException(
            status_code=422,
            detail=f"by=channel needs {name}=<ISO-8601 instant>: a count per channel "
            "is a count over a window, and an unbounded one is a table scan",
        )
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(
            status_code=422, detail=f"{name}={raw!r} is not an ISO-8601 instant"
        ) from None
    # A naive instant from a producer is UTC: every reader in the DAQ core
    # stamps UTC, and guessing a local zone here would shift the window.
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def build_ingest_summary_router(
    *,
    tenancy: TenancyPolicy | None = None,
    summarize: Callable[[str, float | None], dict[str, Any]] | None = None,
    count_channels: Callable[[str, str, datetime, datetime], list[dict[str, Any]]] | None = None,
) -> APIRouter:
    """The read peer of ``/ingest/rows``.

    ``summarize`` is injectable so the route is testable without a database —
    the endpoint's job is tenancy and shape, and mixing a live query into that
    test would only prove Postgres works.
    """
    policy = tenancy if tenancy is not None else TenancyPolicy.from_env()
    query = summarize if summarize is not None else _default_summarize
    per_channel = count_channels if count_channels is not None else _default_count_channels
    router = APIRouter()

    @router.get("/ingest/summary")
    def ingest_summary(
        request: Request,
        since_hours: float | None = None,
        by: str | None = None,
        feed: str | None = None,
        start: str | None = Query(None, alias="from"),
        end: str | None = Query(None, alias="to"),
    ) -> dict:
        if by not in (None, "", "channel"):
            # Ignoring it would answer a different question than the one asked.
            raise HTTPException(
                status_code=422, detail=f"by={by!r}: the only grouping is by=channel"
            )
        grant = policy.grant_for(request)
        if not grant.site:
            # No site on the credential means no scope. Answering "everything"
            # here would make an un-gated mount a cross-site read.
            raise HTTPException(
                status_code=403,
                detail=(
                    "no site on this credential — the summary is scoped to the "
                    "site the credential is bound to, and there is nothing to "
                    "scope it to"
                ),
            )
        if by == "channel":
            if not feed:
                raise HTTPException(
                    status_code=422,
                    detail="by=channel needs feed=<feed>: channel names are only unique within a feed",
                )
            lo, hi = _instant(start, "from"), _instant(end, "to")
            if hi < lo:
                raise HTTPException(status_code=422, detail="the window ends before it starts")
            if hi - lo > timedelta(days=MAX_CHANNEL_WINDOW_DAYS):
                raise HTTPException(
                    status_code=422,
                    detail=f"a per-channel count covers at most {MAX_CHANNEL_WINDOW_DAYS} "
                    "days; split the window",
                )
            channels = per_channel(grant.site, feed, lo, hi)
            return {
                "site": grant.site,
                "by": "channel",
                "feed": feed,
                "from": lo.isoformat(),
                "to": hi.isoformat(),
                "channels": channels,
                "total_rows": sum(int(c.get("rows", 0)) for c in channels),
                "truncated": len(channels) >= MAX_CHANNELS,
            }
        if since_hours is not None and since_hours <= 0:
            raise HTTPException(status_code=422, detail="since_hours must be positive")

        result = query(grant.site, since_hours)
        # Said out loud: zero rows is a legitimate, useful answer, and a
        # producer that has been posting 2xx all morning needs to be told the
        # difference between "accepted" and "landed".
        if not result.get("total_rows"):
            result["note"] = (
                "no rows in silver for this site"
                + (f" in the last {since_hours}h" if since_hours else "")
                + ". A 2xx from /ingest/rows means the batch was accepted, not "
                "that it was normalized into silver — an unclaimed schema_ref "
                "lands in bronze and stops there."
            )
        return result

    return router


__all__ = [
    "MAX_CHANNELS",
    "MAX_CHANNEL_WINDOW_DAYS",
    "MAX_STREAMS",
    "build_ingest_summary_router",
]
