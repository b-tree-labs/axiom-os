# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Pull what an ingest edge holds into this node's bronze (ADR-177).

The owning node opens every connection: it asks the edge for outbox records
after its cursor, fetches each batch by content hash, checks the hash, and
lands the rows through the same row sink a direct push would use. The cursor
advances only after a batch is durable here, so an interrupted pull resumes
where it stopped; and because the row sink deduplicates by row
``content_hash``, a batch delivered twice lands its rows once. Together those
are exactly-once into bronze without any coordination with the edge.
"""

from __future__ import annotations

import hashlib
import json
import os
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ...ingest_sink.tabular import PushRowBatch, TabularIngestSink


class EdgeIntegrityError(RuntimeError):
    """A batch's bytes do not match the hash the edge recorded for it."""


class Edge(Protocol):
    def records(self, after: int, limit: int) -> list[dict]: ...

    def content(self, source: str, content_hash: str) -> bytes: ...


class FileCursor:
    """The last outbox sequence this node has made durable, in one small file."""

    def __init__(self, path: str | os.PathLike) -> None:
        self.path = Path(path)

    def get(self) -> int:
        try:
            return int(json.loads(self.path.read_text())["after"])
        except FileNotFoundError:
            return 0

    def set(self, after: int) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"after": int(after)}))
        tmp.replace(self.path)


class HttpEdge:
    """An edge over HTTPS, with this node's own credential."""

    def __init__(self, base_url: str, *, token: str, timeout: float = 60.0) -> None:
        self.base = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}", "User-Agent": "axiom-edge-pull"}
        self.timeout = timeout

    def _get(self, path: str) -> bytes:
        req = urllib.request.Request(self.base + path, headers=self._headers)
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310 - operator-configured URL
            return resp.read()

    def records(self, after: int, limit: int) -> list[dict]:
        q = urllib.parse.urlencode({"after": after, "limit": limit})
        return list(json.loads(self._get(f"/edge/outbox?{q}"))["records"])

    def content(self, source: str, content_hash: str) -> bytes:
        return self._get(
            f"/edge/content/{urllib.parse.quote(source, safe='')}/{urllib.parse.quote(content_hash, safe='')}"
        )


@dataclass
class PullReport:
    records: int = 0
    rows_in: int = 0
    rows_landed: int = 0
    rows_duplicate: int = 0
    after: int = 0


class EdgePuller:
    """Drain an edge's outbox into local row sinks, one durable batch at a time."""

    def __init__(
        self,
        edge: Edge,
        *,
        sink_for: Callable[[str], TabularIngestSink],
        cursor: FileCursor,
        source_map: dict[str, str] | None = None,
        page: int = 500,
    ) -> None:
        self.edge, self.sink_for, self.cursor = edge, sink_for, cursor
        self.source_map = dict(source_map or {})
        self.page = page

    def pull(self) -> PullReport:
        report = PullReport(after=self.cursor.get())
        while True:
            records = self.edge.records(after=report.after, limit=self.page)
            if not records:
                return report
            for rec in records:
                blob = self.edge.content(rec["source"], rec["content_hash"])
                if hashlib.sha256(blob).hexdigest() != rec["content_hash"]:
                    raise EdgeIntegrityError(
                        f"batch {rec['item_id']!r} (seq {rec['seq']}) does not match its recorded hash"
                    )
                local = self.source_map.get(rec["source"], rec["source"])
                batch = PushRowBatch(
                    item_id=rec["item_id"],
                    schema_ref=rec["schema_ref"],
                    rows=json.loads(blob),
                    etag=rec.get("etag"),
                    source_path=rec.get("source_path"),
                    metadata=dict(rec.get("metadata") or {}),
                )
                res = self.sink_for(local).ingest_rows(local, [batch])
                if res.errored:
                    raise RuntimeError(f"batch {rec['item_id']!r} (seq {rec['seq']}) failed to land locally")
                report.records += 1
                report.rows_in += res.rows_in
                report.rows_landed += res.rows_landed
                report.rows_duplicate += res.rows_duplicate
                report.after = int(rec["seq"])
                self.cursor.set(report.after)  # only after the batch is durable here


__all__ = ["Edge", "EdgeIntegrityError", "EdgePuller", "FileCursor", "HttpEdge", "PullReport"]
