# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A slow intake never starves a site's backlog drain behind its live data.

Found as a flaky reconnect-flood test on a loaded Mac: after the 120 s
deadline only 20 of 200 backlog records (four requests) had drained, under a
backlog budget that drains them in about a minute. Each pass sends live first,
then backlog requests until a ``backlog_slice_s`` deadline. That deadline was
set before the live lane ran, so a pass whose live requests took longer than
the slice (a busy intake, or any intake a WAN round trip away) reached the
backlog with its slice already spent and sent none. A site with a collector
running and a slow link would hold its outage backlog for as long as live
data kept arriving.

Here a fake clock makes every live request take longer than the backlog slice
while the collector keeps adding live data. Each pass must still start at
least one backlog request, after live, so the backlog drains and live still
waits behind at most one backlog request.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from axiom.extensions.builtins.data_platform.forward import Forwarder
from axiom.extensions.builtins.data_platform.forward.forwarder import Probe
from axiom.extensions.builtins.data_platform.sources.edge.puller import FileCursor

SOURCE = "site-a-src"
LIVE_REQUEST_S = 0.6  # longer than the forwarder's 0.5 s backlog slice
BACKLOG_REQUEST_S = 0.05


class Clock:
    def __init__(self) -> None:
        self.t = 1_800_000_000.0

    def __call__(self) -> float:
        return self.t


class Outbox:
    def __init__(self, root: Path, clock: Clock) -> None:
        self.root, self.clock = root, clock
        self.recs: list[dict] = []
        self.blobs: dict[str, bytes] = {}

    def add(self, prefix: str, n: int = 1) -> None:
        for _ in range(n):
            seq = len(self.recs) + 1
            blob = json.dumps([{"channel": "c", "ts": f"t{seq}", "value": seq}]).encode()
            h = hashlib.sha256(blob).hexdigest()
            self.blobs[h] = blob
            self.recs.append({
                "seq": seq, "source": SOURCE, "content_hash": h, "item_id": f"{prefix}-{seq}",
                "schema_ref": "s/v1", "metadata": {},
                "received_at": datetime.fromtimestamp(self.clock.t, UTC).isoformat(),
            })

    def records(self, after: int, limit: int) -> list[dict]:
        return [r for r in self.recs if r["seq"] > after][:limit]

    def last_seq(self) -> int:
        return len(self.recs)

    @property
    def directory(self) -> Path:
        return self.root

    def content(self, source: str, content_hash: str) -> bytes:
        return self.blobs[content_hash]


class SlowIntake:
    """An intake a live request takes 0.6 s to reach; notes what it was sent, in order."""

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.refusal = None
        self.events: list[tuple[str, list[str]]] = []

    def probe(self) -> Probe:
        return Probe(True, "")

    def send(self, rec: dict, body: bytes, *, lane: str = "live") -> tuple[bool, str]:
        self.clock.t += LIVE_REQUEST_S if lane == "live" else BACKLOG_REQUEST_S
        self.events.append((lane, [b["item_id"] for b in json.loads(body)["batches"]]))
        return True, ""

    def beat(self, beat: dict) -> bool:
        return True


def _world(tmp_path: Path, backlog: int = 50):
    clock = Clock()
    outbox = Outbox(tmp_path, clock)
    outbox.add("old", backlog)  # collected while the intake was down
    clock.t += 3600.0  # an hour later the intake is back
    intake = SlowIntake(clock)
    fwd = Forwarder(outbox, cursor=FileCursor(tmp_path / "cursor.json"), intake=intake,  # type: ignore[arg-type]
                    up_after=1, live_window_s=1.0, backlog_records_per_request=5,
                    backlog_request_target_s=2.0, clock=clock,
                    now=lambda: datetime.fromtimestamp(clock.t, UTC))
    return clock, outbox, intake, fwd


def test_the_backlog_drains_while_every_live_request_outlasts_the_backlog_slice(tmp_path):
    clock, outbox, intake, fwd = _world(tmp_path, backlog=50)
    passes = 0
    while passes < 60 and fwd.health()["catch_up"].get("active") is not False:
        outbox.add("live", 2)  # the collector keeps going: two batches since the last pass
        fwd.forward_once()
        clock.t += 0.05
        passes += 1

    backlog_sent = [i for lane, items in intake.events if lane == "backlog" for i in items]
    assert backlog_sent, "no backlog request was ever sent while live data kept arriving"
    assert fwd.health()["catch_up"]["active"] is False, fwd.health()["catch_up"]
    assert backlog_sent == [f"old-{s}" for s in range(1, 51)]  # oldest first, each once


def test_live_still_goes_first_in_every_pass(tmp_path):
    clock, outbox, intake, fwd = _world(tmp_path, backlog=50)
    for _ in range(20):
        outbox.add("live", 2)
        waiting = {r["item_id"] for r in outbox.recs if r["item_id"].startswith("live-")} - {
            i for lane, items in intake.events if lane == "live" for i in items}
        before = len(intake.events)
        fwd.forward_once()
        clock.t += 0.05
        this_pass = intake.events[before:]
        first_backlog = next((n for n, (lane, _) in enumerate(this_pass) if lane == "backlog"), len(this_pass))
        sent_first = {i for lane, items in this_pass[:first_backlog] if lane == "live" for i in items}
        assert waiting <= sent_first, (waiting - sent_first, this_pass)
