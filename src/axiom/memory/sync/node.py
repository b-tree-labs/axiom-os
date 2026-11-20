# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Node-to-node continuous sync — the D2 primitive across the A2A hop (A3).

P4's :class:`~axiom.memory.sync.engine.SyncEngine` reconciles a principal's
memory harness-to-harness on ONE filesystem through a local hub. A3 keeps that
engine **unchanged** and lifts the "other side" to a REMOTE Axiom node reached
over the federation ``axiom://`` transport (:mod:`.transport`). Hub-and-spoke
still holds — each node's store is a reconciliation point — but the spoke is now
a machine boundary, not a local file.

Nothing about the D2 primitive is re-implemented. Outbound reuses the engine's
gated-snapshot (serving gate + LWW loser filter — so ``vault`` / secret /
cross-account / tier-restricted content never leaves a node); inbound reuses
:func:`~axiom.memory.absorb.importer.import_candidates` (origin-preserving
provenance, secret→vault-inbound) and
:func:`~axiom.memory.sync.conflict.resolve_streaming_conflicts` (LWW + the P2
conflict queue). The only genuinely new mechanics are:

- a **node coordinate** (``axiom://<node-id>``) naming the peer, mapped to the
  serving gate's :class:`~axiom.memory.serving.ConsumerCoordinate` — a peer
  node is a deployment tier, so the tier boundary applies across the hop;
- **node-scoped echo** (:mod:`.echo`, extended not rewritten): a fragment we
  push to a peer is recorded against that peer, and recognised when it echoes
  back from that peer — never re-imported (no ping-pong across the boundary);
- a **default-deny authorizer**: a node only syncs with a peer it is
  authorized to, in either direction.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from axiom.memory.absorb.base import FragmentCandidate
from axiom.memory.absorb.importer import ImportReport, import_candidates
from axiom.memory.addressing import format_node_uri
from axiom.memory.fragment import CognitiveType, SourceOrigin
from axiom.memory.serving import (
    TIER_REMOTE,
    ConsumerCoordinate,
    Denial,
    looks_like_secret,
)
from axiom.memory.sync.conflict import resolve_streaming_conflicts
from axiom.memory.sync.echo import is_echo, record_echo
from axiom.memory.sync.engine import SyncEngine
from axiom.memory.sync.transport import NodeSyncMessage, NodeTransport

_AGENT = "axi-memory"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


# ---------------------------------------------------------------------------
# Node addressing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NodeCoordinate:
    """A sync peer, identified by its ``axiom://<node-id>`` coordinate.

    ``deployment_tier`` classifies the peer node's *exposure domain* for the
    serving gate: a user's own trusted/hosted node is a controlled tier
    (``local``); an untrusted or third-party node is ``remote`` and controlled
    content will not ride the wire to it. Defaults to ``remote`` (fail-safe).
    """

    node_id: str
    account: str
    deployment_tier: str = TIER_REMOTE
    display_name: str = ""

    @property
    def uri(self) -> str:
        return format_node_uri(self.node_id)

    def consumer(
        self, principal: str, account_set: frozenset[str]
    ) -> ConsumerCoordinate:
        """The serving-gate view of this peer node as a memory consumer."""
        return ConsumerCoordinate(
            principal=principal,
            harness=self.uri,
            account=self.account,
            deployment_tier=self.deployment_tier,
            compatible_accounts=account_set,
        )


# ---------------------------------------------------------------------------
# Trust / authority — a real default-deny leg (assessment doc §"The A3 seam")
# ---------------------------------------------------------------------------


class PeerNotAuthorized(RuntimeError):
    """Raised when a node would sync with a peer it is not authorized to."""


@dataclass
class PeerAuthorizer:
    """Default-deny allow-list of authorized peer node ids.

    A node never syncs — outbound or inbound — with a peer outside this set.
    The set may be seeded from a federation
    :class:`~axiom.vega.federation.policy.TrustProfile`'s ``declared_peers``;
    the gap (recorded in the assessment) is only that the allow-list is not yet
    fed by the live cohort registry / trust-graph walk, not that the check is
    fake.
    """

    authorized: set[str] = field(default_factory=set)

    @classmethod
    def from_trust_profile(cls, profile: Any) -> PeerAuthorizer:
        return cls(authorized=set(profile.declared_peers))

    def is_authorized(self, node_id: str) -> bool:
        return node_id in self.authorized

    def require(self, node_id: str) -> None:
        if not self.is_authorized(node_id):
            raise PeerNotAuthorized(
                f"peer {node_id!r} is not authorized for sync (default-deny; "
                "add it to the node's declared peers first)"
            )


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PushResult:
    """What one :meth:`NodeSyncEngine.push_to` offered a peer."""

    peer_node_id: str
    sent: int
    denials: list[Denial] = field(default_factory=list)
    message_id: str | None = None


@dataclass(frozen=True)
class ReceiveResult:
    """What one :meth:`NodeSyncEngine.receive` drained from the transport."""

    applied: int = 0
    suppressed_echo: int = 0
    vaulted: int = 0
    rejected: int = 0


# ---------------------------------------------------------------------------
# The node-to-node sync engine
# ---------------------------------------------------------------------------


@dataclass
class NodeSyncEngine:
    """Continuous bidirectional sync between two nodes' Axiom stores.

    Wraps the P4 :class:`SyncEngine` (reused for the gate + LWW + import) and
    routes the "other side" over a :class:`NodeTransport`.
    """

    engine: SyncEngine
    local_node: NodeCoordinate
    transport: NodeTransport
    authorizer: PeerAuthorizer
    now_fn: Callable[[], str] = _now_iso
    session_id: str = "session://cross-mem-node-sync"
    epoch: int = 0

    # ---- outbound: local store → peer node --------------------------------

    def push_to(self, peer: NodeCoordinate) -> PushResult:
        """Offer this node's gated memory to an authorized peer node.

        The serving gate (reused via ``gated_snapshot``) drops ``vault`` /
        secret / cross-account / tier-restricted fragments and the LWW losers,
        so only what may legitimately cross the boundary is sent. Every sent
        fragment is recorded in the node-scoped echo index so the peer's echo
        back is never re-imported here.
        """
        self.authorizer.require(peer.node_id)
        consumer = peer.consumer(self.engine.principal, self.engine.account_set)
        snapshot, denials = self.engine.gated_snapshot(
            consumer, session_id=self.session_id, epoch=self.epoch,
        )
        entries = tuple((e.fragment_id, e.text) for e in snapshot.entries)
        for entry in snapshot.entries:
            record_echo(
                self.engine.composition,
                principal=self.engine.principal,
                target=peer.node_id,
                fragment_id=entry.fragment_id,
                text=entry.text,
                node=peer.node_id,
            )
        message = NodeSyncMessage(
            origin_node=self.local_node.node_id,
            origin_account=self.local_node.account,
            entries=entries,
            sent_at=self.now_fn(),
        )
        # Only touch the wire when there is something to reconcile.
        message_id = None
        if entries:
            self.transport.send(peer.node_id, message)
            message_id = message.message_id
        return PushResult(
            peer_node_id=peer.node_id,
            sent=len(entries),
            denials=denials,
            message_id=message_id,
        )

    # ---- inbound: peer node → local store ---------------------------------

    def receive(self) -> ReceiveResult:
        """Drain reconciliation messages addressed to this node and import them.

        Poll is non-destructive; a message is acked only after it is applied
        (apply → ack), so a crash between the two re-delivers on restart and the
        D2 idempotency key + node-scoped echo land it exactly once. Messages
        from an unauthorized origin are rejected and dropped, never imported.
        """
        applied = suppressed = vaulted = rejected = 0
        for message in self.transport.poll(self.local_node.node_id):
            if not self.authorizer.is_authorized(message.origin_node):
                rejected += 1
                self.transport.ack(self.local_node.node_id, message.message_id)
                continue
            report, msg_suppressed = self._import_message(message)
            applied += report.imported
            vaulted += report.secrets_vaulted
            suppressed += msg_suppressed
            self.transport.ack(self.local_node.node_id, message.message_id)
        return ReceiveResult(
            applied=applied,
            suppressed_echo=suppressed,
            vaulted=vaulted,
            rejected=rejected,
        )

    def _import_message(self, message: NodeSyncMessage) -> tuple[ImportReport, int]:
        """Import one message's entries via the D2 primitive (echo-suppressed)."""
        candidates: list[FragmentCandidate] = []
        suppressed = 0
        for source_ref, text in message.entries:
            # Node-scoped echo: text we pushed to this peer, echoing back.
            if is_echo(
                self.engine.composition, text=text, node=message.origin_node
            ):
                suppressed += 1
                continue
            candidates.append(
                FragmentCandidate(
                    # Single canonical text key → the rendered text is a fixed
                    # point across hops, so the returning echo hashes identically.
                    content={"text": text},
                    cognitive_type=CognitiveType.SEMANTIC.value,
                    origin=SourceOrigin(
                        harness=format_node_uri(message.origin_node),
                        account=message.origin_account,
                        source_ref=source_ref,
                        imported_at=self.now_fn(),
                    ),
                )
            )
        report = import_candidates(
            self.engine.composition,
            candidates,
            principal=self.engine.principal,
            accountable_human_id=self.engine.accountable_human_id,
            dedup=self.engine.dedup,
            secret_detector=looks_like_secret,
        )
        resolve_streaming_conflicts(
            self.engine.composition,
            principal=self.engine.principal,
            now_fn=self.engine.now_fn,
        )
        return report, suppressed


# ---------------------------------------------------------------------------
# Thin multi-peer driver (managed-service parity with P4's SyncService)
# ---------------------------------------------------------------------------


@dataclass
class NodeSyncService:
    """Drive one node's sync against a set of peers — receive every tick,
    push on a reconciliation round. A faithful node-level analogue of P4's
    :class:`~axiom.memory.sync.service.SyncService` (inbound continuous,
    outbound on a round), kept deliberately thin: the durability lives in the
    transport inbox + the D2 idempotency key, not a second queue."""

    node_engine: NodeSyncEngine
    peers: list[NodeCoordinate] = field(default_factory=list)

    def receive(self) -> ReceiveResult:
        return self.node_engine.receive()

    def push_round(self) -> list[PushResult]:
        return [self.node_engine.push_to(peer) for peer in self.peers]

    def tick(self) -> ReceiveResult:
        """Inbound-only tick (what a runner heartbeat calls between rounds)."""
        return self.node_engine.receive()

    def recover(self) -> ReceiveResult:
        """Restart reconciliation: re-drain the durable transport inbox."""
        return self.node_engine.receive()


def peers_from(coords: Iterable[NodeCoordinate]) -> list[NodeCoordinate]:
    """Small convenience for building a peer list."""
    return list(coords)


__all__ = [
    "NodeCoordinate",
    "NodeSyncEngine",
    "NodeSyncService",
    "PeerAuthorizer",
    "PeerNotAuthorized",
    "PushResult",
    "ReceiveResult",
    "peers_from",
]
