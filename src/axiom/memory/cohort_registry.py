# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""CohortRegistry — sharded per-cohort fragment address registry (#48).

Per ADR-027. Single coordinator per cohort (not global), read-cached
everywhere, write-queued during failover.

This is the MVP alternative to a global DHT: resilient enough for
cohorts up to ~10k members, complexity-bounded, and designed so
the *addressing scheme* is DHT-friendly when we eventually upgrade.

Writes require the coordinator:
- When the coordinator is reachable, writes hit the index immediately.
- When unreachable, writes queue in `pending_writes`.
- On failover election (`elect_coordinator`), the queue drains into
  the new coordinator's index.

Reads never fail as long as a local snapshot exists.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Propagation-mode selector
# ---------------------------------------------------------------------------


def propagation_mode_for_size(cohort_size: int) -> str:
    """Auto-select push/pull/gossip by cohort size.

    - < 100: push (instant broadcast, trivial)
    - 100 – 9,999: pull (topic subscription)
    - >= 10,000: gossip (epidemic, eventual consistency)
    """
    if cohort_size < 100:
        return "push"
    if cohort_size < 10_000:
        return "pull"
    return "gossip"


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CohortRegistry:
    """Fragment address index for one cohort.

    `index` maps fragment_id → frozenset of node_ids that hold a
    replica. `pending_writes` accumulates writes made while the
    coordinator is unreachable; a successful failover election
    drains them.
    """

    classroom_id: str
    coordinator_node: str
    index: dict[str, frozenset[str]] = field(default_factory=dict)
    coordinator_reachable: bool = True
    pending_writes: tuple[dict, ...] = ()
    propagation_mode: str | None = None

    # ------- Register / deregister -----------------------------------------

    def register(self, fragment_id: str, held_by: str) -> CohortRegistry:
        """Add a node to the replica set for a fragment."""
        if not self.coordinator_reachable:
            return dataclasses.replace(
                self,
                pending_writes=tuple([
                    *self.pending_writes,
                    {"op": "register", "fragment_id": fragment_id,
                     "held_by": held_by},
                ]),
            )
        existing = self.index.get(fragment_id, frozenset())
        new_index = dict(self.index)
        new_index[fragment_id] = frozenset(existing | {held_by})
        return dataclasses.replace(self, index=new_index)

    def deregister(
        self, fragment_id: str, held_by: str
    ) -> CohortRegistry:
        """Remove a node from the replica set for a fragment."""
        if not self.coordinator_reachable:
            return dataclasses.replace(
                self,
                pending_writes=tuple([
                    *self.pending_writes,
                    {"op": "deregister", "fragment_id": fragment_id,
                     "held_by": held_by},
                ]),
            )
        existing = self.index.get(fragment_id, frozenset())
        new_set = existing - {held_by}
        new_index = dict(self.index)
        if new_set:
            new_index[fragment_id] = new_set
        else:
            new_index.pop(fragment_id, None)
        return dataclasses.replace(self, index=new_index)

    # ------- Queries -------------------------------------------------------

    def locate(self, fragment_id: str) -> frozenset[str]:
        """Return nodes holding a replica. Served from cache during failover."""
        return self.index.get(fragment_id, frozenset())

    # ------- Snapshot / restore for read cache -----------------------------

    def snapshot(self) -> dict:
        """Produce a JSON-safe snapshot for local caching."""
        return {
            "classroom_id": self.classroom_id,
            "coordinator_node": self.coordinator_node,
            "index": {k: sorted(v) for k, v in self.index.items()},
            "propagation_mode": self.propagation_mode,
        }

    @classmethod
    def from_snapshot(cls, snap: dict) -> CohortRegistry:
        return cls(
            classroom_id=snap["classroom_id"],
            coordinator_node=snap["coordinator_node"],
            index={k: frozenset(v) for k, v in snap["index"].items()},
            propagation_mode=snap.get("propagation_mode"),
        )

    # ------- Coordinator failover ------------------------------------------

    def mark_coordinator_unreachable(self) -> CohortRegistry:
        return dataclasses.replace(self, coordinator_reachable=False)

    def elect_coordinator(self, new_coordinator: str) -> CohortRegistry:
        """Promote a new coordinator + drain pending writes into the index."""
        r = dataclasses.replace(
            self,
            coordinator_node=new_coordinator,
            coordinator_reachable=True,
        )
        # Drain pending_writes sequentially into the new index
        drained_index = dict(r.index)
        for op in r.pending_writes:
            fid = op["fragment_id"]
            node = op["held_by"]
            if op["op"] == "register":
                current = drained_index.get(fid, frozenset())
                drained_index[fid] = frozenset(current | {node})
            elif op["op"] == "deregister":
                current = drained_index.get(fid, frozenset())
                remaining = current - {node}
                if remaining:
                    drained_index[fid] = remaining
                else:
                    drained_index.pop(fid, None)
        return dataclasses.replace(r, index=drained_index, pending_writes=())
