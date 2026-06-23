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

import json
from datetime import datetime
from pathlib import Path

from axiom.rag.ingest_router import Disposition, RouteDecision

from ..contracts import FetchedItem


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


__all__ = ["FilesystemBronzeSink"]
