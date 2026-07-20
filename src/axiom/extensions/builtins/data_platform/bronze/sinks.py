# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Bronze sink backends.

``FilesystemBronzeSink`` is the lean default and the development
backstop. Layout:

::

    <root>/
      <source_name>/
        _records/<YYYY-MM-DD>/<item_id>.json     # ALLOW/QUARANTINE
        _content/<sha2>/<sha256>                 # content-addressed blob
        _quarantine/<YYYY-MM-DD>/<item_id>.json  # QUARANTINE sidecars
        _excluded/<YYYY-MM-DD>/<item_id>.json    # EXCLUDE decision logs

Content blobs are content-addressed under ``_content/<sha2>/<sha256>``
where ``<sha2>`` is the first two hex chars (avoids one giant
directory). Re-writing the same content is a no-op.

The Iceberg-backed ``LakehouseBronzeSink`` lands in Slice 3 under the
``[heavy]`` extra, with the same protocol shape so the writer is
agnostic.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

from axiom.rag.ingest_router import Disposition, RouteDecision

from ..contracts import FetchedItem, RowBatch
from .router import TabularWriteResult


class FilesystemBronzeSink:
    """File-backed sink — substrate-of-record for the lean tier."""

    def __init__(self, *, root: Path) -> None:
        self.root = Path(root)

    def write_content(self, *, content: bytes, content_hash: str) -> Path:
        prefix = content_hash[:2]
        rest = content_hash
        blob_dir = self.root / "_content" / prefix
        blob_dir.mkdir(parents=True, exist_ok=True)
        blob = blob_dir / rest
        if not blob.exists():
            # Atomic-ish write: temp file in the same dir, then rename.
            tmp = blob.with_suffix(".part")
            tmp.write_bytes(content)
            tmp.replace(blob)
        return blob

    def write_record(
        self,
        *,
        item: FetchedItem,
        decision: RouteDecision,
        tier: str | None,
        content_hash: str | None,
        fetched_at: datetime,
    ) -> Path:
        day = fetched_at.date().isoformat()
        subdir = {
            Disposition.ALLOW: "_records",
            Disposition.QUARANTINE: "_quarantine",
            Disposition.EXCLUDE: "_excluded",
        }[decision.disposition]
        rec_dir = self.root / item.source_name / subdir / day
        rec_dir.mkdir(parents=True, exist_ok=True)
        rec_path = rec_dir / f"{_safe_id(item.item_id)}.json"

        sidecar = {
            "source_name": item.source_name,
            "item_id": item.item_id,
            "display_name": item.display_name,
            "content_type": item.content_type,
            "size": item.size,
            "modified_at": item.modified_at.isoformat() if item.modified_at else None,
            "etag": item.etag,
            "source_path": item.source_path,
            "extra": dict(item.extra),
            # Decision stamping — auditors read this without re-running the gate.
            "disposition": decision.disposition.value,
            "tier": tier,
            "matched_rule": decision.matched,
            "reason": decision.reason,
            "content_sha256": content_hash,
            "fetched_at": fetched_at.isoformat(),
        }
        rec_path.write_text(json.dumps(sidecar, indent=2, sort_keys=True))
        return rec_path


def _safe_id(item_id: str) -> str:
    """Conservative filename-safe rendering of an item id.

    Box ids are numeric, but other sources may carry slashes / colons —
    keep the filename portable across macOS / Linux / Windows.
    """
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in item_id)[:128]


def _row_hash(row: dict) -> str:
    """Stable content hash of one row — the silver-tier ``content_hash`` dedup
    key, applied per row. Canonical JSON (sorted keys, tight separators,
    ``str`` fallback for non-JSON cells) so the same logical row always hashes
    the same regardless of dict insertion order or numeric/temporal typing.
    """
    return hashlib.sha256(
        json.dumps(row, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


class FilesystemTabularBronzeSink:
    """File-backed tabular sink — the lean default + dev backstop for the row
    lane (ADR-001), peer to :class:`FilesystemBronzeSink`.

    Layout::

        <root>/<source_name>/
          _content/<sha2>/<sha256>              # raw batch payload, content-addressed
          _rows/<YYYY-MM-DD>/<item_id>.jsonl    # ALLOW rows (one JSON obj/line)
          _quarantine_rows/<YYYY-MM-DD>/...     # QUARANTINE rows (land, not promoted)
          _excluded/<YYYY-MM-DD>/<item_id>.json # EXCLUDE decision log (no rows land)
          _seen.txt                             # row content_hash ledger (dedup tier)

    Row-level dedup: a row whose ``content_hash`` is already in ``_seen.txt`` is
    a duplicate and is not re-landed — the silver-tier ``content_hash`` dedup,
    applied per row. The ledger is persistent, so a later run of the same source
    keeps deduping across process restarts (a fresh sink re-reads it lazily).
    """

    def __init__(self, *, root: Path) -> None:
        self.root = Path(root)
        self._seen_cache: dict[str, set[str]] = {}

    def _seen_path(self, source_name: str) -> Path:
        return self.root / source_name / "_seen.txt"

    def _load_seen(self, source_name: str) -> set[str]:
        cached = self._seen_cache.get(source_name)
        if cached is not None:
            return cached
        seen: set[str] = set()
        p = self._seen_path(source_name)
        if p.exists():
            seen = {ln.strip() for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()}
        self._seen_cache[source_name] = seen
        return seen

    def write_content(self, *, content: bytes, content_hash: str, source_name: str) -> Path:
        """Persist the raw batch payload, content-addressed (replay/audit)."""
        blob_dir = self.root / source_name / "_content" / content_hash[:2]
        blob_dir.mkdir(parents=True, exist_ok=True)
        blob = blob_dir / content_hash
        if not blob.exists():
            tmp = blob.with_suffix(".part")
            tmp.write_bytes(content)
            tmp.replace(blob)
        return blob

    def write_rows(
        self,
        *,
        batch: RowBatch,
        decision: RouteDecision,
        tier: str | None,
        fetched_at: datetime,
    ) -> TabularWriteResult:
        day = fetched_at.date().isoformat()
        src = batch.source_name

        if decision.disposition is Disposition.EXCLUDE:
            exc_dir = self.root / src / "_excluded" / day
            exc_dir.mkdir(parents=True, exist_ok=True)
            (exc_dir / f"{_safe_id(batch.item_id)}.json").write_text(
                json.dumps(
                    {
                        "source_name": src,
                        "item_id": batch.item_id,
                        "schema_ref": batch.schema_ref,
                        "rows_in": len(batch.rows),
                        "disposition": decision.disposition.value,
                        "reason": decision.reason,
                        "matched_rule": decision.matched,
                        "fetched_at": fetched_at.isoformat(),
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return TabularWriteResult(
                item_id=batch.item_id, disposition=decision.disposition, tier=tier,
                rows_in=len(batch.rows), rows_landed=0, rows_duplicate=0, fetched_at=fetched_at,
            )

        raw_hash = hashlib.sha256(batch.raw).hexdigest()
        self.write_content(content=batch.raw, content_hash=raw_hash, source_name=src)

        seen = self._load_seen(src)
        subdir = "_rows" if decision.disposition is Disposition.ALLOW else "_quarantine_rows"
        landed_lines: list[str] = []
        new_hashes: list[str] = []
        landed = dup = 0
        for row in batch.rows:
            h = _row_hash(row)
            if h in seen:
                dup += 1
                continue
            seen.add(h)
            new_hashes.append(h)
            landed_lines.append(
                json.dumps(
                    {
                        "source_name": src,
                        "item_id": batch.item_id,
                        "schema_ref": batch.schema_ref,
                        "row_hash": h,
                        "row": row,
                        "tier": tier,
                        "disposition": decision.disposition.value,
                        "raw_sha256": raw_hash,
                        "fetched_at": fetched_at.isoformat(),
                    },
                    sort_keys=True,
                    default=str,
                )
            )
            landed += 1

        if landed_lines:
            rows_dir = self.root / src / subdir / day
            rows_dir.mkdir(parents=True, exist_ok=True)
            with (rows_dir / f"{_safe_id(batch.item_id)}.jsonl").open("a", encoding="utf-8") as fh:
                for line in landed_lines:
                    fh.write(line + "\n")
            seen_path = self._seen_path(src)
            seen_path.parent.mkdir(parents=True, exist_ok=True)
            with seen_path.open("a", encoding="utf-8") as fh:
                for h in new_hashes:
                    fh.write(h + "\n")

        return TabularWriteResult(
            item_id=batch.item_id, disposition=decision.disposition, tier=tier,
            rows_in=len(batch.rows), rows_landed=landed, rows_duplicate=dup, fetched_at=fetched_at,
        )


__all__ = ["FilesystemBronzeSink", "FilesystemTabularBronzeSink"]
