# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The audit's numbers must come from the console's own receipts, and
the audit must be able to report zero (a fully-evidenced fleet)."""

from __future__ import annotations

from axiom.bench.unproven_green import audit


def _status(nodes):
    return {"generated_at": "2026-09-21T18:00:00+00:00", "nodes": nodes}


def _node(node_id, kinds):
    return {"node_id": node_id, "rollup": "unproven", "kinds": kinds}


def test_counts_refused_kinds_and_unmeasured_services():
    status = _status([
        _node("n1", {
            "heartbeat": {"status": "green", "evidence": "received within cadence window"},
            "service_health": {
                "status": "unproven",
                "evidence": "claimed without observed latency or degraded: API Server, Router",
            },
            "backup": {"status": "green", "evidence": "artifact x, size_bytes=1"},
        }),
    ])
    result = audit(status)
    assert result["kinds_total"] == 3
    assert result["kinds_green_with_evidence"] == 2
    assert result["kinds_refused"] == 1
    assert result["services_claimed_without_measurement"] == 2
    assert 0 < result["unproven_green_share"] < 1


def test_fully_evidenced_fleet_reports_zero_the_audit_can_pass():
    status = _status([
        _node("n1", {
            "heartbeat": {"status": "green", "evidence": "received within cadence window"},
        }),
    ])
    result = audit(status)
    assert result["unproven_green_share"] == 0.0
    assert result["services_claimed_without_measurement"] == 0


def test_empty_fleet_is_zero_not_crash():
    assert audit(_status([]))["kinds_total"] == 0
