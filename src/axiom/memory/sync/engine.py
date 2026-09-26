# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The bidirectional sync engine — the D2 primitive applied both ways (P4).

Hub-and-spoke, Axiom store the single reconciliation point:

- **Inbound** (:meth:`SyncEngine.apply_inbound`): a detected source change →
  ``import(from_coord → Axiom)`` with origin-preserving provenance. Echo is
  suppressed two ways (the managed-block strip already ran in detection; the
  content-hash echo index catches the rest), so a fragment we wrote out is
  never re-imported. Secret-class content routes to ``vault`` (OQ6). Open
  conflicts are resolved streaming LWW-by-event-time (loser stays queued).
- **Outbound** (:meth:`SyncEngine.propagate_to`): the principal's memory →
  the fail-closed serving gate for the destination peer coordinate → the
  P3 two-zone snapshot → instruction-file write-back. ``vault`` and secret
  content never pass the gate, so they never sync outbound. LWW losers are
  dropped. Every fragment we write out is recorded in the echo index.

The engine itself is stateless beyond the ledger + registry it is handed; the
managed service (:mod:`.service`) owns the durable queue, lease, and recovery.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from axiom.memory.absorb.base import FragmentCandidate
from axiom.memory.absorb.importer import ImportReport, _candidate_text, import_candidates
from axiom.memory.dedup import DedupEngine
from axiom.memory.fragment import fragment_from_dict
from axiom.memory.rendering import EpochSnapshot, InstructionFileWriteBack, pin_epoch
from axiom.memory.serving import (
    ConsumerCoordinate,
    Denial,
    ServableItem,
    ServingGate,
    looks_like_secret,
)
from axiom.memory.sync.conflict import loser_fragment_ids, resolve_streaming_conflicts
from axiom.memory.sync.detect import DetectedChange
from axiom.memory.sync.echo import is_echo, record_echo


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class OutboundResult:
    """What one :meth:`SyncEngine.propagate_to` produced."""

    snapshot: EpochSnapshot
    written: list[str]
    denials: list[Denial]
    served: int


@dataclass
class SyncEngine:
    """Continuous bidirectional sync over one principal's memory.

    ``account_set`` is the set of the user's own harness accounts that may
    receive each other's memory (the sync plan's compatible-account set — the
    honest provisioning of P3 OQ3's ``compatible_accounts``). Cross-account
    isolation still holds against any account outside it.
    """

    composition: Any
    principal: str
    account_set: frozenset[str] = frozenset()
    now_fn: Callable[[], str] = _now_iso
    accountable_human_id: str | None = None
    dedup: DedupEngine | None = None
    gate: ServingGate = field(default_factory=ServingGate)

    # ---- inbound -----------------------------------------------------------

    def apply_inbound(self, change: DetectedChange) -> ImportReport:
        """Import one detected change into Axiom, then resolve open conflicts."""
        fresh = [c for c in change.candidates if not self._is_echo(c)]
        report = import_candidates(
            self.composition,
            fresh,
            principal=self.principal,
            accountable_human_id=self.accountable_human_id,
            dedup=self.dedup,
            secret_detector=looks_like_secret,
        )
        resolve_streaming_conflicts(
            self.composition, principal=self.principal, now_fn=self.now_fn,
        )
        return report

    def _is_echo(self, cand: FragmentCandidate) -> bool:
        return is_echo(self.composition, text=_candidate_text(cand))

    # ---- outbound ----------------------------------------------------------

    def _servable_items(self) -> list[ServableItem]:
        """Every live fragment owned by the principal, as a gate-ready view."""
        seen: set[str] = set()
        items: list[ServableItem] = []
        for artifact in self.composition.artifact_registry.list(kind="fragment"):
            data = artifact.data or {}
            prov = data.get("provenance") or {}
            if prov.get("principal_id") != self.principal:
                continue
            if data.get("id") in seen:
                continue
            seen.add(data["id"])
            items.append(ServableItem.from_fragment(fragment_from_dict(data)))
        return items

    def gated_snapshot(
        self, consumer: ConsumerCoordinate, *, session_id: str, epoch: int
    ) -> tuple[EpochSnapshot, list[Denial]]:
        """Build the destination peer's write-back snapshot (gate + LWW filter)."""
        items = self._servable_items()
        allowed, denials = self.gate.filter(items, consumer)
        losers = loser_fragment_ids(self.composition, principal=self.principal)
        winners = [i for i in allowed if i.fragment_id not in losers]
        snapshot = pin_epoch(session_id, epoch, winners)
        return snapshot, denials

    def propagate_to(
        self,
        consumer: ConsumerCoordinate,
        *,
        targets: list[InstructionFileWriteBack],
        cadence: str,
        session_id: str,
        epoch: int,
    ) -> OutboundResult:
        """Write the principal's gated memory into a destination peer's files.

        Only the authored instruction-file layer is written (D8); ``vault`` and
        secret content are absent by construction (they never pass the gate).
        Every written fragment is recorded in the echo index so the peer's own
        change detector never re-imports it.
        """
        snapshot, denials = self.gated_snapshot(
            consumer, session_id=session_id, epoch=epoch,
        )
        written: list[str] = []
        for wb in targets:
            if wb.sync(snapshot, cadence=cadence):
                written.append(str(wb.path))
        for entry in snapshot.entries:
            record_echo(
                self.composition,
                principal=self.principal,
                target=f"{consumer.harness}/{consumer.account}",
                fragment_id=entry.fragment_id,
                text=entry.text,
            )
        return OutboundResult(
            snapshot=snapshot,
            written=written,
            denials=denials,
            served=len(snapshot.entries),
        )


__all__ = ["OutboundResult", "SyncEngine"]
