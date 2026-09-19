# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Project the gold tier into the serving catalog.

The lakehouse stays the analytical source of truth; this keeps a small, current
projection the API can read in a millisecond instead of aggregating gold on
every request.

The shape of this function was decided by measurement on a real installation,
and the numbers are recorded because they are the argument against the obvious
implementations.

The obvious one is a single `GROUP BY site, stream, channel` over the whole gold
tier. On a 59.3M-row, 25GB table that plans as a parallel sequential scan, and
at fifteen-minute cadence it stopped finishing: every run died at exactly 600
seconds, cut by a TCP proxy between the client and the database while the
database itself logged nothing, having terminated nothing.

The next obvious one is to make that aggregate use the (site, stream, channel,
ts) index. Forcing it produced a *worse* plan — cost 4.64M against 2.16M — since
59M index entries are not cheaper to walk than the heap. The index was never the
missing piece; the full scan was.

What works is making the work proportional to what arrived rather than to what
is stored. Channels come from a loose index scan, which walks the btree one
group at a time and found 220 channels in 0.05s. Each channel's new rows are
then counted from the watermark the catalog already holds in ``last_ts``, which
is an exact index prefix and costs about a millisecond. Measured end to end:
0.96s.

``full=True`` ignores the watermarks and recomputes exactly (61s on that same
table). That is the re-baseline for the one change an incremental pass cannot
observe: rows backfilled behind a channel's ``last_ts``.
"""

from __future__ import annotations

from datetime import UTC, datetime

#: Distinct (site, stream, channel) without reading the table: a loose index
#: scan walks the btree one group at a time. 220 groups in 0.05s, where the
#: equivalent GROUP BY reads every row.
_GROUPS_SQL = """
WITH RECURSIVE t AS (
    (SELECT site, stream, channel FROM {src} ORDER BY site, stream, channel LIMIT 1)
    UNION ALL
    SELECT n.* FROM t, LATERAL (
        SELECT s.site, s.stream, s.channel FROM {src} s
        WHERE (s.site, s.stream, s.channel) > (t.site, t.stream, t.channel)
        ORDER BY s.site, s.stream, s.channel LIMIT 1
    ) n
)
SELECT site, stream, channel FROM t
"""

#: One channel's delta. The predicate is an exact prefix of the existing index,
#: so this is a range scan over only the rows above the watermark.
_DELTA_SQL = """
SELECT count(*) AS n, min(ts) AS first_ts, max(ts) AS last_ts, min(unit) AS unit
FROM {src}
WHERE site = :site AND stream = :stream AND channel = :channel
"""


def _instant(value):
    """A comparable datetime from whatever the driver or the catalog handed us.

    Postgres returns `min(ts)` as a datetime; the catalog columns are Text, so
    the stored watermark comes back as a string — and the two formats differ in
    fractional digits ("…15.458+00" vs "…15.458000+00:00"), so comparing them as
    strings is wrong even when it does not raise. SQLite returns strings on both
    sides, which is why this only showed up against the real database.
    """
    from datetime import datetime

    if value is None or isinstance(value, datetime):
        return value
    text_value = str(value).strip()
    try:
        return datetime.fromisoformat(text_value)
    except ValueError:
        # Trailing "+00" rather than "+00:00" is accepted by Postgres, not by
        # fromisoformat on every version we support.
        if len(text_value) > 3 and text_value[-3] in "+-":
            try:
                return datetime.fromisoformat(text_value + ":00")
            except ValueError:
                pass
        return None


def _newest(*values):
    known = [(_instant(v), v) for v in values if v is not None]
    known = [(d, v) for d, v in known if d is not None]
    return max(known)[1] if known else None


def _oldest(*values):
    known = [(_instant(v), v) for v in values if v is not None]
    known = [(d, v) for d, v in known if d is not None]
    return min(known)[1] if known else None


def project_catalog(session, *, source: str = "gold.signals", full: bool = False) -> None:
    """Refresh the serving catalog from the gold tier.

    ``full=True`` recomputes every channel from scratch, ignoring watermarks.
    Use it to re-baseline after data is backfilled behind a channel's last_ts,
    which is the one change an incremental pass cannot observe.
    """
    from sqlalchemy import text

    # The loose index scan is a Postgres optimisation — it needs row comparison
    # and LATERAL. Elsewhere (the SQLite the tests run on) fall back to DISTINCT,
    # which is correct everywhere and only slow on a table the size of the node's.
    dialect = session.get_bind().dialect.name
    discover = (
        _GROUPS_SQL.format(src=source)
        if dialect == "postgresql"
        else f"SELECT DISTINCT site, stream, channel FROM {source} "
             "ORDER BY site, stream, channel"
    )
    groups = session.execute(text(discover)).fetchall()
    if not groups:
        # An empty read is a fault, not an instruction. The database may be
        # unreachable, a migration mid-flight, the view mid-rebuild. Changing
        # nothing leaves the catalog stale, which is recoverable; treating it
        # as "there are no channels" would retire every row below and make a
        # transient fault look like data loss.
        return

    existing = {}
    for site, stream, channel, rows, first_ts, last_ts, unit in session.execute(
        text(
            "SELECT site, stream, channel, rows, first_ts, last_ts, unit "
            "FROM site_catalog_channel"
        )
    ):
        existing[(site, stream, channel)] = (rows, first_ts, last_ts, unit)

    # `full` discards the watermarks, not the knowledge of which rows exist —
    # retirement below needs the latter in both modes.
    known = {} if full else dict(existing)

    now = datetime.now(UTC)
    for site, stream, channel in groups:
        prior = known.get((site, stream, channel))
        sql = _DELTA_SQL.format(src=source)
        params = {"site": site, "stream": stream, "channel": channel}
        if prior is not None and prior[2]:
            # Count only what arrived after the watermark we already hold.
            sql += " AND ts > :watermark"
            params["watermark"] = prior[2]

        n, first_ts, last_ts, unit = session.execute(text(sql), params).fetchone()
        if prior is None:
            if not n:
                continue
            rows, first, last = n, first_ts, last_ts
            unit = unit or ""
        else:
            old_rows, old_first, old_last, old_unit = prior
            if not n:
                continue  # nothing new; leave the row exactly as it is
            rows = (old_rows or 0) + n
            # Bound rather than replace: an incremental pass only ever widens
            # the span it already knows about. Compared as instants, not as
            # strings — the two sides arrive in different formats.
            first = _oldest(old_first, first_ts)
            last = _newest(old_last, last_ts)
            unit = unit or old_unit or ""

        session.execute(
            text(
                "INSERT INTO site_catalog_channel "
                "  (site, stream, channel, unit, rows, first_ts, last_ts, updated_at) "
                "VALUES (:site, :stream, :channel, :unit, :rows, :first_ts, :last_ts, :updated_at) "
                "ON CONFLICT (site, stream, channel) DO UPDATE SET "
                "  unit = excluded.unit, rows = excluded.rows, "
                "  first_ts = excluded.first_ts, last_ts = excluded.last_ts, "
                "  updated_at = excluded.updated_at"
            ),
            {
                "site": site, "stream": stream, "channel": channel,
                "unit": unit or "", "rows": rows,
                "first_ts": str(first), "last_ts": str(last), "updated_at": now,
            },
        )

    # Retire what the source no longer has.
    #
    # The counts above are incremental, but the channel list is not: `discover`
    # enumerates every distinct (site, stream, channel) in the source on every
    # pass. So a key that is absent from it is genuinely gone, and that holds on
    # an incremental pass as much as a full one — which matters, because the
    # scheduled pass is the incremental one and a channel would otherwise be
    # advertised until somebody remembered to re-baseline.
    #
    # Found live: the catalog offered tamu-bubbleloop/loop.instrument with three
    # channels at 978 rows each while silver held none. The projection only ever
    # visited channels the source still had, so a vanished one was never looked
    # at and its row survived every pass. A partner clicking that gets an empty
    # chart, which reads as their data being broken.
    live = {(site, stream, channel) for site, stream, channel in groups}
    for site, stream, channel in [key for key in existing if key not in live]:
        session.execute(
            text(
                "DELETE FROM site_catalog_channel "
                "WHERE site = :site AND stream = :stream AND channel = :channel"
            ),
            {"site": site, "stream": stream, "channel": channel},
        )

    session.commit()


__all__ = ["project_catalog"]
