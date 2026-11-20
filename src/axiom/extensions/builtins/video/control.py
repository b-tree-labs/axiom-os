# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``instrument video start`` and ``instrument video stop``: mark a source, and bring its capture up or down.

``start`` marks the source started and, unless a live copy already holds its
claim, spawns a capture service for it (registered in the process registry,
detached, logging to ``<state>/video/logs/<name>.log``) and waits until that
process holds the claim. ``stop`` clears the mark: the holder notices within a
second, kills its decoder and releases the claim. Only if it has not let go
within the drain time, and it is a process ``start`` spawned for this source
alone, is it sent ``SIGTERM``.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from .naming import command as _command
from . import state as vs
from .boot import running_services
from .connector import ConnectorNotFound, NotAVideoConnector, is_active, load
from .ffmpeg import FFmpegTool, scrub
from .frame import EXTRA, numpy_available
from .naming import ENV as NAMING_ENV
from .sources import VideoSourceError

#: How long ``stop`` waits for the holder to let go (the manifest's ``drain_s``).
DRAIN_S = 10.0
#: How long ``start`` waits for the spawned service to hold the claim.
START_WAIT_S = 20.0
#: How long a holder gets to exit after ``SIGTERM``.
TERM_WAIT_S = 5.0

#: The module a capture process runs.
SERVICE_MODULE = "axiom.extensions.builtins.video.services.capture"


def _cli() -> str:
    from axiom.extensions.builtins.video.naming import cli_name

    return cli_name()


def _singleton(name: str) -> str:
    return f"video:{name}"


def service_command(names: Sequence[str]) -> list[str]:
    """The capture service for exactly these sources (no credential is on it)."""
    cmd = [sys.executable, "-m", SERVICE_MODULE]
    for name in names:
        cmd += ["--source", name]
    return cmd


def spawn_capture(name: str, *, state_dir: Path) -> subprocess.Popen:
    """Start a detached capture process for ``name``, registered as ours."""
    from axiom.infra import process_registry

    log_file = vs.log_path(name, state_dir)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    registry = process_registry.ProcessRegistry(state_dir)
    record_id, env = process_registry.prepare_spawn(scope=registry.scope)
    env["AXI_STATE_DIR"] = str(state_dir)
    env[NAMING_ENV] = _cli()  # the capture process names the command that started it
    cmd = service_command([name])
    detach: dict[str, Any]
    if os.name == "nt":  # pragma: no cover - exercised on Windows nodes only
        detach = {
            "creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        }
    else:
        detach = {"start_new_session": True}
    with open(log_file, "ab") as out:
        proc = subprocess.Popen(  # noqa: S603 - argv list; the command holds only the source's name
            cmd, stdin=subprocess.DEVNULL, stdout=out, stderr=out, env=env, close_fds=True, **detach
        )
    process_registry.try_register(
        proc.pid,
        registry=registry,
        record_id=record_id,
        kind="service",
        purpose=f"video capture {name}",
        extension="video",
        detached=True,
        singleton_key=_singleton(name),
        command=cmd,
    )
    return proc


def _log_tail(name: str, state_dir: Path, lines: int = 5) -> str:
    try:
        text = vs.log_path(name, state_dir).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return scrub(" / ".join(line.strip() for line in text.splitlines()[-lines:] if line.strip()))


def start(
    name: str,
    *,
    state_dir: Path,
    wait_s: float = START_WAIT_S,
    spawn: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """``{name, pid, state, already_running, via}``; raises ``VideoSourceError`` with a fix.

    ``via`` says who captures it: ``already-held`` (a live copy had it), ``service`` (a
    running ``--all`` service picked it up) or ``spawned`` (a process started for it).
    """
    try:
        load(name, state_dir=state_dir)
    except (ConnectorNotFound, NotAVideoConnector) as exc:
        raise VideoSourceError(
            str(exc),
            code="not-declared",
            fix=f"declare it with `{_command()} add {name} --kind rtsp --url <rtsp://host/path>`",
        ) from None
    if not is_active(name, state_dir=state_dir):
        raise VideoSourceError(
            f"{name} has not produced a frame, so it is not active",
            code="not-verified",
            fix=f"run `{_command()} verify {name}`; if it fails, `{_command()} diagnose {name}` names the fix",
        )
    FFmpegTool.locate().require()
    if not numpy_available():
        raise VideoSourceError(
            "numpy is not installed", code="numpy-missing", fix=f"pip install '{EXTRA}'"
        )

    vs.mark_started(name, state_dir)
    holder = vs.claim_holder(name, state_dir)
    if holder and holder["alive"]:
        live = vs.read_live(name, state_dir) or {}
        return {
            "name": name,
            "pid": holder["pid"],
            "state": live.get("state", "connecting"),
            "already_running": True,
            "via": "already-held",
        }

    services = running_services(state_dir)
    if services:
        return _hand_to_service(name, services, state_dir=state_dir, wait_s=wait_s)

    proc = (spawn or spawn_capture)(name, state_dir=state_dir)
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        holder = vs.claim_holder(name, state_dir)
        if holder and holder["alive"] and holder["pid"] == proc.pid:
            live = vs.read_live(name, state_dir) or {}
            return {
                "name": name,
                "pid": proc.pid,
                "state": live.get("state", "connecting"),
                "already_running": False,
                "via": "spawned",
            }
        code = proc.poll()
        if code is not None:
            raise VideoSourceError(
                f"the capture process exited ({code}) before it held {name}: {_log_tail(name, state_dir) or 'no output'}",
                code="error",
                fix=f"read {vs.log_path(name, state_dir)}; `{_command()} diagnose {name}` names what is wrong",
            )
        time.sleep(0.1)
    raise VideoSourceError(
        f"the capture process {proc.pid} did not take {name} within {wait_s:.0f} s",
        code="timeout",
        fix=f"`{_command()} status {name}` shows whether another copy holds it; the log is {vs.log_path(name, state_dir)}",
    )


def _hand_to_service(
    name: str, services: list[dict[str, Any]], *, state_dir: Path, wait_s: float
) -> dict[str, Any]:
    """The source is marked; a service capturing every started source picks it up."""
    pids = {s["pid"] for s in services}
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        holder = vs.claim_holder(name, state_dir)
        if holder and holder["alive"] and holder["pid"] in pids:
            live = vs.read_live(name, state_dir) or {}
            return {
                "name": name,
                "pid": holder["pid"],
                "state": live.get("state", "connecting"),
                "already_running": False,
                "via": "service",
            }
        time.sleep(0.1)
    raise VideoSourceError(
        f"the capture service (pid {', '.join(map(str, sorted(pids)))}) did not take {name} within {wait_s:.0f} s",
        code="timeout",
        fix=f"`{_command()} status {name}` shows what holds it; the service's own log is in its service manager "
        "(for systemd: `journalctl --user -u neut-video-capture`)",
    )


def _ours(name: str, pid: int, state_dir: Path) -> bool:
    """Is ``pid`` a capture process ``start`` spawned for ``name`` alone, and still that process?"""
    from axiom.infra import process_registry

    for rec in process_registry.ProcessRegistry(state_dir).records():
        if rec.pid == pid and rec.singleton_key == _singleton(name):
            return process_registry.verify_ours(rec)[0]
    return False


def _wait_released(name: str, pid: int, state_dir: Path, wait_s: float) -> bool:
    deadline = time.monotonic() + wait_s
    while True:
        holder = vs.claim_holder(name, state_dir)
        if holder is None or not holder["alive"] or holder["pid"] != pid:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)


def stop(name: str, *, state_dir: Path, wait_s: float = DRAIN_S) -> dict[str, Any]:
    """``{name, was_running, stopped, pid, signalled}``."""
    was_started = vs.clear_started(name, state_dir)
    holder = vs.claim_holder(name, state_dir)
    if holder is None or not holder["alive"]:
        vs.release_claim(name, state_dir)
        return {
            "name": name,
            "was_started": was_started,
            "was_running": False,
            "stopped": True,
            "pid": None,
            "signalled": False,
        }
    pid = holder["pid"]
    if _wait_released(name, pid, state_dir, wait_s):
        return {
            "name": name,
            "was_started": was_started,
            "was_running": True,
            "stopped": True,
            "pid": pid,
            "signalled": False,
        }
    signalled = False
    if isinstance(pid, int) and _ours(name, pid, state_dir):
        try:
            os.kill(pid, signal.SIGTERM)
            signalled = True
        except OSError:
            pass
    stopped = _wait_released(name, pid, state_dir, TERM_WAIT_S if signalled else 0.0)
    return {
        "name": name,
        "was_started": was_started,
        "was_running": True,
        "stopped": stopped,
        "pid": pid,
        "signalled": signalled,
    }


__all__ = [
    "DRAIN_S",
    "SERVICE_MODULE",
    "START_WAIT_S",
    "service_command",
    "spawn_capture",
    "start",
    "stop",
]
