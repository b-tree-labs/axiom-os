# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Layer 2: Vitals monitoring — disk, memory, network, leak detection, thresholds.

Deterministic resource tracking. No LLM. Soft dependency on psutil — degrades
gracefully to shutil.disk_usage without it.
"""

from __future__ import annotations

import os
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from .network import NetworkLedger, NetworkStats


@dataclass
class VitalsThresholds:
    """Configurable thresholds for resource monitoring."""

    disk_pct_warn: float = 80.0
    disk_pct_crit: float = 95.0
    mem_pct_warn: float = 85.0
    mem_pct_crit: float = 95.0
    leak_min_entries: int = 10
    leak_min_bytes: int = 50_000_000  # 50 MB
    latency_spike_factor: float = 3.0
    error_burst_pct: float = 20.0
    bandwidth_surge_factor: float = 5.0


@dataclass
class VitalsSnapshot:
    """Point-in-time resource snapshot."""

    timestamp: str = ""
    # Disk
    scratch_used_bytes: int = 0
    scratch_free_bytes: int = 0
    scratch_total_bytes: int = 0
    scratch_pct: float = 0.0
    project_dir_bytes: int = 0
    # Memory (soft — requires psutil)
    process_rss_bytes: int | None = None
    system_mem_pct: float | None = None
    # Scratch manifest stats
    active_entries: int = 0
    entries_by_owner: dict[str, int] = field(default_factory=dict)
    bytes_by_owner: dict[str, int] = field(default_factory=dict)
    # Network
    net: NetworkStats | None = None

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now(UTC).isoformat()

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "timestamp": self.timestamp,
            "scratch_used_bytes": self.scratch_used_bytes,
            "scratch_free_bytes": self.scratch_free_bytes,
            "scratch_total_bytes": self.scratch_total_bytes,
            "scratch_pct": self.scratch_pct,
            "project_dir_bytes": self.project_dir_bytes,
            "process_rss_bytes": self.process_rss_bytes,
            "system_mem_pct": self.system_mem_pct,
            "active_entries": self.active_entries,
            "entries_by_owner": self.entries_by_owner,
            "bytes_by_owner": self.bytes_by_owner,
        }
        if self.net is not None:
            d["net"] = {
                "total_requests": self.net.total_requests,
                "total_errors": self.net.total_errors,
                "error_rate_pct": self.net.error_rate_pct,
                "avg_latency_ms": self.net.avg_latency_ms,
                "p95_latency_ms": self.net.p95_latency_ms,
            }
        return d


@dataclass
class LeakSignal:
    """A detected resource leak pattern."""

    owner: str = ""
    pattern: str = ""        # "accumulating" | "unreleased" | "orphaned_pid"
    evidence: str = ""
    entry_count: int = 0
    total_bytes: int = 0
    first_seen: str = ""


class PressureLevel:
    """Resource pressure levels."""

    NOMINAL = "nominal"
    ELEVATED = "elevated"
    CRITICAL = "critical"


class VitalsMonitor:
    """Samples system vitals, detects leaks, and checks pressure thresholds."""

    def __init__(
        self,
        mgr: Any,
        ledger: NetworkLedger | None = None,
        bus: Any | None = None,
        thresholds: VitalsThresholds | None = None,
    ):
        self._mgr = mgr
        self._ledger = ledger or NetworkLedger.shared()
        self._bus = bus
        self._thresholds = thresholds or VitalsThresholds()
        self._history: deque[VitalsSnapshot] = deque(maxlen=120)
        self._leak_history: dict[str, int] = {}  # owner -> previous entry count

    @property
    def thresholds(self) -> VitalsThresholds:
        return self._thresholds

    @property
    def history(self) -> list[VitalsSnapshot]:
        return list(self._history)

    def set_bus(self, bus: Any) -> None:
        self._bus = bus

    def sample(self) -> VitalsSnapshot:
        """Collect a vitals snapshot and append to history."""
        status = self._mgr.status()
        entries = self._mgr.all_entries()

        # Per-owner breakdown
        entries_by_owner: dict[str, int] = {}
        bytes_by_owner: dict[str, int] = {}
        for e in entries:
            entries_by_owner[e.owner] = entries_by_owner.get(e.owner, 0) + 1
            bytes_by_owner[e.owner] = bytes_by_owner.get(e.owner, 0) + e.size_bytes

        snap = VitalsSnapshot(
            scratch_used_bytes=status.get("total_size_bytes", 0),
            scratch_free_bytes=status.get("disk_free_bytes", 0),
            scratch_total_bytes=status.get("disk_total_bytes", 0),
            scratch_pct=status.get("disk_used_pct", 0.0),
            active_entries=len(entries),
            entries_by_owner=entries_by_owner,
            bytes_by_owner=bytes_by_owner,
        )

        # Memory (soft psutil dependency)
        try:
            import psutil
            proc = psutil.Process(os.getpid())
            snap.process_rss_bytes = proc.memory_info().rss
            snap.system_mem_pct = psutil.virtual_memory().percent
        except (ImportError, Exception):
            pass

        # Network
        snap.net = self._ledger.stats()

        self._history.append(snap)

        # Publish snapshot
        self._publish("tidy.vitals", snap.to_dict())

        return snap

    def detect_leaks(self) -> list[LeakSignal]:
        """Scan manifest for leak patterns."""
        signals: list[LeakSignal] = []
        entries = self._mgr.all_entries()
        now = datetime.now(UTC)

        # Group by owner
        by_owner: dict[str, list] = {}
        for e in entries:
            by_owner.setdefault(e.owner, []).append(e)

        for owner, owner_entries in by_owner.items():
            count = len(owner_entries)
            total_bytes = sum(e.size_bytes for e in owner_entries)

            # Accumulating: entry count grew monotonically over recent samples
            prev_count = self._leak_history.get(owner, 0)
            if (count > prev_count
                    and count >= self._thresholds.leak_min_entries):
                # Check if it's been growing over the last few samples
                growing = self._check_monotonic_growth(owner)
                if growing:
                    signals.append(LeakSignal(
                        owner=owner,
                        pattern="accumulating",
                        evidence=(
                            f"{owner}: {count} entries ({self._format_bytes(total_bytes)}), "
                            f"growing monotonically"
                        ),
                        entry_count=count,
                        total_bytes=total_bytes,
                        first_seen=min(e.created_at for e in owner_entries),
                    ))

            self._leak_history[owner] = count

            # Unreleased: transient entries older than 5 minutes
            for e in owner_entries:
                if e.retention == "transient":
                    try:
                        created = datetime.fromisoformat(e.created_at)
                        age = (now - created).total_seconds()
                        if age > 300:
                            signals.append(LeakSignal(
                                owner=owner,
                                pattern="unreleased",
                                evidence=(
                                    f"{owner}: transient entry {e.id} alive for "
                                    f"{age:.0f}s (expected <300s)"
                                ),
                                entry_count=1,
                                total_bytes=e.size_bytes,
                                first_seen=e.created_at,
                            ))
                    except (ValueError, TypeError):
                        pass

            # Orphaned PID
            for e in owner_entries:
                if e.pid > 0 and not self._pid_alive(e.pid):
                    signals.append(LeakSignal(
                        owner=owner,
                        pattern="orphaned_pid",
                        evidence=f"{owner}: entry {e.id} owned by dead PID {e.pid}",
                        entry_count=1,
                        total_bytes=e.size_bytes,
                        first_seen=e.created_at,
                    ))

        return signals

    def check_pressure(self) -> str:
        """Check resource pressure against thresholds. Returns PressureLevel."""
        t = self._thresholds
        level = PressureLevel.NOMINAL

        # Disk pressure
        if self._history:
            snap = self._history[-1]
            if snap.scratch_pct >= t.disk_pct_crit:
                level = PressureLevel.CRITICAL
            elif snap.scratch_pct >= t.disk_pct_warn:
                level = max(level, PressureLevel.ELEVATED, key=_pressure_rank)

            # Memory pressure
            if snap.system_mem_pct is not None:
                if snap.system_mem_pct >= t.mem_pct_crit:
                    level = PressureLevel.CRITICAL
                elif snap.system_mem_pct >= t.mem_pct_warn:
                    level = max(level, PressureLevel.ELEVATED, key=_pressure_rank)

            # Network anomalies
            if snap.net and snap.net.anomalies:
                for anomaly in snap.net.anomalies:
                    if anomaly.severity == "critical":
                        self._publish("tidy.net_anomaly", {
                            "kind": anomaly.kind,
                            "caller": anomaly.caller,
                            "evidence": anomaly.evidence,
                        })
                        level = max(level, PressureLevel.ELEVATED, key=_pressure_rank)

        # Publish pressure events
        if level == PressureLevel.CRITICAL:
            self._publish("tidy.pressure_critical", {"level": level})
        elif level == PressureLevel.ELEVATED:
            self._publish("tidy.pressure", {"level": level})

        return level

    def _check_monotonic_growth(self, owner: str) -> bool:
        """Check if an owner's entry count has been growing over recent snapshots."""
        if len(self._history) < 3:
            return False
        recent = list(self._history)[-5:]
        counts = [snap.entries_by_owner.get(owner, 0) for snap in recent]
        # Monotonically non-decreasing with at least some growth
        return all(b >= a for a, b in zip(counts, counts[1:])) and counts[-1] > counts[0]

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            return False
        except OSError:
            return False

    @staticmethod
    def _format_bytes(n: int) -> str:
        for unit in ("B", "KB", "MB", "GB"):
            if abs(n) < 1024:
                return f"{n:.1f} {unit}"
            n = int(n / 1024)
        return f"{n:.1f} TB"

    def _publish(self, topic: str, data: dict[str, Any]) -> None:
        if self._bus is not None:
            try:
                self._bus.publish(topic, data, source="tidy.vitals")
            except Exception:
                pass


def _pressure_rank(level: str) -> int:
    """Rank pressure levels for comparison."""
    return {
        PressureLevel.NOMINAL: 0,
        PressureLevel.ELEVATED: 1,
        PressureLevel.CRITICAL: 2,
    }.get(level, 0)
