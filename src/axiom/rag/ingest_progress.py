# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Progress event stream for advanced ingest (spec-rag-ingest-advanced §6).

One internal progress state; the TTY panel and the headless JSON stream are
both renderers over it. Keeping the state and JSON renderer free of any
terminal dependency is what makes them unit-testable. The clock is injected so
throughput is deterministic in tests. ETA (`eta_seconds`) is left None here and
populated by calibration (U4).
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass


@dataclass
class ProgressSnapshot:
    files_done: int
    files_total: int
    files_skipped: int
    chunks_done: int
    retries: int
    elapsed_s: float
    throughput_cps: float
    eta_seconds: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class RunInfo:
    """Static run header shown above the progress block."""

    path: str
    target: str
    corpus: str
    embedding_backend: str
    checkpoint_path: str


class ProgressState:
    """Accumulates ingest progress; `snapshot()` is what renderers read."""

    def __init__(self, files_total: int, *, clock=time.monotonic):
        self._files_total = files_total
        self._clock = clock
        self._start = clock()
        self._files_done = 0
        self._files_skipped = 0
        self._chunks_done = 0
        self._retries = 0
        self.eta_seconds: float | None = None  # set by calibration (U4)

    def batch_done(self, chunks: int) -> None:
        self._chunks_done += chunks

    def file_done(self) -> None:
        self._files_done += 1

    def file_skipped(self) -> None:
        self._files_skipped += 1

    def retry(self) -> None:
        self._retries += 1

    def snapshot(self) -> ProgressSnapshot:
        elapsed = self._clock() - self._start
        throughput = self._chunks_done / elapsed if elapsed > 0 else 0.0
        return ProgressSnapshot(
            files_done=self._files_done,
            files_total=self._files_total,
            files_skipped=self._files_skipped,
            chunks_done=self._chunks_done,
            retries=self._retries,
            elapsed_s=elapsed,
            throughput_cps=throughput,
            eta_seconds=self.eta_seconds,
        )


class JsonEventSink:
    """Headless renderer: one JSON object per file completion + heartbeats."""

    def __init__(self, write=print):
        self._write = write

    def file_completed(self, file: str, chunks: int, snapshot: ProgressSnapshot) -> None:
        self._emit("file", snapshot, file=file, chunks=chunks)

    def heartbeat(self, snapshot: ProgressSnapshot) -> None:
        self._emit("heartbeat", snapshot)

    def run_completed(self, snapshot: ProgressSnapshot) -> None:
        self._emit("done", snapshot)

    def _emit(self, event: str, snapshot: ProgressSnapshot, **extra) -> None:
        self._write(json.dumps({"event": event, **extra, **snapshot.to_dict()}))


def _bar(done: int, total: int, width: int = 30) -> str:
    frac = (done / total) if total else 0.0
    filled = int(frac * width)
    return "[" + "█" * filled + "░" * (width - filled) + "]"


def _fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    m, sec = divmod(s, 60)
    if m < 60:
        return f"{m}m {sec:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m"


def format_progress(info: RunInfo, snap: ProgressSnapshot) -> str:
    """Render the TTY progress block as plain text (the CLI wraps it in rich.Live)."""
    pct = (snap.files_done / snap.files_total * 100) if snap.files_total else 0.0
    return "\n".join(
        [
            f"Ingesting {info.path} → {info.target}/{info.corpus}",
            f"{_bar(snap.files_done, snap.files_total)} {pct:.0f}% · "
            f"{snap.files_done:,} / {snap.files_total:,} files",
            f"   throughput: {snap.throughput_cps:.0f} chunks/s   "
            f"elapsed: {_fmt_duration(snap.elapsed_s)}   ETA: {_fmt_duration(snap.eta_seconds)}",
            f"   embedding: {info.embedding_backend}   destination: {info.target}",
            f"   skipped: {snap.files_skipped}   retried: {snap.retries}   "
            f"checkpoint: {info.checkpoint_path}",
        ]
    )
