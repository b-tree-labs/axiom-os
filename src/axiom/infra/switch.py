# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Replace a served app with no refused request (ADR-182 D3).

The supervisor owns the listening socket for the whole life of the service.
Each copy of the app is a child process that accepts on that same socket. A
switch:

1. records a ``deploy`` change before anything starts (ADR-182 D5a);
2. starts the new copy beside the old one, on the same socket;
3. waits for the new copy to say it is ready — after its startup has run,
   not merely after its process exists;
4. only then tells the old copy to drain: it reports not-ready, stops
   accepting, and finishes the requests it holds for up to ``drain_s``.

If the new copy never becomes ready it is stopped and the old copy never
noticed. Nothing a client sent is lost at any step, because connections the
kernel has queued but nobody has accepted yet belong to the socket — which the
supervisor holds — rather than to either copy. This is the same mechanism as a
systemd socket unit or an nginx binary upgrade.

The child side is :func:`axiom.extensions.builtins.http.server.run_server`,
which reads ``AXI_LISTEN_FD``, ``AXI_READY_FD`` and ``AXI_DRAIN_S``.
"""

from __future__ import annotations

import os
import select
import signal
import socket
import subprocess
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from axiom.infra.change_intent import record_change

__all__ = ["Supervisor", "SwitchResult"]


@dataclass(frozen=True)
class SwitchResult:
    outcome: str  # switched | rejected_before_switch
    detail: str = ""


class Supervisor:
    """Own one listening socket; run, and switch, copies of an app on it."""

    def __init__(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        host: str = "127.0.0.1",
        port: int = 0,
        drain_s: float = 30.0,
        ready_timeout_s: float = 60.0,
        subject_prefix: str = "app",
        backlog: int = 1024,
        listen: bool = True,
    ) -> None:
        self.argv = list(argv)
        self.env = dict(env if env is not None else os.environ)
        self.host = host
        self.drain_s = drain_s
        self.ready_timeout_s = ready_timeout_s
        self.subject_prefix = subject_prefix
        self._backlog = backlog
        #: A worker (``listen=False``) has no socket; it is switched by overlap
        #: and told it is the only copy (SIGUSR1) once its predecessor exits.
        self.listen = listen
        self._requested_port = port
        self._sock: socket.socket | None = None
        self._current: subprocess.Popen | None = None
        self._running = False
        self.port = 0

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        if self.listen:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((self.host, self._requested_port))
            sock.listen(self._backlog)
            sock.set_inheritable(True)
            self._sock = sock
            self.port = sock.getsockname()[1]
        self._running = True
        child, why = self._spawn(self.env, self.ready_timeout_s)
        if child is None:
            self.stop()
            raise RuntimeError(f"first copy never became ready: {why}")
        self._current = child
        self._tell_sole(child)

    def stop(self) -> None:
        if self._current is not None:
            self._drain(self._current)
            self._current = None
        if self._sock is not None:
            self._sock.close()
            self._sock = None
        self._running = False

    # -- the switch ------------------------------------------------------------

    def switch(
        self,
        *,
        env: Mapping[str, str] | None = None,
        subject: str = "",
        ready_timeout_s: float | None = None,
    ) -> SwitchResult:
        if not self._running or self._current is None:
            raise RuntimeError("supervisor is not running")
        label = f"{self.subject_prefix} {subject}".strip()
        with record_change("deploy", label) as change:
            new, why = self._spawn(
                dict(env) if env is not None else self.env,
                ready_timeout_s if ready_timeout_s is not None else self.ready_timeout_s,
            )
            if new is None:
                change.outcome = "rejected_before_switch"
                change.detail = why
                return SwitchResult("rejected_before_switch", why)
            old, self._current = self._current, new
            if env is not None:
                self.env = dict(env)
            detail = self._drain(old)
            self._tell_sole(new)
            change.outcome = "switched"
            change.detail = detail
            return SwitchResult("switched", detail)

    def run_forever(self, *, poll_s: float = 0.25) -> None:
        """Serve until SIGTERM/SIGINT; switch on SIGHUP.

        A copy that dies on its own is replaced at once. That is a crash, not a
        change Axiom chose to make, so it is not recorded as one: the outage it
        caused is attributed by the evidence, not by this supervisor's say-so.
        """
        flags = {"switch": False, "stop": False}
        signal.signal(signal.SIGHUP, lambda *_: flags.__setitem__("switch", True))
        signal.signal(signal.SIGTERM, lambda *_: flags.__setitem__("stop", True))
        signal.signal(signal.SIGINT, lambda *_: flags.__setitem__("stop", True))
        try:
            while not flags["stop"]:
                if flags["switch"]:
                    flags["switch"] = False
                    self.switch(subject="reload")
                elif self._current is not None and self._current.poll() is not None:
                    child, _ = self._spawn(self.env, self.ready_timeout_s)
                    if child is not None:
                        self._current = child
                        self._tell_sole(child)
                time.sleep(poll_s)
        finally:
            self.stop()

    # -- internals -------------------------------------------------------------

    def _spawn(
        self, env: Mapping[str, str], timeout_s: float
    ) -> tuple[subprocess.Popen | None, str]:
        read_fd, write_fd = os.pipe()
        child_env = {
            **env,
            "AXI_SUPERVISED": "1",
            "AXI_READY_FD": str(write_fd),
            "AXI_DRAIN_S": str(self.drain_s),
        }
        fds: tuple[int, ...] = (write_fd,)
        if self._sock is not None:
            child_env["AXI_LISTEN_FD"] = str(self._sock.fileno())
            fds = (self._sock.fileno(), write_fd)
        try:
            child = subprocess.Popen(self.argv, env=child_env, pass_fds=fds)
        finally:
            os.close(write_fd)
        try:
            deadline = time.monotonic() + timeout_s
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    why = f"not ready within {timeout_s:g}s"
                    break
                readable, _, _ = select.select([read_fd], [], [], min(remaining, 0.25))
                if readable:
                    if os.read(read_fd, 1) == b"R":
                        return child, ""
                    why = f"exited before ready (code {child.wait(timeout=5)})"
                    break
                if child.poll() is not None:
                    why = f"exited before ready (code {child.returncode})"
                    break
        finally:
            os.close(read_fd)
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)
        return None, why

    def _tell_sole(self, child: subprocess.Popen) -> None:
        """A worker learns it is the only copy, so pending work is orphaned.

        Not sent to a served app: SIGUSR1's default action ends a process that
        has not asked for it, and a served app has nothing to recover.
        """
        if self.listen or child.poll() is not None:
            return
        child.send_signal(signal.SIGUSR1)

    def _drain(self, child: subprocess.Popen) -> str:
        if child.poll() is not None:
            return f"old copy had already exited (code {child.returncode})"
        child.send_signal(signal.SIGTERM)
        try:
            child.wait(timeout=self.drain_s + 5)
            return ""
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=10)
            return f"old copy still busy after {self.drain_s:g}s drain; stopped"


def main(argv: Sequence[str] | None = None) -> int:
    """``python -m axiom.infra.switch [--listen HOST:PORT | --worker] -- CMD...``

    Runs CMD under a supervisor: SIGHUP switches it with no gap, SIGTERM
    drains it and exits. For systemd: ``ExecReload=/bin/kill -HUP $MAINPID``.
    """
    import argparse

    p = argparse.ArgumentParser(prog="python -m axiom.infra.switch")
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--listen", metavar="HOST:PORT", help="own this socket; children accept on it")
    mode.add_argument("--worker", action="store_true", help="no socket; switch by overlap")
    p.add_argument("--drain-s", type=float, default=30.0)
    p.add_argument("--ready-timeout-s", type=float, default=60.0)
    p.add_argument("--subject", default="app", help="names the service in change records")
    p.add_argument("cmd", nargs=argparse.REMAINDER)
    args = p.parse_args(argv)
    cmd = args.cmd[1:] if args.cmd[:1] == ["--"] else args.cmd
    if not cmd:
        p.error("a command to run is required after --")
    host, port = "127.0.0.1", 0
    if args.listen:
        host, _, port_s = args.listen.rpartition(":")
        port = int(port_s)
    sup = Supervisor(
        cmd,
        host=host or "127.0.0.1",
        port=port,
        drain_s=args.drain_s,
        ready_timeout_s=args.ready_timeout_s,
        subject_prefix=args.subject,
        listen=not args.worker,
    )
    sup.start()
    sup.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
