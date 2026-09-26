# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Bucketed points from the gold tier — the drawing query.

This does not read the serving catalog. The catalog holds counts and spans, not
points, so a chart has to go to gold; what keeps that affordable is that the
predicate is an exact prefix of the (site, stream, channel, ts) index.

Each bucket carries ``avg``, ``min``, ``max`` and ``n`` rather than an average
alone. A mean hides exactly what a reader is usually looking for — an excursion
inside the bucket — and once the rows have been read, the band costs nothing
extra to compute. Dropping it would mean the client either draws a misleadingly
smooth line or asks again at a finer bucket.

Empty buckets are skipped rather than emitted as nulls. A gap in the data and a
value of zero are different facts, and a client that plots only what it received
cannot confuse them.
"""

from __future__ import annotations

from datetime import UTC, datetime

#: A chart cannot usefully draw more than this per channel, and asking for more
#: is how a range request turns into a scan.
MAX_SERIES_POINTS = 2000

#: Beyond this, one request fans out into that many index walks.
MAX_CHANNELS_PER_REQUEST = 12


def bucketed_series(
    session,
    *,
    site: str,
    stream: str,
    channels: list[str],
    bucket_s: int,
    t_from: str | None = None,
    t_to: str | None = None,
    source: str = "gold.signals",
) -> dict:
    """Bucketed points for one or more channels, flat and channel-tagged.

    ``t_from``/``t_to`` are optional: omitting them means "everything this
    channel has", which is bounded in practice because the predicate is an
    index prefix and the result is capped per channel.
    """
    from sqlalchemy import text as sql_text

    if not channels:
        raise ValueError("channels is required (comma-separated)")
    if len(channels) > MAX_CHANNELS_PER_REQUEST:
        raise ValueError(
            f"at most {MAX_CHANNELS_PER_REQUEST} channels per request "
            f"(asked for {len(channels)})"
        )
    try:
        step = int(bucket_s)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"bucket_s must be whole seconds, got {bucket_s!r}") from exc
    if step <= 0:
        raise ValueError(f"bucket_s must be positive, got {step}")

    window = ""
    params: dict = {"step": step, "site": site, "stream": stream,
                    "limit": MAX_SERIES_POINTS}
    if t_from:
        window += " AND ts >= CAST(:t_from AS timestamptz)"
        params["t_from"] = t_from
    if t_to:
        window += " AND ts < CAST(:t_to AS timestamptz)"
        params["t_to"] = t_to

    # The epoch expression is the one dialect-specific piece. Formatting the
    # bucket start happens in Python rather than in SQL: `to_char(... AT TIME
    # ZONE ...)` is Postgres-only, and pushing it down would make this
    # untestable anywhere but a live Postgres — which is how the last
    # dialect-dependent bug in this package reached production green.
    dialect = session.get_bind().dialect.name
    epoch = (
        "extract(epoch from ts)"
        if dialect == "postgresql"
        else "CAST(strftime('%s', ts) AS REAL)"
    )

    points: list[dict] = []
    for channel in channels:
        rows = session.execute(
            sql_text(
                f"""
                SELECT bk, avg(value) AS avg, min(value) AS min,
                       max(value) AS max, count(*) AS n
                FROM (
                    SELECT value, CAST({epoch} / :step AS INTEGER) AS bk
                    FROM {source}
                    WHERE site = :site AND stream = :stream
                      AND channel = :channel
                      AND value IS NOT NULL{window}
                ) w
                GROUP BY bk ORDER BY bk
                LIMIT :limit
                """
            ),
            {**params, "channel": channel},
        ).fetchall()
        for r in rows:
            started = datetime.fromtimestamp(int(r[0]) * step, tz=UTC)
            points.append(
                {
                    "channel": channel,
                    "ts": started.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "avg": round(float(r[1]), 6),
                    "min": round(float(r[2]), 6),
                    "max": round(float(r[3]), 6),
                    "n": int(r[4]),
                }
            )

    return {"series": points}


__all__ = ["MAX_CHANNELS_PER_REQUEST", "MAX_SERIES_POINTS", "bucketed_series"]
