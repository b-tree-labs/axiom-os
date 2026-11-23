# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The event ledger: every ``TriggerEvent``, in its source's DAQ journal (VID-012, VID-022..026).

spec-video §Events. Each source has one journal, ``<state>/video/journal/<source>/``:
the data platform's ``DAQJournal``, written through a ``DAQConsolidator``, so an event
is stored exactly as a reading is. Each trigger is a feed of its own, with a gapless
``seq`` and a hash chain, and names itself as the model that produced the event.

One writer at a time per source: every append holds an in-process lock and an
exclusive file lock beside the journal, re-reads the journal if another process
wrote to it since this one last did, and first truncates a last line a power loss
cut short. A reader skips any line that does not parse and never creates anything.

The journal is the record and the end of the pipeline: nothing here routes, sends
or deletes an event. ``events.jsonl``, which releases before the journal appended
to, is read and never written.
"""

from __future__ import annotations

import contextlib
import heapq
import json
import os
import sys
import threading
from collections.abc import Iterable, Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .triggers import DEFAULT_SEVERITY, TriggerEvent

if TYPE_CHECKING:
    from axiom.extensions.builtins.data_platform.daq import (
        ConsolidatedRecord,
        DAQConsolidator,
        DAQJournal,
        JournaledRecord,
    )

#: ``<state>/video/journal/<source>/``: one DAQ journal per source.
JOURNAL_DIRNAME = "journal"
#: What releases before the journal appended every event to. Read here, never written.
LEGACY_FILENAME = "events.jsonl"
#: The record's schema, and the envelope's classification (spec-video §Events).
SCHEMA_ID = "video/trigger-event-v1"
SOURCE_CLASS = "estimated"
#: What an event that does not say is written as, and what an earlier release wrote (ADR-267).
SENSITIVITY = "internal"
PRODUCER_PREFIX = "video-"
FEED_PREFIX = "video."
#: Records per segment file: the DAQ journal's own default.
SEGMENT_RECORDS = 1000
#: The exclusive lock every writer of one source's journal holds while it appends.
LOCK_FILENAME = "writer.lock"
_SEGMENTS = "segments"
_SEGMENT_GLOB = "seg-*.jsonl"
_EXAMPLE = "2026-11-10T08:00:00+00:00"
_EARLIEST = datetime.min.replace(tzinfo=timezone.utc)


def journal_dir(state_dir: Path, stream: str) -> Path:
    return Path(state_dir) / "video" / JOURNAL_DIRNAME / stream


def legacy_path(state_dir: Path) -> Path:
    return Path(state_dir) / "video" / LEGACY_FILENAME


def producer_id(stream: str) -> str:
    return PRODUCER_PREFIX + stream


def feed_of(trigger: str) -> str:
    """``motion@0.1.0`` is feed ``video.motion``: a new version is a new model, not a new feed."""
    return FEED_PREFIX + trigger.split("@", 1)[0]


def to_record(event: TriggerEvent) -> ConsolidatedRecord:
    from axiom.extensions.builtins.data_platform.daq import ConsolidatedRecord

    return ConsolidatedRecord(
        schema_id=SCHEMA_ID,
        ts=event.ts,
        values={"kind": event.kind, "score": event.score, "detail": dict(event.detail)},
        tags={
            "stream": event.stream,
            "trigger": event.trigger,
            "incident_key": event.incident_key,
            "severity": event.severity,
        },
        quality="good",
        sensitivity=event.sensitivity,
    )


def from_record(rec: JournaledRecord) -> dict[str, Any] | None:
    """The event's row exactly as it was written, or ``None`` for a record that is not one."""
    r = rec.record
    if r.schema_id != SCHEMA_ID:
        return None
    values, tags = dict(r.values), dict(r.tags)
    try:
        event = TriggerEvent(
            stream=tags["stream"],
            trigger=tags["trigger"],
            ts=r.ts,
            kind=values["kind"],
            incident_key=tags["incident_key"],
            severity=tags.get("severity") or DEFAULT_SEVERITY,
            score=values.get("score"),
            detail=values.get("detail") or {},
            sensitivity=r.sensitivity,
        )
    except (KeyError, TypeError, ValueError):
        return None
    return event.as_dict()


# ---- writing ------------------------------------------------------------------------


@contextlib.contextmanager
def _exclusive(path: Path) -> Iterator[None]:
    """An exclusive lock on ``path`` across processes, held for one append."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        if sys.platform == "win32":  # pragma: no cover - exercised on Windows nodes only
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _segment_paths(root: Path) -> list[Path]:
    return sorted((root / _SEGMENTS).glob(_SEGMENT_GLOB))


def _fingerprint(root: Path) -> tuple[tuple[str, int], ...]:
    """What is on disk: every segment's name and size. Any other writer's append changes it."""
    seen = []
    for path in _segment_paths(root):
        with contextlib.suppress(FileNotFoundError):
            seen.append((path.name, path.stat().st_size))
    return tuple(seen)


def _repair_torn_tail(root: Path) -> None:
    """Truncate a last line without its newline: a write a power loss cut short.

    Only a writer holding the lock may do this; a reader skips the line instead.
    """
    paths = _segment_paths(root)
    if not paths:
        return
    with paths[-1].open("r+b") as fh:
        size = fh.seek(0, os.SEEK_END)
        if size == 0:
            return
        fh.seek(size - 1)
        if fh.read(1) == b"\n":
            return
        keep, pos = 0, size
        while pos > 0:
            step = min(65536, pos)
            pos -= step
            fh.seek(pos)
            cut = fh.read(step).rfind(b"\n")
            if cut >= 0:
                keep = pos + cut + 1
                break
        fh.truncate(keep)
        fh.flush()
        os.fsync(fh.fileno())


class _Writer:
    """One source's journal, as this process writes it. Kept open between appends, and
    re-read whenever the journal on disk is not what this process last left there."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._lock = threading.Lock()
        self._journal: DAQJournal | None = None
        self._consolidators: dict[str, DAQConsolidator] = {}
        self._seen: tuple[tuple[str, int], ...] | None = None

    def append(self, event: TriggerEvent) -> None:
        from axiom.extensions.builtins.data_platform.daq import DAQConsolidator, DAQJournal

        record = to_record(event)
        feed = feed_of(event.trigger)
        with self._lock, _exclusive(self.root / LOCK_FILENAME):
            _repair_torn_tail(self.root)
            journal = self._journal
            if (
                journal is None
                or journal.segment_records != SEGMENT_RECORDS
                or _fingerprint(self.root) != self._seen
            ):
                journal = self._journal = DAQJournal(self.root, segment_records=SEGMENT_RECORDS)
                self._consolidators = {}
            cons = self._consolidators.get(feed)
            if cons is None or (cons.model_ref, cons.sensitivity) != (
                event.trigger,
                event.sensitivity,
            ):
                # A new consolidator continues the feed's seq and chain from the journal's tail.
                cons = self._consolidators[feed] = DAQConsolidator(
                    producer_id=producer_id(event.stream),
                    feed=feed,
                    journal=journal,
                    source_class=SOURCE_CLASS,
                    model_ref=event.trigger,
                    sensitivity=event.sensitivity,
                )
            try:
                cons.consume(record)
            except BaseException:
                self._journal = None  # whatever reached the disk, the next append reads it again
                raise
            self._seen = _fingerprint(self.root)


_writers: dict[str, _Writer] = {}
_writers_lock = threading.Lock()


def _writer(root: Path) -> _Writer:
    key = os.path.abspath(root)
    with _writers_lock:
        found = _writers.get(key)
        if found is None:
            found = _writers[key] = _Writer(Path(key))
        return found


def append(event: TriggerEvent, state_dir: Path) -> None:
    """Journal ``event`` in its source's journal, as the one writer of that journal."""
    _writer(journal_dir(state_dir, event.stream)).append(event)


# ---- reading ------------------------------------------------------------------------


def parse_since(text: str) -> datetime:
    """An ISO-8601 time (a date alone is its midnight); without an offset it is local time.

    ``ValueError`` with an example otherwise.
    """
    try:
        when = datetime.fromisoformat(str(text).strip())
    except ValueError:
        raise ValueError(
            f"since {text!r} is not an ISO-8601 time; write one like {_EXAMPLE}, or a date like 2026-11-10"
        ) from None
    return when.astimezone()  # aware: kept; naive: read as local time


def _lines(path: Path) -> list[bytes]:
    try:
        return path.read_bytes().splitlines()
    except FileNotFoundError:
        return []


def _journal_rows(root: Path) -> Iterator[dict[str, Any]]:
    from axiom.extensions.builtins.data_platform.daq import JournaledRecord

    for path in _segment_paths(root):
        for line in _lines(path):
            try:
                row = from_record(JournaledRecord.from_dict(json.loads(line)))
            except (KeyError, TypeError, ValueError):
                continue  # a line cut short by a power loss is not the record
            if row is not None:
                yield row


def _legacy_rows(state_dir: Path, stream: str | None) -> Iterator[dict[str, Any]]:
    # Read-only: what releases before the journal wrote. Reading it goes in a later
    # release, marked `# contract:`, once no node's history is only here.
    for line in _lines(legacy_path(state_dir)):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and (stream is None or row.get("stream") == stream):
            row.setdefault("sensitivity", SENSITIVITY)  # all an earlier release wrote
            yield row


def _sources(state_dir: Path, stream: str | None) -> list[str]:
    if stream is not None:
        return [stream]
    root = Path(state_dir) / "video" / JOURNAL_DIRNAME
    try:
        return sorted(p.name for p in root.iterdir() if p.is_dir())
    except FileNotFoundError:
        return []


def _when(row: dict[str, Any]) -> datetime:
    try:
        when = datetime.fromisoformat(str(row.get("ts")))
    except (TypeError, ValueError):
        return _EARLIEST
    return when if when.tzinfo is not None else when.replace(tzinfo=timezone.utc)


def read(
    state_dir: Path, *, stream: str | None = None, since: str | datetime | None = None
) -> list[dict[str, Any]]:
    """Every event, oldest first, optionally one source's, optionally at or after ``since``.

    By time across sources; within one source, in the order it was written.
    """
    after = parse_since(since) if isinstance(since, str) else since
    if after is not None and after.tzinfo is None:
        after = after.astimezone()
    inputs: list[Iterable[dict[str, Any]]] = [_legacy_rows(state_dir, stream)]
    inputs += [_journal_rows(journal_dir(state_dir, s)) for s in _sources(state_dir, stream)]
    rows: list[dict[str, Any]] = []
    for row in heapq.merge(*inputs, key=_when):
        if after is not None:
            try:
                if datetime.fromisoformat(str(row.get("ts"))) < after:
                    continue
            except (TypeError, ValueError):
                continue
        rows.append(row)
    return rows


__all__ = [
    "FEED_PREFIX",
    "JOURNAL_DIRNAME",
    "LEGACY_FILENAME",
    "LOCK_FILENAME",
    "PRODUCER_PREFIX",
    "SCHEMA_ID",
    "SEGMENT_RECORDS",
    "SENSITIVITY",
    "SOURCE_CLASS",
    "append",
    "feed_of",
    "from_record",
    "journal_dir",
    "legacy_path",
    "parse_since",
    "producer_id",
    "read",
    "to_record",
]
