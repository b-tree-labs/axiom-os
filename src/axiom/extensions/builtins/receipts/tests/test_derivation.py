# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A claim you cannot check is a claim you are being asked to believe.

Founder (2026-09-25): "how do we prevent this type of problem where what
the LLM is saying is not congruent with reality ... we need to provide a
window into that kind of resolution, not just fix blindly." And on the
shape of that window: "I don't know enough about how this will be
consumed to be opinionated so be flexible."

So the derivation is DATA. These tests are about what it must contain,
not about how anything draws it.
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
from axiom.extensions.builtins.receipts.cases import case_payload, compose_cases
from axiom.extensions.builtins.receipts.db_models import Base as RcptBase

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
CADENCES = {"heartbeat": 900, "service_health": 900}


@pytest.fixture()
def stores():
    fe = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    FleetBase.metadata.create_all(fe)
    ffac = sessionmaker(bind=fe)

    @contextlib.contextmanager
    def fprov():
        s = ffac()
        try:
            yield s
        finally:
            s.close()

    fleet_store.set_provider(fprov)
    re_ = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    RcptBase.metadata.create_all(re_)
    rs = sessionmaker(bind=re_)()
    with contextlib.closing(rs):
        with fleet_store.session_scope() as fs:
            ingest_reports(
                fs,
                site="local",
                node_id="node-a",
                reporter_principal="@mech:local",
                now=NOW - timedelta(hours=3),
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
            fs.commit()
        yield rs
    fleet_store.reset_provider()
    fe.dispose()
    re_.dispose()


def _case(stores):
    with fleet_store.session_scope() as fs:
        items = fleet_source(fs, now=NOW)
    return compose_cases(items)[0]


def _by_subject(payload):
    return {d["subject"]: d for d in payload["derivations"]}


def test_a_staleness_claim_shows_the_arithmetic(stores):
    d = _by_subject(case_payload(_case(stores)))["claim:heartbeat"]
    values = {i["label"]: i["value"] for i in d["inputs"]}
    # The stored timestamp, the declared cadence, the threshold and the
    # age: everything needed to redo the judgement without us.
    assert values["Declared cadence"] == "900 seconds"
    assert values["Stale after"] == "3 x cadence"
    assert values["Age now"].endswith("seconds")
    assert "2026-09-25T09:00" in values["Last report stored at"]
    assert "more than" in d["rule"]


def test_every_claim_says_how_to_check_it_without_us(stores):
    """Not "trust the receipt". Go and look."""
    d = _by_subject(case_payload(_case(stores)))["claim:heartbeat"]
    assert d["verify"] == "axi fleet status --node node-a"


def test_a_derived_claim_says_what_it_cannot_establish(stores):
    """The distinction UNPROVEN already draws against FAILED, applied to
    the window itself. Hiding it would make a derived claim look observed."""
    d = _by_subject(case_payload(_case(stores)))["claim:heartbeat"]
    assert "not that the node is down" in d["limit"]


def test_the_collapse_into_one_problem_shows_its_rule(stores):
    d = _by_subject(case_payload(_case(stores)))["cause"]
    assert "cannot deliver anything else" in d["rule"]
    # And names what it deliberately does NOT collapse.
    assert "failed or unevidenced is never collapsed" in d["rule"]
    assert "second fault" in d["limit"]


def test_a_fix_that_reported_nothing_is_visible_in_the_window(stores):
    """The one that actually happened: ran_ok said true while the
    capability said it had sent nothing."""
    from axiom.extensions.builtins.receipts.cases import attach_decisions
    from axiom.extensions.builtins.receipts.verdicts import record_verdict

    case = _case(stores)
    record_verdict(
        stores,
        case,
        chosen="fix",
        decider="@sam:local",
        ran_capability="fleet.report",
        ran_ok=False,
        ran_detail="fleet push not configured; node is not enrolled — nothing sent",
    )
    stores.commit()
    [decorated] = attach_decisions(stores, [case])
    d = _by_subject(case_payload(decorated))["fix"]
    values = {i["label"]: i["value"] for i in d["inputs"]}
    assert values["Ran"] == "fleet.report"
    # Verbatim, so a reader can compare what we concluded against what
    # the capability actually said.
    assert "nothing sent" in values["It reported"]
    assert d["claim"] == "The fix did not put it right."
    assert "not that the situation is better" in d["limit"]


def test_the_window_is_a_flat_named_list_so_either_shape_can_read_it(stores):
    """A per-claim drill-down picks one by subject; a whole-case audit
    renders them in order. Neither shape is baked into the payload."""
    payload = case_payload(_case(stores))
    subjects = [d["subject"] for d in payload["derivations"]]
    assert "claim:heartbeat" in subjects
    assert "cause" in subjects
    for d in payload["derivations"]:
        assert set(d) == {"subject", "claim", "inputs", "rule", "verify", "limit"}


def test_it_rides_the_brief_too_not_only_the_drill_down(stores):
    with fleet_store.session_scope() as fs:
        items = fleet_source(fs, now=NOW)
        brief = compose_brief(stores, items, snapshot=False, now=NOW, fleet_session=fs)
    from axiom.extensions.builtins.receipts.brief import brief_payload

    case = brief_payload(brief)["cases"][0]
    assert any(d["subject"] == "claim:heartbeat" for d in case["derivations"])
