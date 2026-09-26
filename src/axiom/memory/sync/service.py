# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The managed sync service — a service block the schedule engine ticks (P4).

ADR-087 D2: continuous sync runs as a managed service under the platform
service-reliability contract and is **event-driven, not busy-polling**. This
does not stand up a new daemon type; it registers work the existing PULSE
runner dispatches, exactly like the schedule engine's other service blocks.

- **Event-driven.** A change trigger (:meth:`SyncService.enqueue`, fed by a
  detector poll or an OS file-watch event) puts durable work on the pending
  queue. :meth:`SyncService.tick` — what the runner calls — *dispatches*
  enqueued work; an empty queue makes the tick a no-op. The runner's cadence is
  a heartbeat, not the busy-poll of the import path.
- **Single-flight.** Only the :class:`LeaseManager` holder drains (a
  non-leader tick skips).
- **Durable + exactly-once.** The pending queue and a fire-log ride the
  artifact registry (same posture as the P2 conflict queue). Ordering per item
  is apply → record-fired → dequeue, so a crash anywhere reprocesses at most
  once and, with the idempotency key, lands each change exactly once.
- **Recovery with no loss and no echo storm.** A restart recovers the durable
  pending queue and re-polls; the content-addressed ``change_id`` + fire-log
  skip already-processed work, and the managed-block strip + echo index keep a
  fragment we wrote out from ever being re-imported.
- **Injectable clock.** ``now_fn`` is the only time source — no wall-clock or
  randomness that would break determinism.

Inbound import is continuous; instruction-file write-back obeys the D6 hard
cadence — only a tick carrying a session-boundary / epoch-rollover cadence
flushes outbound.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from axiom.extensions.builtins.schedule.lease import LeaseManager
from axiom.memory.rendering import EPOCH_ROLLOVER, SESSION_BOUNDARY
from axiom.memory.serving import TIER_LOCAL, ConsumerCoordinate
from axiom.memory.sync.detect import ChangeDetector, DetectedChange
from axiom.memory.sync.engine import SyncEngine
from axiom.memory.sync.writeback import MultiTargetWriteBack

SYNC_TICK_ACTION = "cross_mem.sync_tick"

SYNC_PENDING_KIND = "sync_pending"
SYNC_FIRE_KIND = "sync_fire"

_AGENT = "axi-memory"
_WRITEBACK_CADENCES = frozenset({SESSION_BOUNDARY, EPOCH_ROLLOVER})


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass
class SyncPeer:
    """One harness in the sync plan: its detector (inbound) + write-back (out)."""

    harness: str
    account: str
    detector: ChangeDetector
    writeback: MultiTargetWriteBack
    deployment_tier: str = TIER_LOCAL

    def consumer(
        self, principal: str, account_set: frozenset[str]
    ) -> ConsumerCoordinate:
        return ConsumerCoordinate(
            principal=principal,
            harness=self.harness,
            account=self.account,
            deployment_tier=self.deployment_tier,
            compatible_accounts=account_set,
        )


@dataclass
class SyncTickReport:
    """What one :meth:`SyncService.tick` did."""

    applied: int = 0
    skipped_already_fired: int = 0
    skipped: str | None = None
    written: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass
class SyncService:
    """The managed bidirectional-sync service block."""

    composition: Any
    engine: SyncEngine
    peers: list[SyncPeer]
    lease: LeaseManager
    now_fn: Callable[[], datetime] = _now
    session_id: str = "session://cross-mem-sync"
    epoch: int = 0

    # ---- event-driven triggers --------------------------------------------

    def enqueue(self, change: DetectedChange) -> bool:
        """Durably enqueue a change (idempotent). Returns True if newly queued.

        A change already processed (fire-log hit) or already pending is not
        re-enqueued — the content-addressed ``change_id`` makes this stable
        across a restart.
        """
        cid = change.change_id
        if self.composition.artifact_registry.find_by_name(SYNC_FIRE_KIND, cid):
            return False  # already processed
        if self.composition.artifact_registry.find_by_name(SYNC_PENDING_KIND, cid):
            return False  # already pending
        self.composition.artifact_registry.register(
            kind=SYNC_PENDING_KIND,
            name=cid,
            data={
                "change": change.to_dict(),
                "change_id": cid,
                "enqueued_at": self.now_fn().isoformat(),
                "status": "pending",
            },
        )
        return True

    def poll_and_enqueue(self) -> int:
        """Run every peer's detector and enqueue what changed. Returns the count.

        This is the mtime/hash change trigger bridged to the durable queue; in
        a deployment it can be replaced by OS file-watch events calling
        :meth:`enqueue` directly. It never busy-loops the import path.
        """
        enqueued = 0
        for peer in self.peers:
            for change in peer.detector.poll():
                if self.enqueue(change):
                    enqueued += 1
        return enqueued

    def pending_count(self) -> int:
        return len(self.composition.artifact_registry.list(kind=SYNC_PENDING_KIND))

    # ---- the tick the runner dispatches -----------------------------------

    def tick(self, now: datetime | None = None, *, cadence: str | None = None) -> SyncTickReport:
        """Dispatch enqueued work. Only the lease holder drains (single-flight).

        Inbound import runs every tick; outbound write-back runs only when
        ``cadence`` is a session boundary / epoch rollover (D6).
        """
        now = now if now is not None else self.now_fn()
        report = SyncTickReport()

        if self.lease.try_acquire(now) is None:
            report.skipped = "not-leader"
            return report

        self._drain_inbound(report)
        self.lease.maybe_renew(now)

        if cadence in _WRITEBACK_CADENCES:
            self._flush_outbound(report, cadence)
        return report

    def recover(self, now: datetime | None = None) -> SyncTickReport:
        """Restart reconciliation: re-poll, then drain the durable pending queue.

        Re-polling re-detects sources (fresh detector baselines) but the
        fire-log + idempotency key drop already-processed work, and the
        managed-block strip + echo index prevent re-importing our own
        write-backs — so recovery has no loss and no echo storm.
        """
        self.poll_and_enqueue()
        return self.tick(now)

    # ---- internals ---------------------------------------------------------

    def _pending_rows(self) -> list[Any]:
        rows = self.composition.artifact_registry.list(kind=SYNC_PENDING_KIND)
        return sorted(rows, key=lambda a: a.created_at)

    def _drain_inbound(self, report: SyncTickReport) -> None:
        for row in self._pending_rows():
            data = row.data or {}
            cid = data.get("change_id", row.name)
            # Exactly-once: a change already fired successfully is dequeued
            # without re-applying (idempotent even if it re-applied anyway).
            if self.composition.artifact_registry.find_by_name(SYNC_FIRE_KIND, cid):
                self.composition.artifact_registry.delete(row.id, reason="already_fired")
                report.skipped_already_fired += 1
                continue
            try:
                change = DetectedChange.from_dict(data["change"])
                self.engine.apply_inbound(change)
            except Exception as exc:  # noqa: BLE001 — surfaced, never silently dropped
                report.errors.append(f"{cid}: {exc!r}")
                continue
            # apply → record-fired → dequeue (crash-safe ordering).
            self.composition.artifact_registry.register(
                kind=SYNC_FIRE_KIND,
                name=cid,
                data={"change_id": cid, "outcome": "applied",
                      "fired_at": self.now_fn().isoformat()},
            )
            self.composition.artifact_registry.delete(row.id, reason="applied")
            report.applied += 1

    def _flush_outbound(self, report: SyncTickReport, cadence: str) -> None:
        for peer in self.peers:
            consumer = peer.consumer(self.engine.principal, self.engine.account_set)
            try:
                result = self.engine.propagate_to(
                    consumer,
                    targets=peer.writeback.targets(),
                    cadence=cadence,
                    session_id=self.session_id,
                    epoch=self.epoch,
                )
            except Exception as exc:  # noqa: BLE001
                report.errors.append(f"writeback {peer.harness}: {exc!r}")
                continue
            report.written.extend(result.written)
        if cadence == EPOCH_ROLLOVER:
            self.epoch += 1


@dataclass
class SyncExecutor:
    """Adapts :class:`SyncService` to the schedule engine's ``Executor`` seam.

    The PULSE runner fires a schedule whose ``action`` is
    :data:`SYNC_TICK_ACTION`; ``run`` dispatches it to the service tick. The
    optional envelope ``cadence`` lets a session-boundary schedule flush
    outbound write-back on its own cadence.
    """

    service: SyncService

    def run(self, action: str, envelope: Any) -> str:
        if action != SYNC_TICK_ACTION:
            raise ValueError(f"SyncExecutor cannot run action {action!r}")
        cadence = (envelope or {}).get("cadence") if isinstance(envelope, dict) else None
        report = self.service.tick(cadence=cadence)
        return f"sync-tick:applied={report.applied}:written={len(report.written)}"


__all__ = [
    "SYNC_FIRE_KIND",
    "SYNC_PENDING_KIND",
    "SYNC_TICK_ACTION",
    "SyncExecutor",
    "SyncPeer",
    "SyncService",
    "SyncTickReport",
]
