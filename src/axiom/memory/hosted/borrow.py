# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Query-time foreign_block borrow — personal context, never persisted (A4).

When a user queries a HOSTED serving endpoint, their LOCAL node contributes a
gated + minimized projection of their personal memory as the endpoint's
``foreign_block``, at query time, over the node transport. The hosting node
fuses it into the answer via the P3 serving fusion but NEVER persists it — the
no-push rule already guarantees a foreign corpus is never ingested, and the
fusion is a verbatim text pass-through.

Gated at the SOURCE. The borrow runs through the LOCAL node's OWN
:class:`~axiom.memory.serving.ServingGate` (via
:class:`~axiom.memory.serving_service.MemoryServingService`), so ``vault`` /
secret / cross-account / tier-restricted content is already filtered before
anything leaves the node — the same one-door-out boundary P3 built, reused
whole. A default-deny :class:`~axiom.memory.sync.node.PeerAuthorizer` gates the
requesting hosting endpoint: a node never serves its personal memory to a peer
it has not declared.

Minimized. The projection is top-``k`` + character-budget capped — a compact
block, not a memory dump (security doc §2 minimum-necessary serving). A true
salience mechanism (the main session's #21 retrieval-refinement) is future; the
size/top-k cap is the acceptable MINIMIZE for now (see
``docs/working/cross-mem-a4-open-questions.md``).

Transport seam. A4 reuses A3's :class:`~axiom.memory.sync.transport.NodeTransport`
+ ``PeerAuthorizer`` wholesale for the session-shard sync-home (that leg IS A3
sync). The query-time borrow is a *synchronous request/response* — a shape A3's
one-way ``send``/``poll``/``ack`` channel does not fit — so it rides a sibling
seam built in the identical spirit: :class:`BorrowTransport` (the contract),
:class:`LoopbackBorrowTransport` (the in-process double the tests drive),
:class:`A2ABorrowTransport` (the real wire, a raising seam until OQ-A3-1 lands),
and :func:`borrow_transport` (the factory).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from axiom.memory.serving import ConsumerCoordinate, ServableItem
from axiom.memory.serving_service import EXCLUSION_MARKER, MemoryServingService
from axiom.memory.sync.node import NodeCoordinate, PeerAuthorizer

# Minimize defaults: a compact block sized to a hosted prompt, not a dump.
DEFAULT_BORROW_K = 3
DEFAULT_BORROW_CHAR_BUDGET = 1024


# ---------------------------------------------------------------------------
# Request / response envelopes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BorrowRequest:
    """A hosting endpoint's query-time request for a peer's personal projection.

    ``consumer`` is the hosting endpoint *as a memory consumer* of the peer's
    store — its ``(harness, account, deployment_tier)`` is the exposure domain
    the peer's serving gate evaluates against. ``requester_node_id`` names the
    hosting node so the peer's default-deny authorizer can refuse an undeclared
    endpoint before serving anything.
    """

    query: str
    consumer: ConsumerCoordinate
    requester_node_id: str
    k: int = DEFAULT_BORROW_K
    char_budget: int = DEFAULT_BORROW_CHAR_BUDGET


@dataclass(frozen=True)
class BorrowResponse:
    """The gated + minimized projection the peer returns over the transport.

    ``block`` is the rendered ``foreign_block`` text the hosting node fuses
    verbatim (never parsed, never ingested). ``entries`` carries the
    ``(fragment_id, text)`` pairs for audit/labeling. Nothing here is written to
    the hosting store.
    """

    entries: tuple[tuple[str, str], ...]
    block: str
    served: int
    denied: int
    degraded: bool
    source_node_id: str


# ---------------------------------------------------------------------------
# The seam
# ---------------------------------------------------------------------------


class BorrowUnavailable(RuntimeError):
    """Raised when a borrow cannot be routed to a reachable peer node."""


@runtime_checkable
class BorrowTransport(Protocol):
    """The synchronous request/response contract for a query-time borrow.

    The real A2A wire and the in-process double both satisfy this shape, so the
    hosting endpoint is written once against the seam.
    """

    def request(self, peer_node_id: str, request: BorrowRequest) -> BorrowResponse:
        """Ask ``peer_node_id`` for its gated foreign_block, and return it."""
        ...


Responder = Callable[[BorrowRequest], BorrowResponse]


@dataclass
class LoopbackBorrowTransport:
    """In-process borrow double: a registry of ``node_id -> responder``.

    Both nodes attach to one instance (as A3's ``LoopbackTransport`` broker is
    shared). :meth:`request` dispatches synchronously to the peer's registered
    responder — the real serialize → gate → minimize → render → fuse path end to
    end, with only the machine boundary simulated.
    """

    _responders: dict[str, Responder] = field(default_factory=dict)

    def register(self, node_id: str, responder: Responder) -> None:
        self._responders[node_id] = responder

    def request(self, peer_node_id: str, request: BorrowRequest) -> BorrowResponse:
        responder = self._responders.get(peer_node_id)
        if responder is None:
            raise BorrowUnavailable(
                f"no borrow responder registered for peer {peer_node_id!r} "
                "(the peer node is not reachable on this in-process broker)"
            )
        return responder(request)


class A2ABorrowTransport:
    """The real query-time borrow over the federation ``axiom://`` A2A hop.

    Drops in once the federation layer exposes a node-to-node A2A message
    channel (OQ-A3-1, ``docs/working/cross-mem-a3-federation-assessment.md``).
    Until then :meth:`request` raises, so no code path can mistake the seam for a
    live wire.
    """

    def __init__(self, endpoint: str = "", federation: object | None = None) -> None:
        self.endpoint = endpoint
        self.federation = federation

    def request(self, peer_node_id: str, request: BorrowRequest) -> BorrowResponse:
        raise NotImplementedError(
            "the node-to-node A2A borrow wire is not implemented yet "
            "(federation exposes identity + axiom:// + authority policy, but no "
            "reconciliation message channel — see OQ-A3-1). Use the "
            "LoopbackBorrowTransport double until the wire lands."
        )


def _federation_has_a2a_wire(federation: object | None) -> bool:
    if federation is None:
        return False
    return bool(getattr(federation, "a2a_message_channel", None))


def borrow_transport(
    *,
    federation: object | None = None,
    shared: LoopbackBorrowTransport | None = None,
) -> BorrowTransport:
    """Return the real A2A borrow transport when the federation wire is present,
    else the in-process double (the state today)."""
    if _federation_has_a2a_wire(federation):
        return A2ABorrowTransport(federation=federation)
    return shared if shared is not None else LoopbackBorrowTransport()


# ---------------------------------------------------------------------------
# Minimize + render
# ---------------------------------------------------------------------------


def minimize_items(
    items: list[ServableItem], *, k: int, char_budget: int
) -> list[ServableItem]:
    """Cap a gate-approved projection to a compact block (top-``k`` + size).

    Retrieval already ranked the items, so top-``k`` keeps the most relevant.
    The character budget then bounds total size for the destination prompt. The
    top item is never dropped even if it alone exceeds the budget — an empty
    block is useless, and the gate has already made every item safe to serve.

    This is the acceptable MINIMIZE for now: a size/top-k cap, not a salience
    model (#21 retrieval-refinement is future — see the A4 open questions).
    """
    chosen: list[ServableItem] = []
    used = 0
    for item in items[: max(k, 0)]:
        cost = len((item.text or "").strip())
        if chosen and used + cost > char_budget:
            break
        chosen.append(item)
        used += cost
        if used >= char_budget:
            break
    return chosen


def render_foreign_block(items: list[ServableItem], *, source_node_id: str) -> str:
    """Render a labeled, attributable ``foreign_block`` (byte-stable ordering).

    Carries the cooperative transcript-exclusion marker (security doc §3) and
    names the source node so a leak stays attributable in audit. The block is
    passed verbatim to the hosting node's fusion — never re-parsed, never
    ingested.
    """
    if not items:
        return ""
    lines = [
        EXCLUSION_MARKER,
        f"=== PERSONAL MEMORY (borrowed via axiom://{source_node_id}) ===",
        "",
    ]
    for item in sorted(items, key=lambda i: i.fragment_id):
        lines.append(f"- {(item.text or '').strip()}")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# The source-node responder
# ---------------------------------------------------------------------------


@dataclass
class ForeignBlockBorrower:
    """The LOCAL (peer) node's responder to a hosting endpoint's borrow request.

    Reuses the P3 :class:`MemoryServingService` (recall → gate → serialize) over
    this node's OWN store, so the whole serving boundary — vault-never,
    secret→vault, unlabeled-deny, cross-account, deployment-tier — is enforced at
    the source before the projection leaves. The default-deny authorizer refuses
    an undeclared hosting endpoint.
    """

    serving: MemoryServingService
    node: NodeCoordinate
    authorizer: PeerAuthorizer
    recall_agent: str = "axi"

    def respond(self, request: BorrowRequest) -> BorrowResponse:
        # Default-deny at the source: never serve personal memory to a peer this
        # node has not declared (symmetric with A3's sync authorizer).
        self.authorizer.require(request.requester_node_id)

        result = self.serving.serve(
            request.query,
            consumer=request.consumer,
            recall_agent=self.recall_agent,
            k=max(request.k, 1),
        )
        minimized = minimize_items(
            result.items, k=request.k, char_budget=request.char_budget
        )
        block = render_foreign_block(minimized, source_node_id=self.node.node_id)
        entries = tuple((i.fragment_id, i.text) for i in minimized)
        return BorrowResponse(
            entries=entries,
            block=block,
            served=len(minimized),
            denied=len(result.denials),
            degraded=result.degraded,
            source_node_id=self.node.node_id,
        )


__all__ = [
    "DEFAULT_BORROW_CHAR_BUDGET",
    "DEFAULT_BORROW_K",
    "A2ABorrowTransport",
    "BorrowRequest",
    "BorrowResponse",
    "BorrowTransport",
    "BorrowUnavailable",
    "ForeignBlockBorrower",
    "LoopbackBorrowTransport",
    "borrow_transport",
    "minimize_items",
    "render_foreign_block",
]
