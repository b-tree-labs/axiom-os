# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A small on-disk header cache so repeat ``/renew`` listings are fast.

Listing another harness's sessions means parsing each of the newest files (or session rows) to get a title and a
message count; on a large history that took ~10 s. This cache remembers the *header* of each session keyed by
where it came from and a stamp of its state (file path + mtime_ns + size; for SQLite stores the session id + the
harness's own last-updated stamp), so an unchanged session is never parsed twice.

What it holds, and does not:

* only ids, titles, counts, timestamps, working directory, format version and the source path: **never message
  content**. A title that reads as a secret is replaced by a withheld marker BEFORE it is written;
* it is a pure accelerator: it is safe to delete at any time, and a corrupt, truncated, wrong-version or
  unwritable cache file is treated as empty and rebuilt on the next listing, never an error;
* it lives in the user state dir (``renew-session-index.json``), is written atomically (temp + rename), and is
  capped per harness so it cannot grow without bound.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .sessions import SessionHeader, _looks_secret

VERSION = 1
MAX_ENTRIES_PER_HARNESS = 5000
WITHHELD_TITLE = "(title withheld: reads as a secret)"
_FIELDS = (
    "source_session_id",
    "title",
    "created_at",
    "updated_at",
    "message_count",
    "path",
    "cwd",
    "format_version",
)


def default_path() -> Path:
    from axiom.infra.paths import get_user_state_dir

    return get_user_state_dir() / "renew-session-index.json"


class SessionIndexCache:
    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else default_path()
        self._data: dict[str, dict[str, Any]] | None = None
        self._dirty = False

    # -- load / save ------------------------------------------------------------------------------
    def _load(self) -> dict[str, dict[str, Any]]:
        if self._data is not None:
            return self._data
        data: dict[str, dict[str, Any]] = {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if (
                isinstance(raw, dict)
                and raw.get("version") == VERSION
                and isinstance(raw.get("harnesses"), dict)
            ):
                for harness, entries in raw["harnesses"].items():
                    if isinstance(harness, str) and isinstance(entries, dict):
                        data[harness] = {k: v for k, v in entries.items() if isinstance(v, dict)}
        except (OSError, ValueError, UnicodeError):
            data = {}  # missing, unreadable or corrupt: start empty, rebuild as we list
        self._data = data
        return data

    def save(self) -> None:
        if not self._dirty or self._data is None:
            return
        try:
            for entries in self._data.values():
                if len(entries) > MAX_ENTRIES_PER_HARNESS:
                    keep = sorted(
                        entries.items(),
                        key=lambda kv: str(kv[1].get("h", {}).get("updated_at", "")),
                    )
                    for key, _ in keep[: len(entries) - MAX_ENTRIES_PER_HARNESS]:
                        del entries[key]
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + f".{os.getpid()}.tmp")
            tmp.write_text(
                json.dumps({"version": VERSION, "harnesses": self._data}, separators=(",", ":")),
                encoding="utf-8",
            )
            os.replace(tmp, self.path)
            self._dirty = False
        except OSError:
            pass  # an unwritable cache only costs speed

    # -- entries ----------------------------------------------------------------------------------
    def get(self, harness: str, key: str, stamp: list[Any]) -> SessionHeader | None:
        entry = self._load().get(harness, {}).get(key)
        if not entry or entry.get("stamp") != stamp:
            return None
        h = entry.get("h")
        try:
            return SessionHeader(
                harness=harness,
                source_session_id=str(h["source_session_id"]),
                title=str(h["title"]),
                created_at=str(h["created_at"]),
                updated_at=str(h["updated_at"]),
                message_count=int(h["message_count"]),
                path=Path(str(h["path"])),
                cwd=str(h.get("cwd", "")),
                format_version=str(h.get("format_version", "")),
            )
        except (KeyError, TypeError, ValueError):
            return None

    def put(self, harness: str, key: str, stamp: list[Any], header: SessionHeader) -> None:
        title = WITHHELD_TITLE if _looks_secret(header.title) else header.title
        record = {
            "source_session_id": header.source_session_id,
            "title": title,
            "created_at": header.created_at,
            "updated_at": header.updated_at,
            "message_count": header.message_count,
            "path": str(header.path),
            "cwd": header.cwd,
            "format_version": header.format_version,
        }
        assert set(record) == set(_FIELDS)  # nothing but headers ever goes in
        self._load().setdefault(harness, {})[key] = {"stamp": list(stamp), "h": record}
        self._dirty = True

    def clear(self) -> None:
        self._data = {}
        self._dirty = False
        try:
            self.path.unlink()
        except OSError:
            pass


__all__ = ["SessionIndexCache", "default_path", "WITHHELD_TITLE"]
