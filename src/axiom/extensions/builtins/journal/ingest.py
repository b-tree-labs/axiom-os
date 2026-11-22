# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Ingest: a live bus subscription and a catch-up ticker, both into the one writer.

``start()`` runs in the serving process (the API contribution calls it once). It subscribes
one handler to the bus with the pattern ``>`` and ``fail_mode="ignore"``, runs one catch-up,
then catches up every ``JOURNAL_CATCH_UP_SECONDS`` on a daemon ticker. A live event the process
missed is filled by catch-up; a duplicate is ignored by the writer's key. Every failure is
swallowed and counted: the journal never fails a publisher.

There is no write skill and no schedule entry (LB-J1-A7: no journal skill has side effects, and
the scheduler can only run a skill), so the ticker is the schedule.
"""

from __future__ import annotations

import logging
import os
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import text

from . import rows, sources, store, writer

log = logging.getLogger("axiom.journal.ingest")

CATCH_UP_SECONDS_ENV = "JOURNAL_CATCH_UP_SECONDS"
DEFAULT_CATCH_UP_SECONDS = 60


def load_position(source: str) -> dict[str, Any] | None:
    with store.session_scope() as s:
        return s.execute(
            text("SELECT position FROM journal_cursors WHERE source = :source"), {"source": source}
        ).scalar()


def save_position(source: str, position: dict[str, Any]) -> None:
    import json

    with store.session_scope() as s:
        s.execute(
            text(
                "INSERT INTO journal_cursors (source, position, updated_at) "
                "VALUES (:source, CAST(:position AS jsonb), :now) "
                "ON CONFLICT (source) DO UPDATE SET position = EXCLUDED.position, "
                "updated_at = EXCLUDED.updated_at"
            ),
            {
                "source": source,
                "position": json.dumps(position, default=str),
                "now": datetime.now(UTC),
            },
        )
        s.commit()


class Ingest:
    def __init__(self) -> None:
        self.bus: Any = None
        self.state_dir: Path | None = None
        self.sources: list[sources.Source] = sources.default_sources()
        self._sub: Any = None
        self._ticker: threading.Thread | None = None
        self._stop = threading.Event()
        self._failures = 0
        self.last_error: str | None = None
        self._lock = threading.RLock()

    # -- lifecycle ---------------------------------------------------------------------

    @property
    def started(self) -> bool:
        return self._sub is not None

    def start(
        self,
        *,
        bus: Any = None,
        state_dir: Path | None = None,
        ticker: bool = True,
        catch_up: bool = True,
        interval: float | None = None,
    ) -> Ingest:
        """Subscribe, catch up once, and keep catching up.

        Idempotent while started on the same bus and state dir. A caller that names a different
        bus or state dir knows better than a start that happened as a side effect (the API
        contribution registering in a process that also runs tests): the old subscription and
        ticker stop and ingest starts again on what was asked for.
        """
        if bus is None:
            from axiom.infra.bus import get_default_eventbus

            bus = get_default_eventbus()
        if state_dir is None:
            from axiom.infra.paths import get_user_state_dir

            state_dir = get_user_state_dir()
        state_dir = Path(state_dir)
        with self._lock:
            if self._sub is not None:
                if bus is self.bus and state_dir == self.state_dir:
                    return self
                self.stop()
            self.bus = bus
            self.state_dir = state_dir
            self._sub = bus.subscribe(">", self.on_event, fail_mode="ignore", source="journal")
            stop_event = threading.Event()
            self._stop = stop_event
        if catch_up:
            self.catch_up()
        if ticker:
            seconds = interval or float(
                os.environ.get(CATCH_UP_SECONDS_ENV) or DEFAULT_CATCH_UP_SECONDS
            )
            self._ticker = threading.Thread(
                target=self._tick,
                args=(seconds, stop_event),
                name="axiom-journal-catch-up",
                daemon=True,
            )
            self._ticker.start()
        return self

    def stop(self) -> None:
        with self._lock:
            self._stop.set()
            if self._sub is not None and self.bus is not None:
                try:
                    self.bus.unsubscribe(self._sub)
                except Exception:  # noqa: BLE001
                    pass
            self._sub = None
            self._ticker = None

    def _tick(self, seconds: float, stop_event: threading.Event) -> None:
        while not stop_event.wait(seconds):
            if self._sub is None or stop_event.is_set():
                return
            self.catch_up()

    # -- the two paths -----------------------------------------------------------------

    def failures(self) -> int:
        return self._failures

    def forget_failures(self) -> None:
        """Test isolation: the counter and the last error start over."""
        self._failures = 0
        self.last_error = None

    def on_event(self, subject: str, payload: dict[str, Any]) -> None:
        """The live path. Never raises."""
        try:
            row = rows.from_event(subject, payload)
            if row is not None:
                writer.append(row)
        except Exception as exc:  # noqa: BLE001 - isolation is the contract
            self._failures += 1
            self.last_error = f"{subject}: {type(exc).__name__}: {exc}"
            log.debug("journal: live event %s not written", subject, exc_info=True)

    def catch_up(self, only: list[str] | None = None) -> dict[str, dict[str, Any]]:
        """Read every durable source from its position and append what is new, once each."""
        report: dict[str, dict[str, Any]] = {}
        state_dir = self.state_dir
        if state_dir is None:
            from axiom.infra.paths import get_user_state_dir

            state_dir = get_user_state_dir()
        for source in self.sources:
            if only and source.name not in only:
                continue
            entry: dict[str, Any] = {"appended": 0, "seen": 0, "error": None}
            try:
                position = load_position(source.name)
                found, position = source.read(position, state_dir=state_dir)
                entry["seen"] = len(found)
                for row in found:
                    if writer.append(row) is not None:
                        entry["appended"] += 1
                save_position(source.name, position)
                entry["position"] = position
            except Exception as exc:  # noqa: BLE001 - one source never stops the rest
                self._failures += 1
                entry["error"] = self.last_error = f"{source.name}: {type(exc).__name__}: {exc}"
                log.debug("journal: catch-up of %s failed", source.name, exc_info=True)
            report[source.name] = entry
        return report

    def lag(self) -> dict[str, dict[str, Any]]:
        """Per durable source: the saved position and the source's head, for ``verify``."""
        state_dir = self.state_dir
        if state_dir is None:
            from axiom.infra.paths import get_user_state_dir

            state_dir = get_user_state_dir()
        out: dict[str, dict[str, Any]] = {}
        for source in self.sources:
            try:
                out[source.name] = {
                    "position": load_position(source.name),
                    "head": source.head(state_dir=state_dir),
                }
            except Exception as exc:  # noqa: BLE001
                out[source.name] = {"error": f"{type(exc).__name__}: {exc}"}
        return out


_INGEST = Ingest()


def start(**kw: Any) -> Ingest:
    return _INGEST.start(**kw)


def stop() -> None:
    _INGEST.stop()


def current() -> Ingest:
    return _INGEST


__all__ = ["Ingest", "current", "load_position", "save_position", "start", "stop"]
