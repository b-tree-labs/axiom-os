# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Record each boot of this machine and whether the run before it ended cleanly.

ADR-182 D5a attributes an outage to **power** only on positive evidence: a fresh
boot with no clean stop before it. A clean OS shutdown stops services with
SIGTERM, and a supervised service records a clean stop when it gets one; a
power loss records nothing. So at each start this module compares the
machine's boot time with the last one it saw:

* same boot: a service restart, not a boot — nothing recorded;
* a new boot: recorded, with ``clean_shutdown`` true only if the last thing
  written before it was a clean stop;
* the first boot ever seen: recorded with ``clean_shutdown = None``, because
  nothing was running before it to judge.

An unreadable boot time records nothing: no evidence is better than invented
evidence. Ledger: ``<state>/availability/boots.jsonl`` (``AXI_BOOT_LEDGER``).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

__all__ = ["boot_time", "boots", "ledger_path", "record_clean_stop", "record_start"]


def ledger_path() -> Path:
    override = os.environ.get("AXI_BOOT_LEDGER")
    if override:
        return Path(override)
    from axiom.infra.paths import get_platform_state_dir

    return get_platform_state_dir() / "availability" / "boots.jsonl"


def boot_time() -> datetime | None:
    """When this machine booted, or None if the OS will not say."""
    try:
        if sys.platform.startswith("linux"):
            for line in Path("/proc/stat").read_text().splitlines():
                if line.startswith("btime "):
                    return datetime.fromtimestamp(int(line.split()[1]), UTC)
        elif sys.platform == "darwin":
            out = subprocess.run(
                ["sysctl", "-n", "kern.boottime"], capture_output=True, text=True, timeout=5
            ).stdout
            # "{ sec = 1759912345, usec = 123456 } Wed Oct  8 ..."
            sec = int(out.split("sec =")[1].split(",")[0])
            return datetime.fromtimestamp(sec, UTC)
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return None
    return None


def _entries(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(raw))
        except ValueError:
            continue  # a torn last line from a power loss is itself unremarkable
    return out


def _append(path: Path, entry: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, sort_keys=True) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def record_start(*, path: Path | None = None, boot_time: datetime | None | str = "detect") -> None:
    """A service is starting: record a boot if the machine has booted since."""
    target = path or ledger_path()
    booted = globals()["boot_time"]() if boot_time == "detect" else boot_time
    if booted is None:
        return
    entries = _entries(target)
    seen = [e for e in entries if e.get("event") == "boot"]
    at = booted.isoformat()
    if seen and seen[-1].get("at") == at:
        _append(target, {"event": "start", "boot": at})
        return
    if not seen:
        clean = None
    else:
        last = next(
            (e for e in reversed(entries) if e.get("event") in ("start", "clean_stop")), None
        )
        clean = bool(last and last.get("event") == "clean_stop")
    _append(target, {"event": "boot", "at": at, "clean_shutdown": clean})
    _append(target, {"event": "start", "boot": at})


def record_clean_stop(*, path: Path | None = None) -> None:
    """A service was stopped on purpose (SIGTERM), not lost with the machine."""
    _append(path or ledger_path(), {"event": "clean_stop", "at": datetime.now(UTC).isoformat()})


def boots(*, path: Path | None = None) -> list[dict]:
    """Every recorded boot, oldest first: ``{"at": iso, "clean_shutdown": bool|None}``."""
    return [
        {"at": e["at"], "clean_shutdown": e.get("clean_shutdown")}
        for e in _entries(path or ledger_path())
        if e.get("event") == "boot" and e.get("at")
    ]
