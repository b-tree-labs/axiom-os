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
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from .tenancy import TenancyPolicy

#: Bounded so a partner cannot turn the endpoint into a table scan. Silver is
#: tens of millions of rows; a summary is a summary.
MAX_STREAMS = 200


def _default_summarize(site: str, since_hours: float | None) -> dict[str, Any]:
    """Count the site's rows in silver, grouped by stream and class.

    Filtered by ``site`` first: it is the leading column of the only usable
    index on ``silver.signals``, and a query that reaches for ``schema_ref``
    instead is a sequential scan over tens of millions of rows.
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
        f"SELECT stream, schema_ref, source_class, count(*) AS n, "  # noqa: S608 - fixed clauses
        f"min(ts) AS first_ts, max(ts) AS last_ts "
        f"FROM silver.signals WHERE {where} "
        f"GROUP BY 1, 2, 3 ORDER BY n DESC LIMIT :lim"
    )
    with eng.connect() as conn:
        rows = conn.execute(sql, params).fetchall()

    streams = [
        {
            "stream": r.stream,
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
        "streams": streams,
        "total_rows": sum(s["rows"] for s in streams),
        "truncated": len(streams) >= MAX_STREAMS,
    }


def build_ingest_summary_router(
    *,
    tenancy: TenancyPolicy | None = None,
    summarize: Callable[[str, float | None], dict[str, Any]] | None = None,
) -> APIRouter:
    """The read peer of ``/ingest/rows``.

    ``summarize`` is injectable so the route is testable without a database —
    the endpoint's job is tenancy and shape, and mixing a live query into that
    test would only prove Postgres works.
    """
    policy = tenancy if tenancy is not None else TenancyPolicy.from_env()
    query = summarize if summarize is not None else _default_summarize
    router = APIRouter()

    @router.get("/ingest/summary")
    def ingest_summary(request: Request, since_hours: float | None = None) -> dict:
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


__all__ = ["MAX_STREAMS", "build_ingest_summary_router"]
