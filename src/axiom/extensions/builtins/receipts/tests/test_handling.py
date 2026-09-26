# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Why is a person looking at this at all?

Founder feedback (2026-09-24): "why can't we just automatically deal with
this already? Like why is this getting bubbled up? It feels like the bar
for human intervention needs to be significant ... we run the risk of
[...] bothering our users about something that they don't want to be
bothered about."

The answer must not be a judgement someone tuned. Every reason below is
a FACT the platform can check about itself, and the surface states which
one applies. A case that trips none of them did not need a person.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from axiom.extensions.builtins.receipts.brief import OversightItem
from axiom.extensions.builtins.receipts.cases import compose_cases
from axiom.extensions.builtins.receipts.db_models import Base
from axiom.extensions.builtins.receipts.handling import (
    HELD,
    NEVER_WORKED,
    NO_FIX,
    NOT_OURS,
    TRIED,
    handling_for,
)
from axiom.extensions.builtins.receipts.verdicts import record_verdict, stamp_outcomes

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)


@pytest.fixture()
def session():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    with contextlib.closing(s):
        yield s
    engine.dispose()


def _case(entity="node-a", kind="heartbeat", status="stale"):
    return compose_cases(
        [
            OversightItem(
                entity_kind="node",
                entity_id=entity,
                claim_kind=kind,
                status=status,
                evidence="e",
                site="s",
            )
        ]
    )[0]


def test_a_fixable_case_about_ourselves_needs_nobody(session):
    """The whole point. A declared fix, for this node, never tried and
    never held, is not a question for a person."""
    h = handling_for(session, _case(), this_node="node-a")
    assert h.can_run is True
    assert h.needs_person is False
    assert h.fix_summary == "Make this node report now."


def test_a_case_about_someone_elses_node_says_so(session):
    """fleet.report reports about the node it runs on. Offering to run it
    for a DIFFERENT silent node would be a lie told with a button."""
    h = handling_for(session, _case(entity="node-b"), this_node="node-a")
    assert h.can_run is False
    assert h.needs_person is True
    assert h.reason == NOT_OURS
    assert "node-b" in h.because


def test_a_condition_with_no_declared_fix_says_that_plainly(session):
    h = handling_for(session, _case(kind="backup", status="failed"), this_node="node-a")
    assert h.can_run is False
    assert h.reason == NO_FIX
    assert h.because == "No automatic fix is declared for this."


def test_a_fix_that_already_ran_and_did_not_work_is_the_strongest_reason(session):
    """This is the signal most worth a person's time, and it arrives with
    the evidence that the obvious thing was already tried."""
    case = _case()
    record_verdict(session, case, chosen="fix", decider="@axiom:local", now=NOW, ran_ok=True)
    session.commit()
    h = handling_for(session, case, this_node="node-a", now=NOW + timedelta(minutes=5))
    assert h.needs_person is True
    assert h.reason == TRIED
    assert "already ran" in h.because


def test_a_held_case_is_left_alone(session):
    case = _case()
    record_verdict(session, case, chosen="hold", decider="@ben:s", now=NOW)
    session.commit()
    h = handling_for(session, case, this_node="node-a")
    assert h.needs_person is True
    assert h.reason == HELD
    assert h.can_run is False  # a held case is not quietly un-held by a runner


def test_the_first_run_of_an_unproven_fix_is_worth_watching(session):
    """Graduation is measured, not configured: once the record shows this
    fix clearing this case, later occurrences stop asking."""
    case = _case()
    h = handling_for(session, case, this_node="node-a", require_precedent=True)
    assert h.needs_person is True
    assert h.reason == NEVER_WORKED

    record_verdict(session, case, chosen="fix", decider="@axiom:local", now=NOW, ran_ok=True)
    session.commit()
    stamp_outcomes(session, set(), now=NOW + timedelta(minutes=2))
    session.commit()

    again = handling_for(session, _case(), this_node="node-a", require_precedent=True)
    assert again.needs_person is False, "a fix that cleared this before has earned the run"


def test_every_reason_is_a_sentence_a_person_can_read(session):
    for case, node in (
        (_case(), "node-b"),
        (_case(kind="backup", status="failed"), "node-a"),
    ):
        h = handling_for(session, case, this_node=node)
        assert h.because.endswith("."), h.because
        assert h.because[0].isupper(), h.because
        for internal in ("claim", "capability", "self_only", "remedy"):
            assert internal not in h.because.lower()


def test_a_fix_that_ran_and_did_nothing_is_not_a_fix(session):
    """fleet.report exits ok on an unenrolled node because a probe never
    warns. Reading that as a successful fix would tell a person the thing
    was handled when nothing happened at all."""
    from axiom.extensions.builtins.receipts.handling import DID_NOT_WORK

    case = _case()
    record_verdict(
        session,
        case,
        chosen="fix",
        decider="@axiom:local",
        now=NOW,
        ran_ok=False,
        ran_detail="fleet push not configured; node is not enrolled — nothing sent",
    )
    session.commit()
    h = handling_for(session, case, this_node="node-a")
    assert h.needs_person is True
    assert h.reason == DID_NOT_WORK
    assert h.can_run is False, "offering the same no-op fix again is a loop, not a pathway"
    # The diagnosis travels with the sentence rather than sitting in a
    # column nobody reads.
    assert "not enrolled" in h.because
