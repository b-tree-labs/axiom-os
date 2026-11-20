# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A producing site's sharing policy, read from its ``[share]`` table (ADR-180 §4).

The forwarder sends what :meth:`TableSharePolicy.allows` returns and waits on
:meth:`TableSharePolicy.hold_until`. The table::

    [share]
    feeds = ["<feed>"]        # required; ["*"] shares every feed
    channels = []              # optional narrowing within a feed
    since = "2026-10-01"       # optional; nothing older is sent
    delay_minutes = 0          # optional embargo after a batch lands locally

A policy decides only what leaves from now on. It never deletes anything the
host already holds, and turning sharing off is stopping the forwarder, not a
policy that excludes everything (which would advance the cursor past data the
site may later decide to share). A batch the policy excludes is passed over;
widening the policy later is filled by reconcile, not by rewinding the
forwarder.
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta
from typing import Any


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)


class TableSharePolicy:
    def __init__(self, table: dict[str, Any]) -> None:
        t = dict(table or {})
        if "feeds" not in t:
            raise ValueError(
                '[share] needs a "feeds" list: the feeds that may leave this node, or ["*"] for all. '
                "Nothing is assumed."
            )
        self.feeds = [str(f) for f in t["feeds"]]
        self.all_feeds = "*" in self.feeds
        self.channels = {str(c) for c in t.get("channels") or []}
        since = t.get("since")
        self.since = _parse_ts(f"{since}T00:00:00+00:00" if since and len(str(since)) == 10 else since)
        if since and self.since is None:
            raise ValueError(f'[share] since = "{since}" is not a date (YYYY-MM-DD) or timestamp')
        self.delay = timedelta(minutes=float(t.get("delay_minutes") or 0))

    def allows(self, record: dict, rows: list[dict]) -> list[dict]:
        out: list[dict] = []
        for row in rows:
            feed = str(row.get("feed") or (record.get("metadata") or {}).get("feed") or "")
            if not self.all_feeds and feed not in self.feeds:
                continue
            if self.since is not None:
                ts = _parse_ts(row.get("ts"))
                if ts is None or ts < self.since:
                    continue
            if self.channels:
                values = row.get("values")
                if isinstance(values, dict):
                    kept = {k: v for k, v in values.items() if k in self.channels}
                    if not kept:
                        continue
                    if len(kept) != len(values):
                        row = copy.deepcopy(row)
                        row["values"] = kept
                        row.setdefault("tags", {})["share_subset"] = "channels"
                elif str(row.get("channel") or "") not in self.channels:
                    continue
            out.append(row)
        return out

    def hold_until(self, record: dict) -> datetime | None:
        if not self.delay:
            return None
        landed = _parse_ts(record.get("received_at"))
        return landed + self.delay if landed is not None else None

    def describe(self) -> str:
        parts = ["every feed" if self.all_feeds else "feeds " + ", ".join(self.feeds)]
        if self.channels:
            parts.append("channels " + ", ".join(sorted(self.channels)))
        if self.since is not None:
            parts.append("from " + self.since.date().isoformat())
        if self.delay:
            parts.append(f"after a {int(self.delay.total_seconds() // 60)} minutes delay")
        return ("Sharing " + "; ".join(parts) + ". Stopping sharing never deletes "
                "what the host already holds.")


__all__ = ["TableSharePolicy"]
