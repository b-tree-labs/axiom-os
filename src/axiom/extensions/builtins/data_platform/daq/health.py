# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Health — one ``details`` mapping, silence detection, and the credited guard.

Spec §7: extend the subsystem's ``Health.details`` (``last_record_at``,
``journal_bytes``, ``backpressure``) with ``seq``, per-cursor lag, trip count
and connection state — no parallel state object. Silence (§7.2) is the failure
to design for: a producer that is up, reachable, and delivering nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from .envelope import SignalEnvelope


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


@dataclass(frozen=True)
class SilenceVerdict:
    silent: bool
    silent_for_s: float | None
    reason: str


def silence(
    last_record_at: str | None,
    *,
    expected_interval_s: float,
    factor: float = 3.0,
    now: datetime | None = None,
) -> SilenceVerdict:
    """Silent when ``now - last_record_at`` exceeds ``expected_interval_s * factor``,
    or when nothing has ever been recorded."""
    now = now or datetime.now(UTC)
    last = _parse(last_record_at)
    if last is None:
        return SilenceVerdict(True, None, "no record has ever been consolidated")
    gap = (now - last).total_seconds()
    limit = expected_interval_s * factor
    if gap > limit:
        return SilenceVerdict(True, gap, f"{gap:.1f}s since last record > {limit:.1f}s")
    return SilenceVerdict(False, gap, "ok")


@dataclass
class CreditedGuard:
    """A credited consumer's deadline logic (§4.2–4.3): a ``seq`` gap or a
    record older than ``deadline_s`` is a **trip**, not an alert. Once tripped
    it stays tripped until :meth:`reset` — control authority does not come back
    by itself."""

    deadline_s: float
    tripped: bool = False
    trips: list[str] = field(default_factory=list)
    _last_seq: int | None = None

    def observe(
        self, envelope: SignalEnvelope, record_ts: str, *, now: datetime | None = None
    ) -> bool:
        now = now or datetime.now(UTC)
        if self._last_seq is not None and envelope.seq != self._last_seq + 1:
            self._trip(f"seq gap {self._last_seq} → {envelope.seq}")
        ts = _parse(record_ts)
        if ts is not None and (now - ts).total_seconds() > self.deadline_s:
            self._trip(f"stale: record {record_ts} older than {self.deadline_s}s")
        self._last_seq = envelope.seq
        return self.tripped

    def _trip(self, why: str) -> None:
        self.tripped = True
        self.trips.append(why)

    def reset(self) -> None:
        self.tripped = False
        self._last_seq = None


def merge_details(*parts: dict) -> dict:
    """The one ``Health.details`` mapping: later parts win on key clashes."""
    out: dict = {}
    for p in parts:
        out.update(p)
    return out


__all__ = ["CreditedGuard", "SilenceVerdict", "merge_details", "silence"]
