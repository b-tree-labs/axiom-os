# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Install / uninstall the axiom-memory heartbeat as a recurring task.

macOS: writes a launchd plist at
``~/Library/LaunchAgents/com.axiom.memory.heartbeat.plist`` that runs
``axi memory heartbeat`` every ``interval_seconds`` (default 3600 = 1h).
launchctl is invoked to bootstrap the agent so it starts firing
immediately and persists across reboots.

Linux: out of scope for this milestone; a systemd-timer-based install
follows the same shape and lands when there's a Linux user to dogfood
with.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

LABEL = "com.axiom.memory.heartbeat"


def _default_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def _default_log_dir() -> Path:
    return Path.home() / ".axi" / "logs"


def _xml_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def render_heartbeat_plist(
    *,
    axi_binary: str,
    interval_seconds: int = 3600,
    log_dir: str | os.PathLike = "",
) -> str:
    """Render the launchd plist XML for the heartbeat agent.

    Pure function; takes no side-effects. ``log_dir`` is optional —
    when empty, stdout/stderr go to the system log via launchd default.
    """
    log_dir_str = str(log_dir) if log_dir else ""
    stdout_xml = ""
    stderr_xml = ""
    if log_dir_str:
        out_path = _xml_escape(f"{log_dir_str}/heartbeat.out.log")
        err_path = _xml_escape(f"{log_dir_str}/heartbeat.err.log")
        stdout_xml = (
            f"\n  <key>StandardOutPath</key>\n  <string>{out_path}</string>"
        )
        stderr_xml = (
            f"\n  <key>StandardErrorPath</key>\n  <string>{err_path}</string>"
        )

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>{LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{_xml_escape(axi_binary)}</string>
    <string>memory</string>
    <string>heartbeat</string>
  </array>
  <key>StartInterval</key>
  <integer>{interval_seconds}</integer>
  <key>RunAtLoad</key>
  <true/>{stdout_xml}{stderr_xml}
</dict>
</plist>
"""


def _launchctl_load(plist_path: Path) -> tuple[bool, str]:
    """Best-effort `launchctl load -w <plist>`. Returns (ok, message)."""
    if shutil.which("launchctl") is None:
        return False, "launchctl not found (non-macOS host?)"
    try:
        result = subprocess.run(
            ["launchctl", "load", "-w", str(plist_path)],
            check=False, capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            return True, "loaded"
        return False, (result.stderr or result.stdout or "load failed").strip()
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


def _launchctl_unload(plist_path: Path) -> tuple[bool, str]:
    """Best-effort `launchctl unload -w <plist>`. Returns (ok, message)."""
    if shutil.which("launchctl") is None:
        return False, "launchctl not found (non-macOS host?)"
    try:
        result = subprocess.run(
            ["launchctl", "unload", "-w", str(plist_path)],
            check=False, capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            return True, "unloaded"
        return False, (result.stderr or result.stdout or "unload failed").strip()
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


def install_heartbeat_plist(
    *,
    axi_binary: str | None = None,
    plist_path: Path | None = None,
    log_dir: Path | str | None = None,
    interval_seconds: int = 3600,
    load: bool = True,
) -> dict[str, Any]:
    """Render and write the heartbeat plist; optionally load via launchctl."""
    if axi_binary is None:
        axi_binary = shutil.which("axi") or str(
            Path(sys.executable).parent / "axi"
        )
    plist_path = plist_path or _default_plist_path()
    log_dir = Path(log_dir) if log_dir is not None else _default_log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    plist_path.parent.mkdir(parents=True, exist_ok=True)

    xml = render_heartbeat_plist(
        axi_binary=axi_binary,
        interval_seconds=interval_seconds,
        log_dir=log_dir,
    )
    plist_path.write_text(xml)

    loaded = False
    load_message = "skipped (load=False)"
    if load:
        ok, msg = _launchctl_load(plist_path)
        loaded = ok
        load_message = msg

    return {
        "action": "installed",
        "plist_path": str(plist_path),
        "log_dir": str(log_dir),
        "axi_binary": axi_binary,
        "interval_seconds": interval_seconds,
        "loaded": loaded,
        "load_message": load_message,
    }


def uninstall_heartbeat_plist(
    *,
    plist_path: Path | None = None,
    unload: bool = True,
) -> dict[str, Any]:
    """Unload + remove the heartbeat plist if present."""
    plist_path = plist_path or _default_plist_path()

    if not plist_path.exists():
        return {
            "action": "uninstalled",
            "plist_path": str(plist_path),
            "removed": False,
            "unloaded": False,
            "unload_message": "no plist on disk",
        }

    unloaded = False
    unload_message = "skipped (unload=False)"
    if unload:
        ok, msg = _launchctl_unload(plist_path)
        unloaded = ok
        unload_message = msg

    plist_path.unlink()
    return {
        "action": "uninstalled",
        "plist_path": str(plist_path),
        "removed": True,
        "unloaded": unloaded,
        "unload_message": unload_message,
    }
