# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The hosted serving endpoint — borrow, fuse, never persist (A4).

Ties the two A4 capabilities to a single query surface. On a hosted recall the
endpoint serves its OWN corpus (hosting-node-authoritative) and, over the node
transport, borrows the querying user's personal ``foreign_block`` from their
LOCAL node — gated + minimized at the source. It fuses the two through the P3
serving fusion and returns the answer. The borrow is NEVER persisted: fusion is
a verbatim pass-through, and the endpoint asserts its store gained no fragments
across the call (structural, symmetric with P3's ``assert_no_push``).

Session continuity (the transient shard) is owned by
:class:`~axiom.memory.hosted.shard.SessionShardManager`; this endpoint holds the
query-time borrow + fusion path.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from axiom.memory.hosted.borrow import (
    DEFAULT_BORROW_CHAR_BUDGET,
    DEFAULT_BORROW_K,
    BorrowRequest,
    BorrowResponse,
    BorrowTransport,
)
from axiom.memory.serving import ConsumerCoordinate
from axiom.memory.serving_service import MemoryServingService, ServedResult
from axiom.memory.sync.node import NodeCoordinate


class HostedPersistError(RuntimeError):
    """Raised if a hosted recall changed the hosting store — the borrow must
    never persist. A structural guard, not an expected path."""


@dataclass(frozen=True)
class HostedAnswer:
    """What one :meth:`HostedEndpoint.recall` produced."""

    block: str
    foreign_block: str
    own: ServedResult
    borrowed: BorrowResponse
    principal: str
    query: str


@dataclass
class HostedEndpoint:
    """A hosting node's query surface: serve own corpus + borrow + fuse."""

    node: NodeCoordinate
    composition: Any
    serving: MemoryServingService
    borrow_transport: BorrowTransport
    now_fn: Callable[[], datetime]
    borrow_k: int = DEFAULT_BORROW_K
    borrow_char_budget: int = DEFAULT_BORROW_CHAR_BUDGET
    own_k: int = 5
    own_recall_agent: str = "axi"

    def _consumer(self, principal: str, account: str) -> ConsumerCoordinate:
        """The hosting endpoint as a memory consumer for this user's session.

        ``(harness, account, deployment_tier)`` is the endpoint's exposure
        domain — the peer's serving gate evaluates the borrow against it, so a
        remote-tier or wrong-account endpoint is filtered at the source.
        """
        return ConsumerCoordinate(
            principal=principal,
            harness=self.node.uri,
            account=account,
            deployment_tier=self.node.deployment_tier,
            compatible_accounts=frozenset({account}),
        )

    def recall(
        self, query: str, *, principal: str, account: str, borrow_from: NodeCoordinate
    ) -> HostedAnswer:
        """Answer a hosted query: own corpus + borrowed personal foreign_block.

        The borrow rides the transport to the user's LOCAL node, which gates +
        minimizes at the source. The hosting store is never written — asserted.
        """
        before = len(self.composition.artifact_registry.list(kind="fragment"))
        consumer = self._consumer(principal, account)

        own = self.serving.serve(
            query, consumer=consumer, k=self.own_k, recall_agent=self.own_recall_agent,
        )
        request = BorrowRequest(
            query=query,
            consumer=consumer,
            requester_node_id=self.node.node_id,
            k=self.borrow_k,
            char_budget=self.borrow_char_budget,
        )
        borrowed = self.borrow_transport.request(borrow_from.node_id, request)

        block = self.serving.fuse_side_by_side(own, borrowed.block)

        after = len(self.composition.artifact_registry.list(kind="fragment"))
        if after != before:
            raise HostedPersistError(
                f"hosted recall changed the hosting store ({before} -> {after} "
                "fragments): the borrowed foreign_block must never persist "
                "(no-push, ADR-087 D7)"
            )
        return HostedAnswer(
            block=block,
            foreign_block=borrowed.block,
            own=own,
            borrowed=borrowed,
            principal=principal,
            query=query,
        )


__all__ = [
    "HostedAnswer",
    "HostedEndpoint",
    "HostedPersistError",
]
