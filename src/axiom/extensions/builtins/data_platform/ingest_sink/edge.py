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
    """Append-only, numbered, idempotent record of what this edge holds.

    Two small indexes keep every operation independent of the outbox's size,
    which is never trimmed and grows by one line per batch for as long as the
    edge runs:

    * ``index/keys/<hash[:3]>`` — one shard per hash prefix, a line per
      ``(source, content_hash)`` held. A duplicate check reads one shard.
    * ``index/offsets`` — the byte offset of every ``STRIDE``-th record, so a
      page after a late cursor seeks instead of scanning from the first line.

    ``index/state`` records how far into the outbox both indexes reach. Lines
    beyond it (an outbox written before the index existed, or a crash between
    the outbox write and the index write) are indexed on the next write, under
    the same lock, so the indexes can never be ahead of the outbox and a
    duplicate can never slip past them.
    """

    STRIDE = 256

    def __init__(self, directory: str | os.PathLike) -> None:
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "outbox.jsonl"
        self.index = self.dir / "index"
        self._lock = threading.Lock()

    @classmethod
    def from_env(cls) -> "EdgeOutbox | None":
        raw = os.environ.get(OUTBOX_DIR_ENV, "").strip()
        return cls(raw) if raw else None

    # -- index ----------------------------------------------------------------

    def _state(self) -> dict:
        try:
            return json.loads((self.index / "state").read_text())
        except (FileNotFoundError, ValueError):
            return {"bytes": 0, "last": 0}

    def _save_state(self, state: dict) -> None:
        tmp = self.index / "state.tmp"
        tmp.write_text(json.dumps(state))
        tmp.replace(self.index / "state")

    def _shard(self, content_hash: str) -> Path:
        return self.index / "keys" / content_hash[:3]

    def _held(self, source: str, content_hash: str) -> bool:
        shard = self._shard(content_hash)
        if not shard.exists():
            return False
        needle = f"{source}\t{content_hash}"
        with open(shard, encoding="utf-8") as f:
            return any(line.rstrip("\n") == needle for line in f)

    def _index_one(self, rec: dict, offset: int) -> None:
        shard = self._shard(rec["content_hash"])
        shard.parent.mkdir(parents=True, exist_ok=True)
        with open(shard, "a", encoding="utf-8") as f:
            f.write(f"{rec['source']}\t{rec['content_hash']}\n")
        if (int(rec["seq"]) - 1) % self.STRIDE == 0:
            with open(self.index / "offsets", "a", encoding="utf-8") as f:
                f.write(f"{int(rec['seq'])} {offset}\n")

    def _catch_up(self) -> dict:
        """Index any outbox lines the indexes do not cover yet. Under the lock."""
        self.index.mkdir(parents=True, exist_ok=True)
        state = self._state()
        size = self.path.stat().st_size if self.path.exists() else 0
        if size <= state["bytes"]:
            return state
        with open(self.path, "rb") as f:
            f.seek(state["bytes"])
            offset = state["bytes"]
            for raw in f:
                if not raw.endswith(b"\n"):
                    break  # a torn last line: left for the writer that owns it
                if raw.strip():
                    rec = json.loads(raw)
                    self._index_one(rec, offset)
                    state["last"] = max(state["last"], int(rec["seq"]))
                offset += len(raw)
            state["bytes"] = offset
        self._save_state(state)
        return state

    # -- writing ------------------------------------------------------------

    def _locked(self):
        lockf = open(self.dir / ".lock", "a")
        # The file lock covers a second process on the same edge (two workers);
        # the thread lock covers this one. Elsewhere (a developer's Windows
        # laptop running the tests) the thread lock is all there is.
        try:
            import fcntl
        except ImportError:  # pragma: no cover - non-POSIX
            fcntl = None
        if fcntl is not None:
            fcntl.flock(lockf, fcntl.LOCK_EX)

        class _Release:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *exc):
                if fcntl is not None:
                    fcntl.flock(lockf, fcntl.LOCK_UN)
                lockf.close()

        return _Release()

    def record(self, *, source: str, batch: Any, content_hash: str, rows_landed: int) -> dict | None:
        """Append one batch, unless this edge already holds it. Returns the
        record written, or ``None`` for a batch already recorded."""
        with self._lock, self._locked():
            state = self._catch_up()
            if self._held(source, content_hash):
                return None
            rec = {
                "seq": state["last"] + 1,
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
            line = (json.dumps(rec, sort_keys=True) + "\n").encode("utf-8")
            with open(self.path, "ab") as f:
                offset = f.tell()
                f.write(line)
                f.flush()
                os.fsync(f.fileno())
            self._index_one(rec, offset)
            self._save_state({"bytes": offset + len(line), "last": rec["seq"]})
            return rec

    # -- reading ------------------------------------------------------------

    def _seek_point(self, after: int) -> int:
        """The byte offset of the latest indexed record at or before ``after + 1``."""
        best = 0
        try:
            with open(self.index / "offsets", encoding="utf-8") as f:
                for line in f:
                    seq_s, off_s = line.split()
                    if int(seq_s) <= after + 1:
                        best = int(off_s)
                    else:
                        break
        except (FileNotFoundError, ValueError):
            return 0
        return best

    def read(self, *, after: int, limit: int = MAX_PAGE) -> list[dict]:
        """Records with ``seq > after``, oldest first, at most ``limit``."""
        if not self.path.exists():
            return []
        out: list[dict] = []
        with open(self.path, "rb") as f:
            f.seek(self._seek_point(after))
            for raw in f:
                if not raw.endswith(b"\n") or not raw.strip():
                    continue
                rec = json.loads(raw)
                if int(rec["seq"]) > after:
                    out.append(rec)
                    if len(out) >= limit:
                        break
        return out

    def last_seq(self) -> int:
        with self._lock, self._locked():
            return int(self._catch_up()["last"])

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
        from .edge_retention import maybe_prune_after_page

        principal = getattr(request.state, "principal", None)
        maybe_prune_after_page(outbox, principal=str(getattr(principal, "handle", principal)), after=after,
                               bronze_root_for=bronze_root_for)
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
            from .edge_retention import pruned_through

            held = [r for r in outbox.read(after=0, limit=10**9)
                    if r["source"] == source and r["content_hash"] == content_hash]
            if held and int(held[0]["seq"]) <= pruned_through(outbox):
                raise HTTPException(
                    status_code=410,
                    detail="every downstream already holds this batch, so the edge let it go",
                ) from exc
            raise HTTPException(status_code=404, detail="no such batch on this edge") from exc
        return Response(content=blob, media_type="application/octet-stream",
                        headers={"X-Content-SHA256": content_hash})

    return router


def build_health_router():
    from fastapi import APIRouter

    router = APIRouter()

    @router.get("/healthz")
    def healthz():
        """Ok only when this node can still write where it lands data.

        A node with a full disk used to answer "ok" here while every push
        failed to land. The check writes and removes a small file in the
        outbox folder (or the state folder), and reports the free space.
        """
        import shutil
        import uuid

        from fastapi.responses import JSONResponse

        where = Path(os.environ.get(OUTBOX_DIR_ENV, "").strip()
                     or os.environ.get("AXI_STATE_DIR", "").strip() or ".")
        body: dict = {"status": "ok", "path": str(where)}
        try:
            body["free_bytes"] = shutil.disk_usage(where).free
            probe = where / f".healthz-{uuid.uuid4().hex}"
            probe.write_bytes(b"ok")
            probe.unlink()
        except OSError as exc:
            body.update(status="cannot_write", reason=exc.strerror or str(exc))
            return JSONResponse(body, status_code=503)
        outbox = EdgeOutbox.from_env()
        if outbox is not None:
            from .edge_retention import health_detail

            body.update(health_detail(outbox))
        from .headroom import watched

        disks = watched()
        if disks:
            # Information, not a failure: the node still answers while there
            # is room, and says early that a watched volume is filling.
            body["disks"] = disks
            body["disk_alarm"] = any(d["alarm"] for d in disks)
        return body

    return router


__all__ = ["DOWNSTREAM_ENV", "MAX_PAGE", "OUTBOX_DIR_ENV", "EdgeOutbox", "build_edge_router", "build_health_router"]
