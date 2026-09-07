# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The Journal — durable, written **first**, read through per-subscriber cursors.

Every consumer (the Transmitter included) is a cursor into this one Journal;
there is no second buffer. Records are appended to JSONL segments under
``root/segments`` and each cursor's committed position lives in
``root/cursors/<name>.json``, so a restart resumes exactly where the consumer
left off (at-least-once from the consumer's point of view).

Overflow (``max_bytes``) is policy-gated by what the stream carries
(:func:`validate_overflow_policy`): ``block_producer`` is mandatory for
controlled sensitivities, ``drop_oldest`` only for open/internal, and
``trip_on_gap`` for credited streams — never drop silently, never block the
Reader: the record is not stored, its ``seq`` is still consumed, and the gap
is the discontinuity marker every credited consumer trips on.

Delivered records are not kept forever: :meth:`DAQJournal.compact` removes
whole segments that lie entirely behind **every** cursor's committed position
(the Transmitter's and each subscriber's) — those records have been durably
handed on, so dropping the segment loses nothing, while a lagging cursor
pins its segments in place. Without compaction a healthy producer fills its
``max_bytes`` with delivered history and then blocks on itself.

Encryption at rest keyed through the secrets store is **not built**; the
Journal reports ``at_rest="plaintext"`` in health rather than pretending.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from .envelope import CONTROLLED_SENSITIVITIES, JournaledRecord, canonical_json

SEGMENT_PREFIX = "seg-"
SEGMENT_SUFFIX = ".jsonl"


class OverflowPolicy(str, Enum):
    BLOCK_PRODUCER = "block_producer"
    DROP_OLDEST = "drop_oldest"
    TRIP_ON_GAP = "trip_on_gap"


class JournalFull(Exception):
    """``block_producer`` engaged: the Journal is at ``max_bytes``."""


def validate_overflow_policy(
    policy: OverflowPolicy, *, sensitivity: str, delivery_class: str
) -> OverflowPolicy:
    """Spec §4 / §11 'overflow gating': refuse the combinations that would lose
    a controlled record or stall a credited stream."""
    if policy is OverflowPolicy.DROP_OLDEST and sensitivity in CONTROLLED_SENSITIVITIES:
        raise ValueError(f"drop_oldest is refused for sensitivity {sensitivity!r}")
    if policy is OverflowPolicy.BLOCK_PRODUCER and delivery_class == "credited":
        raise ValueError("block_producer is refused for a credited stream (use trip_on_gap)")
    if policy is OverflowPolicy.TRIP_ON_GAP and delivery_class != "credited":
        raise ValueError("trip_on_gap is only for credited streams")
    return policy


@dataclass
class _Segment:
    path: Path
    base: int  # global offset of the first record
    count: int
    size: int


class DAQJournal:
    def __init__(
        self,
        root: Path,
        *,
        max_bytes: int | None = None,
        policy: OverflowPolicy = OverflowPolicy.BLOCK_PRODUCER,
        segment_records: int = 1000,
    ) -> None:
        self.root = Path(root)
        self.max_bytes = max_bytes
        self.policy = policy
        self.segment_records = max(1, int(segment_records))
        self._segments_dir = self.root / "segments"
        self._cursors_dir = self.root / "cursors"
        self._segments_dir.mkdir(parents=True, exist_ok=True)
        self._cursors_dir.mkdir(parents=True, exist_ok=True)
        self._segments: list[_Segment] = []
        self.trips = 0  # records not stored under trip_on_gap
        self.dropped = 0  # records discarded under drop_oldest
        self.compacted = 0  # delivered records released by compact()
        self._scan()

    # -- layout ------------------------------------------------------------

    def _scan(self) -> None:
        segs: list[_Segment] = []
        for p in sorted(self._segments_dir.glob(f"{SEGMENT_PREFIX}*{SEGMENT_SUFFIX}")):
            base = int(p.name[len(SEGMENT_PREFIX) : -len(SEGMENT_SUFFIX)])
            with p.open("rb") as fh:
                count = sum(1 for _ in fh)
            segs.append(_Segment(path=p, base=base, count=count, size=p.stat().st_size))
        self._segments = segs

    @property
    def head(self) -> int:
        """Global offset of the oldest record still held."""
        return self._segments[0].base if self._segments else 0

    @property
    def end(self) -> int:
        """Global offset one past the newest record (== total appended, less nothing)."""
        if not self._segments:
            return 0
        last = self._segments[-1]
        return last.base + last.count

    def size_bytes(self) -> int:
        return sum(s.size for s in self._segments)

    # -- write -------------------------------------------------------------

    def append(self, rec: JournaledRecord) -> int | None:
        """Durably append; return the record's global offset, or ``None`` when
        ``trip_on_gap`` refused storage (the caller has already consumed the
        seq, so the gap is visible downstream)."""
        line = canonical_json(rec.to_dict()) + b"\n"
        if self.max_bytes is not None and self.size_bytes() + len(line) > self.max_bytes:
            if self.policy is OverflowPolicy.BLOCK_PRODUCER:
                raise JournalFull(f"journal at {self.size_bytes()} bytes >= {self.max_bytes}")
            if self.policy is OverflowPolicy.TRIP_ON_GAP:
                self.trips += 1
                return None
            self._drop_oldest_until(len(line))
        seg = self._segments[-1] if self._segments else None
        if seg is None or seg.count >= self.segment_records:
            base = self.end
            path = self._segments_dir / f"{SEGMENT_PREFIX}{base:012d}{SEGMENT_SUFFIX}"
            seg = _Segment(path=path, base=base, count=0, size=0)
            self._segments.append(seg)
        with seg.path.open("ab") as fh:
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())
        seg.count += 1
        seg.size += len(line)
        return seg.base + seg.count - 1

    def _drop_oldest_until(self, needed: int) -> None:
        while (
            self.max_bytes is not None
            and len(self._segments) > 1
            and self.size_bytes() + needed > self.max_bytes
        ):
            oldest = self._segments.pop(0)
            self.dropped += oldest.count
            oldest.path.unlink(missing_ok=True)
        if self.max_bytes is not None and self.size_bytes() + needed > self.max_bytes:
            # a single live segment: roll it so the next append starts fresh
            if self._segments and self._segments[0].count > 0:
                oldest = self._segments.pop(0)
                self.dropped += oldest.count
                oldest.path.unlink(missing_ok=True)

    # -- read --------------------------------------------------------------

    def read(self, offset: int, limit: int) -> list[tuple[int, JournaledRecord]]:
        """Up to ``limit`` records from global ``offset`` (skipping anything
        dropped below :attr:`head`)."""
        out: list[tuple[int, JournaledRecord]] = []
        offset = max(offset, self.head)
        for seg in self._segments:
            if seg.base + seg.count <= offset:
                continue
            with seg.path.open("rb") as fh:
                for i, raw in enumerate(fh):
                    pos = seg.base + i
                    if pos < offset:
                        continue
                    out.append((pos, JournaledRecord.from_dict(json.loads(raw))))
                    if len(out) >= limit:
                        return out
        return out

    def iter_all(self) -> Iterator[JournaledRecord]:
        for _, rec in self.read(self.head, limit=10**12):
            yield rec

    def tail(
        self, *, producer_id: str | None = None, stream: str | None = None
    ) -> JournaledRecord | None:
        """The newest record (optionally of one stream) — what a restarted
        Consolidator resumes ``seq``/``prev_hash`` from."""
        for seg in reversed(self._segments):
            with seg.path.open("rb") as fh:
                lines = fh.readlines()
            for raw in reversed(lines):
                rec = JournaledRecord.from_dict(json.loads(raw))
                e = rec.envelope
                if (producer_id is None or e.producer_id == producer_id) and (
                    stream is None or e.stream == stream
                ):
                    return rec
        return None

    # -- cursors -----------------------------------------------------------

    def _cursor_path(self, name: str) -> Path:
        if not name or "/" in name or name.startswith("."):
            raise ValueError(f"invalid cursor name {name!r}")
        return self._cursors_dir / f"{name}.json"

    def cursor(self, name: str) -> int:
        p = self._cursor_path(name)
        if not p.exists():
            return self.head
        return int(json.loads(p.read_text()).get("offset", self.head))

    def commit(self, name: str, offset: int) -> None:
        p = self._cursor_path(name)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"offset": int(offset)}))
        os.replace(tmp, p)

    def lag(self, name: str) -> int:
        return max(0, self.end - self.cursor(name))

    def cursor_names(self) -> list[str]:
        return sorted(p.stem for p in self._cursors_dir.glob("*.json"))

    # -- retention ---------------------------------------------------------

    def compact(self, *, keep_segments: int = 1) -> int:
        """Release segments every cursor has already passed. Returns the number
        of records released. The newest ``keep_segments`` segments are always
        kept (the live segment, and the tail a restart resumes from). With no
        cursors at all nothing is released — an unconsumed Journal is not
        delivered history."""
        names = self.cursor_names()
        if not names or len(self._segments) <= keep_segments:
            return 0
        floor = min(self.cursor(n) for n in names)
        released = 0
        while len(self._segments) > keep_segments:
            oldest = self._segments[0]
            if oldest.base + oldest.count > floor:
                break  # some cursor still needs a record in this segment
            self._segments.pop(0)
            oldest.path.unlink(missing_ok=True)
            released += oldest.count
        self.compacted += released
        return released

    # -- health ------------------------------------------------------------

    def health_details(self) -> dict:
        return {
            "journal_bytes": self.size_bytes(),
            "journal_records": self.end - self.head,
            "journal_head": self.head,
            "journal_end": self.end,
            "cursor_lag": {n: self.lag(n) for n in self.cursor_names()},
            "trip_count": self.trips,
            "dropped": self.dropped,
            "compacted": self.compacted,
            "overflow_policy": self.policy.value,
            "at_rest": "plaintext",
        }


__all__ = ["DAQJournal", "JournalFull", "OverflowPolicy", "validate_overflow_policy"]
