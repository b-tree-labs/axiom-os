# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The ``capture`` service: one supervisor thread per started source (spec-video §Design).

Run for named sources (what ``instrument video start`` spawns)::

    python -m axiom.extensions.builtins.video.services.capture --source cam-1

or for every started source, following the started marks as they change (what a
deploy runs: a new copy started beside the old one waits on each claim and takes
over as the old copy lets go, ADR-182)::

    python -m axiom.extensions.builtins.video.services.capture --all

``SIGTERM`` or ``SIGINT`` stops every supervisor, kills its decoder and releases its
claim; the started marks are left alone, because a switch is not a stop.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import threading
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from .. import boot
from .. import state as vs
from ..bus import FrameBus
from ..capture import Decoder, SessionState, SourceSupervisor
from ..connector import ConnectorNotFound, NotAVideoConnector, load
from ..sources import InvalidSourceConfig

log = logging.getLogger(__name__)

#: States in which a source is held, or on its way to being held.
_HEALTHY = (
    SessionState.RUNNING,
    SessionState.CONNECTING,
    SessionState.RECONNECTING,
    SessionState.WAITING,
)


class CaptureService:
    """AEOS §4.4 service: ``start``, ``stop``, ``status``, ``health_check``."""

    def __init__(
        self,
        names: Sequence[str] | None = None,
        *,
        state_dir: Path | None = None,
        decoder: Decoder | None = None,
        supervisor_factory: Callable[..., SourceSupervisor] | None = None,
        watch_s: float = 1.0,
    ) -> None:
        if state_dir is None:
            from axiom.infra.paths import get_user_state_dir

            state_dir = get_user_state_dir()
        self.state_dir = Path(state_dir)
        self._names = list(dict.fromkeys(names)) if names is not None else None
        self._decoder = decoder
        self._factory = supervisor_factory or SourceSupervisor
        self.watch_s = watch_s
        self._lock = threading.RLock()
        self._sups: dict[str, SourceSupervisor] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._failed: dict[str, dict[str, Any]] = {}
        self._final: dict[str, dict[str, Any]] = {}
        self._stop = threading.Event()
        self._watcher: threading.Thread | None = None
        self._running = False

    @property
    def mode(self) -> str:
        return "all" if self._names is None else "named"

    @property
    def supervisors(self) -> dict[str, SourceSupervisor]:
        with self._lock:
            return dict(self._sups)

    def bus(self, name: str) -> FrameBus | None:
        """The bus of a source this service holds: where a trigger subscribes."""
        sup = self.supervisors.get(name)
        return sup.bus if sup is not None else None

    # ---- lifecycle ------------------------------------------------------------

    def _wanted(self) -> list[str]:
        return list(self._names) if self._names is not None else vs.started_sources(self.state_dir)

    def _launch(self, name: str) -> None:
        try:
            connector = load(name, state_dir=self.state_dir)
        except (ConnectorNotFound, NotAVideoConnector, InvalidSourceConfig) as exc:
            log.warning("video %s: cannot capture it: %s", name, exc)
            self._failed[name] = {
                "name": name,
                "state": SessionState.FAILED.value,
                "error": str(exc),
            }
            return
        state_dir = self.state_dir
        sup = self._factory(
            connector,
            state_dir=state_dir,
            decoder=self._decoder,
            should_run=lambda: vs.is_started(name, state_dir),
        )
        self._failed.pop(name, None)
        self._final.pop(name, None)
        self._sups[name] = sup
        self._threads[name] = sup.start()

    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            self._running = True
            self._stop.clear()
            for name in self._wanted():
                self._launch(name)
            if self.mode == "all":
                # `instrument video start` hands a source to a service that captures every started one,
                # instead of spawning a process of its own (boot.running_services).
                boot.advertise(self.state_dir, mode=self.mode)
        self._watcher = threading.Thread(
            target=self._watch, name="video-capture-watch", daemon=True
        )
        self._watcher.start()

    def _watch(self) -> None:
        while not self._stop.wait(self.watch_s):
            # Stopping a supervisor kills its decoder, which can take seconds: never under the lock.
            for name, sup in self.supervisors.items():
                if not vs.is_started(name, self.state_dir):
                    sup.request_stop()
            with self._lock:
                for name, sup in list(self._sups.items()):
                    if not self._threads[name].is_alive():
                        self._final[name] = sup.snapshot()
                        del self._sups[name], self._threads[name]
                if self.mode == "all":
                    for name in vs.started_sources(self.state_dir):
                        if name not in self._sups and name not in self._failed:
                            self._launch(name)

    def stop(self, timeout: float | None = 10.0) -> None:
        self._stop.set()
        with self._lock:
            sups, threads = dict(self._sups), dict(self._threads)
            was_running, self._running = self._running, False
        if was_running and self.mode == "all":
            boot.withdraw(self.state_dir)
        for sup in sups.values():
            sup.request_stop()
        for name, thread in threads.items():
            thread.join(timeout)
            with self._lock:
                self._final[name] = sups[name].snapshot()
        if self._watcher is not None and self._watcher is not threading.current_thread():
            self._watcher.join(timeout)

    def status(self) -> dict[str, Any]:
        """``{pid, mode, sources: {name: snapshot}}``."""
        with self._lock:
            sources: dict[str, Any] = {**self._final, **self._failed}
            sources.update({name: sup.snapshot() for name, sup in self._sups.items()})
        return {"pid": os.getpid(), "mode": self.mode, "running": self._running, "sources": sources}

    def health_check(self) -> dict[str, Any]:
        """``{ok, sources: {name: state}}``: ok while every source is held or being held."""
        with self._lock:
            states = {name: sup.state for name, sup in self._sups.items()}
            failed = bool(self._failed)
            running = self._running
        ok = running and not failed and all(s in _HEALTHY for s in states.values())
        return {"ok": ok, "sources": {name: s.value for name, s in states.items()}}

    def run_forever(self, *, install_signals: bool = True) -> int:
        """Run until a signal, or (named mode) until every named source has stopped."""
        if install_signals and threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGTERM, signal.SIGINT):
                signal.signal(sig, lambda _s, _f: self._stop.set())
        self.start()
        try:
            while not self._stop.wait(self.watch_s):
                if self.mode == "named":
                    with self._lock:
                        alive = any(t.is_alive() for t in self._threads.values())
                    if not alive:
                        break
        finally:
            self.stop()
        return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m axiom.extensions.builtins.video.services.capture",
        description="Hold video sources: one supervisor per started source (ADR-240).",
    )
    which = parser.add_mutually_exclusive_group(required=True)
    which.add_argument(
        "--source", action="append", dest="sources", help="capture this source (repeatable)"
    )
    which.add_argument(
        "--all", action="store_true", help="capture every started source, following the marks"
    )
    parser.add_argument(
        "--state-dir", default=None, help="the state directory (default: this install's)"
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    from axiom.infra import process_registry

    if not os.environ.get(
        process_registry.MARK_ENV
    ):  # nobody registered us: a deploy or a person started us
        process_registry.try_register(
            os.getpid(), kind="service", purpose="video capture", extension="video", detached=True
        )
    service = CaptureService(
        None if args.all else args.sources,
        state_dir=Path(args.state_dir) if args.state_dir else None,
    )
    log.info(
        "video capture service %s: %s",
        os.getpid(),
        "every started source" if args.all else ", ".join(args.sources),
    )
    return service.run_forever()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
