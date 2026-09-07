# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The LiveDispatcher — local subscribers, each a cursor into the Journal.

A subscriber that raises is skipped for this pump and retried next time; its
cursor does not advance past the failing record, and — cursor isolation — no
other subscriber (nor the Transmitter) is affected.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .envelope import JournaledRecord
from .journal import DAQJournal

Subscriber = Callable[[JournaledRecord], None]


@dataclass
class DispatchResult:
    delivered: dict[str, int]
    failed: dict[str, str]


class LiveDispatcher:
    def __init__(self, journal: DAQJournal, *, batch_size: int = 500) -> None:
        self.journal = journal
        self.batch_size = max(1, int(batch_size))
        self._subs: dict[str, Subscriber] = {}

    def subscribe(self, name: str, fn: Subscriber) -> None:
        if name in self._subs:
            raise ValueError(f"subscriber {name!r} already registered")
        self.journal.cursor(name)  # validates the name
        self._subs[name] = fn

    def pump(self) -> DispatchResult:
        delivered: dict[str, int] = {}
        failed: dict[str, str] = {}
        for name, fn in self._subs.items():
            cursor = f"sub.{name}"
            start = self.journal.cursor(cursor)
            n = 0
            for off, rec in self.journal.read(start, self.batch_size):
                try:
                    fn(rec)
                except Exception as exc:  # noqa: BLE001 — one subscriber's fault stays its own
                    failed[name] = f"{type(exc).__name__}: {exc}"
                    break
                self.journal.commit(cursor, off + 1)
                n += 1
            delivered[name] = n
        return DispatchResult(delivered=delivered, failed=failed)

    def health_details(self) -> dict:
        return {f"sub.{n}": self.journal.lag(f"sub.{n}") for n in self._subs}


__all__ = ["DispatchResult", "LiveDispatcher", "Subscriber"]
