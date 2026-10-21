# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""What an ingest edge may let go of, when several downstreams pull it.

A data platform that moves to a new home runs old and new in parallel: both
pull the same edge until their contents are shown to agree. Each keeps its own
cursor, so reading is already independent; what needs care is deleting. The
edge may delete a batch's bulk only once **every** downstream it serves holds
it, and only after a minimum age.

A downstream acknowledges by pulling. Its ``after`` cursor advances only once
the batches before it are durable on its side, so the cursor sent with a page
request is a statement of what it holds. Acknowledgements only move forward.

What retention deletes is the bulk: the batch payload under ``_content`` and
its rows file, the latter only once every batch recorded under the same item id
is deletable. The outbox itself is never rewritten, so sequence numbers never
restart and a replay of an old batch is still recognised. A request for a
deleted batch's content is answered ``410 Gone``, which a downstream only sees
if it lost its own cursor and has the rows already.

Settings, all optional. With none of them an edge deletes nothing and its
health answer is unchanged:

- ``AXIOM_EDGE_RETAIN_MIN_HOURS``: turn retention on, keeping everything at
  least this long.
- ``AXIOM_EDGE_MAX_LAG_HOURS``: report a downstream whose oldest unacknowledged
  batch is older than this (``/healthz`` says ``degraded`` and names it).
- ``AXIOM_EDGE_DETACH_AFTER_HOURS``: stop waiting for a downstream that far
  behind. Its alarm stays up; without this setting the edge waits for every
  downstream forever.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import defaultdict
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from .edge import DOWNSTREAM_ENV, EdgeOutbox

log = logging.getLogger(__name__)

RETAIN_ENV = "AXIOM_EDGE_RETAIN_MIN_HOURS"
MAX_LAG_ENV = "AXIOM_EDGE_MAX_LAG_HOURS"
DETACH_ENV = "AXIOM_EDGE_DETACH_AFTER_HOURS"

_ACKS = "acks.json"
_STATE = "retention.json"
_lock = threading.Lock()


def downstreams() -> list[str]:
    return [p.strip() for p in os.environ.get(DOWNSTREAM_ENV, "").split(",") if p.strip()]


def _hours(env: str) -> float | None:
    raw = os.environ.get(env, "").strip()
    if not raw:
        return None
    value = float(raw)
    if value < 0:
        raise ValueError(f"{env} must not be negative")
    return value


def _write_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, sort_keys=True))
    os.replace(tmp, path)


class EdgeAcks:
    """Each downstream's acknowledged position, persisted beside the outbox."""

    def __init__(self, directory: str | os.PathLike) -> None:
        self.path = Path(directory) / _ACKS
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def positions(self) -> dict[str, int]:
        if not self.path.exists():
            return {}
        return {k: int(v) for k, v in json.loads(self.path.read_text()).items()}

    def ack(self, principal: str, after: int) -> None:
        with _lock:
            current = self.positions()
            if after > current.get(principal, 0):
                current[principal] = int(after)
                _write_json(self.path, current)


def _age_hours(record: dict, now: float) -> float:
    try:
        received = datetime.fromisoformat(record["received_at"]).timestamp()
    except (KeyError, ValueError):
        return 0.0
    return (now - received) / 3600.0


def _state(outbox: EdgeOutbox) -> dict:
    path = outbox.dir / _STATE
    return json.loads(path.read_text()) if path.exists() else {"pruned_through": 0}


def pruned_through(outbox: EdgeOutbox) -> int:
    return int(_state(outbox).get("pruned_through", 0))


def lag_report(
    outbox: EdgeOutbox,
    acks: EdgeAcks,
    *,
    downstreams: list[str],
    max_lag_hours: float,
    now: float | None = None,
) -> dict:
    """Which downstreams have an unacknowledged batch older than ``max_lag_hours``."""
    now = time.time() if now is None else now
    positions = acks.positions()
    lagging = []
    for principal in downstreams:
        acked = positions.get(principal, 0)
        pending = outbox.read(after=acked, limit=1)
        if not pending:
            continue
        age = _age_hours(pending[0], now)
        if age > max_lag_hours:
            lagging.append(
                {
                    "principal": principal,
                    "acknowledged_through": acked,
                    "behind_batches": outbox.last_seq() - acked,
                    "oldest_unacknowledged_hours": round(age, 2),
                }
            )
    return {"status": "degraded" if lagging else "ok", "lagging": lagging}


def prune(
    outbox: EdgeOutbox,
    acks: EdgeAcks,
    *,
    downstreams: list[str],
    bronze_root_for: Callable[[str], str | os.PathLike],
    min_age_hours: float,
    detach_after_hours: float | None = None,
    now: float | None = None,
) -> dict:
    """Delete the bulk of batches every (non-detached) downstream holds and that are old enough."""
    now = time.time() if now is None else now
    result = {"deleted_batches": 0, "pruned_through": pruned_through(outbox), "detached": []}
    if not downstreams:
        return result
    positions = acks.positions()
    waiting_for: list[int] = []
    for principal in downstreams:
        acked = positions.get(principal, 0)
        pending = outbox.read(after=acked, limit=1)
        if (
            detach_after_hours is not None
            and pending
            and _age_hours(pending[0], now) > detach_after_hours
        ):
            result["detached"].append(principal)
            log.warning(
                "edge retention no longer waits for %s: %s h behind",
                principal,
                round(_age_hours(pending[0], now), 1),
            )
            continue
        waiting_for.append(acked)
    if not waiting_for:
        return result
    floor = min(waiting_for)
    start = result["pruned_through"]
    if floor <= start:
        return result

    records = outbox.read(after=0, limit=10**9)
    deletable = {
        int(r["seq"])
        for r in records
        if start < int(r["seq"]) <= floor and _age_hours(r, now) >= min_age_hours
    }
    if not deletable:
        return result
    # The highest contiguous deletable seq: retention advances without gaps.
    through = start
    while through + 1 in deletable:
        through += 1
    if through == start:
        return result

    by_item: dict[tuple[str, str], int] = defaultdict(int)
    for r in records:
        key = (r["source"], str(r.get("item_id") or ""))
        by_item[key] = max(by_item[key], int(r["seq"]))

    deleted = 0
    for r in records:
        seq = int(r["seq"])
        if not (start < seq <= through):
            continue
        root = Path(bronze_root_for(r["source"])) / r["source"]
        blob = root / "_content" / r["content_hash"][:2] / r["content_hash"]
        blob.unlink(missing_ok=True)
        item = str(r.get("item_id") or "")
        if item and by_item[(r["source"], item)] <= through:
            for sub in ("_rows", "_quarantine_rows"):
                for f in (root / sub).glob(f"*/{item}.jsonl"):
                    f.unlink(missing_ok=True)
        deleted += 1
    _write_json(outbox.dir / _STATE, {"pruned_through": through})
    result.update(deleted_batches=deleted, pruned_through=through)
    if deleted:
        log.info("edge retention deleted %d batch(es) through seq %d", deleted, through)
    return result


def maybe_prune_after_page(
    outbox: EdgeOutbox,
    *,
    principal: str,
    after: int,
    bronze_root_for: Callable[[str], str | os.PathLike],
) -> None:
    """Called on every export page request: record the acknowledgement, then
    prune if retention is on. Never fails the page request."""
    acks = EdgeAcks(outbox.dir)
    acks.ack(principal, after)
    try:
        min_age = _hours(RETAIN_ENV)
        if min_age is None:
            return
        prune(
            outbox,
            acks,
            downstreams=downstreams(),
            bronze_root_for=bronze_root_for,
            min_age_hours=min_age,
            detach_after_hours=_hours(DETACH_ENV),
        )
    except Exception:  # noqa: BLE001 - retention must never break a pull
        log.exception("edge retention failed; nothing was lost, it will retry on the next pull")


def health_detail(outbox: EdgeOutbox) -> dict:
    """Extra ``/healthz`` fields when a lag limit is set; empty otherwise."""
    max_lag = _hours(MAX_LAG_ENV)
    if max_lag is None:
        return {}
    report = lag_report(
        outbox, EdgeAcks(outbox.dir), downstreams=downstreams(), max_lag_hours=max_lag
    )
    detail = {"downstreams": report["status"], "lagging": report["lagging"]}
    if report["lagging"]:
        log.warning("edge downstream lag: %s", ", ".join(d["principal"] for d in report["lagging"]))
    return detail


__all__ = [
    "DETACH_ENV",
    "MAX_LAG_ENV",
    "RETAIN_ENV",
    "EdgeAcks",
    "downstreams",
    "health_detail",
    "lag_report",
    "maybe_prune_after_page",
    "prune",
    "pruned_through",
]
