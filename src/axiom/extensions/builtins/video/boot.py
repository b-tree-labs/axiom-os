# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Capture that comes back after a reboot: ``capture --all`` as a unit the OS starts.

A started source is a durable mark (``<state>/video/started/<name>``), and a
capture service in ``--all`` mode follows the marks. Registered with the
platform's service manager (``axiom.infra.services``: a systemd user unit,
launchd, or Task Scheduler) and pinned to this state directory, it picks every
started source back up after a reboot. Nothing else here persists a process.

A running ``--all`` service advertises itself in ``<state>/video/services/<pid>.json``
(with its process start time, so a reused pid is not mistaken for it), and
``instrument video start`` hands it the source instead of spawning a process of its own.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from .naming import ENV as NAMING_ENV
from .naming import command as _command
from .sources import VideoSourceError

#: The unit's name (``neut-video-capture.service`` under systemd).
SERVICE_NAME = "video-capture"

#: Environment the unit carries over from the installing shell, beyond the state directory.
#: Named, never copied wholesale: a shell's environment holds credentials.
CARRIED_ENV = (
    "AXIOM_VIDEO_FFMPEG",
    "AXIOM_VIDEO_FFPROBE",
    "AXIOM_FOREIGN_SECRETS_BACKEND",
    "AXIOM_MODE",
)

#: Providers that do not bring a process back after a reboot.
NOT_PERSISTENT = ("subprocess",)


def _cli() -> str:
    from axiom.extensions.builtins.video.naming import cli_name

    return cli_name()


def service_definition(state_dir: Path) -> dict[str, Any]:
    """``{name, binary, args, env, interval_secs}`` for the unit."""
    from .control import SERVICE_MODULE

    env = {"AXI_STATE_DIR": str(state_dir)}
    env.update({k: os.environ[k] for k in CARRIED_ENV if os.environ.get(k)})
    env[NAMING_ENV] = _cli()  # the fixes the service writes name the command that installed it
    return {
        "name": SERVICE_NAME,
        "binary": sys.executable,
        "args": ["-m", SERVICE_MODULE, "--all"],
        "env": env,
        "interval_secs": 0,
    }


def make_manager(*, state_dir: Path, name: str = SERVICE_NAME) -> Any:
    """The platform ``ServiceManager`` for the unit."""
    from axiom.infra.services import ServiceManager

    spec = service_definition(state_dir)
    return ServiceManager(
        name=name,
        binary=spec["binary"],
        args=spec["args"],
        env=spec["env"],
        interval_secs=spec["interval_secs"],
    )


def linger_enabled() -> bool | None:
    """Will a systemd user unit outlive the login? ``None`` where that question does not apply."""
    if not sys.platform.startswith("linux"):
        return None
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
    try:
        done = subprocess.run(
            ["loginctl", "show-user", user, "--property=Linger", "--value"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if done.returncode != 0:
        return None
    return done.stdout.strip().lower() == "yes"


def _linger_fix() -> str:
    user = os.environ.get("USER") or "$USER"
    return f"run `sudo loginctl enable-linger {user}` so the unit starts at boot and outlives the login"


def install(state_dir: Path, *, name: str = SERVICE_NAME) -> dict[str, Any]:
    """Register and start the unit. ``{installed, started, provider, survives_reboot, fix}``.

    Raises ``VideoSourceError`` (with a fix) where no provider survives a reboot or
    registration fails.
    """
    manager = make_manager(state_dir=state_dir, name=name)
    provider = manager.provider_name
    if provider in NOT_PERSISTENT:
        raise VideoSourceError(
            "this machine has no service manager that brings a process back after a reboot "
            "(a systemd user session, launchd or Task Scheduler), so capture would not survive one",
            code="no-service-manager",
            fix="enable systemd (on WSL: `[boot] systemd=true` in /etc/wsl.conf, then `wsl --shutdown`), "
            "or start a capture service from the machine's own boot scripts with "
            f"`{sys.executable} -m axiom.extensions.builtins.video.services.capture --all`",
        )
    if not manager.install():
        raise VideoSourceError(
            f"{provider} refused to register the {name} unit",
            code="install-failed",
            fix=f"read the log of the {provider} user manager (for systemd: `journalctl --user -n 50`) and retry",
        )
    started = bool(manager.start())
    linger = linger_enabled() if provider == "systemd" else None
    survives = linger is not False
    return {
        "name": name,
        "installed": True,
        "started": started,
        "provider": provider,
        "survives_reboot": survives,
        "linger": linger,
        "state_dir": str(state_dir),
        "fix": "" if survives else _linger_fix(),
    }


def uninstall(state_dir: Path, *, name: str = SERVICE_NAME) -> dict[str, Any]:
    """Stop and remove the unit; the started marks stay."""
    from . import state as vs

    manager = make_manager(state_dir=state_dir, name=name)
    stopped = bool(manager.stop())
    removed = bool(manager.uninstall())
    started = vs.started_sources(state_dir)
    note = (
        f"still started, and nothing captures them after a reboot now: {', '.join(started)}; "
        f"`{_command()} start <name>` captures one until then"
        if started
        else ""
    )
    return {
        "name": name,
        "stopped": stopped,
        "uninstalled": removed,
        "provider": manager.provider_name,
        "started": started,
        "note": note,
    }


def service_status(state_dir: Path, *, name: str = SERVICE_NAME) -> dict[str, Any]:
    """``{installed, provider, state, running, note}``; ``running`` lists advertised --all services."""
    from axiom.infra.services import ServiceStatus

    running = running_services(state_dir)
    try:
        manager = make_manager(state_dir=state_dir, name=name)
        info = manager.status()
        provider, state = manager.provider_name, str(info.status)
    except Exception as exc:  # noqa: BLE001 - status is a read; a broken manager is reported, not raised
        provider, state = "unknown", f"unknown ({type(exc).__name__})"
    installed = (
        state not in (ServiceStatus.NOT_INSTALLED, ServiceStatus.UNKNOWN)
        and provider not in NOT_PERSISTENT
    )
    note = (
        "" if installed else f"capture does not come back after a reboot: `{_command()} install`"
    )
    return {
        "name": name,
        "installed": installed,
        "provider": provider,
        "state": state,
        "running": running,
        "note": note,
    }


# ---- a running --all service ---------------------------------------------------------


def _services_dir(state_dir: Path) -> Path:
    return Path(state_dir) / "video" / "services"


def advertise(state_dir: Path, *, mode: str) -> Path:
    """Say this process is a capture service capturing every started source."""
    from axiom.infra import process_registry
    from axiom.infra.state import atomic_write

    pid = os.getpid()
    info = process_registry.probe(pid)
    path = _services_dir(state_dir) / f"{pid}.json"
    atomic_write(path, {"pid": pid, "started_at": info.start if info else None, "mode": mode})
    return path


def withdraw(state_dir: Path) -> None:
    path = _services_dir(state_dir) / f"{os.getpid()}.json"
    for p in (path, path.with_suffix(".json.lock")):
        with contextlib.suppress(FileNotFoundError):
            p.unlink()


def running_services(state_dir: Path) -> list[dict[str, Any]]:
    """The advertised services whose process is still the one that advertised."""
    from axiom.infra import process_registry

    out = []
    for path in sorted(_services_dir(state_dir).glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        pid, started_at = data.get("pid"), data.get("started_at")
        live = process_registry.probe(pid) if isinstance(pid, int) else None
        if live is None or (
            started_at is not None
            and abs(live.start - float(started_at)) > process_registry.START_TOLERANCE_S
        ):
            continue  # left by a process that is gone; a read does not tidy it away
        out.append({"pid": pid, "mode": data.get("mode", "all")})
    return out


__all__ = [
    "CARRIED_ENV",
    "NOT_PERSISTENT",
    "SERVICE_NAME",
    "advertise",
    "install",
    "linger_enabled",
    "make_manager",
    "running_services",
    "service_definition",
    "service_status",
    "uninstall",
    "withdraw",
]
