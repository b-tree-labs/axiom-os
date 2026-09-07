# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Durable dedup for ``axi notifications send``.

``SendContext.dedup_log`` was an in-memory dict, so suppression only ever
worked *within one process*. Every caller that forks per run — which is every
shell script on a timer — got a fresh empty log and re-sent forever.

That is not theoretical. A node-health watchdog passed
``--dedup-key watchdog:failed:system:<date>``, documented itself as "no re-alert
storm every 15 min", and delivered 96 identical alerts a day into a shared
channel for weeks. The key was accepted and honoured perfectly; the log simply
did not outlive the process.

``FileDedupLog`` is a mapping with a TTL, persisted as JSON, safe for concurrent
writers. It satisfies the same interface the send path already uses, so nothing
in ``send()`` changes: a durable store is a *substitution*, not a rewrite.

The ``DedupLog`` SQL table (``db_models``) remains the destination for
deployments with a database. This is the floor for the ones without — and the
floor is what a shell script on a timer actually hits.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from collections.abc import MutableMapping
from pathlib import Path
from typing import Iterator

#: One day. Long enough that a standing condition alerts once per day, short
#: enough that a genuinely recurring problem is not silenced forever.
DEFAULT_TTL_SECONDS = 86_400


def default_dedup_path() -> Path:
    base = os.environ.get("AXIOM_STATE_DIR") or os.path.expanduser("~/.axi/state")
    return Path(base) / "notifications" / "dedup.json"


class FileDedupLog(MutableMapping):
    """``(actor, dedup_key) -> receipt_id``, persisted with a TTL.

    Reads on every lookup rather than caching: the whole point is that another
    process wrote it. A corrupt or unreadable file degrades to "nothing is
    deduplicated" — losing suppression is noisy, but losing an alert entirely
    is the failure that matters.
    """

    def __init__(self, path: Path | None = None, *, ttl: float = DEFAULT_TTL_SECONDS,
                 now: float | None = None) -> None:
        self._path = Path(path) if path else default_dedup_path()
        self._ttl = ttl
        self._now_override = now

    def _now(self) -> float:
        return self._now_override if self._now_override is not None else time.time()

    @staticmethod
    def _encode(key: tuple[str, str]) -> str:
        return "\x1f".join(key)

    @staticmethod
    def _decode(raw: str) -> tuple[str, str]:
        actor, _, dedup = raw.partition("\x1f")
        return (actor, dedup)

    def _load(self) -> dict[str, list]:
        try:
            data = json.loads(self._path.read_text())
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        cutoff = self._now()
        return {
            k: v
            for k, v in data.items()
            if isinstance(v, list) and len(v) == 2 and v[1] > cutoff
        }

    def _save(self, data: dict[str, list]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic replace: a timer firing mid-write must never read a half file.
        fd, tmp = tempfile.mkstemp(dir=str(self._path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(data, fh)
            os.replace(tmp, self._path)
        except OSError:
            with contextlib_suppress():
                os.unlink(tmp)

    # -- MutableMapping ----------------------------------------------------
    def __getitem__(self, key: tuple[str, str]) -> str:
        entry = self._load().get(self._encode(key))
        if entry is None:
            raise KeyError(key)
        return entry[0]

    def __setitem__(self, key: tuple[str, str], receipt_id: str) -> None:
        data = self._load()
        data[self._encode(key)] = [receipt_id, self._now() + self._ttl]
        self._save(data)

    def __delitem__(self, key: tuple[str, str]) -> None:
        data = self._load()
        if data.pop(self._encode(key), None) is None:
            raise KeyError(key)
        self._save(data)

    def __iter__(self) -> Iterator[tuple[str, str]]:
        return (self._decode(k) for k in self._load())

    def __len__(self) -> int:
        return len(self._load())


class contextlib_suppress:
    """Tiny local suppressor — avoids importing contextlib for one cleanup."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc) -> bool:
        return True


__all__ = ["DEFAULT_TTL_SECONDS", "FileDedupLog", "default_dedup_path"]
