# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Reach is computed from declared edges — and when nothing is declared
it says ZERO DECLARED, never "unknown" (PRD R19)."""

from __future__ import annotations

import contextlib

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from axiom.extensions.builtins.fleet import store as fleet_store
from axiom.extensions.builtins.fleet.db_models import Base as FleetBase
from axiom.extensions.builtins.fleet.ingest import ingest_reports
from axiom.extensions.builtins.receipts.brief import OversightItem
from axiom.extensions.builtins.receipts.cases import compose_cases
from axiom.extensions.builtins.receipts.reach import compute_reach


@pytest.fixture()
def fleet():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    FleetBase.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)

    @contextlib.contextmanager
    def provider():
        s = factory()
        try:
            yield s
        finally:
            s.close()

    fleet_store.set_provider(provider)
    yield provider
    fleet_store.reset_provider()
    engine.dispose()


def _case(entity="node-a", site="site-a"):
    return compose_cases(
        [
            OversightItem(
                entity_kind="node",
                entity_id=entity,
                claim_kind="heartbeat",
                status="stale",
                evidence="no report",
                site=site,
            )
        ]
    )[0]


def _seed(provider, node="node-a", services=("api", "chat")):
    with provider() as s:
        ingest_reports(
            s,
            site="site-a",
            node_id=node,
            reporter_principal=f"@{node}-reporter:site-a",
            cadences={"heartbeat": 900, "service_health": 900},
            reports=[
                {"kind": "heartbeat", "payload": {}},
                {
                    "kind": "service_health",
                    "payload": {
                        "services": [
                            {"name": n, "status": "healthy", "latency_ms": 5} for n in services
                        ]
                    },
                },
            ],
        )
        s.commit()


def test_nothing_declared_reads_as_zero_declared_not_unknown(fleet):
    with fleet() as s:
        reach = compute_reach(s, _case())
    assert reach.declared is False
    assert "0 declared dependents" in reach.summary()
    # The word that makes a person do the work in their head:
    assert "unknown" not in reach.summary().lower()


def test_services_the_node_itself_declared_are_the_systems(fleet):
    _seed(fleet, services=("api", "chat", "recall"))
    with fleet() as s:
        reach = compute_reach(s, _case())
    assert reach.systems == ["api", "chat", "recall"]
    assert reach.declared is True
    assert "3 systems" in reach.summary()
    # Provenance travels with the number.
    assert any("service(s) this node reports" in src for src in reach.sources)


def test_the_reporting_principal_is_an_accountable_person(fleet):
    _seed(fleet)
    with fleet() as s:
        reach = compute_reach(s, _case())
    assert "@node-a-reporter:site-a" in reach.people
    assert any("principal reporting" in src for src in reach.sources)


def test_prior_deciders_are_carried_as_people_to_tell(fleet):
    _seed(fleet)
    with fleet() as s:
        reach = compute_reach(s, _case(), prior_deciders=["@ben:site-a"])
    assert "@ben:site-a" in reach.people
    assert any("prior decider" in src for src in reach.sources)


def test_edges_belong_to_the_entity_they_were_declared_on(fleet):
    _seed(fleet, node="node-a", services=("api",))
    _seed(fleet, node="node-b", services=("recall", "search"))
    with fleet() as s:
        a = compute_reach(s, _case("node-a"))
        b = compute_reach(s, _case("node-b"))
    assert a.systems == ["api"]
    assert b.systems == ["recall", "search"]


def test_an_entity_kind_with_no_wired_source_says_so(fleet):
    case = compose_cases(
        [
            OversightItem(
                entity_kind="twin",
                entity_id="t1",
                claim_kind="drift",
                status="unproven",
                evidence="-",
                site="site-a",
            )
        ]
    )[0]
    with fleet() as s:
        reach = compute_reach(s, case)
    assert reach.declared is False
    assert any("no edge source wired" in src for src in reach.sources)


def test_payload_is_json_ready_and_carries_its_provenance(fleet):
    import json

    _seed(fleet)
    with fleet() as s:
        payload = compute_reach(s, _case()).payload()
    round_tripped = json.loads(json.dumps(payload))
    assert set(round_tripped) == {
        "systems",
        "schedules",
        "people",
        "sources",
        "declared",
        "summary",
    }
    assert round_tripped["sources"]


def test_reach_counts_read_as_english():
    """The summary is a sentence a person reads, not a template dump.
    'person'/'people', not '1 person · 2 persons'."""
    from axiom.extensions.builtins.receipts.reach import Reach

    assert Reach(people=["@a"]).summary() == "1 person"
    assert Reach(people=["@a", "@b"]).summary() == "2 people"
    assert Reach(systems=["api"], people=["@a", "@b"]).summary() == "1 system · 2 people"


def test_reach_names_prior_deciders_readably_and_counts_them_once(fleet):
    """Reach lists PEOPLE. A person who decided twice is one person, and
    a person whose handle is a provider GUID still has to read as a
    person — the same label the decision record captured."""
    import contextlib

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from axiom.extensions.builtins.receipts.brief import OversightItem
    from axiom.extensions.builtins.receipts.cases import attach_reach, compose_cases
    from axiom.extensions.builtins.receipts.db_models import Base
    from axiom.extensions.builtins.receipts.verdicts import record_verdict

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    with contextlib.closing(session):
        case = compose_cases(
            [
                OversightItem(
                    entity_kind="node",
                    entity_id="node-a",
                    claim_kind="backup",
                    status="failed",
                    evidence="runner artifact missing",
                    next_action="check the artifact",
                    site="s",
                )
            ]
        )[0]
        handle = "@78f6cda0-4068-4c73-b8f7-53210aee4379:s"
        record_verdict(session, case, chosen="hold", decider=handle, decider_label="Lee, Robin")
        record_verdict(
            session, case, chosen="acknowledge", decider=handle, decider_label="Lee, Robin"
        )
        session.commit()

        with fleet() as fsession:
            [decorated] = attach_reach(fsession, session, [case])
        people = decorated.reach["people"]
        assert "Lee, Robin" in people
        assert not any("78f6cda0" in p for p in people)
        assert people.count("Lee, Robin") == 1  # one person, two decisions
    engine.dispose()


# --- One name for one concept (2026-09-26) ---


def test_the_payload_is_keyed_reach_with_blast_as_a_temporary_alias():
    """The concept had two names in two governing documents:
    spec-case-construct §2 defines **Reach**; the terminology ledger admitted
    "blast radius (borderline; vivid enough to pass)". Reach won.

    The alias exists only because the READER cannot be renamed here: the prop
    lives in appkit's shared CaseCard, consumed through a `file:` dependency
    on another repository. Both keys carry the same object, so no surface can
    see them disagree.
    """
    from axiom.extensions.builtins.receipts.brief import OversightItem
    from axiom.extensions.builtins.receipts.cases import case_payload, compose_cases

    [case] = compose_cases(
        [
            OversightItem(
                entity_kind="node",
                entity_id="node-a",
                claim_kind="heartbeat",
                status="stale",
                evidence="nothing arrived",
                site="local",
            )
        ]
    )
    payload = case_payload(case)
    assert "reach" in payload, "reach is the canonical key"
    assert payload["blast"] is payload["reach"], (
        "the alias must be the same object, so the two names cannot disagree"
    )


def test_the_blast_alias_is_removed_once_appkit_stops_reading_it():
    """A deprecation with no removal condition is a permanent second name.

    This fails the moment APPKIT stops mentioning `blast`, which is the signal
    the alias has done its job and should come out of `case_payload` — so the
    cleanup is prompted rather than remembered.

    IT WATCHES APPKIT'S OWN BUNDLE, not this console's. It used to read the
    console's bundle, which worked only while appkit was vendored into it: the
    day the console started importing appkit from the node instead, the string
    left this repo's build and the tripwire fired — announcing that appkit had
    stopped reading `blast` when nothing about appkit had changed at all.

    A tripwire that watches a proxy fires when the proxy moves. This one reads
    the thing it is actually about.
    """
    from pathlib import Path

    try:
        import axiom_appkit
    except ImportError:
        return  # appkit not installed here; nothing to gate on

    # EVERY served chunk, not just `appkit.js`. That file is a ~10 KB
    # re-export facade; the code lives in the `runtime-*.js` chunk it imports.
    # Reading the facade alone found nothing and would have reported that
    # appkit had dropped `blast` while `CaseCard` was still rendering it from
    # a live prop — a removal instruction generated by looking at the wrong
    # file.
    static = Path(axiom_appkit.__file__).resolve().parent / "static"
    chunks = sorted(static.glob("*.js"))
    if not chunks:
        return  # no built bundle in this checkout; nothing to gate on

    still_reads_blast = any(
        "blast" in c.read_text(encoding="utf-8", errors="ignore") for c in chunks
    )
    assert still_reads_blast, (
        "appkit no longer reads 'blast', so the deprecated alias in "
        "case_payload has outlived its reason — remove it and delete this test"
    )
