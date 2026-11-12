# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A read-only local monitor: a page, a snapshot, and a live stream.

A long-running process (a producer, a collector) writes two files: a JSON
snapshot of its current state and an append-only event log. This serves a page
and streams both to it over Server-Sent Events. It is a separate process on
purpose: it reads files and never reaches into the process it watches, so a
monitor cannot stall, crash or command what it monitors.

It answers GET and nothing else, and it binds to loopback unless the caller
says ``allow_remote=True`` in so many words.
"""

from __future__ import annotations

import ipaddress
import json
import os
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


class EventLog:
    """An append-only JSONL log of what happened, bounded in size."""

    def __init__(self, path: str | Path, *, max_bytes: int = 1_000_000) -> None:
        self.path = Path(path)
        self.max_bytes = max_bytes
        self._lock = threading.Lock()

    def append(self, kind: str, message: str, *, fix: str = "", level: str = "info",
               at: float | None = None, **extra: Any) -> dict[str, Any]:
        event = {"at": time.time() if at is None else at, "kind": kind, "level": level,
                 "message": message, "fix": fix, **extra}
        line = json.dumps(event, default=str) + "\n"
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.is_file() and self.path.stat().st_size + len(line) > self.max_bytes:
                # Keep the newer half: a monitor wants recent history, and a
                # log that grows without bound fills the disk it is watching.
                data = self.path.read_bytes()
                keep = data[len(data) // 2:]
                keep = keep[keep.find(b"\n") + 1:]
                tmp = self.path.with_suffix(".tmp")
                tmp.write_bytes(keep)
                os.replace(tmp, self.path)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line)
        return event

    def tail(
        self, offset: int, *, after: float | None = None, limit: int = 500
    ) -> tuple[list[dict[str, Any]], int]:
        """Events after byte ``offset``, and the offset to resume from.

        ``after`` is the time of the last event the caller already has. Rotation
        moves byte offsets, so when it is detected the file is read from the top
        and ``after`` is what keeps already-seen events from being sent twice.
        """
        if not self.path.is_file():
            return [], 0
        size = self.path.stat().st_size
        rotated = offset > size
        if rotated:
            offset = 0
        with self.path.open("rb") as fh:
            fh.seek(offset)
            data = fh.read()
        events = []
        for raw in data.splitlines()[-limit:]:
            try:
                ev = json.loads(raw)
            except ValueError:
                continue
            if after is not None and float(ev.get("at") or 0) <= after:
                continue
            events.append(ev)
        return events, offset + len(data)


def _is_loopback(host: str) -> bool:
    if host in ("localhost", ""):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class LiveFeed:
    """Serve ``pages``, ``/snapshot`` and an SSE stream at ``/events``.

    ``periodic`` maps an event name to ``(callable, seconds)``: slower panels
    computed on their own schedule and pushed as that event.
    """

    def __init__(
        self,
        *,
        snapshot_path: str | Path,
        events_path: str | Path,
        pages: dict[str, tuple[bytes, str]],
        host: str = "127.0.0.1",
        port: int = 8790,
        poll_s: float = 1.0,
        periodic: dict[str, tuple[Callable[[], Any], float]] | None = None,
        allow_remote: bool = False,
    ) -> None:
        if not allow_remote and not _is_loopback(host):
            raise ValueError(
                f"{host} is not a loopback address; this monitor only listens on this "
                "machine unless allow_remote=True is passed deliberately"
            )
        self.snapshot_path = Path(snapshot_path)
        self.log = EventLog(events_path)
        self.pages = dict(pages)
        self.poll_s = poll_s
        self.periodic = dict(periodic or {})
        self._cache: dict[str, Any] = {}
        self._stop = threading.Event()
        feed = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # quiet: this is a monitor, not a web server
                return

            def _refuse(self):
                self.send_response(405)
                self.send_header("Allow", "GET")
                self.end_headers()

            do_POST = do_PUT = do_PATCH = do_DELETE = _refuse  # noqa: N815

            def do_GET(self):  # noqa: N802
                path = self.path.split("?", 1)[0]
                if path in feed.pages:
                    body, ctype = feed.pages[path]
                    self._send(200, body, ctype)
                elif path == "/snapshot":
                    self._send(200, feed._snapshot_bytes(), "application/json")
                elif path == "/events":
                    feed._stream(self)
                else:
                    self._send(404, b"not found", "text/plain")

            def _send(self, code, body, ctype):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

        self._server = ThreadingHTTPServer((host, port), Handler)
        self._server.daemon_threads = True
        self.port = self._server.server_address[1]
        for name, (fn, every) in self.periodic.items():
            threading.Thread(target=self._refresh, args=(name, fn, every), daemon=True).start()

    def _refresh(self, name: str, fn: Callable[[], Any], every: float) -> None:
        while not self._stop.is_set():
            try:
                self._cache[name] = fn()
            except Exception as exc:  # noqa: BLE001 — a slow panel never takes the page down
                self._cache[name] = {"error": f"{type(exc).__name__}: {exc}"}
            self._stop.wait(every)

    def _snapshot_bytes(self) -> bytes:
        try:
            return self.snapshot_path.read_bytes()
        except OSError:
            return b'{"channels": {}, "note": "the collector has not written a snapshot yet"}'

    def _stream(self, handler: BaseHTTPRequestHandler) -> None:
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.send_header("Cache-Control", "no-store")
        handler.end_headers()
        mtime = -1.0
        offset = max(0, (self.log.path.stat().st_size - 20_000) if self.log.path.is_file() else 0)
        sent_periodic: dict[str, Any] = {}
        last_beat = time.time()
        last_at: float | None = None

        def emit(event: str, data: Any) -> None:
            handler.wfile.write(f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n".encode())

        try:
            while not self._stop.is_set():
                try:
                    m = self.snapshot_path.stat().st_mtime
                except OSError:
                    m = -1.0
                if m != mtime:
                    mtime = m
                    emit("snapshot", json.loads(self._snapshot_bytes() or b"{}"))
                events, offset = self.log.tail(offset, after=last_at)
                for ev in events:
                    emit("log", ev)
                    last_at = max(last_at or 0.0, float(ev.get("at") or 0))
                for name, value in list(self._cache.items()):
                    if sent_periodic.get(name) is not value:
                        sent_periodic[name] = value
                        emit(name, value)
                if time.time() - last_beat > 15:
                    handler.wfile.write(b": keepalive\n\n")
                    last_beat = time.time()
                handler.wfile.flush()
                time.sleep(self.poll_s)
        except (BrokenPipeError, ConnectionResetError, ValueError):
            return

    def serve_forever(self) -> None:
        self._server.serve_forever(poll_interval=0.2)

    def shutdown(self) -> None:
        self._stop.set()
        self._server.shutdown()
        self._server.server_close()


__all__ = ["EventLog", "LiveFeed"]
