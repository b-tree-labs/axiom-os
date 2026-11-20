# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Stale credential holders — a running process left behind by a rotation.

The failure this exists for: rotate a credential, update it at rest, and every
long-running process that already read it keeps the old value forever. Its
environment was a snapshot taken at ``exec``. The unit stays ``active`` and
reports healthy while every operation needing that credential fails.

Detection needs no store and no vendor API — just two probes disagreeing. If a
process holds ``KEY=X`` and the env file that supplies ``KEY`` holds ``Y``, the
process is running on a value that no longer exists on disk. Restart it.

The comparison is by variable NAME across probes and by fingerprint within one,
so nothing here ever handles or reveals a value.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from axiom.extensions.builtins.secrets.discovery.model import RawHit, fingerprint

#: ``pid 123 (comm) KEY`` — the shape ProcessEnvProbe emits.
_PROC_LOCATOR = re.compile(r"^pid (\d+) \((?P<comm>[^)]*)\) (?P<key>.+)$")
#: ``/path/to/file.env:KEY`` — the shape EnvFileProbe emits.
_FILE_LOCATOR = re.compile(r"^(?P<path>.+):(?P<key>[^:]+)$")


@dataclass(frozen=True)
class StaleHolder:
    pid: str
    process: str
    key: str
    held_fingerprint: str
    disk_fingerprint: str
    source: str
    """The on-disk location whose value the process disagrees with."""

    @property
    def remediation(self) -> str:
        return (
            f"restart the unit owning pid {self.pid} ({self.process}) — it holds a "
            f"pre-rotation {self.key} and cannot pick up the new value without one"
        )

    def to_dict(self) -> dict:
        return {
            "pid": self.pid,
            "process": self.process,
            "key": self.key,
            "held_fingerprint": self.held_fingerprint,
            "disk_fingerprint": self.disk_fingerprint,
            "source": self.source,
            "remediation": self.remediation,
        }


def correlate_stale(hits: list[RawHit]) -> list[StaleHolder]:
    """Find processes holding a credential that on-disk configuration replaced.

    Only credentials linkable to an on-disk source are compared — by target
    first, then by variable name. A process holding something no file supplies
    is not evidence of staleness: it may have been passed a value directly, and
    guessing there produces exactly the false positives that get a check
    ignored.
    """
    by_target: dict[str, tuple[str, str]] = {}
    by_key: dict[str, tuple[str, str]] = {}
    for h in hits:
        if h.probe != "env-file":
            continue
        m = _FILE_LOCATOR.match(h.locator)
        if not m:
            continue
        fp, path = fingerprint(h.value), m.group("path")
        if h.target:
            by_target[h.target] = (fp, path)
        if m.group("key") != "?":
            by_key[m.group("key")] = (fp, path)

    stale: list[StaleHolder] = []
    seen: set[tuple[str, str]] = set()
    for h in hits:
        if h.probe != "process-env":
            continue
        m = _PROC_LOCATOR.match(h.locator)
        if not m:
            continue
        key = m.group("key")
        # Target first: it links the same credential across different names.
        entry = by_target.get(h.target or "") or by_key.get(key)
        if entry is None:
            continue
        disk_fp, source = entry
        held_fp = fingerprint(h.value)
        if held_fp == disk_fp:
            continue
        pid = m.group(1)
        if (pid, key) in seen:
            continue
        seen.add((pid, key))
        stale.append(
            StaleHolder(
                pid=pid,
                process=m.group("comm"),
                key=key,
                held_fingerprint=held_fp,
                disk_fingerprint=disk_fp,
                source=source,
            )
        )
    return stale


__all__ = ["StaleHolder", "correlate_stale"]
