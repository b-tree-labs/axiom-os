# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The fleet status view: per-node per-kind evaluations + worst-wins
rollup, staleness from declared cadences, site scoping at the query."""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from axiom.extensions.builtins.fleet import store
from axiom.extensions.builtins.fleet.db_models import Base
from axiom.extensions.builtins.fleet.ingest import ingest_reports
from axiom.extensions.builtins.fleet.view import fleet_status

NOW = datetime(2026, 9, 21, 12, 0, 0, tzinfo=UTC)


@pytest.fixture()
def session_factory():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)

    @contextlib.contextmanager
    def provider():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    store.set_provider(provider)
    yield factory
    store.reset_provider()
    engine.dispose()


def _push(s, node, site, kind, payload, at, cadences=None):
    return ingest_reports(
        s,
        site=site,
        node_id=node,
        reporter_principal=f"@{node}:{site}",
        reports=[{"kind": kind, "payload": payload}],
        now=at,
        cadences=cadences,
    )


def test_view_rolls_up_worst_and_carries_evidence(session_factory):
    with store.session_scope() as s:
        _push(
            s,
            "n1",
            "site-a",
            "heartbeat",
            {},
            NOW - timedelta(minutes=5),
            cadences={"heartbeat": 900, "service_health": 900},
        )
        _push(
            s,
            "n1",
            "site-a",
            "service_health",
            {"services": [{"name": "api", "status": "unhealthy"}]},
            NOW - timedelta(minutes=5),
        )
        s.commit()
        out = fleet_status(s, now=NOW)
    (node,) = out["nodes"]
    assert node["node_id"] == "n1"
    assert node["rollup"] == "failed"
    assert node["kinds"]["heartbeat"]["status"] == "green"
    assert node["kinds"]["service_health"]["status"] == "failed"
    assert "api" in node["kinds"]["service_health"]["evidence"]
    assert node["kinds"]["service_health"]["signature_state"] == "unverified"
    # The declared cadence rides the payload so no client hardcodes it (R3).
    assert node["kinds"]["heartbeat"]["cadence_seconds"] == 900
    # Undeclared kinds carry the server-side default, still server-declared.
    assert node["kinds"]["service_health"]["cadence_seconds"] == 900


def test_view_stale_heartbeat_by_declared_cadence(session_factory):
    with store.session_scope() as s:
        _push(
            s,
            "n1",
            "site-a",
            "heartbeat",
            {},
            NOW - timedelta(hours=2),
            cadences={"heartbeat": 900},
        )
        s.commit()
        out = fleet_status(s, now=NOW)
    assert out["nodes"][0]["rollup"] == "stale"


def test_view_node_with_no_reports_is_unknown_never_green(session_factory):
    from axiom.extensions.builtins.fleet.db_models import FleetNode

    with store.session_scope() as s:
        s.add(FleetNode(node_id="silent", site="site-a", enrolled_at=NOW))
        s.commit()
        out = fleet_status(s, now=NOW)
    assert out["nodes"][0]["rollup"] == "unknown"


def test_view_site_filter_scopes_rows(session_factory):
    with store.session_scope() as s:
        _push(s, "n1", "site-a", "heartbeat", {}, NOW)
        _push(s, "n2", "site-b", "heartbeat", {}, NOW)
        s.commit()
        out = fleet_status(s, sites=["site-a"], now=NOW)
    assert [n["node_id"] for n in out["nodes"]] == ["n1"]


def test_view_archived_nodes_excluded_by_default(session_factory):
    from axiom.extensions.builtins.fleet.db_models import FleetNode

    with store.session_scope() as s:
        _push(s, "n1", "site-a", "heartbeat", {}, NOW)
        s.commit()
        s.get(FleetNode, "n1").archived_at = NOW
        s.commit()
        out = fleet_status(s, now=NOW)
    assert out["nodes"] == []
