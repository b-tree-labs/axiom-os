# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""What a site holds, maintained where it changes rather than counted on read.

Asking "what channels does this site have, in what units, over what span" was a
`GROUP BY` over every row the site owns. On a site with 29.5 million of them
that is a parallel sequential scan reading two million buffers: **8.9 seconds**,
paid by whoever opened a page, every five minutes, for an answer that changes
only when something is conformed.

So it is written when it changes. Conform knows exactly which
`(site, feed, channel)` it touched and what it wrote, and updating one row per
channel there costs nothing measurable against the work it is already doing.

## What it is not

Not a cache. A cache is allowed to be wrong for a while and is rebuilt from the
truth; this IS the truth about a site's shape, with the same standing as the
rows it summarises. What makes that safe is that it is only ever written by the
thing that writes the rows, and that a full rebuild is always available and
always agrees.

## Why the counts are what they are

`rows` is a count of what conform wrote, accumulated. It is exact after a
rebuild and exact after any sequence of conform passes, because a pass reports
what it actually inserted rather than what it was handed — an idempotent
re-run inserts nothing and adds nothing.

`first` and `last` widen; they never narrow. A channel whose oldest reading was
deleted would keep the old `first` until a rebuild, which is the right trade: a
span that is too wide is visibly a span, and a span that silently narrows hides
that data went missing.

## The revision, and why everything downstream wants it

Every write stamps `computed_at`. The newest stamp for a site is its
**revision**, and reading it is one row. That is what lets a cache anywhere —
in the serving process, in a browser — hold an answer indefinitely and check
whether it is still current for almost nothing. Without it every layer has to
guess with a timer, and a timer is either too slow to be correct or too fast to
be a cache.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

__all__ = [
    "CATALOGUE_TABLE",
    "CatalogueRow",
    "ensure_schema",
    "read",
    "rebuild",
    "record",
    "revision",
]

CATALOGUE_TABLE = "gold.signal_catalogue"

_DDL = f"""
CREATE SCHEMA IF NOT EXISTS gold;
CREATE TABLE IF NOT EXISTS {CATALOGUE_TABLE} (
    site         text        NOT NULL,
    feed         text        NOT NULL,
    channel      text        NOT NULL,
    unit         text,
    rows         bigint      NOT NULL DEFAULT 0,
    first_ts     timestamptz,
    last_ts      timestamptz,
    computed_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (site, feed, channel)
);
CREATE INDEX IF NOT EXISTS signal_catalogue_site_computed
    ON {CATALOGUE_TABLE} (site, computed_at DESC);
-- `stream` became `feed` on 2026-09-28. A deployed catalogue still carries the
-- old name, and CREATE TABLE IF NOT EXISTS is a no-op over it, so the rename
-- has to be said out loud. Metadata only: the primary key follows the column
-- by identity rather than by name.
DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.columns
              WHERE table_schema = 'gold' AND table_name = 'signal_catalogue'
                AND column_name = 'stream')
     AND NOT EXISTS (SELECT 1 FROM information_schema.columns
              WHERE table_schema = 'gold' AND table_name = 'signal_catalogue'
                AND column_name = 'feed')
  THEN ALTER TABLE gold.signal_catalogue RENAME COLUMN stream TO feed;
  END IF;
END $$;
"""


@dataclass(frozen=True)
class CatalogueRow:
    site: str
    feed: str
    channel: str
    unit: str
    rows: int
    first: str
    last: str
    computed_at: str

    def as_document(self) -> dict[str, Any]:
        """The shape a serving lane hands out."""
        return {
            "feed": self.feed,
            "channel": self.channel,
            "unit": self.unit or "",
            "rows": self.rows,
            "first": self.first,
            "last": self.last,
        }


def ensure_schema(cur) -> None:
    """Create the table if it is not there. Safe to call on every pass."""
    cur.execute(_DDL)


def record(cur, site: str, written: Iterable[Mapping[str, Any]]) -> int:
    """Fold what a conform pass wrote into the catalogue. Returns rows touched.

    *written* is one mapping per channel the pass actually inserted for:
    ``{"feed", "channel", "unit", "rows", "first", "last"}``. A pass that
    inserted nothing passes nothing and the revision does not move, which is
    what keeps an idempotent re-run from busting every cache downstream.
    """
    touched = 0
    for entry in written:
        if not entry.get("rows"):
            continue
        cur.execute(
            f"""
            INSERT INTO {CATALOGUE_TABLE}
                   (site, feed, channel, unit, rows, first_ts, last_ts, computed_at)
            VALUES (%(site)s, %(feed)s, %(channel)s, %(unit)s, %(rows)s,
                    %(first)s, %(last)s, now())
            ON CONFLICT (site, feed, channel) DO UPDATE SET
                -- A unit only ever ARRIVES here. A pass that wrote rows with no
                -- unit must not erase a unit an earlier pass established, or a
                -- single un-declared batch would silently un-declare a channel.
                unit  = COALESCE(EXCLUDED.unit, {CATALOGUE_TABLE}.unit),
                rows  = {CATALOGUE_TABLE}.rows + EXCLUDED.rows,
                -- Widen, never narrow. A span that is too wide is visibly a
                -- span; one that silently narrows hides that data went missing.
                first_ts = LEAST({CATALOGUE_TABLE}.first_ts, EXCLUDED.first_ts),
                last_ts  = GREATEST({CATALOGUE_TABLE}.last_ts, EXCLUDED.last_ts),
                computed_at = now()
            """,
            {
                "site": site,
                **{k: entry.get(k) for k in ("feed", "channel", "unit", "rows", "first", "last")},
            },
        )
        touched += 1
    return touched


def rebuild(cur, site: str) -> int:
    """Recompute a site from its rows. The expensive, always-correct path.

    The only thing that repairs a count after rows are deleted outside conform,
    and the thing a new site needs once. Everything else folds in as it writes.
    """
    cur.execute(
        f"""
        INSERT INTO {CATALOGUE_TABLE}
               (site, feed, channel, unit, rows, first_ts, last_ts, computed_at)
        SELECT site, feed, channel, min(unit), count(*), min(ts), max(ts), now()
          FROM silver.signals
         WHERE site = %s
         GROUP BY site, feed, channel
        ON CONFLICT (site, feed, channel) DO UPDATE SET
            unit = EXCLUDED.unit,
            rows = EXCLUDED.rows,
            first_ts = EXCLUDED.first_ts,
            last_ts = EXCLUDED.last_ts,
            computed_at = now()
        """,
        (site,),
    )
    rebuilt = cur.rowcount
    # A channel that no longer has rows is no longer a channel. Left behind it
    # would offer a picker something that draws nothing.
    cur.execute(
        f"""
        DELETE FROM {CATALOGUE_TABLE} c
         WHERE c.site = %s
           AND NOT EXISTS (SELECT 1 FROM silver.signals s
                            WHERE s.site = c.site AND s.feed = c.feed
                              AND s.channel = c.channel)
        """,
        (site,),
    )
    return rebuilt


def read(cur, site: str) -> list[CatalogueRow]:
    """A site's channels, from the catalogue. One index range."""
    cur.execute(
        f"""SELECT site, feed, channel, unit, rows, first_ts, last_ts, computed_at
              FROM {CATALOGUE_TABLE} WHERE site = %s
             ORDER BY feed, channel""",
        (site,),
    )
    return [
        CatalogueRow(
            site=r[0],
            feed=r[1],
            channel=r[2],
            unit=r[3] or "",
            rows=int(r[4]),
            first=r[5].isoformat() if r[5] else "",
            last=r[6].isoformat() if r[6] else "",
            computed_at=r[7].isoformat() if r[7] else "",
        )
        for r in cur.fetchall()
    ]


def revision(cur, site: str) -> str:
    """This site's revision: the newest stamp in its catalogue, or "".

    One row, by index. This is what every cache downstream checks instead of
    guessing with a timer — a timer is either too slow to be correct or too
    fast to be a cache.
    """
    cur.execute(f"SELECT max(computed_at) FROM {CATALOGUE_TABLE} WHERE site = %s", (site,))
    got = cur.fetchone()
    return got[0].isoformat() if got and got[0] else ""
