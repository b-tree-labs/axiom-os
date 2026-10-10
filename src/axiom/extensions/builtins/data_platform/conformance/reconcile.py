# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Compare two copies of a site's readings, fill gaps, flag conflicts (ADR-180 §5).

One copy is the producing site's own node ("local"), the other the host's
("upstream"). Neither is assumed right: the node property
``record_of_truth`` (``"local"`` or ``"upstream"``) decides only which way a
gap may be filled.

Windows are ``(site, feed, channel, source_class, hour)``. Each side reports a
count and an order-stable hash of its readings in the window. Only a window
whose summaries differ is fetched row by row and compared on the reading
identity ``(site, feed, channel, ts, source_class)``.

====================  ==============================================================
finding               action
====================  ==============================================================
local only            filled upstream (record of truth ``upstream``, or ``local`` and
                      the site's sharing allows the reading)
upstream only         filled locally when the record of truth is ``upstream``, or
                      when a local-first site opted in; otherwise counted as
                      ``not_filled_by_choice``. A reading older than the site's own
                      retention is ``expired_locally``: never filled, never a delete
both, different       a conflict: both values kept, reported, recorded in
                      ``silver.reading_conflicts`` on the local side. Never resolved
====================  ==============================================================

A filled reading keeps its ``row_hash``, so the receiving side deduplicates it
exactly as if it had arrived the normal way.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from . import pg_upsert

_IDENTITY = ("site", "feed", "channel", "ts", "source_class")
_COMPARED = ("value", "unit", "quality")
_COLUMNS = (
    "site", "feed", "channel", "ts", "value", "unit", "quality", "source_class",
    "schema_ref", "row_hash", "model_ref", "basis", "uncertainty", "role",
    "derivation", "quality_reason",
)

CONFLICTS_DDL = """CREATE TABLE IF NOT EXISTS silver.reading_conflicts (
    site text NOT NULL, feed text NOT NULL, channel text NOT NULL,
    ts timestamptz NOT NULL, source_class text NOT NULL,
    local_value double precision, upstream_value double precision,
    local_unit text, upstream_unit text,
    local_quality text, upstream_quality text,
    first_seen timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (site, feed, channel, ts, source_class)
)"""


class PgSide:
    """One copy, read and written through a Postgres connection."""

    def __init__(self, conn) -> None:
        self.conn = conn

    def summaries(self, *, site: str, start: str, end: str) -> dict[tuple, tuple[int, str]]:
        rows = self.conn.execute(
            """SELECT site, feed, channel, source_class, date_trunc('hour', ts) AS hour,
                      count(*),
                      md5(string_agg(
                        to_char(ts AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US') || '|' ||
                        coalesce(value::text, '') || '|' || coalesce(unit, '') || '|' || quality,
                        ',' ORDER BY ts))
               FROM silver.signals
               WHERE site = %s AND ts >= %s AND ts < %s
               GROUP BY 1, 2, 3, 4, 5""",
            (site, start, end),
        ).fetchall()
        return {tuple(r[:5]): (int(r[5]), r[6]) for r in rows}

    def rows(self, window: tuple) -> dict[tuple, dict[str, Any]]:
        site, feed, channel, source_class, hour = window
        cur = self.conn.execute(
            f"""SELECT {", ".join(_COLUMNS)} FROM silver.signals
                WHERE site = %s AND feed = %s AND channel = %s AND source_class = %s
                  AND ts >= %s AND ts < %s + interval '1 hour'""",
            (site, feed, channel, source_class, hour, hour),
        )
        out = {}
        for rec in cur.fetchall():
            row = dict(zip(_COLUMNS, rec, strict=True))
            out[tuple(row[k] for k in _IDENTITY)] = row
        return out

    def write(self, rows: list[dict[str, Any]]) -> None:
        up = pg_upsert(self.conn.cursor())
        for row in rows:
            up({k: v for k, v in row.items() if v is not None or k in ("value", "unit")})

    def record_conflicts(self, conflicts: list[dict[str, Any]]) -> None:
        self.conn.execute(CONFLICTS_DDL)
        for c in conflicts:
            # ADR-128 E4: reconcile maintains the tier, recording where two
            # copies of it disagree.
            self.conn.execute(
                """INSERT INTO silver.reading_conflicts
                   (site, feed, channel, ts, source_class, local_value, upstream_value,
                    local_unit, upstream_unit, local_quality, upstream_quality)
                   VALUES (%(site)s, %(feed)s, %(channel)s, %(ts)s, %(source_class)s,
                           %(local_value)s, %(upstream_value)s, %(local_unit)s,
                           %(upstream_unit)s, %(local_quality)s, %(upstream_quality)s)
                   ON CONFLICT (site, feed, channel, ts, source_class) DO NOTHING""",
                c,
            )


@dataclass
class ReconcileReport:
    windows_compared: int = 0
    windows_differing: int = 0
    filled_local: int = 0
    filled_upstream: int = 0
    not_filled_by_choice: int = 0
    not_shared: int = 0
    expired_locally: int = 0
    conflicts: int = 0
    conflict_list: list[dict[str, Any]] = field(default_factory=list)
    #: The mode this run applied, so a report says which copy it treated as truth.
    record_of_truth: str = ""


def _as_dt(value: str | datetime | None) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def reconcile(
    local,
    upstream,
    *,
    site: str,
    start: str,
    end: str,
    record_of_truth: str | None = None,
    accept_upstream_fill: bool = False,
    may_share: Callable[[dict[str, Any]], bool] | None = None,
    local_retained_from: str | datetime | None = None,
    dry_run: bool = False,
) -> ReconcileReport:
    if record_of_truth is None:
        # The node's declaration (ADR-180 §3): the mode is a property of the
        # node, so a scheduled reconcile follows a switch without a code path
        # of its own.
        from axiom.infra import node_functions

        record_of_truth = node_functions.load().record_of_truth_in_force
    if record_of_truth not in ("local", "upstream"):
        raise ValueError('record_of_truth must be "local" or "upstream"')
    retained_from = _as_dt(local_retained_from)
    report = ReconcileReport(record_of_truth=record_of_truth)
    mine = local.summaries(site=site, start=start, end=end)
    theirs = upstream.summaries(site=site, start=start, end=end)
    to_local: list[dict] = []
    to_upstream: list[dict] = []
    conflicts: list[dict] = []

    for window in sorted(set(mine) | set(theirs), key=lambda w: (w[4], w[:4])):
        report.windows_compared += 1
        if mine.get(window) == theirs.get(window):
            continue
        report.windows_differing += 1
        a = local.rows(window) if window in mine else {}
        b = upstream.rows(window) if window in theirs else {}
        for key in a.keys() - b.keys():
            row = a[key]
            # The site's sharing policy holds in BOTH modes (ADR-180 §4). It
            # used to be skipped in contributor mode, so a feed the site had
            # withheld reached the host through reconcile.
            if may_share is None or may_share(row):
                to_upstream.append(row)
            else:
                report.not_shared += 1
        for key in b.keys() - a.keys():
            row = b[key]
            if retained_from is not None and row["ts"] < retained_from:
                report.expired_locally += 1
            elif record_of_truth == "upstream" or accept_upstream_fill:
                to_local.append(row)
            else:
                report.not_filled_by_choice += 1
        for key in a.keys() & b.keys():
            x, y = a[key], b[key]
            if any(x[k] != y[k] for k in _COMPARED):
                conflicts.append({
                    **{k: x[k] for k in _IDENTITY},
                    "local_value": x["value"], "upstream_value": y["value"],
                    "local_unit": x["unit"], "upstream_unit": y["unit"],
                    "local_quality": x["quality"], "upstream_quality": y["quality"],
                })

    report.filled_local = len(to_local)
    report.filled_upstream = len(to_upstream)
    report.conflicts = len(conflicts)
    report.conflict_list = conflicts
    if not dry_run:
        if to_local:
            local.write(to_local)
        if to_upstream:
            upstream.write(to_upstream)
        if conflicts:
            local.record_conflicts(conflicts)
    return report


__all__ = ["CONFLICTS_DDL", "PgSide", "ReconcileReport", "reconcile"]
