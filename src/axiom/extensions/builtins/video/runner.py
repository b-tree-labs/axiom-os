# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The trigger runner: the triggers attached to one source, in the copy that holds it.

spec-video §Trigger runner. One thread per attached trigger, each with its own
subscription on the source's bus (``trigger:<name>``), so a slow trigger drops its
own oldest frames and nobody else waits. A trigger that raises is counted in its
health and does not stop capture or another trigger; a trigger that raises
:class:`TriggerConfigError` is ``failed``, with its fix, and is no longer fed.

Every event a trigger emits is checked against the contract before it is
appended to the ledger: it names this stream and this trigger, a ``started`` opens
a key that is not open, an ``ended`` closes one that is. An episode is one
incident and is never left open. Its key is kept on disk while it is open
(``<state>/video/episodes/<stream>.json``) and its ``ended`` is written by the
runner when the trigger cannot write it: when capture stops, when ``connector
reload`` replaces or detaches the trigger, when the next holder of the source
finds what a crashed one left open, and when the source is revoked.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

from . import ledger
from . import state as vs
from .bus import DEFAULT_MAXSIZE, FrameBus
from .frame import utc_now_iso
from .triggers import (
    StreamTrigger,
    TriggerCatalog,
    TriggerConfigError,
    TriggerEvent,
    split_attachment,
)
from .triggers import catalog as default_catalog

log = logging.getLogger(__name__)

#: Why the capture service, not the trigger, closed an episode (``detail.reason``).
REASON_STOPPED = "capture stopped"
REASON_DETACHED = "detached by connector reload"
REASON_RECONFIGURED = "reconfigured by connector reload"
REASON_LOST = "capture ended before the episode closed"
REASON_REVOKED = "the source was revoked"
#: ``detail.closed_by`` of an episode the capture service closed.
CLOSED_BY = "capture"
#: The prefix of a trigger's subscription name on the bus.
SUBSCRIPTION_PREFIX = "trigger:"


def check_attachments(
    tables: Iterable[Mapping[str, Any]], catalog: TriggerCatalog | None = None
) -> list[str]:
    """Every reason these attachments cannot run, each with its fix; ``[]`` if they all build.

    Builds each trigger (that is what checks its config) and starts none.
    """
    found = catalog if catalog is not None else default_catalog()
    errors: list[str] = []
    names, problems = _names(tables)
    errors += problems
    for name, config in names.items():
        if config is None:
            continue
        try:
            found.build({"name": name, **config})
        except TriggerConfigError as exc:
            errors.append(_reason(exc))
    return errors


def close_open_episodes(stream: str, state_dir: Path, *, reason: str) -> list[TriggerEvent]:
    """Write ``ended`` for every episode left open on disk for ``stream``, and forget them."""
    closed = []
    for row in vs.read_open_episodes(stream, Path(state_dir)).values():
        try:
            started = TriggerEvent.from_dict(row)
        except ValueError:
            continue
        ended = _closing(started, reason)
        ledger.append(ended, Path(state_dir))
        closed.append(ended)
    vs.write_open_episodes(stream, {}, Path(state_dir))
    return closed


def _reason(exc: TriggerConfigError) -> str:
    return f"{exc}. Fix: {exc.fix}" if exc.fix else str(exc)


def _names(
    tables: Iterable[Mapping[str, Any]],
) -> tuple[dict[str, dict[str, Any] | None], list[str]]:
    """``{name: config}`` in order. ``None`` means "keep whatever runs under this name": the
    name is attached twice, or its table does not read. Either is a problem."""
    names: dict[str, dict[str, Any] | None] = {}
    problems: list[str] = []
    for table in tables:
        try:
            name, config = split_attachment(table)
        except TriggerConfigError as exc:
            problems.append(_reason(exc))
            named = table.get("name") if isinstance(table, Mapping) else None
            if isinstance(named, str) and named:
                names[named] = None  # a broken change does not detach what runs
            continue
        if name in names:
            if names[name] is not None:
                problems.append(
                    f"trigger {name!r} is attached twice; a source has one attachment per trigger. "
                    f"Fix: keep one [[connector.params.triggers]] table for {name!r}"
                )
            names[name] = None
            continue
        names[name] = config
    return names, problems


def _closing(started: TriggerEvent, reason: str) -> TriggerEvent:
    """The ``ended`` the capture service writes for an episode its trigger cannot close."""
    return TriggerEvent(
        stream=started.stream,
        trigger=started.trigger,
        ts=utc_now_iso(),
        kind="ended",
        incident_key=started.incident_key,
        severity=started.severity,
        detail={"reason": reason, "closed_by": CLOSED_BY, "started": started.ts},
    )


class TriggerRunner:
    """One attached trigger on one stream's bus, on a thread of its own."""

    name: str
    config: dict[str, Any]
    trigger: StreamTrigger

    def __init__(
        self,
        stream: str,
        name: str,
        config: Mapping[str, Any],
        trigger: StreamTrigger,
        *,
        bus: FrameBus,
        record: Callable[[TriggerEvent], None],
        maxsize: int = DEFAULT_MAXSIZE,
        poll_s: float = 0.2,
    ) -> None:
        self.stream = stream
        self.name = name
        self.config = dict(config)
        self.trigger = trigger
        self.label = f"{getattr(trigger, 'name', name)}@{getattr(trigger, 'version', '?')}"
        self._bus = bus
        self._record = record
        self._maxsize = maxsize
        self._poll_s = poll_s
        self._lock = threading.Lock()
        self._state = "stopped"
        self._frames = self._events = self._errors = 0
        self._last_error: str | None = None
        self._fix: str | None = None
        self._open: dict[str, TriggerEvent] = {}
        self._sub: Any = None
        self._thread: threading.Thread | None = None
        self._stopping = threading.Event()

    @property
    def state(self) -> str:
        """``running``, ``failed`` or ``stopped``."""
        return self._state

    @property
    def open_episodes(self) -> dict[str, TriggerEvent]:
        """The ``started`` event of every episode this trigger has open, by incident key."""
        with self._lock:
            return dict(self._open)

    def start(self) -> None:
        try:
            self.trigger.start()
        except TriggerConfigError as exc:
            self._fail(exc.args[0] if exc.args else str(exc), exc.fix)
            return
        except Exception as exc:  # noqa: BLE001 - a trigger that cannot start is that trigger's failure
            self._fail(f"it did not start: {type(exc).__name__}: {exc}", None)
            return
        self._sub = self._bus.subscribe(SUBSCRIPTION_PREFIX + self.name, maxsize=self._maxsize)
        self._state = "running"
        self._thread = threading.Thread(
            target=self._run, name=f"video-trigger-{self.stream}-{self.name}", daemon=True
        )
        self._thread.start()

    def _run(self) -> None:
        sub = self._sub
        while not self._stopping.is_set():
            frame = sub.get(timeout=self._poll_s)
            if frame is None:
                if sub.closed:
                    return
                continue
            try:
                events = list(self.trigger.observe(frame) or ())
            except TriggerConfigError as exc:
                with self._lock:
                    self._frames += 1
                self._fail(exc.args[0] if exc.args else str(exc), exc.fix)
                self._bus.unsubscribe(sub.name)
                return
            except Exception as exc:  # noqa: BLE001 - one bad frame never stops capture or anyone else
                with self._lock:
                    self._frames += 1
                    self._errors += 1
                    self._last_error = f"{type(exc).__name__}: {exc}"
                log.warning("video %s: trigger %s raised: %s", self.stream, self.name, exc)
                continue
            for event in events:
                self._accept(event)
            with self._lock:
                self._frames += 1

    def _refuse(self, why: str) -> None:
        with self._lock:
            self._errors += 1
            self._last_error = why
        log.warning("video %s: trigger %s: %s", self.stream, self.name, why)

    def _accept(self, event: Any) -> None:
        if not isinstance(event, TriggerEvent):
            self._refuse(f"observe returned {type(event).__name__}, not a TriggerEvent; dropped")
            return
        if event.stream != self.stream:
            self._refuse(f"an event for stream {event.stream!r}, not {self.stream!r}; dropped")
            return
        if event.trigger != self.label:
            self._refuse(f"an event names trigger {event.trigger!r}, not {self.label!r}; dropped")
            return
        key = event.incident_key
        with self._lock:
            is_open = key in self._open
        if event.kind == "started" and is_open:
            self._refuse(f"started {key}, which is already open; dropped (an episode starts once)")
            return
        if event.kind == "ended" and not is_open:
            self._refuse(f"ended {key}, which never started; dropped")
            return
        try:
            self._record(event)
        except Exception as exc:  # noqa: BLE001 - a full disk is counted; capture goes on
            self._refuse(f"could not write the event to the ledger: {type(exc).__name__}: {exc}")
        with self._lock:
            if event.kind == "started":
                self._open[key] = event
            elif event.kind == "ended":
                self._open.pop(key, None)
            self._events += 1

    def _fail(self, message: str, fix: str | None) -> None:
        with self._lock:
            self._state = "failed"
            self._errors += 1
            self._last_error, self._fix = message, fix or None
        log.warning("video %s: trigger %s failed: %s", self.stream, self.name, message)

    def stop(self, *, reason: str | None = REASON_STOPPED) -> list[TriggerEvent]:
        """Stop feeding it and tell it to stop. With a ``reason``, write ``ended`` for every
        episode it has open; ``None`` writes nothing (the copy no longer holds the source)."""
        self._stopping.set()
        if self._sub is not None:
            self._bus.unsubscribe(self._sub.name)
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5.0)
        closed: list[TriggerEvent] = []
        if reason is not None:
            with self._lock:
                still_open, self._open = list(self._open.values()), {}
            for started in still_open:
                ended = _closing(started, reason)
                try:
                    self._record(ended)
                    closed.append(ended)
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "video %s: could not close %s: %s", self.stream, started.incident_key, exc
                    )
        try:
            self.trigger.stop()
        except Exception as exc:  # noqa: BLE001 - stopping goes on
            log.warning("video %s: trigger %s raised on stop: %s", self.stream, self.name, exc)
        if self._state != "failed":
            self._state = "stopped"
        return closed

    def health(self) -> dict[str, Any]:
        """``name``, ``trigger``, ``state``, ``frames``, ``events``, ``errors``, ``last_error``,
        ``fix``, ``dropped``, ``open``, ``config`` and the trigger's own ``health``."""
        try:
            own = dict(self.trigger.health() or {})
        except Exception as exc:  # noqa: BLE001
            own = {"error": f"{type(exc).__name__}: {exc}"}
        sub = self._sub
        with self._lock:
            return {
                "name": self.name,
                "trigger": self.label,
                "state": self._state,
                "frames": self._frames,
                "events": self._events,
                "errors": self._errors,
                "last_error": self._last_error,
                "fix": self._fix,
                "dropped": sub.dropped if sub is not None else 0,
                "open": sorted(self._open),
                "config": dict(self.config),
                "health": own,
            }


class TriggerSet:
    """The triggers attached to one source, applied from its connector file."""

    def __init__(
        self,
        stream: str,
        *,
        bus: FrameBus,
        state_dir: Path,
        catalog: TriggerCatalog | None = None,
    ) -> None:
        self.stream = stream
        self._bus = bus
        self._state_dir = Path(state_dir)
        self._catalog = catalog
        self._lock = threading.RLock()
        self._episodes = threading.Lock()
        self._runners: dict[str, TriggerRunner] = {}
        #: Attachments that never built, shown in health beside the runners.
        self._unbuilt: dict[str, dict[str, Any]] = {}

    @property
    def runners(self) -> dict[str, TriggerRunner]:
        with self._lock:
            return dict(self._runners)

    def _record(self, event: TriggerEvent) -> None:
        """Append to the ledger, and keep the episodes that are open on disk."""
        ledger.append(event, self._state_dir)
        if event.kind not in ("started", "ended"):
            return
        with self._episodes:
            episodes = vs.read_open_episodes(self.stream, self._state_dir)
            if event.kind == "started":
                episodes[event.incident_key] = event.as_dict()
            else:
                episodes.pop(event.incident_key, None)
            vs.write_open_episodes(self.stream, episodes, self._state_dir)

    def recover(self) -> list[TriggerEvent]:
        """Close what a previous holder of this source left open (:data:`REASON_LOST`)."""
        with self._episodes:
            return close_open_episodes(self.stream, self._state_dir, reason=REASON_LOST)

    def apply(self, tables: Iterable[Mapping[str, Any]], *, atomic: bool = False) -> dict[str, Any]:
        """Make the running triggers these: keep an unchanged one, replace a changed one, detach a
        removed one.

        ``atomic`` (a reload): any attachment that does not build means nothing changes, and
        what ran keeps running (ADR-220 §3). Otherwise (capture starting, with nothing
        previous to keep) what builds runs and the rest is ``failed``.

        Returns ``{"running": [names], "errors": [reasons], "applied": bool}``.
        """
        found = self._catalog if self._catalog is not None else default_catalog()
        names, errors = _names(tables)
        with self._lock:
            built: dict[str, tuple[dict[str, Any], StreamTrigger]] = {}
            unbuilt: dict[str, dict[str, Any]] = {}
            for name, config in names.items():
                if config is None:
                    continue  # attached twice, or unreadable: whatever runs now keeps running
                current = self._runners.get(name)
                if current is not None and current.config == config and current.state == "running":
                    continue
                try:
                    _, cfg, trigger = found.build({"name": name, **config})
                except TriggerConfigError as exc:
                    errors.append(_reason(exc))
                    if current is None:
                        unbuilt[name] = _unbuilt(name, config, exc)
                    continue
                built[name] = (cfg, trigger)
            if atomic and errors:
                return {"running": self._running(), "errors": errors, "applied": False}
            for name, (cfg, trigger) in built.items():
                current = self._runners.get(name)
                if current is not None:
                    current.stop(reason=REASON_RECONFIGURED)
                runner = TriggerRunner(
                    self.stream, name, cfg, trigger, bus=self._bus, record=self._record
                )
                self._runners[name] = runner
                self._unbuilt.pop(name, None)
                runner.start()
            self._unbuilt.update(unbuilt)
            for name in [n for n in self._runners if n not in names]:
                self._runners.pop(name).stop(reason=REASON_DETACHED)
            for name in [n for n in self._unbuilt if n not in names or n in self._runners]:
                self._unbuilt.pop(name)
            return {"running": self._running(), "errors": errors, "applied": True}

    def _running(self) -> list[str]:
        return sorted(n for n, r in self._runners.items() if r.state == "running")

    def stop(self, *, reason: str | None = REASON_STOPPED) -> None:
        with self._lock:
            runners, self._runners = list(self._runners.values()), {}
            self._unbuilt.clear()
        for runner in runners:
            runner.stop(reason=reason)

    def health(self) -> list[dict[str, Any]]:
        """Each runner's health, by name, failed ones included."""
        with self._lock:
            rows = [r.health() for r in self._runners.values()] + list(self._unbuilt.values())
        return sorted(rows, key=lambda r: r["name"])


def _unbuilt(name: str, config: Mapping[str, Any], exc: TriggerConfigError) -> dict[str, Any]:
    return {
        "name": name,
        "trigger": None,
        "state": "failed",
        "frames": 0,
        "events": 0,
        "errors": 1,
        "last_error": str(exc),
        "fix": exc.fix or None,
        "dropped": 0,
        "open": [],
        "config": dict(config),
        "health": {},
    }


__all__ = [
    "CLOSED_BY",
    "REASON_DETACHED",
    "REASON_LOST",
    "REASON_RECONFIGURED",
    "REASON_REVOKED",
    "REASON_STOPPED",
    "SUBSCRIPTION_PREFIX",
    "TriggerRunner",
    "TriggerSet",
    "check_attachments",
    "close_open_episodes",
]
