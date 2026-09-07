# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Recently-revoked set — ADR-103 decision 10.

Grants and revocations are not symmetric risks. Someone who gains access a
minute late is inconvenienced; someone who *retains* access a minute too long is
an incident. Treating both with one cache policy prices the cheap case and the
expensive case identically.

So removals get their own fast path: a compact, bounded, local set that the
resolver consults on every resolution with no directory round-trip. Additions
may arrive lazily on the normal projection cadence; removals take effect on the
next resolution — even against a token minted before the revocation, which is
precisely the window ADR-086 exists to close.

Bounded two ways, because an unbounded "security" structure becomes its own
outage: entries expire after ``window`` (by then the directory projection has
caught up and the slow path agrees), and the set evicts oldest-first past
``max_entries``.
"""

from __future__ import annotations

import json
import os
import time
from collections import OrderedDict
from pathlib import Path


class RevokedSet:
    def __init__(self, *, window: float = 900.0, max_entries: int = 10_000) -> None:
        self._window = window
        self._max = max_entries
        self._entries: OrderedDict[tuple[str, str], float] = OrderedDict()

    def __len__(self) -> int:
        return len(self._entries)

    @staticmethod
    def _key(subject: str, group_id: str) -> tuple[str, str]:
        return (subject, group_id)

    def record(self, subject: str, group_id: str, *, now: float | None = None) -> None:
        now = time.time() if now is None else now
        key = self._key(subject, group_id)
        self._entries.pop(key, None)
        self._entries[key] = now
        while len(self._entries) > self._max:
            self._entries.popitem(last=False)

    def clear_entry(self, subject: str, group_id: str) -> None:
        """A re-grant must not leave someone locked out by a stale revocation."""
        self._entries.pop(self._key(subject, group_id), None)

    def is_revoked(self, subject: str, group_id: str, *, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        at = self._entries.get(self._key(subject, group_id))
        if at is None:
            return False
        if (now - at) > self._window:
            self._entries.pop(self._key(subject, group_id), None)
            return False
        return True


class JsonFileRevokedSet(RevokedSet):
    """A :class:`RevokedSet` that survives the process.

    The in-memory set closes the token-lifetime window only while the process
    that recorded the revocation is alive; a serve restart between a sync and
    the next resolution would silently reopen it. This variant writes every
    mutation to a small JSON file (atomic, 0600) and reloads it on start,
    dropping entries already past ``window``. The reconciler (which records
    revocations) and the resolver (which consults them) point at the same
    file — that is the whole contract.
    """

    def __init__(
        self,
        path: str | os.PathLike,
        *,
        window: float = 900.0,
        max_entries: int = 10_000,
    ) -> None:
        super().__init__(window=window, max_entries=max_entries)
        self._path = Path(path)
        self._load()

    @property
    def path(self) -> Path:
        return self._path

    def _load(self) -> None:
        if not self._path.is_file():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8") or "{}")
        except (OSError, ValueError):
            return  # unreadable file → start empty; the next record() rewrites it
        # Expiry is judged lazily by ``is_revoked`` against the caller's clock
        # (which tests drive synthetically); loading only restores, bounded by
        # ``max_entries`` like every other write.
        for entry in raw.get("entries", []) or []:
            try:
                subject, group_id, at = str(entry[0]), str(entry[1]), float(entry[2])
            except (IndexError, TypeError, ValueError):
                continue
            self._entries[self._key(subject, group_id)] = at
        while len(self._entries) > self._max:
            self._entries.popitem(last=False)

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(self._path.name + ".tmp")
        payload = {
            "window": self._window,
            "entries": [[s, g, at] for (s, g), at in self._entries.items()],
        }
        tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, self._path)

    def record(self, subject: str, group_id: str, *, now: float | None = None) -> None:
        super().record(subject, group_id, now=now)
        self._save()

    def clear_entry(self, subject: str, group_id: str) -> None:
        super().clear_entry(subject, group_id)
        self._save()


__all__ = ["JsonFileRevokedSet", "RevokedSet"]
