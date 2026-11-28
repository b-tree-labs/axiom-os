# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Triggers that take events, for the ADR-264 tests (VID-027..030).

``Listener`` is what a consumer's next stage would ship: a ``BaseTrigger`` with
``on_event``. It keeps a timeline of what it was given (``("frame", frame)`` and
``("event", event)``, in the order it saw them), the thread of every call, and
whether ``on_event`` ever ran while ``observe`` was running. Its config says how to
answer an event (``respond = "occurred"``), whether to raise, how slow to be, an
optional ``gate`` (a ``threading.Event``) ``on_event`` waits on, and an optional
``hold`` (a ``threading.Event``) ``observe`` waits on, with ``holding`` set once it
is waiting: a trigger that is behind because the test says so, not because a sleep
happened to be long enough. ``Relay`` is the
same under another name, for a chain of three. Every instance is kept in
:data:`BUILT`.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Mapping
from typing import Any

from axiom.extensions.builtins.video import ledger
from axiom.extensions.builtins.video.triggers import (
    BaseTrigger,
    TriggerConfigError,
    TriggerEvent,
    incident_key,
)

#: Every Listener (and Relay) built, in order.
BUILT: list[Listener] = []


class Listener(BaseTrigger):
    name = "listener"
    version = "1.0.0"
    description = "a test double that takes events and says what it was given"

    def __init__(self, config: Mapping[str, Any] | None = None) -> None:
        config = dict(config or {})
        self.respond = config.pop("respond", None)
        self.raise_on_event = bool(config.pop("raise_on_event", False))
        self.config_error_on_event = bool(config.pop("config_error_on_event", False))
        self.mutate = bool(config.pop("mutate", False))
        self.sleep_s = float(config.pop("sleep_s", 0.0))
        self.gate: threading.Event | None = config.pop("gate", None)
        self.hold: threading.Event | None = config.pop("hold", None)
        self.holding = threading.Event()
        self.journal = config.pop("journal", None)
        self.given = dict(config)  # what the factory was handed, before BaseTrigger
        super().__init__(config)
        self.timeline: list[tuple[str, Any]] = []
        self.threads: set[int] = set()
        self.overlaps = 0
        self.in_journal: list[bool] = []
        self.given_at: list[float] = []
        self._busy = False
        self._n = 0
        BUILT.append(self)

    @property
    def events(self) -> list[TriggerEvent]:
        return [x for kind, x in self.timeline if kind == "event"]

    @property
    def frames(self) -> list[Any]:
        return [x for kind, x in self.timeline if kind == "frame"]

    def observe(self, frame):
        self.threads.add(threading.get_ident())
        self._busy = True
        try:
            self.timeline.append(("frame", frame))
            if self.hold is not None and not self.hold.is_set():
                self.holding.set()
                self.hold.wait(timeout=30)
            if self.sleep_s:
                time.sleep(self.sleep_s)
        finally:
            self._busy = False
        return ()

    def on_event(self, event: TriggerEvent):
        self.threads.add(threading.get_ident())
        if self._busy:
            self.overlaps += 1
        self.given_at.append(time.monotonic())
        if self.gate is not None:
            self.gate.wait(timeout=30)
        if self.journal is not None:
            rows = ledger.read(self.journal, stream=event.stream)
            self.in_journal.append(event.as_dict() in rows)
        self.timeline.append(("event", event))
        if self.mutate:
            event.detail["touched"] = self.name
        if self.config_error_on_event:
            raise TriggerConfigError("the region is outside the frame", fix="set a smaller region")
        if self.raise_on_event:
            raise RuntimeError("scripted failure in on_event")
        if not self.respond:
            return ()
        self._n += 1
        return [
            TriggerEvent(
                stream=event.stream,
                trigger=self.label,
                ts=event.ts,
                kind=self.respond,
                incident_key=incident_key(event.stream, self.label, f"r{self._n}"),
                severity=self.severity,
                detail={"cause": event.incident_key},
            )
        ]


class Relay(Listener):
    name = "relay"


__all__ = ["BUILT", "Listener", "Relay"]
