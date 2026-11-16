# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Prove, from the database and the bronze files, that data is flowing bronze → silver → gold.

"The pipeline is healthy" has been wrong more than once while every individual
check was green: a conform loop that advanced but never read a new connector,
a feed that stopped at its source while the pipeline was fine, a deploy that
left the schema behind the code. Row counts cannot tell those apart, and
neither can a lag number alone. This module separates them, per connector:

``FLOWING``
    Silver is current for the feed's own cadence, nothing is waiting in bronze,
    and gold serves the same latest reading silver holds.
``SOURCE_SILENT``
    Bronze has nothing the conform pass has not read, and silver has stopped
    advancing. The pipeline did its job; the producer stopped. A different
    person fixes this one.
``BACKLOG``
    Bytes are waiting in bronze and the watermarks are still moving: catching
    up, not stuck. Reported with how far behind it is.
``PIPELINE_STUCK``
    Bytes are waiting in bronze and the watermarks have not moved within
    ``stuck_after_s``. The pipeline, not the producer, is what to chase.
``NEVER_FLOWED``
    A connector is configured and silver holds nothing for its site.
``GOLD_DISAGREES``
    Silver's latest reading is not what gold serves.

The report also carries the two facts whose absence caused the failures this
was written after: whether the schema stamp is current (so a deploy's schema
step will take no lock), and which sessions are idle in a transaction on
``silver.signals`` and for how long.

Every claim is read from the catalogue, the watermark table or the filesystem
at the moment of the call; nothing is remembered between calls. A reader can
verify any line by running the query in this module.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

FLOWING = "FLOWING"
SOURCE_SILENT = "SOURCE_SILENT"
BACKLOG = "BACKLOG"
PIPELINE_STUCK = "PIPELINE_STUCK"
NEVER_FLOWED = "NEVER_FLOWED"
GOLD_DISAGREES = "GOLD_DISAGREES"

#: Verdicts that mean the pipeline, not the producer, needs attention.
PIPELINE_FAULTS = frozenset({PIPELINE_STUCK, NEVER_FLOWED, GOLD_DISAGREES})

#: A feed is stale past this multiple of its own observed gap (as gold.ingest_stale).
LAG_FACTOR = 4.0
#: Watermarks that have not moved in this long, with bytes waiting, are stuck.
STUCK_AFTER_S = 1800.0
#: A session this long silent inside a transaction is called out.
IDLE_TXN_REPORT_S = 60


@dataclass
class FeedProof:
    connector: str
    site: str
    feed: str | None
    verdict: str
    reasons: list[str] = field(default_factory=list)
    bronze_bytes: int = 0
    conformed_bytes: int = 0
    backlog_bytes: int = 0
    bronze_newest_age_s: float | None = None
    watermark_age_s: float | None = None
    silver_points: int | None = None
    silver_last_ts: str | None = None
    silver_lag_s: float | None = None
    typical_gap_s: float | None = None
    gold_last_ts: str | None = None


def _bronze_files(root: Path, connector: str) -> tuple[int, float | None, str]:
    base = root / connector
    total = 0
    newest = None
    for dirpath, _dirs, files in os.walk(base):
        if "_rows" not in Path(dirpath).parts:
            continue
        for name in files:
            if not name.endswith(".jsonl"):
                continue
            try:
                st = os.stat(os.path.join(dirpath, name))
            except OSError:
                continue
            total += st.st_size
            newest = st.st_mtime if newest is None else max(newest, st.st_mtime)
    return total, newest, str(base)


def _watermarks(cur, root: Path, connector: str) -> tuple[int, float | None]:
    """Bytes the conform pass has read of one connector, and seconds since it last moved.

    Watermark paths are stored relative to the bronze root, so a connector's
    files are the ones under ``<connector>/``.
    """
    from .conformance.watermarks import WATERMARKS_TABLE

    cur.execute("SELECT to_regclass(%s)", (WATERMARKS_TABLE,))
    if cur.fetchone()[0] is None:
        return 0, None
    cur.execute(
        f"SELECT coalesce(sum(offset_bytes), 0), "
        f"extract(epoch FROM now() - max(updated_at)) "
        f"FROM {WATERMARKS_TABLE} WHERE root = %s AND path LIKE %s",
        (str(root), connector + "/%"),
    )
    conformed, age = cur.fetchone()
    return int(conformed or 0), (float(age) if age is not None else None)


def _silver(cur, site: str) -> list[dict[str, Any]]:
    cur.execute(
        "SELECT feed, points, last_ts, extract(epoch FROM lag), "
        "extract(epoch FROM typical_gap) FROM gold.ingest_freshness WHERE site = %s ORDER BY feed",
        (site,),
    )
    return [
        {"feed": r[0], "points": int(r[1]), "last_ts": r[2], "lag": float(r[3]),
         "typical_gap": float(r[4]) if r[4] is not None else None}
        for r in cur.fetchall()
    ]


def _gold_latest(cur, site: str, feed: str):
    cur.execute(
        "SELECT max(ts) FROM gold.signals_latest WHERE site = %s AND feed = %s", (site, feed)
    )
    row = cur.fetchone()
    return row[0] if row else None


def _holders(cur) -> list[dict]:
    from .conformance import schema_state

    try:
        found = schema_state.holders(cur)
    except Exception:  # noqa: BLE001 — a status report must not die on its own probe
        return []
    return [h for h in found if h["state"] == "idle in transaction" and h["silent_s"] >= IDLE_TXN_REPORT_S]


def _judge(proof: FeedProof, *, stuck_after_s: float) -> None:
    waiting = proof.backlog_bytes > 0
    if proof.silver_points in (None, 0):
        proof.verdict = NEVER_FLOWED
        proof.reasons.append(
            "bronze holds data but silver holds none for this site"
            if proof.bronze_bytes
            else "nothing in bronze or silver yet"
        )
        return
    if proof.gold_last_ts is not None and proof.silver_last_ts is not None:
        if str(proof.gold_last_ts) != str(proof.silver_last_ts):
            proof.verdict = GOLD_DISAGREES
            proof.reasons.append(
                f"silver latest {proof.silver_last_ts} but gold serves {proof.gold_last_ts}"
            )
            return
    if waiting:
        moved = proof.watermark_age_s is not None and proof.watermark_age_s < stuck_after_s
        if moved:
            proof.verdict = BACKLOG
            proof.reasons.append(
                f"{proof.backlog_bytes} bytes waiting in bronze; the conform pass advanced "
                f"{proof.watermark_age_s:.0f}s ago"
            )
        else:
            proof.verdict = PIPELINE_STUCK
            age = "never" if proof.watermark_age_s is None else f"{proof.watermark_age_s:.0f}s ago"
            proof.reasons.append(
                f"{proof.backlog_bytes} bytes waiting in bronze and the conform pass last advanced {age}"
            )
        return
    gap = proof.typical_gap_s
    if gap and proof.silver_lag_s is not None and proof.silver_lag_s > LAG_FACTOR * gap:
        proof.verdict = SOURCE_SILENT
        proof.reasons.append(
            f"nothing waiting in bronze; silver is {proof.silver_lag_s:.0f}s behind a "
            f"{gap:.0f}s cadence, so the producer stopped sending"
        )
        return
    proof.verdict = FLOWING
    proof.reasons.append("silver current for the feed's own cadence, nothing waiting, gold agrees")


def prove(
    cur,
    connectors: list[Any],
    *,
    stuck_after_s: float = STUCK_AFTER_S,
    observe_s: float = 0.0,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, Any]:
    """Judge every configured connector. Reads only.

    *observe_s* > 0 samples the conform watermarks twice that far apart, so
    "advancing" is observed rather than inferred from a timestamp.
    """
    from .conformance import GOLD_SIGNALS_DDL, SILVER_DDL, schema_state

    started = now()
    results: list[FeedProof] = []
    first: dict[str, int] = {}
    if observe_s > 0:
        for c in connectors:
            conformed, _ = _watermarks(cur, Path(c.bronze_root), c.name)
            first[c.name] = conformed
        sleep(observe_s)

    for c in connectors:
        root = Path(c.bronze_root)
        total, newest, _prefix = _bronze_files(root, c.name)
        conformed, wm_age = _watermarks(cur, root, c.name)
        base = dict(
            connector=c.name,
            site=c.site,
            bronze_bytes=total,
            conformed_bytes=min(conformed, total),
            backlog_bytes=max(0, total - conformed),
            bronze_newest_age_s=(time.time() - newest) if newest else None,
            watermark_age_s=wm_age,
        )
        feeds = _silver(cur, c.site)
        if not feeds:
            p = FeedProof(feed=None, verdict="", silver_points=0, **base)
            _judge(p, stuck_after_s=stuck_after_s)
            results.append(p)
            continue
        for f in feeds:
            gold_last = _gold_latest(cur, c.site, f["feed"])
            p = FeedProof(
                feed=f["feed"],
                verdict="",
                silver_points=f["points"],
                silver_last_ts=str(f["last_ts"]),
                silver_lag_s=f["lag"],
                typical_gap_s=f["typical_gap"],
                gold_last_ts=str(gold_last) if gold_last is not None else None,
                **base,
            )
            if observe_s > 0 and p.backlog_bytes > 0:
                moved = conformed > first.get(c.name, conformed)
                if moved:
                    p.watermark_age_s = 0.0
            _judge(p, stuck_after_s=stuck_after_s)
            results.append(p)

    cur.execute("SELECT 1")
    current = None
    try:
        no_index = [s for s in (*SILVER_DDL, *GOLD_SIGNALS_DDL) if "CREATE INDEX" not in s.upper()]
        current = schema_state.is_current(cur, no_index, superset=[*SILVER_DDL, *GOLD_SIGNALS_DDL])
    except Exception:  # noqa: BLE001
        current = None
    blockers = _holders(cur)
    faults = [p for p in results if p.verdict in PIPELINE_FAULTS]
    overall = (
        "FLOWING"
        if not faults and results and all(p.verdict in (FLOWING, SOURCE_SILENT, BACKLOG) for p in results)
        else ("NO_CONNECTORS" if not results else "ATTENTION")
    )
    return {
        "overall": overall,
        "generated_at": started.isoformat(),
        "observed_s": observe_s,
        "schema_current": current,
        "idle_in_transaction": blockers,
        "feeds": [asdict(p) for p in results],
        "counts": {v: sum(1 for p in results if p.verdict == v)
                   for v in (FLOWING, SOURCE_SILENT, BACKLOG, PIPELINE_STUCK, NEVER_FLOWED, GOLD_DISAGREES)},
    }
