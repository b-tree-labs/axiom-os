# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The A2A transport seam for node-to-node memory sync (A3).

P4 reconciled memory harness-to-harness on ONE filesystem through a local hub.
A3 lifts the "other side" of the D2 import primitive to a REMOTE Axiom node
reached over the federation ``axiom://`` A2A hop. This module is the **named
seam** the sync engine calls to push/pull reconciliation messages — the single
place the real inter-node wire drops in.

Maturity (see ``docs/working/cross-mem-a3-federation-assessment.md``): the
federation layer's identity + ``axiom://`` URI + authority policy are real, but
there is **no node-to-node A2A message channel for arbitrary reconciliation
payloads yet** (no inbound message route, no outbound sync client — only
pack-registry / heartbeat HTTP and an in-process signed-finding receive
pipeline). So A3 ships:

- :class:`NodeTransport` — the send/poll/ack contract (the seam interface).
- :class:`LoopbackTransport` — an in-process broker **double** that implements
  it exactly. It exercises the real message path (serialize → send → deliver →
  poll → deserialize → import → ack); only the machine boundary is simulated.
  This is what the two-node lock-step drives, exactly as P2 drove a
  credential-seamed cloud cluster and P4 drove skeleton cloud detectors.
- :class:`A2AFederationTransport` — the real transport, present as a named seam
  whose :meth:`send` raises until the federation wire lands, so it can never
  silently pretend to deliver.
- :func:`node_transport` — the factory that returns the real transport when a
  federation A2A messaging endpoint is present, else the double. Today it
  returns the double.

Poll is **non-destructive** until :meth:`NodeTransport.ack`, mirroring P4's
apply → record-fired → dequeue ordering: a node that crashes after delivery but
before ack re-polls the same message on restart, and the D2 idempotency key +
node-scoped echo land it exactly once (no loss, no echo storm).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

# ---------------------------------------------------------------------------
# The reconciliation message
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NodeSyncMessage:
    """One reconciliation envelope pushed to a peer node over the A2A hop.

    ``entries`` is the gated, LWW-filtered set the origin node offers — each a
    ``(source_ref, text)`` pair, where ``source_ref`` is the origin fragment id
    (stable) and ``text`` is the gate-approved servable text (``vault`` /
    secret / cross-account / tier-restricted content is already absent by
    construction — it never passes the origin node's serving gate). The
    receiving node imports each entry through the D2 primitive with
    origin-preserving provenance.
    """

    origin_node: str
    origin_account: str
    entries: tuple[tuple[str, str], ...]
    sent_at: str

    @property
    def message_id(self) -> str:
        """Content-addressed id — stable across a redelivery, so a broker (or
        the real wire) never double-enqueues and an ack targets one message."""
        canonical = json.dumps(
            {
                "origin_node": self.origin_node,
                "origin_account": self.origin_account,
                "entries": [list(e) for e in self.entries],
                "sent_at": self.sent_at,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]

    def to_dict(self) -> dict:
        return {
            "origin_node": self.origin_node,
            "origin_account": self.origin_account,
            "entries": [list(e) for e in self.entries],
            "sent_at": self.sent_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> NodeSyncMessage:
        return cls(
            origin_node=data["origin_node"],
            origin_account=data["origin_account"],
            entries=tuple((e[0], e[1]) for e in data.get("entries", [])),
            sent_at=data["sent_at"],
        )


# ---------------------------------------------------------------------------
# The seam interface
# ---------------------------------------------------------------------------


@runtime_checkable
class NodeTransport(Protocol):
    """The send/poll/ack contract between two Axiom nodes.

    The real A2A wire and the in-process double both satisfy this shape, so the
    sync engine is written once against the seam.
    """

    def send(self, peer_node_id: str, message: NodeSyncMessage) -> None:
        """Deliver ``message`` to ``peer_node_id`` (idempotent per message id)."""
        ...

    def poll(self, local_node_id: str) -> list[NodeSyncMessage]:
        """Return messages pending for ``local_node_id`` (non-destructive)."""
        ...

    def ack(self, local_node_id: str, message_id: str) -> None:
        """Acknowledge a processed message so it is not redelivered."""
        ...


# ---------------------------------------------------------------------------
# The in-process double
# ---------------------------------------------------------------------------


@dataclass
class LoopbackTransport:
    """In-process A2A broker double: a per-node inbox both nodes attach to.

    Shared by the nodes under test (both hold the same instance), it moves a
    :class:`NodeSyncMessage` from sender to the recipient's inbox and lets the
    recipient poll/ack it — the real send/deliver/poll/import path end to end,
    with only the machine boundary simulated. The inbox outlives a node's
    engine (a message on the wire survives a node crash), so restart-recovery
    is exercised faithfully.
    """

    # node_id -> {message_id -> message}
    _inboxes: dict[str, dict[str, NodeSyncMessage]] = field(default_factory=dict)

    def send(self, peer_node_id: str, message: NodeSyncMessage) -> None:
        self._inboxes.setdefault(peer_node_id, {})[message.message_id] = message

    def poll(self, local_node_id: str) -> list[NodeSyncMessage]:
        inbox = self._inboxes.get(local_node_id, {})
        # Stable order (message id) so a re-poll is deterministic.
        return [inbox[mid] for mid in sorted(inbox)]

    def ack(self, local_node_id: str, message_id: str) -> None:
        self._inboxes.get(local_node_id, {}).pop(message_id, None)


# ---------------------------------------------------------------------------
# The real transport — named seam, unwired until the federation A2A wire lands
# ---------------------------------------------------------------------------


class A2AFederationTransport:
    """The real inter-node transport over the federation ``axiom://`` A2A hop.

    This is the drop-in the factory returns once the federation layer exposes a
    node-to-node A2A message channel (assessment doc §"What is MISSING"). Until
    then its :meth:`send` raises, so no code path can mistake the seam for a
    live wire.
    """

    def __init__(self, endpoint: str = "", federation: object | None = None) -> None:
        self.endpoint = endpoint
        self.federation = federation

    def _unwired(self) -> NotImplementedError:
        return NotImplementedError(
            "the node-to-node A2A message wire is not implemented yet "
            "(federation exposes identity + axiom:// + authority policy, but no "
            "reconciliation message channel — see "
            "docs/working/cross-mem-a3-federation-assessment.md). Use the "
            "LoopbackTransport double until the wire lands."
        )

    def send(self, peer_node_id: str, message: NodeSyncMessage) -> None:
        raise self._unwired()

    def poll(self, local_node_id: str) -> list[NodeSyncMessage]:
        raise self._unwired()

    def ack(self, local_node_id: str, message_id: str) -> None:
        raise self._unwired()


def _federation_has_a2a_wire(federation: object | None) -> bool:
    """True only when the federation layer exposes a live node-to-node A2A
    message channel. Today: always False (the wire does not exist)."""
    if federation is None:
        return False
    # The real probe will look for an A2A message-send/receive endpoint on the
    # federation object. No such surface exists yet, so we stay honest.
    return bool(getattr(federation, "a2a_message_channel", None))


def node_transport(
    *,
    federation: object | None = None,
    shared: LoopbackTransport | None = None,
) -> NodeTransport:
    """Return the real A2A transport when the federation wire is present, else
    the in-process double.

    ``shared`` lets callers (and tests) pass a single broker both nodes attach
    to. With no federation wire — the state today — this returns the
    :class:`LoopbackTransport` double.
    """
    if _federation_has_a2a_wire(federation):
        return A2AFederationTransport(federation=federation)
    return shared if shared is not None else LoopbackTransport()


__all__ = [
    "A2AFederationTransport",
    "LoopbackTransport",
    "NodeSyncMessage",
    "NodeTransport",
    "node_transport",
]
