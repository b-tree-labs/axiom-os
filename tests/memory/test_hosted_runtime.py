# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A4 unit seam — the borrow transport double, minimize cap, and shard TTL.

The fine-grained companion to ``test_hosted_chat.py`` (the acceptance gate).
Mirrors A3's split (``test_sync_node_transport.py`` beside the lock-step gate):
the query-time borrow rides a request/response transport seam built in the same
spirit as A3's async ``LoopbackTransport`` — an in-process double behind a
factory, with a raising real-A2A seam so nothing mistakes it for a live wire.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from axiom.memory.hosted import (
    DEFAULT_BORROW_CHAR_BUDGET,
    DEFAULT_BORROW_K,
    A2ABorrowTransport,
    BorrowRequest,
    BorrowResponse,
    BorrowUnavailable,
    LoopbackBorrowTransport,
    SessionShard,
    borrow_transport,
    minimize_items,
)
from axiom.memory.serving import ConsumerCoordinate, ServableItem


def _item(fid: str, text: str) -> ServableItem:
    return ServableItem(
        fragment_id=fid, cognitive_type="semantic", visibility="public",
        classification={"level": "unclassified"}, account="acct-a", text=text,
    )


def _consumer() -> ConsumerCoordinate:
    return ConsumerCoordinate(
        principal="@a:home", harness="axiom://node-h", account="acct-a",
        compatible_accounts=frozenset({"acct-a"}),
    )


# ---------------------------------------------------------------------------
# The request/response transport seam
# ---------------------------------------------------------------------------


class TestBorrowTransportSeam:
    def test_factory_returns_loopback_double_without_federation_wire(self):
        wire = borrow_transport()
        assert isinstance(wire, LoopbackBorrowTransport)

    def test_factory_passes_through_shared_double(self):
        shared = LoopbackBorrowTransport()
        assert borrow_transport(shared=shared) is shared

    def test_loopback_dispatches_to_registered_responder(self):
        wire = LoopbackBorrowTransport()
        captured = {}

        def responder(req: BorrowRequest) -> BorrowResponse:
            captured["query"] = req.query
            return BorrowResponse(
                entries=(("f1", "hi"),), block="=== X ===\n- hi",
                served=1, denied=0, degraded=False, source_node_id="node-src",
            )

        wire.register("node-src", responder)
        resp = wire.request(
            "node-src",
            BorrowRequest(query="q", consumer=_consumer(), requester_node_id="node-h"),
        )
        assert captured["query"] == "q"
        assert resp.served == 1 and resp.source_node_id == "node-src"

    def test_request_to_unregistered_peer_raises(self):
        wire = LoopbackBorrowTransport()
        with pytest.raises(BorrowUnavailable):
            wire.request(
                "node-missing",
                BorrowRequest(query="q", consumer=_consumer(), requester_node_id="node-h"),
            )

    def test_real_a2a_transport_is_a_raising_seam(self):
        # Honest seam: the real inter-node borrow wire is not implemented (the
        # federation A2A message channel is OQ-A3-1). It must never pretend.
        real = A2ABorrowTransport()
        with pytest.raises(NotImplementedError):
            real.request(
                "node-src",
                BorrowRequest(query="q", consumer=_consumer(), requester_node_id="node-h"),
            )


# ---------------------------------------------------------------------------
# Minimize — top-k + size cap (a compact block, not a memory dump)
# ---------------------------------------------------------------------------


class TestMinimize:
    def test_top_k_cap(self):
        items = [_item(f"f{i}", f"note {i}") for i in range(10)]
        chosen = minimize_items(items, k=3, char_budget=10_000)
        assert len(chosen) == 3
        assert [i.fragment_id for i in chosen] == ["f0", "f1", "f2"]  # rank order

    def test_char_budget_cap(self):
        items = [_item(f"f{i}", "x" * 100) for i in range(10)]
        chosen = minimize_items(items, k=10, char_budget=250)
        assert 1 <= len(chosen) <= 3  # budget-bounded, not all ten

    def test_single_oversized_item_still_served(self):
        # The top item is never dropped even if it alone exceeds the budget —
        # a false negative costs recall, but an empty block is useless.
        items = [_item("f0", "y" * 500)]
        chosen = minimize_items(items, k=5, char_budget=50)
        assert [i.fragment_id for i in chosen] == ["f0"]

    def test_empty_in_empty_out(self):
        assert minimize_items([], k=3, char_budget=100) == []

    def test_defaults_are_sane(self):
        assert DEFAULT_BORROW_K >= 1
        assert DEFAULT_BORROW_CHAR_BUDGET >= 256


# ---------------------------------------------------------------------------
# Session shard TTL — deterministic via the injectable clock
# ---------------------------------------------------------------------------


class TestSessionShardTTL:
    def test_expiry_is_computed_from_created_at_plus_ttl(self):
        t0 = datetime(2026, 7, 15, 12, 0, 0, tzinfo=UTC)
        shard = SessionShard(
            principal="@a:home", account="acct-a", session_id="s://1",
            hosting_node_id="node-h", created_at=t0, ttl_seconds=3600,
        )
        assert shard.expires_at == t0 + timedelta(seconds=3600)
        assert not shard.is_expired(t0)
        assert not shard.is_expired(t0 + timedelta(seconds=3599))
        assert shard.is_expired(t0 + timedelta(seconds=3600))  # boundary = expired
        assert shard.is_expired(t0 + timedelta(seconds=7200))
