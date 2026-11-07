# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Where a chart-kinds group reads its readings: gold, in one of three forms.

Every form is gold. None reads silver, and none can be pointed at it: an export
and a snapshot are copies of ``gold.signals`` rows, and a node is read through
its data door, which holds every verb to gold.

- :class:`ExportSource`: a **gold export**, a directory of windows. Each window
  is a ``window.json`` manifest beside Parquet files of gold rows (columns
  ``channel, ts, value, unit, quality``). The manifest names the window's
  ``source`` (the site), its ``start`` and ``end``, and for each file the
  ``feed`` it holds. Full rate, with every reading's quality verdict.
- :class:`SnapshotSource`: a **bucketed gold snapshot**, one JSON document
  ``{site, channels: [{feed, channel, unit, source_class}], series: {"feed|channel":
  [[epoch_ms, value], ...]}}``. Coarse (minutes), many days, no verdicts: a chart
  drawn from it says the snapshot carries none.
- :class:`DoorSource`: a node's gold through its data door
  (:class:`~axiom.extensions.builtins.scidisplay.node_figure_source.NodeFigureSource`),
  for a deployment that serves its own gold.

A reading is ``(t, value, quality)``: ``t`` in epoch seconds, ``value`` None when
a verdict withheld it, ``quality`` one of the platform's verdicts or None when
the form carries none. Bucketing keeps the worst verdict and never averages a
withheld reading in.
"""

from __future__ import annotations

import json
import math
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

Reading = tuple[float, float | None, str | None]

#: A verdict that withholds a reading from a mean.
WITHHELD = frozenset({"bad"})


class SourceError(RuntimeError):
    """The source cannot answer this read, and why (said to the reader, not raised past them)."""


def split_ref(ref: str) -> tuple[str, str]:
    """``feed:channel`` (or ``feed|channel``) as its two halves."""
    for sep in ("|", ":"):
        if sep in ref:
            feed, _, channel = ref.partition(sep)
            return feed.strip(), channel.strip()
    raise SourceError(f"{ref!r} names no feed: write it as feed:channel")


def _epoch(value: Any) -> float:
    if isinstance(value, datetime):
        return (value if value.tzinfo else value.replace(tzinfo=UTC)).timestamp()
    if isinstance(value, int | float):
        return float(value)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()


def bucket(readings: Sequence[Reading], start: float, end: float, step: int) -> list[Reading]:
    """Readings in ``[start, end)`` as one point per ``step`` seconds that had any.

    A bucket's value is the mean of the readings no verdict withheld; with none
    left it is None and the bucket is ``bad`` (held). A bucket that dropped some
    withheld readings and kept others is ``suspect``: drawn, and marked.
    """
    out: list[Reading] = []
    if step <= 0:
        return [r for r in readings if start <= r[0] < end]
    current: int | None = None
    total, kept, dropped, worst = 0.0, 0, 0, None
    seen: set[str] = set()

    def flush() -> None:
        if current is None:
            return
        t = start + current * step
        if kept:
            q = "suspect" if dropped else (worst if worst not in (None, "good") else ("good" if seen else None))
            out.append((t, total / kept, q))
        else:
            out.append((t, None, "bad" if dropped else (worst or None)))

    for t, v, q in readings:
        if t < start or t >= end:
            continue
        k = int((t - start) // step)
        if k != current:
            flush()
            current, total, kept, dropped, worst = k, 0.0, 0, 0, None
            seen = set()
        if q:
            seen.add(q)
        if q in WITHHELD or v is None or (isinstance(v, float) and math.isnan(v)):
            dropped += 1
            continue
        if q in ("stale", "suspect", "saturated") and worst in (None, "good"):
            worst = q
        total += float(v)
        kept += 1
    flush()
    return out


class GoldSource(Protocol):
    """What a chart-kinds group reads from."""

    def read(self, feed: str, channel: str, start: datetime, end: datetime, step: int) -> list[Reading]: ...

    def unit(self, feed: str, channel: str) -> str: ...

    def lineage(self) -> str: ...

    def carries_quality(self) -> bool: ...


# ---------------------------------------------------------------------------
# A gold export: windows of full-rate gold rows.
# ---------------------------------------------------------------------------


@dataclass
class _Window:
    site: str
    start: float
    end: float
    files: dict[str, Path]  # feed -> parquet
    units: dict[tuple[str, str], str]
    exported_at: str
    exported_from: str


@dataclass
class ExportSource:
    """A directory of gold export windows for one site (see the module doc)."""

    root: Path
    site: str
    _windows: list[_Window] | None = field(default=None, repr=False)
    _cache: dict[tuple[str, str, str], list[Reading]] = field(default_factory=dict, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def windows(self) -> list[_Window]:
        if self._windows is None:
            found: list[_Window] = []
            for manifest in sorted(Path(self.root).expanduser().glob("**/window.json")):
                try:
                    doc = json.loads(manifest.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if str(doc.get("source") or "") != self.site:
                    continue
                files: dict[str, Path] = {}
                units: dict[tuple[str, str], str] = {}
                for name, entry in (doc.get("files") or {}).items():
                    feed = str((entry or {}).get("feed") or "")
                    path = manifest.parent / f"{name}.parquet"
                    if feed and path.exists():
                        files[feed] = path
                        for ch, unit in ((doc.get("channels") or {}).get(name) or {}).items():
                            units[(feed, ch)] = str(unit or "")
                if files:
                    found.append(_Window(self.site, _epoch(doc["start"]), _epoch(doc["end"]), files, units,
                                         str(doc.get("exported_at") or ""), str(doc.get("exported_from") or "")))
            self._windows = found
        return self._windows

    def days(self) -> list[date]:
        return sorted({datetime.fromtimestamp(w.start, UTC).date() for w in self.windows()})

    def _raw(self, w: _Window, feed: str, channel: str) -> list[Reading]:
        key = (str(w.files.get(feed)), feed, channel)
        with self._lock:
            if key in self._cache:
                return self._cache[key]
        path = w.files.get(feed)
        if path is None:
            return []
        try:
            import pyarrow as pa
            import pyarrow.parquet as pq
        except ImportError as exc:  # pragma: no cover - the export form needs pyarrow
            raise SourceError("reading a gold export needs pyarrow (pip install pyarrow)") from exc
        table = pq.read_table(path, columns=["channel", "ts", "value", "quality"],
                              filters=[("channel", "=", channel)])
        col = table["ts"]
        unit = getattr(col.type, "unit", "us")
        per_s = {"s": 1.0, "ms": 1e3, "us": 1e6, "ns": 1e9}[unit]
        ts = [n / per_s for n in col.cast(pa.int64()).to_pylist()]
        rows = list(zip(ts, table["value"].to_pylist(), table["quality"].to_pylist(), strict=True))
        rows.sort(key=lambda r: r[0])
        with self._lock:
            if len(self._cache) > 64:
                self._cache.pop(next(iter(self._cache)))
            self._cache[key] = rows
        return rows

    def read(self, feed: str, channel: str, start: datetime, end: datetime, step: int) -> list[Reading]:
        a, b = start.timestamp(), end.timestamp()
        out: list[Reading] = []
        for w in self.windows():
            if w.end < a or w.start >= b:
                continue
            out.extend(bucket(self._raw(w, feed, channel), a, b, step))
        return out

    def unit(self, feed: str, channel: str) -> str:
        for w in self.windows():
            if (feed, channel) in w.units:
                return w.units[(feed, channel)]
        return ""

    def lineage(self) -> str:
        ws = self.windows()
        if not ws:
            return f"gold export at {self.root} (no windows for {self.site})"
        days = ", ".join(sorted({datetime.fromtimestamp(w.start, UTC).date().isoformat() for w in ws}))
        origin = ws[0].exported_from or "gold"
        return f"{origin}, exported windows {days}"

    def carries_quality(self) -> bool:
        return True


# ---------------------------------------------------------------------------
# A bucketed gold snapshot.
# ---------------------------------------------------------------------------


@dataclass
class SnapshotSource:
    """A bucketed snapshot of gold rows (see the module doc). No verdicts."""

    path: Path
    site: str
    _doc: dict[str, Any] | None = field(default=None, repr=False)

    def doc(self) -> dict[str, Any]:
        if self._doc is None:
            try:
                doc = json.loads(Path(self.path).expanduser().read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise SourceError(f"the gold snapshot at {self.path} cannot be read: {exc}") from exc
            if str(doc.get("site") or "") != self.site:
                raise SourceError(f"the gold snapshot at {self.path} is of {doc.get('site')!r}, not {self.site!r}")
            self._doc = doc
        return self._doc

    def read(self, feed: str, channel: str, start: datetime, end: datetime, step: int) -> list[Reading]:
        pts = (self.doc().get("series") or {}).get(f"{feed}|{channel}")
        if pts is None:
            return []
        a, b = start.timestamp(), end.timestamp()
        rows: list[Reading] = [(ms / 1000.0, None if v is None else float(v), None) for ms, v in pts]
        return bucket(rows, a, b, step)

    def unit(self, feed: str, channel: str) -> str:
        for c in self.doc().get("channels") or []:
            if c.get("feed") == feed and c.get("channel") == channel:
                return str(c.get("unit") or "")
        return ""

    def lineage(self) -> str:
        windows = self.doc().get("windows") or []
        span = f"{windows[0][0]}..{windows[-1][1]}" if windows else "its recorded span"
        return f"gold.signals snapshot, bucketed, {span}"

    def carries_quality(self) -> bool:
        return False


# ---------------------------------------------------------------------------
# A node's gold through its data door.
# ---------------------------------------------------------------------------


@dataclass
class DoorSource:
    """A node's ``gold.signals`` through its data door."""

    figure_source: Any  # NodeFigureSource
    site: str

    def read(self, feed: str, channel: str, start: datetime, end: datetime, step: int) -> list[Reading]:
        from axiom.extensions.builtins.scidisplay.node_figure_source import Channel

        chan = Channel(site=self.site, feed=feed, channel=channel, unit="", rows=0, first=None, last=None)
        graded: dict[tuple[str, str], dict[datetime, str | None]] = {}
        got = self.figure_source.read(self.site, [chan], start, end, max(1, step), quality=graded)
        q = graded.get((feed, channel), {})
        return [(t.timestamp(), v, q.get(t)) for t, v in got.get((feed, channel), [])]

    def unit(self, feed: str, channel: str) -> str:
        for c in self.figure_source.readable(self.site):
            if c.feed == feed and c.channel == channel:
                return c.unit
        return ""

    def lineage(self) -> str:
        return f"gold.signals on the node, through its data door ({self.site})"

    def carries_quality(self) -> bool:
        return True


def open_source(spec: Mapping[str, Any], site: str, *, base: Path | None = None) -> GoldSource:
    """A source from its configuration: ``{export = dir}``, ``{snapshot = file}`` or ``{door = url, key_ref}``."""

    def _path(raw: Any) -> Path:
        p = Path(str(raw)).expanduser()
        return p if p.is_absolute() or base is None else (base / p)

    if spec.get("export"):
        return ExportSource(_path(spec["export"]), site)
    if spec.get("snapshot"):
        return SnapshotSource(_path(spec["snapshot"]), site)
    if spec.get("door"):
        from axiom.extensions.builtins.scidisplay.node_figure_source import (
            DataDoor,
            NodeFigureSource,
            key_from_vault,
        )

        ref = str(spec.get("key_ref") or "")
        if not ref:
            raise SourceError("a door source names the vault reference of its key (key_ref)")
        door = DataDoor(node_url=str(spec["door"]), key=lambda: key_from_vault(ref))
        return DoorSource(NodeFigureSource(door), site)
    raise SourceError("a source is one of export (a directory), snapshot (a file) or door (a node URL)")


def day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime(day.year, day.month, day.day, tzinfo=UTC)
    return start, start + timedelta(days=1)


__all__ = [
    "DoorSource",
    "ExportSource",
    "GoldSource",
    "Reading",
    "SnapshotSource",
    "SourceError",
    "bucket",
    "day_bounds",
    "open_source",
    "split_ref",
]
