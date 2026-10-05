# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TIDY heartbeat-liveness audit — watch the watcher.

Per the 2026-06-01 RIVET autopsy: the per-agent heartbeat.jsonl files
froze for 28 hours because a CLI noun-renaming dropped `release
heartbeat` from the skill registry. The launchd dispatcher kept firing
the (now-wrong) command, every fire succeeded, no exception
propagated, and the operator had no signal that RIVET was silent.

This module closes that loop. On every TIDY tick:

1. Walk ``~/.axi/agents/*/heartbeat.jsonl`` (and any equivalent the
   manifest declares).
2. For each, compare last-modified time + last-entry timestamp to the
   agent's declared ``heartbeat_interval`` (with a generous 3× tolerance
   to absorb fork delays, retries, etc.).
3. Emit a hygiene finding for any agent that's gone quiet.

The finding can be routed by TIDY's existing notification surface (the
operator inbox when HERALD ships; the JSONL signal file in the
meantime).

This is also the precedent for the broader **unit_inventory_audit**
follow-up (item 4 of the supervisor reset plan): a single agent's
silence is the canary; the supervisor's lifecycle is the next layer.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

log = logging.getLogger("axiom.hygiene.heartbeat_liveness")

# How many heartbeat_intervals stale before we consider an agent dead.
DEFAULT_TOLERANCE_FACTOR = 3.0

# Fallback when an agent's manifest doesn't declare an interval.
DEFAULT_FALLBACK_INTERVAL_SEC = 300


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AgentLiveness:
    """Per-agent liveness state."""

    name: str
    heartbeat_path: Path
    last_entry_at: datetime | None
    """Most recent ts from the JSONL (None when file empty or unparseable)."""
    file_mtime: datetime | None
    """Filesystem mtime (None when file doesn't exist)."""
    interval_seconds: int
    """The agent's declared heartbeat_interval."""
    is_dead: bool
    """True when last activity is beyond tolerance × interval."""
    seconds_since_last: float | None
    """How long since the last heartbeat fired (None when never fired)."""

    @property
    def staleness_factor(self) -> float | None:
        """How many intervals stale; None if no last entry."""
        if self.seconds_since_last is None:
            return None
        return self.seconds_since_last / self.interval_seconds


@dataclass(frozen=True)
class LivenessFinding:
    """One hygiene finding emitted to TIDY's signal stream."""

    agent: str
    severity: str
    """``stale`` (mild), ``dead`` (urgent), or ``never_fired`` (install bug)."""
    detail: str
    last_activity: datetime | None
    interval_seconds: int


# ---------------------------------------------------------------------------
# Discovery + scanning
# ---------------------------------------------------------------------------


def _axi_home() -> Path:
    return Path(os.environ.get("AXIOM_HOME", str(Path.home() / ".axi")))


def discover_heartbeat_paths(
    axi_home: Path | None = None,
) -> Iterable[tuple[str, Path]]:
    """Yield ``(agent_name, heartbeat_jsonl_path)`` for every agent dir."""
    axi_home = axi_home or _axi_home()
    agents_dir = axi_home / "agents"
    if not agents_dir.is_dir():
        return
    for child in agents_dir.iterdir():
        if not child.is_dir():
            continue
        # Skip the supervisor's own scratch dir.
        if child.name.startswith(".") or child.name.startswith("_"):
            continue
        hb = child / "heartbeat.jsonl"
        if hb.exists():
            yield child.name, hb


def read_last_heartbeat_ts(path: Path) -> datetime | None:
    """Parse the JSONL's last entry's ``ts`` field. None on any failure."""
    try:
        # Read just the tail; heartbeat files can be huge.
        size = path.stat().st_size
        with path.open("rb") as f:
            tail_size = min(size, 65536)
            f.seek(max(0, size - tail_size))
            tail = f.read().decode("utf-8", errors="ignore")
    except OSError:
        return None
    last_line = ""
    for line in tail.splitlines():
        s = line.strip()
        if s:
            last_line = s
    if not last_line:
        return None
    try:
        d = json.loads(last_line)
        ts_str = d.get("ts")
        if not ts_str:
            return None
        return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except (json.JSONDecodeError, ValueError):
        return None


def _file_mtime(path: Path) -> datetime | None:
    try:
        return datetime.fromtimestamp(
            path.stat().st_mtime, tz=timezone.utc
        )
    except OSError:
        return None


def assess_agent(
    name: str,
    heartbeat_path: Path,
    *,
    interval_seconds: int,
    tolerance_factor: float = DEFAULT_TOLERANCE_FACTOR,
    now: datetime | None = None,
) -> AgentLiveness:
    """Build an ``AgentLiveness`` for one agent."""
    now = now or datetime.now(timezone.utc)
    last_entry = read_last_heartbeat_ts(heartbeat_path)
    mtime = _file_mtime(heartbeat_path)
    seconds_since = (
        (now - last_entry).total_seconds() if last_entry else None
    )
    tolerance = interval_seconds * tolerance_factor
    is_dead = (
        last_entry is None
        or (seconds_since is not None and seconds_since > tolerance)
    )
    return AgentLiveness(
        name=name,
        heartbeat_path=heartbeat_path,
        last_entry_at=last_entry,
        file_mtime=mtime,
        interval_seconds=interval_seconds,
        is_dead=is_dead,
        seconds_since_last=seconds_since,
    )


def audit_all_agents(
    *,
    axi_home: Path | None = None,
    intervals: dict[str, int] | None = None,
    tolerance_factor: float = DEFAULT_TOLERANCE_FACTOR,
    now: datetime | None = None,
) -> list[AgentLiveness]:
    """Top-level audit. Returns liveness records for every agent dir.

    ``intervals`` maps agent name → heartbeat_interval (seconds). Falls
    back to ``DEFAULT_FALLBACK_INTERVAL_SEC`` for unknown agents.
    """
    intervals = intervals or {}
    out: list[AgentLiveness] = []
    for name, path in discover_heartbeat_paths(axi_home):
        interval = intervals.get(name, DEFAULT_FALLBACK_INTERVAL_SEC)
        out.append(
            assess_agent(
                name,
                path,
                interval_seconds=interval,
                tolerance_factor=tolerance_factor,
                now=now,
            )
        )
    return out


# ---------------------------------------------------------------------------
# Findings
# ---------------------------------------------------------------------------


def to_findings(records: Iterable[AgentLiveness]) -> list[LivenessFinding]:
    """Convert liveness records into hygiene findings.

    Only emits findings for dead/stale agents. A healthy agent produces
    no finding (no news is good news).
    """
    findings: list[LivenessFinding] = []
    for rec in records:
        if not rec.is_dead:
            continue
        if rec.last_entry_at is None:
            findings.append(
                LivenessFinding(
                    agent=rec.name,
                    severity="never_fired",
                    detail=(
                        f"agent {rec.name!r} has heartbeat.jsonl but no "
                        "valid entries — likely an install-time wiring bug "
                        "(missing skill, manifest typo, bad heartbeat_command)"
                    ),
                    last_activity=None,
                    interval_seconds=rec.interval_seconds,
                )
            )
            continue
        sf = rec.staleness_factor or 0
        severity = "dead" if sf >= 6 else "stale"
        findings.append(
            LivenessFinding(
                agent=rec.name,
                severity=severity,
                detail=(
                    f"agent {rec.name!r} last fired "
                    f"{rec.seconds_since_last:.0f}s ago "
                    f"(interval={rec.interval_seconds}s; "
                    f"staleness factor {sf:.1f}× tolerance)"
                ),
                last_activity=rec.last_entry_at,
                interval_seconds=rec.interval_seconds,
            )
        )
    return findings


__all__ = [
    "AgentLiveness",
    "DEFAULT_FALLBACK_INTERVAL_SEC",
    "DEFAULT_TOLERANCE_FACTOR",
    "LivenessFinding",
    "assess_agent",
    "audit_all_agents",
    "discover_heartbeat_paths",
    "read_last_heartbeat_ts",
    "to_findings",
]
