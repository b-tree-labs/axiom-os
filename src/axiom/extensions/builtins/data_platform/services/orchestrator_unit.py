# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Run the data orchestrator as a per-user systemd service.

The orchestrator (:mod:`.orchestrator`) is the only production host of PULSE's
tick loop: every cadence on a node (backups, follow-ups, kit automations) fires
from it. Nothing started it by default, so a cadence registered on a node
without it was a schedule row that never fired. This writes the smallest unit
that runs it, restarts it on failure, and brings it back after a reboot (with
linger enabled for the user).

It never writes a credential. The database settings (``AXIOM_DB_URL``,
``DP1_RAG_DSN``) stay in the env file the node's other services already read;
the unit references that file with ``EnvironmentFile=``.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

UNIT = "axiom-data-orchestrator.service"
MODULE = "axiom.extensions.builtins.data_platform.orchestration"


def render_unit(*, python: str, env_file: str | None) -> str:
    env_line = f"EnvironmentFile={env_file}\n" if env_file else ""
    return (
        "# Written by `data orchestrator-install`. The PULSE tick loop: scheduled cadences,\n"
        "# kit automations and follow-ups fire from this process. Settings come from the\n"
        "# operator's env file, when one is named; this unit holds no credential.\n"
        "[Unit]\n"
        "Description=Axiom data orchestrator (PULSE tick loop), per-user\n"
        "After=network-online.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"{env_line}"
        f"ExecStart={python} -m {MODULE}\n"
        "Restart=on-failure\n"
        "RestartSec=10\n"
        "NoNewPrivileges=true\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def running_orchestrators() -> list[int]:
    """PIDs of orchestrator processes already running for any user on this host."""
    try:
        out = subprocess.run(["pgrep", "-f", MODULE], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return []
    return [int(p) for p in out.stdout.split() if p.isdigit() and int(p) != os.getpid()]


def _run(argv: list[str]) -> tuple[int, str]:
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)
    return r.returncode, (r.stderr or r.stdout or "").strip()


def install(
    *,
    home: Path | None = None,
    python: str | None = None,
    env_file: str | None,
    write_only: bool,
    force: bool = False,
    run: Callable[[list[str]], tuple[int, str]] | None = None,
    running: Callable[[], list[int]] | None = None,
) -> dict[str, Any]:
    """Write the unit, then (unless ``write_only``) enable and start it."""
    run = run or _run
    home = Path(home or Path.home())
    unit_path = home / ".config" / "systemd" / "user" / UNIT
    already = (running or running_orchestrators)()
    if already and not force:
        return {
            "unit": str(unit_path), "started": False, "already_running": already,
            "refused": (f"an orchestrator is already running here (pid {', '.join(map(str, already))}). "
                        f"Two would fire every cadence's checks twice; restart that one instead so it "
                        f"loads this release (it projects kit automations when it starts)"),
            "next": [],
        }
    unit_path.parent.mkdir(parents=True, exist_ok=True)
    unit_path.write_text(render_unit(python=python or sys.executable, env_file=env_file), encoding="utf-8")
    steps = [["systemctl", "--user", "daemon-reload"], ["systemctl", "--user", "enable", "--now", UNIT]]
    nxt = [f"systemctl --user status {UNIT}", f"journalctl --user -u {UNIT} -f",
           "loginctl enable-linger $USER   # once, as an administrator, so it survives logout and reboot"]
    if write_only:
        return {"unit": str(unit_path), "started": False, "already_running": [],
                "next": ["systemctl --user daemon-reload", f"systemctl --user enable --now {UNIT}", *nxt]}
    for argv in steps:
        code, detail = run(argv)
        if code != 0:
            return {"unit": str(unit_path), "started": False, "already_running": [],
                    "error": f"`{' '.join(argv)}` failed: {detail or 'no detail'}", "next": nxt}
    return {"unit": str(unit_path), "started": True, "already_running": [], "next": nxt}


__all__ = ["MODULE", "UNIT", "install", "render_unit", "running_orchestrators"]
