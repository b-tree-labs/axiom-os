# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the single-node leader lease state machine.

Per spec-axiom-schedule §1: acquire / renew / release semantics on the
in-memory lease (PULSE-1). The Postgres-backed variant lands in a
follow-up integration test once the DB harness is wired.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from axiom.extensions.builtins.schedule.lease import LeaseManager


T0 = datetime(2026, 5, 31, 12, 0, 0, tzinfo=UTC)


def test_initial_acquire_returns_lease():
    mgr = LeaseManager(node_id="node-a", ttl_seconds=30)
    lease = mgr.try_acquire(T0)
    assert lease is not None
    assert lease.node_id == "node-a"
    assert lease.expires_at == T0 + timedelta(seconds=30)
    assert mgr.held(T0)


def test_held_returns_false_after_expiry():
    mgr = LeaseManager(node_id="node-a", ttl_seconds=30)
    mgr.try_acquire(T0)
    assert mgr.held(T0 + timedelta(seconds=29))
    assert not mgr.held(T0 + timedelta(seconds=31))


def test_renew_extends_expiry():
    mgr = LeaseManager(node_id="node-a", ttl_seconds=30)
    mgr.try_acquire(T0)
    # Cross the renewal threshold (ttl/3 = 10s).
    mgr.maybe_renew(T0 + timedelta(seconds=11))
    assert mgr.held(T0 + timedelta(seconds=40))


def test_renew_no_op_before_threshold():
    mgr = LeaseManager(node_id="node-a", ttl_seconds=30)
    mgr.try_acquire(T0)
    # 5s elapsed; threshold is 10s. Should not extend expiry.
    mgr.maybe_renew(T0 + timedelta(seconds=5))
    assert not mgr.held(T0 + timedelta(seconds=31))


def test_release_clears_holding():
    mgr = LeaseManager(node_id="node-a", ttl_seconds=30)
    mgr.try_acquire(T0)
    mgr.release(T0 + timedelta(seconds=5))
    assert not mgr.held(T0 + timedelta(seconds=5))


def test_competing_node_cannot_acquire_held_lease():
    mgr_a = LeaseManager(node_id="node-a", ttl_seconds=30)
    mgr_b = LeaseManager(node_id="node-b", ttl_seconds=30)
    # Simulate shared state: same lease object held by mgr_a; mgr_b sees its own
    # empty view. In the real Postgres-backed variant this is one row; here
    # the in-memory shim demonstrates the contract — a competing engine cannot
    # take a held lease.
    mgr_a.try_acquire(T0)
    # Manually mirror mgr_a's lease into mgr_b to simulate a shared row read:
    mgr_b._current = mgr_a._current
    assert mgr_b.try_acquire(T0 + timedelta(seconds=1)) is None
