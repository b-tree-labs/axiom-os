# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Persistence seam for :class:`IngestRunReport`.

``RunStore`` is the single source of truth for ingest-run history. The
durable ``ingest_runs`` table is a Postgres ``RunStore`` (ADR-052,
``axiom.infra.db.session_for("data_platform")``) added behind this seam
without touching any job; until then the in-memory + JSONL stores give the
same interface so the funnel works — and is fully testable — with no DB.

Domain-agnostic: a store persists a report dict; it knows nothing about
sources, sinks, or domains.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from .report import IngestRunReport


@runtime_checkable
class RunStore(Protocol):
    """Persist + read back ingest-run reports."""

    def save(self, report: IngestRunReport) -> None:
        """Upsert a run by ``report.run_id`` (idempotent on re-save)."""
        ...

    def get(self, run_id: str) -> dict[str, Any] | None: ...

    def recent(self, limit: int = 20, *, source: str | None = None) -> list[dict[str, Any]]:
        """Most-recent-first run summaries, optionally filtered by source."""
        ...


class InMemoryRunStore:
    """Process-local store — the default when no durable store is wired.

    Thread-safe: ``run_ingest`` writes from a bounded worker pool.
    """

    def __init__(self) -> None:
        self._runs: dict[str, dict[str, Any]] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()

    def save(self, report: IngestRunReport) -> None:
        d = report.to_dict()
        with self._lock:
            if report.run_id not in self._runs:
                self._order.append(report.run_id)
            self._runs[report.run_id] = d

    def get(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            d = self._runs.get(run_id)
            return dict(d) if d is not None else None

    def recent(self, limit: int = 20, *, source: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            ids = list(reversed(self._order))
            out: list[dict[str, Any]] = []
            for rid in ids:
                d = self._runs[rid]
                if source is not None and d.get("source") != source:
                    continue
                out.append(dict(d))
                if len(out) >= limit:
                    break
            return out


class JsonlRunStore:
    """Append-only JSONL store — durable across processes without a DB.

    Each ``save`` appends the full report as one JSON line. ``get`` /
    ``recent`` read the last-wins record per ``run_id`` (so a finished run
    supersedes its earlier RUNNING snapshot). Good for a single-host
    deployment and as the bridge until the Postgres store lands.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def save(self, report: IngestRunReport) -> None:
        line = json.dumps(report.to_dict(), default=str)
        with self._lock, self._path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    def _load_last_wins(self) -> dict[str, dict[str, Any]]:
        runs: dict[str, dict[str, Any]] = {}
        if not self._path.exists():
            return runs
        with self._path.open(encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    d = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                rid = d.get("run_id")
                if rid:
                    runs[rid] = d
        return runs

    def get(self, run_id: str) -> dict[str, Any] | None:
        return self._load_last_wins().get(run_id)

    def recent(self, limit: int = 20, *, source: str | None = None) -> list[dict[str, Any]]:
        runs = list(self._load_last_wins().values())
        if source is not None:
            runs = [r for r in runs if r.get("source") == source]
        runs.sort(key=lambda r: r.get("started_at") or 0, reverse=True)
        return runs[:limit]


__all__ = ["RunStore", "InMemoryRunStore", "JsonlRunStore"]
