# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""What an ingest edge holds, counted the way the producer counted it.

On a platform node ``GET /ingest/summary`` counts the published tier. An edge
has no database: what it holds is the bronze it landed, and "landed" from the
producer's side means exactly that, held durably by the intake. These counts
read that bronze for the caller's own site (the connectors bound to it) and
count one reading per non-null channel value per row, which is how the
collector's ledger counts what it sent, so the two can be compared.

Bounded the same way as the platform count: a window of at most
``MAX_CHANNEL_WINDOW_DAYS`` and ``MAX_CHANNELS`` channels. An edge holds a
retention window, not a history, so a scan of its bronze is small.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .summary import MAX_CHANNELS, MAX_STREAMS


def _when(ts: Any) -> datetime | None:
    try:
        d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=UTC)


def _site_rows(site: str, *, state_dir: Path | None = None):
    """Every distinct landed row of the connectors bound to ``site``."""
    from ..agents.plinth.connectors import list_connectors

    seen: set[str] = set()
    for conn in list_connectors(state_dir=state_dir):
        if conn.site != site or not conn.bronze_root:
            continue
        rows_dir = Path(conn.bronze_root) / conn.name / "_rows"
        if not rows_dir.is_dir():
            continue
        for f in sorted(rows_dir.rglob("*.jsonl")):
            with open(f, encoding="utf-8") as fh:
                for line in fh:
                    if not line.strip():
                        continue
                    rec = json.loads(line)
                    key = str(rec.get("row_hash") or "")
                    if key and key in seen:
                        continue
                    seen.add(key)
                    row = rec.get("row")
                    if isinstance(row, dict):
                        yield row


def count_channels(site: str, feed: str, start: datetime, end: datetime, *, state_dir: Path | None = None) -> list[dict[str, Any]]:
    """Readings per channel of one feed in ``[start, end]``, from this edge's bronze."""
    acc: dict[str, dict[str, Any]] = {}
    for row in _site_rows(site, state_dir=state_dir):
        if str(row.get("feed") or "") != feed:
            continue
        when = _when(row.get("ts"))
        if when is None or when < start or when > end:
            continue
        ts = when.isoformat()
        for name, value in dict(row.get("values") or {}).items():
            if value is None:
                continue
            c = acc.setdefault(name, {"channel": name, "rows": 0, "first_ts": ts, "last_ts": ts, "units": []})
            c["rows"] += 1
            c["first_ts"] = min(c["first_ts"], ts)
            c["last_ts"] = max(c["last_ts"], ts)
    return [acc[k] for k in sorted(acc)][:MAX_CHANNELS]


def summarize(site: str, since_hours: float | None, *, state_dir: Path | None = None) -> dict[str, Any]:
    """Readings per feed for the site, from this edge's bronze."""
    floor = None
    if since_hours is not None:
        floor = datetime.now(UTC).timestamp() - float(since_hours) * 3600
    feeds: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in _site_rows(site, state_dir=state_dir):
        when = _when(row.get("ts"))
        if when is None or (floor is not None and when.timestamp() < floor):
            continue
        n = sum(1 for v in dict(row.get("values") or {}).values() if v is not None)
        if not n:
            continue
        key = (str(row.get("feed") or ""), str(row.get("schema_id") or ""), str(row.get("source_class") or "measured"))
        ts = when.isoformat()
        f = feeds.setdefault(key, {"feed": key[0], "schema_ref": key[1], "source_class": key[2],
                                    "rows": 0, "first_ts": ts, "last_ts": ts})
        f["rows"] += n
        f["first_ts"] = min(f["first_ts"], ts)
        f["last_ts"] = max(f["last_ts"], ts)
    out = sorted(feeds.values(), key=lambda f: -f["rows"])[:MAX_STREAMS]
    return {"site": site, "feeds": out, "total_rows": sum(f["rows"] for f in out),
            "truncated": len(feeds) > MAX_STREAMS, "counted_at": "ingest edge"}


__all__ = ["count_channels", "summarize"]
