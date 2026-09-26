# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""NetworkLedger — request tracking, bandwidth, latency, and anomaly detection.

Lightweight request ledger using only stdlib. Subsystems call record() after
each HTTP request. No monkey-patching — explicit instrumentation at call sites.
"""

from __future__ import annotations

import statistics
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime


@dataclass
class RequestRecord:
    """A single HTTP request observation."""

    timestamp: str = ""
    caller: str = ""           # e.g. "gateway.anthropic", "extractor.gitlab"
    endpoint: str = ""         # URL host + path (no query params)
    method: str = "GET"
    status_code: int = 0
    latency_ms: float = 0.0
    bytes_sent: int = 0
    bytes_received: int = 0
    error: str | None = None

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now(UTC).isoformat()


@dataclass
class CallerStats:
    """Per-caller aggregate stats within a time window."""

    requests: int = 0
    errors: int = 0
    bytes_sent: int = 0
    bytes_received: int = 0
    avg_latency_ms: float = 0.0


@dataclass
class NetworkAnomaly:
    """A detected network anomaly."""

    kind: str = ""       # "latency_spike" | "error_burst" | "bandwidth_surge" | "endpoint_down"
    caller: str = ""
    evidence: str = ""   # human-readable
    severity: str = ""   # "warning" | "critical"


@dataclass
class NetworkStats:
    """Rolling window aggregate computed from recent RequestRecords."""

    window_seconds: int = 300
    total_requests: int = 0
    total_errors: int = 0
    error_rate_pct: float = 0.0
    total_bytes_sent: int = 0
    total_bytes_received: int = 0
    avg_latency_ms: float = 0.0
    p95_latency_ms: float = 0.0
    by_caller: dict[str, CallerStats] = field(default_factory=dict)
    anomalies: list[NetworkAnomaly] = field(default_factory=list)


@dataclass
class _CallerBaseline:
    """Exponential moving average baseline for a caller."""

    avg_latency_ms: float = 0.0
    avg_error_rate: float = 0.0
    avg_bytes_per_req: float = 0.0
    sample_count: int = 0
    _alpha: float = 0.1  # EMA smoothing factor

    def update(self, latency_ms: float, is_error: bool, total_bytes: int) -> None:
        if self.sample_count == 0:
            self.avg_latency_ms = latency_ms
            self.avg_error_rate = 1.0 if is_error else 0.0
            self.avg_bytes_per_req = float(total_bytes)
        else:
            a = self._alpha
            self.avg_latency_ms = a * latency_ms + (1 - a) * self.avg_latency_ms
            self.avg_error_rate = a * (1.0 if is_error else 0.0) + (1 - a) * self.avg_error_rate
            self.avg_bytes_per_req = a * total_bytes + (1 - a) * self.avg_bytes_per_req
        self.sample_count += 1


# Process-global singleton
_shared_instance: NetworkLedger | None = None
_shared_lock = threading.Lock()


class NetworkLedger:
    """Thread-safe HTTP request ledger with anomaly detection."""

    def __init__(self, max_records: int = 2000):
        self._records: deque[RequestRecord] = deque(maxlen=max_records)
        self._baselines: dict[str, _CallerBaseline] = {}
        self._lock = threading.Lock()
        # Track consecutive errors per endpoint
        self._consecutive_errors: dict[str, int] = {}

    @classmethod
    def shared(cls) -> NetworkLedger:
        """Return the process-global NetworkLedger singleton."""
        global _shared_instance
        if _shared_instance is None:
            with _shared_lock:
                if _shared_instance is None:
                    _shared_instance = cls()
        return _shared_instance

    def record(self, rec: RequestRecord) -> None:
        """Record an HTTP request observation."""
        with self._lock:
            self._records.append(rec)

            # Update baseline
            if rec.caller not in self._baselines:
                self._baselines[rec.caller] = _CallerBaseline()
            self._baselines[rec.caller].update(
                rec.latency_ms,
                rec.error is not None or rec.status_code >= 400,
                rec.bytes_sent + rec.bytes_received,
            )

            # Track consecutive errors
            key = f"{rec.caller}:{rec.endpoint}"
            if rec.error is not None or rec.status_code >= 500:
                self._consecutive_errors[key] = self._consecutive_errors.get(key, 0) + 1
            else:
                self._consecutive_errors[key] = 0

    def stats(self, window_seconds: int = 300) -> NetworkStats:
        """Compute aggregate stats over the recent window."""
        cutoff = time.time() - window_seconds

        with self._lock:
            recent = [
                r for r in self._records
                if self._record_time(r) > cutoff
            ]

        if not recent:
            return NetworkStats(window_seconds=window_seconds)

        latencies = [r.latency_ms for r in recent]
        errors = [r for r in recent if r.error is not None or r.status_code >= 400]

        # Per-caller breakdown
        by_caller: dict[str, CallerStats] = {}
        for r in recent:
            if r.caller not in by_caller:
                by_caller[r.caller] = CallerStats()
            cs = by_caller[r.caller]
            cs.requests += 1
            if r.error is not None or r.status_code >= 400:
                cs.errors += 1
            cs.bytes_sent += r.bytes_sent
            cs.bytes_received += r.bytes_received

        # Compute avg latency per caller
        caller_latencies: dict[str, list[float]] = {}
        for r in recent:
            caller_latencies.setdefault(r.caller, []).append(r.latency_ms)
        for caller, cs in by_caller.items():
            lats = caller_latencies.get(caller, [])
            cs.avg_latency_ms = round(statistics.mean(lats), 1) if lats else 0.0

        # p95 latency
        sorted_lats = sorted(latencies)
        p95_idx = int(len(sorted_lats) * 0.95)
        p95 = sorted_lats[min(p95_idx, len(sorted_lats) - 1)]

        return NetworkStats(
            window_seconds=window_seconds,
            total_requests=len(recent),
            total_errors=len(errors),
            error_rate_pct=round(len(errors) / len(recent) * 100, 1) if recent else 0.0,
            total_bytes_sent=sum(r.bytes_sent for r in recent),
            total_bytes_received=sum(r.bytes_received for r in recent),
            avg_latency_ms=round(statistics.mean(latencies), 1),
            p95_latency_ms=round(p95, 1),
            by_caller=by_caller,
            anomalies=self.detect_anomalies(),
        )

    def detect_anomalies(self) -> list[NetworkAnomaly]:
        """Compare recent window against baselines for anomaly detection."""
        from .vitals import VitalsThresholds
        thresholds = VitalsThresholds()
        anomalies: list[NetworkAnomaly] = []
        cutoff_60s = time.time() - 60

        with self._lock:
            recent_60s: dict[str, list[RequestRecord]] = {}
            for r in self._records:
                if self._record_time(r) > cutoff_60s:
                    recent_60s.setdefault(r.caller, []).append(r)

            # Check each caller against its baseline
            for caller, records in recent_60s.items():
                baseline = self._baselines.get(caller)
                if baseline is None or baseline.sample_count < 10:
                    continue

                # Latency spike
                avg_lat = statistics.mean([r.latency_ms for r in records])
                if avg_lat > baseline.avg_latency_ms * thresholds.latency_spike_factor:
                    anomalies.append(NetworkAnomaly(
                        kind="latency_spike",
                        caller=caller,
                        evidence=(
                            f"{caller}: avg {avg_lat:.0f}ms vs baseline "
                            f"{baseline.avg_latency_ms:.0f}ms "
                            f"({avg_lat / baseline.avg_latency_ms:.1f}x)"
                        ),
                        severity="warning",
                    ))

                # Error burst
                error_count = sum(
                    1 for r in records
                    if r.error is not None or r.status_code >= 400
                )
                error_rate = error_count / len(records) * 100 if records else 0
                if (error_rate > thresholds.error_burst_pct
                        and baseline.avg_error_rate < 0.02):
                    anomalies.append(NetworkAnomaly(
                        kind="error_burst",
                        caller=caller,
                        evidence=(
                            f"{caller}: {error_count} errors in 60s "
                            f"({error_rate:.0f}% vs baseline "
                            f"{baseline.avg_error_rate * 100:.0f}%)"
                        ),
                        severity="critical",
                    ))

                # Bandwidth surge
                avg_bytes = statistics.mean([
                    r.bytes_sent + r.bytes_received for r in records
                ])
                if (baseline.avg_bytes_per_req > 0
                        and avg_bytes > baseline.avg_bytes_per_req * thresholds.bandwidth_surge_factor):
                    anomalies.append(NetworkAnomaly(
                        kind="bandwidth_surge",
                        caller=caller,
                        evidence=(
                            f"{caller}: avg {avg_bytes:.0f} bytes/req vs "
                            f"baseline {baseline.avg_bytes_per_req:.0f} "
                            f"({avg_bytes / baseline.avg_bytes_per_req:.1f}x)"
                        ),
                        severity="warning",
                    ))

            # Endpoint down: 5+ consecutive errors
            for key, count in self._consecutive_errors.items():
                if count >= 5:
                    caller = key.split(":", 1)[0]
                    endpoint = key.split(":", 1)[1] if ":" in key else "?"
                    anomalies.append(NetworkAnomaly(
                        kind="endpoint_down",
                        caller=caller,
                        evidence=f"{caller}: {count} consecutive errors to {endpoint}",
                        severity="critical",
                    ))

        return anomalies

    @staticmethod
    def _record_time(rec: RequestRecord) -> float:
        """Parse record timestamp to epoch seconds."""
        try:
            return datetime.fromisoformat(rec.timestamp).timestamp()
        except (ValueError, TypeError):
            return 0.0
