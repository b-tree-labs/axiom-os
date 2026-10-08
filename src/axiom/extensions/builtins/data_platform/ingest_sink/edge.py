# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The ingest edge's outbox, and the export a downstream node pulls it through.

An ingest edge (ADR-177) is a node whose only job is to land what producers
push and hold it for the node that owns the data platform. Producers may only
open connections outward, and the owning node may sit where nobody outside can
reach it, so neither can call the other: the edge sits between them, and the
owning node pulls.

The outbox is the edge's record of what it holds: one JSON line per batch that
is durable in bronze, numbered in arrival order. It is idempotent by
``(source, content_hash)``, which is what lets a producer's replay repair a
batch whose outbox write failed (see :class:`~.tabular.TabularIngestSink`).

The export serves only the downstream nodes the edge is configured to export
to (``AXIOM_EDGE_DOWNSTREAM``, a comma-separated list of principals), on top of
the ``edge_export:read`` scope the authz seam checks. A producer has no business
reading other producers' data back out, and a scope alone would let a wildcard
read grant do it; naming the downstream is what makes the export one-way. Unset
means nobody: an edge that has not been told where its data goes exports
nothing.
"""

# No ``from __future__ import annotations`` here: FastAPI imports are kept
# inside the router builders (so the outbox imports without a web stack), and
# postponed annotations would turn their ``Request`` into a string FastAPI
# cannot resolve, quietly making it a required query parameter.

import json
import os
import re
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

#: Where the outbox lives. Set on an ingest edge; unset everywhere else, which
#: is what keeps the export off a node that is not an edge.
OUTBOX_DIR_ENV = "AXIOM_INGEST_OUTBOX_DIR"

#: The principals allowed to pull the export, comma-separated. Fail closed.
DOWNSTREAM_ENV = "AXIOM_EDGE_DOWNSTREAM"

#: Records per pull page. A page is a list of small JSON records, not data.
MAX_PAGE = 1000

_HASH = re.compile(r"^[0-9a-f]{64}$")
_SOURCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")


class EdgeOutbox:
    """Append-only, numbered, idempotent record of what this edge holds."""

    def __init__(self, directory: str | os.PathLike) -> None:
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "outbox.jsonl"
        self._lock = threading.Lock()
        self._known: set[tuple[str, str]] | None = None
        self._last = 0

    @classmethod
    def from_env(cls) -> "EdgeOutbox | None":
        raw = os.environ.get(OUTBOX_DIR_ENV, "").strip()
        return cls(raw) if raw else None

    # -- writing ------------------------------------------------------------

    def _load(self) -> None:
        known: set[tuple[str, str]] = set()
        last = 0
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                rec = json.loads(line)
                known.add((rec["source"], rec["content_hash"]))
                last = max(last, int(rec["seq"]))
        self._known, self._last = known, last

    def record(self, *, source: str, batch: Any, content_hash: str, rows_landed: int) -> dict | None:
        """Append one batch, unless this edge already holds it. Returns the
        record written, or ``None`` for a batch already recorded."""
        with self._lock, open(self.dir / ".lock", "a") as lockf:
            # The file lock covers a second process on the same edge (two
            # workers); the thread lock covers this one. An edge runs on a
            # POSIX host; elsewhere (a developer's Windows laptop running the
            # tests) the thread lock is all there is, and that is one process.
            try:
                import fcntl
            except ImportError:  # pragma: no cover - non-POSIX
                fcntl = None
            if fcntl is not None:
                fcntl.flock(lockf, fcntl.LOCK_EX)
            try:
                self._load()  # always re-read under the lock: another process may have appended
                assert self._known is not None
                if (source, content_hash) in self._known:
                    return None
                rec = {
                    "seq": self._last + 1,
                    "source": source,
                    "item_id": batch.item_id,
                    "schema_ref": batch.schema_ref,
                    "etag": batch.etag,
                    "source_path": batch.source_path,
                    "metadata": dict(batch.metadata),
                    "content_hash": content_hash,
                    "rows": len(batch.rows),
                    "rows_landed": rows_landed,
                    "received_at": datetime.now(UTC).isoformat(),
                }
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, sort_keys=True) + "\n")
                    f.flush()
                    os.fsync(f.fileno())
                self._known.add((source, content_hash))
                self._last = rec["seq"]
                return rec
            finally:
                if fcntl is not None:
                    fcntl.flock(lockf, fcntl.LOCK_UN)

    # -- reading ------------------------------------------------------------

    def read(self, *, after: int, limit: int = MAX_PAGE) -> list[dict]:
        """Records with ``seq > after``, oldest first, at most ``limit``."""
        if not self.path.exists():
            return []
        out: list[dict] = []
        with open(self.path, encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                rec = json.loads(line)
                if int(rec["seq"]) > after:
                    out.append(rec)
                    if len(out) >= limit:
                        break
        return out

    def last_seq(self) -> int:
        with self._lock:
            self._load()
            return self._last

    @staticmethod
    def content(source: str, content_hash: str, *, bronze_root: str | os.PathLike) -> bytes:
        """The batch payload exactly as it landed, addressed by its hash."""
        if not _SOURCE.match(source) or not _HASH.match(content_hash):
            raise ValueError("malformed source or content hash")
        blob = Path(bronze_root) / source / "_content" / content_hash[:2] / content_hash
        return blob.read_bytes()


def _bronze_root_for(source: str) -> Path:
    from ..agents.plinth.connectors import load_connector

    return Path(load_connector(source).bronze_root)


def build_edge_router(outbox: EdgeOutbox, *, bronze_root_for=_bronze_root_for):
    """``GET /edge/outbox`` and ``GET /edge/content/{source}/{hash}``."""
    from fastapi import APIRouter, HTTPException, Query, Request
    from fastapi.responses import Response

    router = APIRouter()

    def _only_downstream(request: Request) -> None:
        allowed = {p.strip() for p in os.environ.get(DOWNSTREAM_ENV, "").split(",") if p.strip()}
        principal = getattr(request.state, "principal", None)
        handle = getattr(principal, "handle", principal)
        if not allowed or str(handle or "") not in allowed:
            raise HTTPException(
                status_code=403,
                detail="the export serves the downstream nodes this edge is configured for, and only them",
            )

    @router.get("/edge/outbox")
    def outbox_page(
        request: Request,
        after: int = Query(0, ge=0),
        limit: int = Query(MAX_PAGE, ge=1, le=MAX_PAGE),
    ) -> dict:
        _only_downstream(request)
        records = outbox.read(after=after, limit=limit)
        return {"records": records, "last_seq": outbox.last_seq()}

    @router.get("/edge/content/{source}/{content_hash}")
    def content(request: Request, source: str, content_hash: str) -> Response:
        _only_downstream(request)
        try:
            blob = outbox.content(source, content_hash, bronze_root=bronze_root_for(source))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except (FileNotFoundError, KeyError) as exc:
            raise HTTPException(status_code=404, detail="no such batch on this edge") from exc
        return Response(content=blob, media_type="application/octet-stream",
                        headers={"X-Content-SHA256": content_hash})

    return router


def build_health_router():
    from fastapi import APIRouter

    router = APIRouter()

    @router.get("/healthz")
    def healthz() -> dict:
        return {"status": "ok"}

    return router


__all__ = ["DOWNSTREAM_ENV", "MAX_PAGE", "OUTBOX_DIR_ENV", "EdgeOutbox", "build_edge_router", "build_health_router"]
