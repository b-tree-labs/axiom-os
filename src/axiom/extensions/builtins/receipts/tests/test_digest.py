# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Arrival: what reaches somebody who is not looking at this.

The journey map scored arrival 9 and found nothing in it — every other
stage was gated on a stage that did not exist. These tests are about the
three rules that keep the fix from becoming the problem it was built
against.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from axiom.extensions.builtins.fleet import store as fleet_store
from axiom.extensions.builtins.fleet.db_models import Base as FleetBase
from axiom.extensions.builtins.fleet.ingest import ingest_reports
from axiom.extensions.builtins.receipts.brief import compose_brief, fleet_source
from axiom.extensions.builtins.receipts.db_models import Base as RcptBase
from axiom.extensions.builtins.receipts.digest import ALL_CLEAR, compose_digest

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
CADENCES = {"heartbeat": 900, "service_health": 900}


@contextlib.contextmanager
def _fleet(*nodes_ago):
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
    with fleet_store.session_scope() as s:
        for node, ago in nodes_ago:
            ingest_reports(
                s,
                site="local",
                node_id=node,
                reporter_principal=f"@{node}:local",
                now=NOW - timedelta(seconds=ago),
                cadences=CADENCES,
                reports=[
                    {"kind": "heartbeat", "payload": {}},
                    {
                        "kind": "service_health",
                        "payload": {
                            "services": [{"name": "api", "status": "healthy", "latency_ms": 9}]
                        },
                    },
                ],
            )
        s.commit()
    try:
        yield
    finally:
        fleet_store.reset_provider()
        engine.dispose()


@pytest.fixture()
def receipts():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    RcptBase.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    with contextlib.closing(s):
        yield s
    engine.dispose()


def _digest(receipts, *nodes_ago, where=""):
    with _fleet(*nodes_ago):
        with fleet_store.session_scope() as fs:
            items = fleet_source(fs, now=NOW)
            # this_node mirrors the skill, which passes the node it runs
            # on; without it no handling is computed and the digest has
            # no "why" to carry.
            brief = compose_brief(
                receipts,
                items,
                site="local",
                snapshot=False,
                now=NOW,
                this_node="the-console",
                fleet_session=fs,
            )
    return compose_digest(brief, where=where)


def test_one_message_for_the_day_not_one_per_case(receipts):
    """The construct is built against alert fatigue. One interruption
    describing three cases is the point; three interruptions is the
    problem it was supposed to solve."""
    d = _digest(receipts, ("node-a", 9000), ("node-b", 9000), ("node-c", 9000))
    assert d.waiting == 3
    assert d.subject == "3 cases waiting on you · local"
    assert d.body.count("stopped reporting") == 3


def test_a_quiet_day_still_arrives(receipts):
    """A digest that only arrives when something is wrong makes 'nothing
    is wrong' indistinguishable from 'the digest is broken'."""
    d = _digest(receipts, ("node-a", 0))
    assert d.waiting == 0
    assert d.needs_anyone is False
    assert ALL_CLEAR in d.body
    # ...and it still says what passed, so the all-clear has evidence.
    assert "check" in d.body


def test_it_says_WHY_a_person_is_needed_not_the_whole_case(receipts):
    """Evidence, reach and derivations live on the surface. Carrying them
    here makes a message nobody reads and a second place to drift."""
    d = _digest(receipts, ("node-a", 9000))
    assert "node-a stopped reporting" in d.body
    assert "only node-a can put it right from its own side" in d.body
    # NOT the evidence, the reach, or the arithmetic.
    assert "seconds" not in d.body
    assert "affected" not in d.body


def test_the_cap_is_on_what_is_shown_never_on_what_is_counted(receipts):
    d = _digest(receipts, *[(f"node-{n}", 9000) for n in "abcde"])
    assert d.waiting == 5, "the count is the true count"
    assert d.body.count("stopped reporting") == 3, "the list is capped"
    assert "2 more not shown" in d.body


def test_a_link_is_omitted_rather_than_faked(receipts):
    """A digest pointing somewhere wrong is worse than one pointing
    nowhere."""
    without = _digest(receipts, ("node-a", 9000))
    assert "http" not in without.body
    with_link = _digest(receipts, ("node-a", 9000), where="https://node.example/receipts/")
    assert "https://node.example/receipts/" in with_link.body


def test_composing_the_arrival_message_does_not_consume_deltas(receipts):
    """Reading is not deciding. A digest that advanced the trust-delta
    baseline would make the next one silently different for having been
    sent."""
    from axiom.extensions.builtins.receipts.db_models import BriefSnapshot

    _digest(receipts, ("node-a", 9000))
    assert receipts.query(BriefSnapshot).count() == 0


def test_a_send_with_nobody_to_reach_is_refused():
    from axiom.extensions.builtins.receipts.skills.digest import run

    result = run({"site": "local"})
    assert not result.ok
    assert "recipient is required" in result.errors[0]


def test_dry_run_shows_what_would_arrive_without_arriving(receipts):
    """How you look at the message before arming a schedule with it."""
    from axiom.extensions.builtins.receipts import store as rcpt_store
    from axiom.extensions.builtins.receipts.skills.digest import run

    @contextlib.contextmanager
    def provider():
        yield receipts

    rcpt_store.set_provider(provider)
    try:
        with _fleet(("node-a", 9000)):
            result = run({"site": "local", "dry_run": True})
    finally:
        rcpt_store.reset_provider()
    assert result.ok
    assert result.value["sent"] is False
    assert result.value["digest"]["waiting"] == 1


def test_a_case_waiting_on_a_click_does_not_read_like_one_waiting_on_judgement(receipts):
    """Found by looking at the real digest. A case nothing is stopping —
    a declared fix that applies here — carries no "why", because nobody's
    judgement is needed. Left bare it reads identically to a case that
    genuinely needs somebody, which is the distinction the whole handling
    model exists to draw."""
    from dataclasses import replace

    from axiom.extensions.builtins.receipts.brief import Brief
    from axiom.extensions.builtins.receipts.cases import Case
    from axiom.extensions.builtins.receipts.digest import compose_digest

    ready = Case(
        case_id="c-1",
        site="local",
        entity_kind="node",
        entity_id="this-node",
        title="this-node stopped reporting",
        severity="stale",
        handling={
            "can_run": True,
            "needs_person": False,
            "because": "",
            "fix_summary": "Make this node report now.",
            "runs": "fleet.report",
            "reason": "",
        },
    )
    brief = Brief(
        generated_at=NOW.isoformat(),
        site="local",
        focus=None,
        needs_you=[],
        trust_deltas=[],
        quiet_line="1 check passed quietly.",
        counts={"cases": 1, "green": 1},
        cases=[ready],
    )
    body = compose_digest(brief).body
    assert "A fix is ready: make this node report now." in body

    # And a case that DOES need judgement still says so, differently.
    needs = replace(
        ready,
        handling={
            **ready.handling,
            "can_run": False,
            "needs_person": True,
            "because": "The fix already ran and this is still happening.",
        },
    )
    other = compose_digest(replace(brief, cases=[needs])).body
    assert "The fix already ran" in other
    assert "A fix is ready" not in other
