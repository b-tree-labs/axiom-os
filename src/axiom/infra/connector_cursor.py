# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Per-connector persistent cursor — seen etags + watermark.

The third piece of connector hardening from the DP-1 stand-up: when
the Box dev token expired ~60 minutes into the run, the next run had
no record of "I've already fetched up to file X with etag Y," so it
re-walked the entire corpus, re-fetched every file, and re-charged
itself every rate-limit token.

This module owns the persistence shape:

::

    {
      "schema": 1,
      "seen_etags": {"<item_id>": "<etag>", ...},
      "watermark": "2026-06-01T05:42:13+00:00"
    }

Atomic write via temp file + ``Path.replace`` — a crash mid-save
leaves the previous cursor intact. Corrupted reads start empty rather
than raise (a connector must never die on its own cursor file).
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path

_SCHEMA_VERSION = 1


class ConnectorCursor:
    """Persistent cursor for one ingest connector instance.

    Construct with a path to a JSON file under
    ``$AXI_STATE/connectors/<source-name>/cursor.json`` (or any
    writable location). Reads on construct; writes on
    :meth:`save`. Mutations between construct and ``save()`` live in
    memory — call ``save()`` after each batch boundary so a crash
    loses at most one batch.

    Watermark advancement is monotonic: :meth:`advance_watermark`
    refuses to move backwards even if the caller passes a later-but-
    earlier event timestamp by accident.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._etags: dict[str, str] = {}
        self._watermark: datetime | None = None
        self._load()

    # ---- read ---------------------------------------------------------------

    def get_etag(self, item_id: str) -> str | None:
        return self._etags.get(item_id)

    def all_etags(self) -> Mapping[str, str]:
        return dict(self._etags)

    def watermark(self) -> datetime | None:
        return self._watermark

    # ---- write --------------------------------------------------------------

    def set_etag(self, item_id: str, etag: str) -> None:
        self._etags[item_id] = etag

    def set_watermark(self, ts: datetime) -> None:
        self._watermark = ts

    def advance_watermark(self, ts: datetime) -> None:
        """Only move forward. Earlier-than-current is silently ignored."""
        if self._watermark is None or ts > self._watermark:
            self._watermark = ts

    # ---- persistence --------------------------------------------------------

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            blob = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            # Corrupted file → start clean. Connector must not die on cursor.
            return
        self._etags = dict(blob.get("seen_etags") or {})
        wm = blob.get("watermark")
        if wm:
            try:
                self._watermark = datetime.fromisoformat(wm)
            except ValueError:
                self._watermark = None

    def save(self) -> None:
        """Atomically persist current state to disk.

        Writes to a sibling tempfile then ``Path.replace`` to the
        target — a crash before ``replace`` leaves the previous
        cursor intact and readable.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        blob = {
            "schema": _SCHEMA_VERSION,
            "seen_etags": dict(self._etags),
            "watermark": self._watermark.isoformat() if self._watermark else None,
        }
        tmp = self.path.with_suffix(self.path.suffix + f".tmp.{os.getpid()}")
        tmp.write_text(json.dumps(blob, indent=2, sort_keys=True))
        tmp.replace(self.path)


__all__ = ["ConnectorCursor"]
