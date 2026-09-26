# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Drilling into a name shows what is RECORDED about it, or nothing.

Founder direction (2026-09-25): every identified entity needs a way to
learn more. The trap is obvious — a profile page is the easiest place in
a product to start inventing — so the rule is that a profile is built
only from stored facts, and an entity nobody has recorded anything about
has no profile rather than an empty one.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from axiom.extensions.builtins.fleet.db_models import Base as FleetBase
from axiom.extensions.builtins.fleet.ingest import ingest_reports
from axiom.extensions.builtins.receipts.brief import OversightItem
from axiom.extensions.builtins.receipts.cases import compose_cases
from axiom.extensions.builtins.receipts.db_models import Base as RcptBase
from axiom.extensions.builtins.receipts.entities import profile_for
from axiom.extensions.builtins.receipts.verdicts import record_verdict, stamp_outcomes

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
CADENCES = {"heartbeat": 900, "service_health": 900}


def _mem(base):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine)()


@pytest.fixture()
def fleet():
    engine, s = _mem(FleetBase)
    with contextlib.closing(s):
        ingest_reports(
            s,
            site="local",
            node_id="node-a",
            reporter_principal="@mech:local",
            now=NOW - timedelta(hours=2),
            cadences=CADENCES,
            reports=[
                {"kind": "heartbeat", "payload": {}},
                {
                    "kind": "service_health",
                    "payload": {
                        "services": [{"name": "api", "status": "healthy", "latency_ms": 12}]
                    },
                },
            ],
        )
        s.commit()
        yield s
    engine.dispose()


@pytest.fixture()
def receipts():
    engine, s = _mem(RcptBase)
    with contextlib.closing(s):
        yield s
    engine.dispose()


def _case(entity="node-a"):
    return compose_cases(
        [
            OversightItem(
                entity_kind="node",
                entity_id=entity,
                claim_kind="heartbeat",
                status="stale",
                evidence="e",
                site="local",
            )
        ]
    )[0]


def test_a_node_is_described_by_its_own_reports(fleet):
    p = profile_for("node", "node-a", fleet_session=fleet, now=NOW)
    assert p.known
    facts = {f.label: f.value for f in p.facts}
    assert facts["Site"] == "local"
    assert "heartbeat" in facts["Reports"] and "service health" in facts["Reports"]
    assert facts["Last heard from"] == "2 hours ago"
    assert facts["Reported by"] == "@mech:local"
    assert facts["Runs"] == "api"
    assert p.sources == ["what this node reported about itself"]


def test_a_person_is_described_by_what_they_decided(fleet, receipts):
    """Not who they are — the platform does not know that — but what they
    have decided and whether it worked. Read from append-only rows, so it
    cannot flatter anybody."""
    case = _case()
    record_verdict(
        receipts, case, chosen="hold", decider="@sam:local", decider_label="Sam Rivera", now=NOW
    )
    receipts.commit()
    stamp_outcomes(receipts, set(), now=NOW + timedelta(minutes=8))
    receipts.commit()
    record_verdict(
        receipts,
        case,
        chosen="fix",
        decider="@sam:local",
        decider_label="Sam Rivera",
        now=NOW + timedelta(minutes=30),
    )
    receipts.commit()

    p = profile_for("person", "@sam:local", receipts_session=receipts, now=NOW + timedelta(hours=1))
    assert p.title == "Sam Rivera"
    facts = {f.label: f.value for f in p.facts}
    assert facts["Decisions"] == "2"
    assert "1 fixed" in facts["Chose"] and "1 held" in facts["Chose"]
    assert facts["Cleared after"] == "8.0 min"

    # A reader clicks the NAME, so the name resolves too.
    by_name = profile_for("person", "Sam Rivera", receipts_session=receipts, now=NOW)
    assert by_name.known and by_name.entity_id == "Sam Rivera"


def test_an_unobserved_outcome_is_stated_not_left_blank(fleet, receipts):
    case = _case()
    record_verdict(receipts, case, chosen="hold", decider="@sam:local", now=NOW)
    receipts.commit()
    p = profile_for("person", "@sam:local", receipts_session=receipts, now=NOW)
    facts = {f.label: f.value for f in p.facts}
    assert facts["Cleared after"] == "not observed yet"


def test_a_service_carries_its_own_measurement(fleet):
    p = profile_for("service", "api", fleet_session=fleet, now=NOW)
    facts = {f.label: f.value for f in p.facts}
    assert facts["Runs on"] == "node-a"
    assert facts["Reported"] == "healthy"
    assert facts["Responded in"] == "12 ms"
    assert p.subtitle == "service on node-a"


def test_a_health_claim_with_no_time_says_it_is_unproven(receipts):
    engine, s = _mem(FleetBase)
    with contextlib.closing(s):
        ingest_reports(
            s,
            site="local",
            node_id="node-b",
            reporter_principal="@x:local",
            now=NOW,
            cadences=CADENCES,
            reports=[
                {
                    "kind": "service_health",
                    "payload": {"services": [{"name": "api", "status": "healthy"}]},
                }
            ],
        )
        s.commit()
        p = profile_for("service", "api", fleet_session=s, now=NOW)
        fact = next(f for f in p.facts if f.label == "Responded in")
        assert fact.value == "not measured"
        assert "unproven" in fact.note
    engine.dispose()


def test_a_thing_nobody_recorded_has_no_profile(fleet, receipts):
    """No empty page that apologises: the caller 404s and the surface then
    shows no drill-down at all."""
    assert not profile_for("node", "never-seen", fleet_session=fleet).known
    assert not profile_for("person", "@nobody:local", receipts_session=receipts).known
    assert not profile_for("service", "not-a-service", fleet_session=fleet).known
    # And a kind nobody has facts for at all.
    assert not profile_for("endpoint", "https://example.org", fleet_session=fleet).known
