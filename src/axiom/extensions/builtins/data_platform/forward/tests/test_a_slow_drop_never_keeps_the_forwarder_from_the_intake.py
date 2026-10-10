# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A slow drop folder never keeps the forwarder from a healthy intake.

Found on the W10 replica (2 CPUs, the edge stopped): the forwarder switched to
the Box drop and sent one outbox record per rclone round trip (about 15 s
each) while the collector added several records a second. The pass sent until
the outbox was empty, which it never was, so after the intake came back the
forwarder stayed on Box for as long as the backlog lasted, never asked the
intake again, and its status file stayed at what it said when the pass began.

Here a fake clock makes the drop slow and the outbox keeps growing while it
sends. A pass now ends after a bounded slice, the intake is asked again
between slices and taken back after ``up_after`` healthy checks, the status
file moves during a drain, many records travel in one drop file, and every
batch arrives exactly once across the two ways.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from axiom.extensions.builtins.data_platform.forward import BoxDropTarget, Forwarder
from axiom.extensions.builtins.data_platform.forward.forwarder import Probe, request_body_many
from axiom.extensions.builtins.data_platform.sources.edge.puller import FileCursor

SOURCE = "site-a-src"


class Clock:
    def __init__(self) -> None:
        self.t = 1_800_000_000.0

    def __call__(self) -> float:
        return self.t


class Outbox:
    """An outbox the collector keeps adding to."""

    def __init__(self, root: Path, clock: Clock) -> None:
        self.root, self.clock = root, clock
        self.recs: list[dict] = []
        self.blobs: dict[str, bytes] = {}

    def add(self, n: int = 1, rows: int = 10) -> None:
        for _ in range(n):
            seq = len(self.recs) + 1
            blob = json.dumps([{"channel": "c", "ts": f"t{seq}-{i}", "value": i} for i in range(rows)]).encode()
            h = hashlib.sha256(blob).hexdigest()
            self.blobs[h] = blob
            self.recs.append({
                "seq": seq, "source": SOURCE, "content_hash": h, "item_id": f"item-{seq}",
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


class Intake:
    def __init__(self, clock: Clock) -> None:
        self.up, self.clock, self.items, self.probes = False, clock, [], 0
        self.refusal = None

    def probe(self) -> Probe:
        self.probes += 1
        return Probe(self.up, "" if self.up else "intake unreachable: URLError")

    def send(self, rec: dict, body: bytes, *, lane: str = "live") -> tuple[bool, str]:
        if not self.up:
            return False, "intake unreachable"
        self.clock.t += 0.05
        self.items += [b["item_id"] for b in json.loads(body)["batches"]]
        return True, ""

    def beat(self, beat: dict) -> bool:
        return True


class SlowBox:
    """A drop folder that takes 15 s a file while the collector adds two records."""

    name = "box"

    def __init__(self, clock: Clock, outbox: Outbox, *, grow_until: int, on_send=None) -> None:
        self.clock, self.outbox, self.grow_until, self.on_send = clock, outbox, grow_until, on_send
        self.files: list[bytes] = []

    def probe(self) -> Probe:
        return Probe(True, "")

    def send(self, rec: dict, body: bytes, *, first_seq: int | None = None) -> tuple[bool, str]:
        self.clock.t += 15.0
        self.files.append(body)
        if len(self.outbox.recs) < self.grow_until:
            self.outbox.add(2)
        if self.on_send:
            self.on_send(self)
        return True, ""

    @property
    def items(self) -> list[str]:
        return [b["item_id"] for f in self.files for b in json.loads(f)["batches"]]


class RecordingCursor(FileCursor):
    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.history: list[int] = []

    def set(self, after: int) -> None:
        self.history.append(after)
        super().set(after)


def _world(tmp_path: Path, *, grow_until: int = 400, on_send=None, **kw):
    clock = Clock()
    outbox = Outbox(tmp_path, clock)
    outbox.add(30)
    intake, box = Intake(clock), SlowBox(clock, outbox, grow_until=grow_until, on_send=on_send)
    cursor = RecordingCursor(tmp_path / "cursor.json")
    fwd = Forwarder(outbox, cursor=cursor, intake=intake, box=box, status_path=tmp_path / "status.json",
                    up_after=3, down_after=1, clock=clock, **kw)
    return clock, outbox, intake, box, cursor, fwd


def test_a_drop_pass_ends_and_the_intake_is_taken_back_while_a_backlog_remains(tmp_path):
    clock, outbox, intake, box, cursor, fwd = _world(tmp_path, box_records_per_drop=5)
    r = fwd.forward_once()
    assert r.current == "box"
    # Bounded: the pass returned with the outbox still ahead of it, and says so.
    assert r.more and cursor.get() < outbox.last_seq()

    intake.up = True
    seen = []
    for _ in range(3):
        seen.append(fwd.forward_once().current)
    assert seen == ["box", "box", "intake"]          # up_after healthy checks, between slices
    assert len(outbox.recs) < box.grow_until          # the backlog was still there when it switched

    for _ in range(200):
        r = fwd.forward_once()
        if not r.more and cursor.get() == outbox.last_seq():
            break
    assert cursor.get() == outbox.last_seq()

    delivered = box.items + intake.items
    assert sorted(delivered, key=lambda s: int(s.split("-")[1])) == [r["item_id"] for r in outbox.recs]
    assert len(delivered) == len(set(delivered))      # nothing twice, nothing skipped
    assert cursor.history == sorted(cursor.history)   # the cursor only moves forward
    switches = json.loads((tmp_path / "status.json").read_text())["switches"]
    assert [s["to"] for s in switches] == ["box", "intake"]


def test_the_status_file_moves_during_a_drain(tmp_path):
    seen: list[tuple[int, int]] = []

    def look(box):
        d = json.loads((tmp_path / "status.json").read_text()) if (tmp_path / "status.json").exists() else {}
        seen.append((d.get("after", -1), (d.get("last_report") or {}).get("sent_box", -1)))

    # A long slice, so one pass sends several drops.
    _clock, _outbox, _intake, box, _cursor, fwd = _world(tmp_path, on_send=look, box_records_per_drop=5,
                                                         lane_slice_s=60.0)
    fwd.forward_once()
    assert len(box.files) >= 3
    # Read while the pass was still sending: the file already showed earlier drops.
    afters = [a for a, _ in seen]
    assert afters[2] > afters[1] > 0 and seen[2][1] > 0


def test_many_records_travel_in_one_drop_file_shaped_like_an_intake_request(tmp_path):
    _clock, outbox, _intake, box, cursor, fwd = _world(tmp_path, grow_until=0, box_records_per_drop=12)
    while True:
        r = fwd.forward_once()
        if not r.more:
            break
    assert cursor.get() == 30 and len(box.files) == 3   # 12 + 12 + 6, not 30 round trips
    first = json.loads(box.files[0])
    pairs = [(rec, json.loads(outbox.content(SOURCE, rec["content_hash"]))) for rec in outbox.recs[:12]]
    assert box.files[0] == request_body_many(pairs, source=SOURCE)   # what /ingest/rows takes
    assert first["source"] == SOURCE and len(first["batches"]) == 12
    assert sum(len(json.loads(f)["batches"]) for f in box.files) == 30


@pytest.mark.skipif(shutil.which("rclone") is None, reason="needs the real rclone; the forwarder-proofs job runs it")
def test_a_missing_drop_folder_is_named(tmp_path):
    missing = tmp_path / "no-such-folder"
    p = BoxDropTarget(str(missing)).probe()
    assert not p.ok
    assert "Box folder not found" in p.reason and str(missing) in p.reason
