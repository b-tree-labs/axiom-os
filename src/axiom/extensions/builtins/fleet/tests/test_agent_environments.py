# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A roster crosses machines the same way every other node fact does, and the
site on it is the one thing the pushing node does not choose.

These are the properties that make "agents I have rights to see" mean
something. Each is cheap to break and silent when broken.

Node, site and agent names here are deliberately generic. Axiom never names a
consumer domain, and this file is in the public mirror's surface — the mirror
guard catches it, which is how the first draft of these tests was caught
naming a real site.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from axiom.extensions.builtins.fleet.db_models import Base, FleetNode
from axiom.extensions.builtins.fleet.ingest import ingest_reports
from axiom.extensions.builtins.fleet.status import Status, evaluate_report
from axiom.extensions.builtins.fleet.view import agent_environments

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    try:
        yield s
    finally:
        s.close()


def _enrol(session, node_id: str, site: str, name: str) -> None:
    session.add(
        FleetNode(node_id=node_id, site=site, display_name=name, enrolled_at=NOW)
    )
    session.flush()


def _push(session, node_id: str, site: str, agents: list[dict], coverage=None) -> None:
    payload: dict = {"agents": agents, "collectedAt": NOW.isoformat()}
    if coverage is not None:
        payload["coverage"] = coverage
    ingest_reports(
        session,
        site=site,
        node_id=node_id,
        reporter_principal=f"@collector:{site}",
        reports=[{"kind": "agents", "payload": payload}],
        now=NOW,
    )
    session.flush()


LIVE = [{"agent": "@alpha:x", "lastReportAt": NOW.isoformat(), "cadenceSeconds": 300}]
DARK = [
    {
        "agent": "@beta:x",
        "lastReportAt": (NOW - timedelta(days=20)).isoformat(),
        "cadenceSeconds": 300,
    }
]


class TestRightsAreTheSelection:
    def test_an_unbounded_scope_sees_every_environment(self, session):
        _enrol(session, "workstation", "site-a", "Workstation")
        _enrol(session, "edge-node", "site-b", "Edge node")
        _push(session, "workstation", "site-a", LIVE)
        _push(session, "edge-node", "site-b", DARK)
        assert {e["id"] for e in agent_environments(session)} == {"workstation", "edge-node"}

    def test_a_scoped_caller_sees_ONLY_its_own_site(self, session):
        _enrol(session, "workstation", "site-a", "Workstation")
        _enrol(session, "edge-node", "site-b", "Edge node")
        _push(session, "workstation", "site-a", LIVE)
        _push(session, "edge-node", "site-b", DARK)
        envs = agent_environments(session, sites=["site-a"])
        assert [e["id"] for e in envs] == ["workstation"]
        # Not merely absent from the list — never selected. A test that only
        # checked the ids would pass on an implementation that loaded both and
        # filtered, which is the one that eventually forgets to.
        assert all("edge-node" not in str(e) for e in envs)

    def test_an_empty_scope_sees_nothing_rather_than_everything(self, session):
        # A scope resolving to no sites must not read as "unbounded". That
        # inversion is the classic fail-open, and it is one character away.
        _enrol(session, "workstation", "site-a", "Workstation")
        _push(session, "workstation", "site-a", LIVE)
        assert agent_environments(session, sites=[]) == []

    def test_the_site_comes_from_the_push_not_the_payload(self, session):
        # The payload may claim anything; the row is written with the site the
        # credential was bound to. Anything else lets a node read another
        # site's console into existence by lying about itself.
        _enrol(session, "workstation", "site-a", "Workstation")
        _push(
            session,
            "workstation",
            "site-a",
            [{"agent": "@x:y", "site": "site-b", "lastReportAt": NOW.isoformat()}],
        )
        assert agent_environments(session, sites=["site-b"]) == []
        assert len(agent_environments(session, sites=["site-a"])) == 1


class TestWhatAnEnvironmentCarries:
    def test_it_carries_the_name_its_operator_gave_it(self, session):
        _enrol(session, "edge-node", "site-b", "Edge node")
        _push(session, "edge-node", "site-b", LIVE)
        [env] = agent_environments(session)
        assert env["name"] == "Edge node"
        assert env["site"] == "site-b"

    def test_it_falls_back_to_the_node_id_when_unnamed(self, session):
        session.add(FleetNode(node_id="n1", site="s", display_name=None, enrolled_at=NOW))
        session.flush()
        _push(session, "n1", "s", LIVE)
        assert agent_environments(session)[0]["name"] == "n1"

    def test_coverage_is_per_environment_and_never_summed(self, session):
        _enrol(session, "workstation", "a", "Mac")
        _enrol(session, "edge-node", "b", "Edge node")
        _push(session, "workstation", "a", LIVE, coverage={"total": 120, "reporting": 62, "offered": 45})
        _push(session, "edge-node", "b", LIVE, coverage={"total": 103, "reporting": 48, "offered": 30})
        envs = {e["id"]: e for e in agent_environments(session)}
        assert envs["workstation"]["coverage"]["total"] == 120
        assert envs["edge-node"]["coverage"]["total"] == 103
        # There is no fleet total anywhere in the payload, deliberately.
        assert not any("total" in e for e in envs.values())

    def test_a_node_that_never_pushed_a_roster_is_OMITTED_not_empty(self, session):
        # An environment with an empty agent list says "nothing runs there".
        # A node that has not reported has said nothing at all, and the two
        # must not render the same.
        _enrol(session, "silent", "a", "Silent")
        assert agent_environments(session) == []

    def test_it_keeps_both_clocks(self, session):
        # collectedAt is when the node gathered; reportedAt is when it landed.
        # The gap between them IS a stale push, and it is invisible if either
        # one is dropped.
        _enrol(session, "workstation", "a", "Mac")
        _push(session, "workstation", "a", LIVE)
        [env] = agent_environments(session)
        assert env["collectedAt"] and env["reportedAt"]


class TestTheEvaluator:
    def _eval(self, payload):
        return evaluate_report(
            kind="agents", payload=payload, received_at=NOW,
            cadence_seconds=900, now=NOW, node_id="n",
        )

    def test_a_silent_agent_past_its_cadence_fails(self, session):
        assert self._eval({"agents": DARK, "collectedAt": NOW.isoformat()}).status == Status.FAILED

    def test_AN_AGENT_NOBODY_ENABLED_IS_NOT_A_FAILURE(self, session):
        # The false finding this surface shipped: five agents that had never
        # been turned on reported as dark. Chosen silence is not a fault.
        stopped = [{**DARK[0], "stoppedByOperator": True}]
        assert self._eval({"agents": stopped, "collectedAt": NOW.isoformat()}).status != Status.FAILED

    def test_an_agent_with_no_declared_cadence_is_not_judged(self, session):
        no_cadence = [{"agent": "@x:y", "lastReportAt": (NOW - timedelta(days=90)).isoformat()}]
        assert self._eval({"agents": no_cadence, "collectedAt": NOW.isoformat()}).status != Status.FAILED

    def test_a_roster_it_cannot_speak_about_is_UNPROVEN_not_green(self, session):
        # Every agent excluded is not health. Returning GREEN would be the
        # reassuring answer to a question nobody could ask.
        stopped = [{**DARK[0], "stoppedByOperator": True}]
        assert self._eval({"agents": stopped, "collectedAt": NOW.isoformat()}).status == Status.UNPROVEN

    def test_a_reporting_roster_is_green(self, session):
        assert self._eval({"agents": LIVE, "collectedAt": NOW.isoformat()}).status == Status.GREEN
