# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Holding one source: the claim, the session, the reconnect loop (spec-video §Design).

A :class:`SourceSupervisor` takes the source's claim, so two copies never both
hold it; a copy that finds the claim held by a live owner waits (``waiting``) and
takes over when it is released, which is how a switch overlaps (ADR-182). Holding
the claim, it re-verifies the source to learn its frame size, opens a decode
session and publishes each frame on the source's bus whether or not anything is
subscribed (VID-009).

Holding the claim, it also runs the source's triggers (``runner.TriggerSet``) from
its connector file, applies a changed attachment when ``connector reload`` asks
(``<state>/video/reload/<name>``, read about once a second), and closes their open
episodes before it lets the claim go. A copy that LOSES its claim writes nothing
more: the copy that took the source over closes what was left open.

A session ends at EOF or when no frame arrives for :func:`stall_timeout_s` (the
first frame of a session is allowed :data:`FIRST_FRAME_GRACE_S` more, for the
camera's handshake and its first keyframe). A lost EPHEMERAL session reconnects
after an exponential back-off from :data:`BACKOFF_BASE_S` to :data:`BACKOFF_CAP_S`
plus jitter, with a new ``session_id`` and ``seq`` from zero; for a REPLAYABLE kind
EOF is the end, and the state is ``finished``.

The decoder is a seam: :class:`FfmpegDecoder` runs the ``ffmpeg`` subprocess; tests
put a scripted one in its place.
"""

from __future__ import annotations

import contextlib
import enum
import logging
import os
import queue
import random
import subprocess
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from . import state as vs
from .bus import FrameBus
from .connector import (
    ConnectorNotFound,
    NotAVideoConnector,
    VideoConnector,
    build_source,
    load,
)
from .ffmpeg import FFmpegTool, SecretInput, classify_failure, redact_command, scrub, secrets_in
from .frame import frame_from_bytes, utc_now_iso
from .runner import REASON_STOPPED, TriggerSet
from .sources import InvalidSourceConfig, VideoSourceError
from .state import CLAIM_STALE_S
from .triggers import TriggerCatalog

log = logging.getLogger(__name__)

BACKOFF_BASE_S = 1.0
BACKOFF_CAP_S = 60.0
BACKOFF_JITTER = 0.25
#: Extra time the first frame of a session gets: the RTSP handshake, probing, and
#: the wait for a keyframe all come before it.
FIRST_FRAME_GRACE_S = 10.0
_EOF = object()


class SessionState(str, enum.Enum):
    STOPPED = "stopped"
    WAITING = "waiting"
    CONNECTING = "connecting"
    RUNNING = "running"
    RECONNECTING = "reconnecting"
    FINISHED = "finished"
    FAILED = "failed"


def stall_timeout_s(fps_cap: int) -> float:
    """Three frame intervals at the cap, but never under five seconds."""
    return max(5.0, 3.0 / max(1, fps_cap))


class Backoff:
    """Exponential back-off with jitter: ``min(cap, base·2ⁿ) · (1 + jitter·rng())``."""

    def __init__(
        self,
        *,
        base: float = BACKOFF_BASE_S,
        cap: float = BACKOFF_CAP_S,
        jitter: float = BACKOFF_JITTER,
        rng: Callable[[], float] = random.random,
    ) -> None:
        self.base, self.cap, self.jitter, self._rng = base, cap, jitter, rng
        self._attempt = 0

    def next_delay(self) -> float:
        delay = min(self.cap, self.base * (2**self._attempt))
        if delay < self.cap:
            self._attempt += 1
        return delay * (1.0 + self.jitter * min(1.0, max(0.0, self._rng())))

    def reset(self) -> None:
        self._attempt = 0


class StallTimeout(Exception):
    """No frame arrived in time; the session is over."""


class DecodeSession(Protocol):
    """One decoding run of one source."""

    pid: int | None
    partial_frames: int

    def read(self, timeout: float) -> bytes | None:
        """One whole frame, ``None`` at EOF; raises :class:`StallTimeout` after ``timeout``."""
        ...

    def close(self) -> None: ...

    def error(self) -> str:
        """What the decoder said, scrubbed."""
        ...


class Decoder(Protocol):
    def open(self, source: Any, *, width: int, height: int, fps_cap: int) -> DecodeSession: ...


# ---- the ffmpeg decoder ---------------------------------------------------------


class _FfmpegSession:
    """One ``ffmpeg`` process: a reader thread cuts stdout into frames, another keeps stderr."""

    def __init__(
        self,
        proc: subprocess.Popen,
        *,
        frame_size: int,
        secrets: list[str],
        on_close: Callable[[], None],
    ) -> None:
        self.pid: int | None = proc.pid
        self.partial_frames = 0
        self._proc = proc
        self._size = frame_size
        self._secrets = secrets
        self._on_close = on_close
        self._frames: queue.Queue[Any] = queue.Queue(maxsize=2)
        self._stderr: deque[str] = deque(maxlen=40)
        self._closed = threading.Event()
        self._eof = False
        self._lock = threading.Lock()
        self._reader = threading.Thread(
            target=self._read_frames, name="video-decode-out", daemon=True
        )
        self._errors = threading.Thread(
            target=self._read_stderr, name="video-decode-err", daemon=True
        )
        self._reader.start()
        self._errors.start()

    def _put(self, item: Any) -> None:
        while not self._closed.is_set():
            try:
                self._frames.put(item, timeout=0.2)
                return
            except queue.Full:
                continue

    def _read_frames(self) -> None:
        out = self._proc.stdout
        try:
            while out is not None and not self._closed.is_set():
                buf = bytearray()
                while len(buf) < self._size:
                    chunk = out.read(self._size - len(buf))
                    if not chunk:
                        break
                    buf += chunk
                if len(buf) == self._size:
                    self._put(bytes(buf))
                    continue
                if buf:
                    self.partial_frames += 1
                break
        except (OSError, ValueError):
            pass
        finally:
            self._put(_EOF)

    def _read_stderr(self) -> None:
        err = self._proc.stderr
        try:
            for line in iter(err.readline, b"") if err is not None else ():
                self._stderr.append(line.decode("utf-8", "replace").rstrip())
        except (OSError, ValueError):
            pass

    def read(self, timeout: float) -> bytes | None:
        if self._eof or self._closed.is_set():
            return None
        try:
            item = self._frames.get(timeout=timeout)
        except queue.Empty:
            raise StallTimeout(f"no frame for {timeout:.0f} s") from None
        if item is _EOF:
            self._eof = True
            return None
        return item

    def close(self) -> None:
        with self._lock:
            if self._closed.is_set():
                return
            self._closed.set()
        # Wake a read already waiting for the next frame. The reader thread queues no
        # end-of-stream once the session is closed, so without this a stop waited out the
        # stall timeout and reported a stall. A full queue wakes it anyway.
        with contextlib.suppress(queue.Full):
            self._frames.put_nowait(_EOF)
        proc = self._proc
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    proc.wait(timeout=2.0)
        for stream in (proc.stdout, proc.stderr):
            with contextlib.suppress(OSError, ValueError):
                if stream is not None:
                    stream.close()
        self._reader.join(timeout=2.0)
        self._errors.join(timeout=2.0)
        self._on_close()

    def error(self) -> str:
        if self._proc.poll() is not None:
            self._errors.join(timeout=2.0)
        return scrub("\n".join(self._stderr), self._secrets)


class FfmpegDecoder:
    """Decode with an ``ffmpeg`` subprocess, registered under its redacted command."""

    def __init__(
        self, tool: FFmpegTool | None = None, *, state_dir: Path | None = None, name: str = ""
    ) -> None:
        self._tool = tool
        self._state_dir = Path(state_dir) if state_dir is not None else None
        self._name = name

    def open(self, source: Any, *, width: int, height: int, fps_cap: int) -> DecodeSession:
        from axiom.infra import process_registry

        tool = self._tool or FFmpegTool.locate()
        tool.require()
        cmd = tool.capture_command(source.ffmpeg_input_args(), fps_cap=fps_cap)
        secret_input = getattr(source, "ffmpeg_secret_input", lambda: None)()
        hidden = SecretInput(cmd, secret_input)
        secrets = [*secrets_in(cmd), *hidden.secrets]
        registry = (
            process_registry.ProcessRegistry(self._state_dir)
            if self._state_dir
            else process_registry.ProcessRegistry()
        )
        record_id, env = process_registry.prepare_spawn(scope=registry.scope)
        # Not a new session: ffmpeg stays in the capture service's process group, so
        # stopping the service stops its decoders, and a dead service's closed pipe ends them.
        # A login reaches ffmpeg on an inherited pipe (SecretInput), never in its argv.
        with hidden as (argv, pass_fds):
            proc = subprocess.Popen(  # noqa: S603 - argv list, no shell, no secret in it
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                pass_fds=pass_fds,
            )
        record = process_registry.try_register(
            proc.pid,
            registry=registry,
            record_id=record_id,
            kind="tool",
            purpose=f"video decode {self._name}".strip(),
            extension="video",
            command=" ".join(redact_command(hidden.described(argv), secrets)),
        )

        def deregister() -> None:
            if record is not None:
                registry.deregister(record.id)

        return _FfmpegSession(
            proc, frame_size=width * height * 3, secrets=secrets, on_close=deregister
        )


# ---- the supervisor -------------------------------------------------------------


class SourceSupervisor:
    """Hold one source and keep a session on it until told to stop."""

    name: str
    bus: FrameBus
    copy_id: str

    def __init__(
        self,
        connector: VideoConnector,
        *,
        state_dir: Path,
        bus: FrameBus | None = None,
        decoder: Decoder | None = None,
        build: Callable[[VideoConnector], Any] | None = None,
        sleep: Callable[[float], None] | None = None,
        rng: Callable[[], float] = random.random,
        clock: Callable[[], float] = time.time,
        should_run: Callable[[], bool] | None = None,
        poll_s: float = 1.0,
        status_interval_s: float = 1.0,
        catalog: TriggerCatalog | None = None,
    ) -> None:
        self.connector = connector
        self.name = connector.name
        self.state_dir = Path(state_dir)
        self.bus = bus or FrameBus(connector.name)
        self.copy_id = uuid.uuid4().hex[:12]
        self._decoder = decoder or FfmpegDecoder(state_dir=self.state_dir, name=self.name)
        self._build = build or build_source
        self._stop = threading.Event()
        self._sleep = sleep or self._stop.wait
        self._backoff = Backoff(rng=rng)
        self._clock = clock
        self._should_run = should_run
        self._wanted = True
        self._wanted_checked = 0.0
        self._poll_s = poll_s
        self._status_interval_s = status_interval_s
        self._fps_cap = int(connector.fps_cap)
        self._stall_s = stall_timeout_s(self._fps_cap)

        self._lock = threading.Lock()
        self._state = SessionState.STOPPED
        self._claim: Any = None
        self._session: DecodeSession | None = None
        self._thread: threading.Thread | None = None
        self._last_write = 0.0
        self._last_claim_check = 0.0

        self._frames = 0
        self._drops = 0
        self._reconnects = 0
        self._session_id: str | None = None
        self._last_frame_at: float | None = None
        self._arrivals: deque[float] = deque(maxlen=max(4, 2 * self._fps_cap))
        self._started_at = clock()
        self._error: str | None = None
        self._fix: str | None = None
        self._probe: dict[str, Any] | None = None
        #: The triggers attached to the source, while this copy holds it (runner.TriggerSet).
        self._catalog = catalog
        self._triggers: TriggerSet | None = None
        self._reload_lock = threading.Lock()
        self._reload_seen: str | None = None
        self._reload_ack: dict[str, Any] | None = None
        self._reload_done: threading.Event | None = None

    # ---- reading ------------------------------------------------------------

    @property
    def state(self) -> SessionState:
        return self._state

    @property
    def triggers(self) -> TriggerSet | None:
        """The attached triggers, while this copy holds the source; ``None`` otherwise."""
        return self._triggers

    @property
    def claim_token(self) -> str | None:
        claim = self._claim
        return claim.token if claim is not None else None

    def _fps(self) -> float | None:
        arrivals = list(self._arrivals)
        if len(arrivals) < 2 or arrivals[-1] <= arrivals[0]:
            return None
        return round((len(arrivals) - 1) / (arrivals[-1] - arrivals[0]), 2)

    @property
    def counters(self) -> dict[str, Any]:
        """``frames``, ``drops``, ``reconnects``, ``fps``, ``session_id``, ``last_frame_at``."""
        return {
            "frames": self._frames,
            "drops": self._drops + self.bus.dropped,
            "reconnects": self._reconnects,
            "fps": self._fps(),
            "session_id": self._session_id,
            "last_frame_at": self._last_frame_at,
        }

    def snapshot(self) -> dict[str, Any]:
        """What ``status`` reads: the counters plus state, pid, copy, probe, error and fix."""
        return {
            "name": self.name,
            "pid": os.getpid(),
            "copy_id": self.copy_id,
            "state": self._state.value,
            **self.counters,
            "started_at": self._started_at,
            "updated_at": self._clock(),
            "updated": utc_now_iso(),
            "probe": self._probe,
            "error": self._error,
            "fix": self._fix,
            "bus": self.bus.counters(),
            "triggers": self._triggers.health() if self._triggers is not None else [],
            "reload": self._reload_ack,
        }

    # ---- control ------------------------------------------------------------

    def start(self) -> threading.Thread:
        thread = threading.Thread(target=self.run, name=f"video-capture-{self.name}", daemon=True)
        self._thread = thread
        thread.start()
        return thread

    def request_stop(self) -> None:
        """Stop soon, from any thread: the current session is closed so a blocked read returns."""
        self._stop.set()
        session = self._session
        if session is not None:
            with contextlib.suppress(Exception):
                session.close()

    def stop(self, timeout: float | None = 10.0) -> None:
        self.request_stop()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)

    def _stopping(self) -> bool:
        if self._stop.is_set():
            return True
        if self._should_run is not None:
            now = time.monotonic()
            if now - self._wanted_checked >= self._poll_s:
                self._wanted_checked = now
                try:
                    self._wanted = bool(self._should_run())
                except Exception:  # noqa: BLE001 - a check that cannot answer keeps the last answer
                    pass
            return not self._wanted
        return False

    # ---- the loop -----------------------------------------------------------

    def run(self) -> SessionState:
        """Hold the source until stopped (or, for a replayable kind, until it ends). Blocks."""
        final = SessionState.STOPPED
        try:
            while not self._stopping():
                if not self._acquire():
                    break
                outcome = self._hold()
                if outcome == "finished":
                    final = SessionState.FINISHED
                    break
                if outcome != "lost":
                    break
            return final
        finally:
            self._close_session()
            self._set_state(final, write=False)
            held = self._claim is not None and self._claim.held()
            # Episodes close while the claim is still ours, so the next holder finds none open.
            self._stop_triggers(REASON_STOPPED if held else None)
            claim, self._claim = self._claim, None
            # The last word goes in BEFORE the claim is released, and only from a copy
            # that still holds it: otherwise a copy that just took over would have its
            # first snapshot overwritten by this one's "stopped".
            if claim is not None and claim.held():
                try:
                    vs.write_live(self.name, self.snapshot(), self.state_dir)
                except OSError as exc:
                    log.warning("video %s: could not write its final status: %s", self.name, exc)
            if claim is not None:
                claim.release()
            vs.clear_standby(self.name, self.copy_id, self.state_dir)
            log.info("video %s: %s", self.name, final.value)

    def _acquire(self) -> bool:
        item = vs.claim_item(self.name, self.state_dir)
        item.parent.mkdir(parents=True, exist_ok=True)
        from axiom.infra import pending_claim

        while not self._stopping():
            claim = pending_claim.claim(item, stale_after_s=vs.CLAIM_STALE_S)
            if claim is not None:
                self._claim = claim
                vs.clear_standby(self.name, self.copy_id, self.state_dir)
                self._start_triggers()
                return True
            if self._state is not SessionState.WAITING:
                log.info("video %s: held by another copy; waiting", self.name)
                self._set_state(SessionState.WAITING)
            self._write()
            self._stop.wait(self._poll_s)
        return False

    def _still_held(self) -> bool:
        now = time.monotonic()
        if now - self._last_claim_check < min(self._poll_s, 1.0):
            return True
        self._last_claim_check = now
        claim = self._claim
        if claim is None or not claim.held():
            return False
        claim.touch()
        return True

    def _hold(self) -> str:
        """Keep sessions on the held source. ``stopped`` | ``finished`` | ``lost`` (the claim)."""
        self._backoff.reset()
        while True:
            if self._stopping():
                return "stopped"
            if not self._still_held():
                return self._lose_claim()
            self._set_state(
                SessionState.RECONNECTING if self._reconnects else SessionState.CONNECTING
            )
            try:
                with self._keeping_the_claim():  # the probe is bounded by its own timeouts
                    source = self._build(self.connector)
                    report = source.verify()
                    self._probe = {
                        "codec": report.codec,
                        "width": report.width,
                        "height": report.height,
                        "fps": report.fps,
                    }
                    session = self._decoder.open(
                        source, width=report.width, height=report.height, fps_cap=self._fps_cap
                    )
            except VideoSourceError as exc:
                self._failed(str(exc), exc.fix)
                if not self._wait_to_reconnect():
                    return "stopped"
                continue
            except Exception as exc:  # noqa: BLE001 - one source's fault never ends the service
                self._failed(f"{type(exc).__name__}: {scrub(str(exc))}", "")
                if not self._wait_to_reconnect():
                    return "stopped"
                continue

            self._session = session
            end, produced = self._stream(session, width=report.width, height=report.height)
            self._drops += session.partial_frames
            if (
                end == "eof"
                and not self._stop.is_set()
                and not getattr(source, "replayable", False)
            ):
                # Status says WHY it is reconnecting, not only that it is.
                said = session.error()
                last = said.splitlines()[-1] if said else ""
                fix = classify_failure(said)[1] if said else ""
                self._failed(
                    f"the stream ended after {produced} frame(s)" + (f": {last}" if last else ""),
                    fix,
                )
            self._close_session()

            if self._stop.is_set() or end == "stopped":
                return "stopped"
            if end == "lost-claim":
                return self._lose_claim()
            if end == "eof" and getattr(source, "replayable", False):
                return "finished"
            if produced:
                self._backoff.reset()
            log.info("video %s: session lost (%s); reconnecting", self.name, end)
            if not self._wait_to_reconnect():
                return "stopped"

    def _stream(self, session: DecodeSession, *, width: int, height: int) -> tuple[str, int]:
        """Publish frames until the session ends. Returns ``(why, frames)``."""
        self._session_id = uuid.uuid4().hex[:16]
        self._arrivals.clear()
        seq = 0
        timeout = self._stall_s + FIRST_FRAME_GRACE_S
        while True:
            if self._stopping():
                return "stopped", seq
            try:
                raw = session.read(timeout)
            except StallTimeout:
                self._failed(f"no frame for {timeout:.0f} s: the stream stalled", "")
                return "stall", seq
            if raw is None:
                return "eof", seq
            frame = frame_from_bytes(
                raw,
                width=width,
                height=height,
                stream=self.name,
                session_id=self._session_id,
                seq=seq,
                pts=seq / self._fps_cap,
            )
            self.bus.publish(frame)
            seq += 1
            self._frames += 1
            self._last_frame_at = self._clock()
            self._arrivals.append(time.monotonic())
            if seq == 1:
                self._error = self._fix = None
                self._set_state(SessionState.RUNNING)
                log.info("video %s: running (session %s)", self.name, self._session_id)
            timeout = self._stall_s
            if not self._still_held():
                return "lost-claim", seq
            self._write()

    def _lose_claim(self) -> str:
        log.warning("video %s: no longer holds its claim; stopping capture", self.name)
        self._close_session()
        self._stop_triggers(None)  # not ours to close any more: the new holder does
        self._claim = None
        self._set_state(SessionState.WAITING)
        return "lost"

    def _failed(self, message: str, fix: str) -> None:
        self._error, self._fix = message, fix or None
        log.warning("video %s: %s", self.name, message)

    def _wait_to_reconnect(self) -> bool:
        self._reconnects += 1
        self._set_state(SessionState.RECONNECTING)
        with self._keeping_the_claim():  # a back-off can outlast the stale window (up to 75 s)
            self._sleep(self._backoff.next_delay())
        return not self._stopping()

    @contextlib.contextmanager
    def _keeping_the_claim(self):
        """Touch the claim every second for the length of a wait that is bounded by design.

        The loop touches its claim as it goes, so a holder that hangs goes stale and can
        be taken over. A back-off or a probe of a camera that does not answer is not a
        hang, and can last longer than the stale window; without this, a live holder
        reads as dead and a second copy takes its source.
        """
        done = threading.Event()
        every = min(1.0, vs.CLAIM_STALE_S / 4)

        def beat() -> None:
            while not done.wait(every):
                claim = self._claim
                if claim is not None:
                    claim.touch()

        beater = threading.Thread(target=beat, name=f"video-claim-{self.name}", daemon=True)
        beater.start()
        try:
            yield
        finally:
            done.set()
            beater.join(timeout=2.0)

    # ---- triggers ---------------------------------------------------------------

    def _attachments(self) -> tuple[Any, ...]:
        """The trigger tables in the source's file now, or the ones it was started with."""
        try:
            return tuple(load(self.name, state_dir=self.state_dir).triggers)
        except (ConnectorNotFound, NotAVideoConnector, InvalidSourceConfig, OSError, ValueError):
            return tuple(self.connector.triggers)

    def _source_changed(self) -> str | None:
        try:
            on_disk = load(self.name, state_dir=self.state_dir)
        except (ConnectorNotFound, NotAVideoConnector, InvalidSourceConfig, OSError, ValueError):
            return None
        now = (dict(on_disk.source), on_disk.credential_ref, on_disk.fps_cap)
        if now == (
            dict(self.connector.source),
            self.connector.credential_ref,
            self.connector.fps_cap,
        ):
            return None
        from .naming import command

        cmd = command()
        return (
            "the source's own settings changed too; they apply when capture restarts: "
            f"`{cmd} stop {self.name}`, then `{cmd} start {self.name}`"
        )

    def _apply_triggers(self, request: str | None, *, atomic: bool) -> None:
        triggers = self._triggers
        if triggers is None:
            return
        try:
            result = triggers.apply(self._attachments(), atomic=atomic)
        except Exception as exc:  # noqa: BLE001 - a broken attachment never stops capture
            result = {"running": [], "errors": [f"{type(exc).__name__}: {exc}"], "applied": False}
        self._reload_ack = {
            "request": request,
            "applied": result["applied"],
            "running": result["running"],
            "errors": result["errors"],
            "note": self._source_changed() if request else None,
            "at": utc_now_iso(),
        }
        for error in result["errors"]:
            log.warning("video %s: %s", self.name, error)

    def _start_triggers(self) -> None:
        self._triggers = TriggerSet(
            self.name, bus=self.bus, state_dir=self.state_dir, catalog=self._catalog
        )
        try:
            closed = self._triggers.recover()
            if closed:
                log.info(
                    "video %s: closed %d episode(s) a previous holder left open",
                    self.name,
                    len(closed),
                )
        except OSError as exc:
            log.warning(
                "video %s: could not close what a previous holder left open: %s", self.name, exc
            )
        with self._reload_lock:
            self._reload_seen = vs.read_reload_request(self.name, self.state_dir)
            self._apply_triggers(self._reload_seen, atomic=False)  # nothing previous to keep
        self._watch_reloads()

    def _watch_reloads(self) -> None:
        """Answer ``connector reload`` about once a second, whatever capture is waiting on.

        A thread of its own, because the capture loop can wait many seconds at a time
        (a probe, a back-off, a camera's first frame) and a reload asked meanwhile was
        answered only after. It never touches the claim: keeping a claim fresh is for
        waits that are bounded by design, and a holder whose capture hangs must still
        go stale and be taken over.
        """
        done = threading.Event()
        self._reload_done = done
        every = min(self._poll_s, 1.0)

        def watch() -> None:
            while not done.wait(every):
                try:
                    self._maybe_reload()
                except Exception as exc:  # noqa: BLE001 - a reload that fails is reported, capture goes on
                    log.warning("video %s: applying a reload failed: %s", self.name, exc)

        threading.Thread(target=watch, name=f"video-reload-{self.name}", daemon=True).start()

    def _maybe_reload(self) -> None:
        if self._triggers is None:
            return
        request = vs.read_reload_request(self.name, self.state_dir)
        if request is None or request == self._reload_seen:
            return
        with self._reload_lock:
            if request == self._reload_seen or self._triggers is None:
                return
            self._reload_seen = request
            log.info("video %s: reloading its triggers", self.name)
            self._apply_triggers(request, atomic=True)
        self._write(force=True)

    def _stop_triggers(self, reason: str | None) -> None:
        if self._reload_done is not None:
            self._reload_done.set()
            self._reload_done = None
        with self._reload_lock:
            triggers, self._triggers = self._triggers, None
        if triggers is not None:
            try:
                triggers.stop(reason=reason)
            except Exception as exc:  # noqa: BLE001 - stopping goes on
                log.warning("video %s: stopping its triggers: %s", self.name, exc)

    def _close_session(self) -> None:
        session, self._session = self._session, None
        if session is not None:
            with contextlib.suppress(Exception):
                session.close()

    # ---- telling other processes ---------------------------------------------

    def _set_state(self, state: SessionState, *, write: bool = True) -> None:
        self._state = state
        if write:
            self._write(force=True)

    def _write(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_write < self._status_interval_s:
            return
        self._last_write = now
        try:
            if self._claim is not None:
                vs.write_live(self.name, self.snapshot(), self.state_dir)
            elif self._state is SessionState.WAITING:
                vs.write_standby(self.name, self.copy_id, self.snapshot(), self.state_dir)
        except OSError as exc:  # a full disk must not stop capture
            log.warning("video %s: could not write its status: %s", self.name, exc)


__all__ = [
    "BACKOFF_BASE_S",
    "BACKOFF_CAP_S",
    "BACKOFF_JITTER",
    "Backoff",
    "CLAIM_STALE_S",
    "DecodeSession",
    "Decoder",
    "FIRST_FRAME_GRACE_S",
    "FfmpegDecoder",
    "SessionState",
    "SourceSupervisor",
    "StallTimeout",
    "stall_timeout_s",
]
