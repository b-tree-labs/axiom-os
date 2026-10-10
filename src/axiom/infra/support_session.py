# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A support session a person at the site opens, and can end (ADR-183, level 2).

``support open --for 2h`` runs here, on the site's node, started by a person at
the site. It asks the edge relay for a session over the node's own outbound
HTTPS connection, starts a shell in a pseudo-terminal, and carries that
terminal's bytes to and from the relay by long-polling. Nothing listens on this
machine and no account is created for anyone: the shell runs as whoever opened
the session, or as the account the deployment names (``support_shell`` in the
wiring, for example a ``sudo -u <service-user>`` line), never as more.

The person who opened it sees everything typed and printed, in their own
terminal, as it happens. It ends when the time is up, when the shell exits,
when they press Ctrl-C there, or when anyone at the site runs
``support close``. Both ends keep a recording (asciicast v2): the relay's, and
this node's own copy under its state directory, which is the site's.
"""

from __future__ import annotations

import base64
import json
import os
import re
import signal
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

MAX_SESSION_S = 8 * 3600
_DURATION = re.compile(r"^\s*(\d+)\s*([smh]?)\s*$")


def parse_duration(text: str) -> int:
    """``90s``, ``30m``, ``2h`` (or bare seconds) to seconds, at most eight hours."""
    m = _DURATION.match(str(text))
    if not m:
        raise ValueError(f"not a duration: {text!r} (use 30m, 2h or 90s)")
    seconds = int(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600}[m.group(2)]
    if not 60 <= seconds <= MAX_SESSION_S:
        raise ValueError("a support session lasts between a minute and eight hours")
    return seconds


class SupportClient:
    """HTTPS calls to the relay's ``/maintenance/sessions`` routes, either side."""

    def __init__(self, base_url: str, token: str, *, timeout_s: float = 40.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout_s = timeout_s

    def _call(self, method: str, path: str, body: Any = None, *, timeout_s: float | None = None) -> Any:
        import urllib.error
        import urllib.request

        data = json.dumps(body).encode() if body is not None else (b"" if method == "POST" else None)
        req = urllib.request.Request(self.base_url + "/maintenance" + path, method=method, data=data, headers={
            "Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout_s or self.timeout_s) as resp:  # noqa: S310
                return json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                body = json.loads(exc.read() or b"{}")
                # FastAPI's {"detail": ...}, or the serving layer's {"error": {"message": ...}}.
                detail = body.get("detail") or (body.get("error") or {}).get("message", "")
            except (ValueError, AttributeError):
                pass
            raise SessionError(exc.code, detail or str(exc)) from exc

    def open(self, *, duration_s: int, reason: str, node: str = "") -> dict[str, Any]:
        return self._call("POST", "/sessions", {"duration_s": duration_s, "reason": reason, "node": node})

    def list(self, site: str) -> list[dict[str, Any]]:
        return self._call("GET", f"/sessions?site={site}")["sessions"]

    def show(self, sid: str) -> dict[str, Any]:
        return self._call("GET", f"/sessions/{sid}")

    def send(self, sid: str, data: bytes) -> None:
        self._call("POST", f"/sessions/{sid}/send", {"data": base64.b64encode(data).decode()})

    def recv(self, sid: str, after: int, *, wait_s: float = 20.0) -> dict[str, Any]:
        out = self._call("GET", f"/sessions/{sid}/recv?after={int(after)}&wait_s={wait_s}",
                         timeout_s=wait_s + 15)
        out["bytes"] = base64.b64decode(out.pop("data", "") or "")
        return out

    def close(self, sid: str) -> dict[str, Any]:
        return self._call("POST", f"/sessions/{sid}/close")

    def recording(self, sid: str) -> str:
        import urllib.request

        req = urllib.request.Request(f"{self.base_url}/maintenance/sessions/{sid}/recording",
                                     headers={"Authorization": f"Bearer {self.token}"})
        with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:  # noqa: S310
            return resp.read().decode()


class SessionError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"HTTP {status}: {detail}")
        self.status = status
        self.detail = detail


def _active_path(state_dir: Path) -> Path:
    return Path(state_dir) / "support-session.json"


def run_session(
    client: SupportClient,
    *,
    duration_s: int,
    reason: str,
    shell: list[str],
    state_dir: Path,
    node: str = "",
    mirror: Callable[[bytes], None] | None = None,
    on_open: Callable[[dict[str, Any]], None] | None = None,
    audit: Callable[..., None] | None = None,
) -> dict[str, Any]:
    """Open a session, carry the shell's bytes until it ends, and say how it ended."""
    import pty

    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    sess = client.open(duration_s=duration_s, reason=reason, node=node)
    sid = sess["id"]
    rec_path = state_dir / "sessions" / f"{sid}.cast"
    rec_path.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    rec = rec_path.open("w", encoding="utf-8")
    rec.write(json.dumps({"version": 2, "width": 120, "height": 40, "timestamp": int(t0),
                          "title": f"support session {sid}", "env": {"REASON": reason}}) + "\n")
    rec_lock = threading.Lock()

    def record(kind: str, data: bytes | str) -> None:
        text = data.decode("utf-8", errors="replace") if isinstance(data, bytes) else data
        with rec_lock:
            rec.write(json.dumps([round(time.time() - t0, 3), kind, text]) + "\n")
            rec.flush()

    _active_path(state_dir).write_text(json.dumps({"id": sid, "pid": os.getpid(),
                                                   "expires_at": sess["expires_at"]}), encoding="utf-8")
    if audit:
        audit("session_opened", id=sid, reason=reason, duration_s=duration_s, shell=shell)
    if on_open:
        on_open(sess)

    pid, fd = pty.fork()
    if pid == 0:  # the child: the shell the operator types into
        try:
            os.execvp(shell[0], shell)
        finally:
            os._exit(127)

    stop = threading.Event()
    ended: dict[str, str] = {}

    def end(why: str) -> None:
        ended.setdefault("why", why)
        stop.set()

    def pump_out() -> None:
        while not stop.is_set():
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                chunk = b""
            if not chunk:
                end("the shell exited")
                return
            record("o", chunk)
            if mirror:
                mirror(chunk)
            try:
                client.send(sid, chunk)
            except SessionError:
                end("the relay ended the session")
                return
            except OSError:
                continue  # lost a chunk to the network; the local recording has it

    def pump_in() -> None:
        after = 0
        while not stop.is_set():
            try:
                got = client.recv(sid, after, wait_s=5.0)
            except SessionError:
                end("the relay ended the session")
                return
            except OSError:
                time.sleep(1.0)
                continue
            after = got["next"]
            if got["bytes"]:
                record("i", got["bytes"])
                # Not mirrored here: the shell echoes what it is given, and
                # that echo reaches the site's terminal through pump_out.
                os.write(fd, got["bytes"])
            if not got["open"]:
                end(f"closed by {got.get('closed_by') or 'the relay'}")
                return

    previous = {}
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous[sig] = signal.signal(sig, lambda *_: end("ended at the site"))
    threads = [threading.Thread(target=pump_out, daemon=True), threading.Thread(target=pump_in, daemon=True)]
    for t in threads:
        t.start()
    deadline = time.monotonic() + duration_s
    try:
        while not stop.wait(0.2):
            if time.monotonic() >= deadline:
                end("the time ran out")
    finally:
        # The handlers stay installed until the recording and the audit line
        # are written: ``support close`` ends the session at the relay and
        # signals this process, and when the relay's end arrives first the
        # signal lands here, during cleanup. With the default handler back in
        # place it killed the process before the end was recorded.
        for sig in (signal.SIGHUP, signal.SIGKILL):
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                break
            time.sleep(0.2)
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            client.close(sid)
        except (SessionError, OSError):
            pass
        record("m", f"session ended: {ended.get('why', 'ended')}")
        rec.close()
        try:
            _active_path(state_dir).unlink()
        except FileNotFoundError:
            pass
        if audit:
            audit("session_closed", id=sid, why=ended.get("why", "ended"), recording=str(rec_path))
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    return {"id": sid, "ended": ended.get("why", "ended"), "recording": str(rec_path)}


def close_active(client: SupportClient | None, state_dir: Path) -> dict[str, Any] | None:
    """End the session open on this node, from any terminal at the site."""
    path = _active_path(Path(state_dir))
    if not path.is_file():
        return None
    active = json.loads(path.read_text(encoding="utf-8"))
    # The signal goes first, while the session process is in its loop with
    # its handler installed; it then ends the session at the relay itself.
    # Closing here too covers a process that is already gone.
    try:
        os.kill(int(active["pid"]), signal.SIGTERM)
    except (ProcessLookupError, ValueError):
        path.unlink(missing_ok=True)
    if client is not None:
        try:
            client.close(active["id"])
        except (SessionError, OSError):
            pass
    return active


def attach(client: SupportClient, sid: str, *, stdin=None, stdout=None) -> str:
    """The operator's end: a raw terminal carried to the session until it ends."""
    import select
    import termios
    import tty

    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout.buffer
    fd = stdin.fileno()
    old = termios.tcgetattr(fd) if os.isatty(fd) else None
    done = threading.Event()
    why: dict[str, str] = {}

    def pull() -> None:
        after = 0
        while not done.is_set():
            try:
                got = client.recv(sid, after, wait_s=5.0)
            except (SessionError, OSError) as exc:
                why["why"] = str(exc)
                break
            after = got["next"]
            if got["bytes"]:
                stdout.write(got["bytes"])
                stdout.flush()
            if not got["open"]:
                why["why"] = f"closed by {got.get('closed_by') or 'the relay'}"
                break
        done.set()

    t = threading.Thread(target=pull, daemon=True)
    t.start()
    try:
        if old is not None:
            tty.setraw(fd)
        while not done.is_set():
            ready, _, _ = select.select([fd], [], [], 0.2)
            if ready:
                data = os.read(fd, 1024)
                if not data or data == b"\x1d":  # Ctrl-] detaches
                    break
                try:
                    client.send(sid, data)
                except SessionError as exc:
                    why["why"] = exc.detail
                    break
    finally:
        done.set()
        if old is not None:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
    return why.get("why", "detached")


__all__ = ["MAX_SESSION_S", "SessionError", "SupportClient", "attach", "close_active", "parse_duration", "run_session"]
