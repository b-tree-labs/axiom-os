# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The decision record: append-only typed decision receipts, outcomes
that are OBSERVED rather than asserted, and precedent answered from the
record instead of from memory."""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from axiom.extensions.builtins.receipts.brief import OversightItem
from axiom.extensions.builtins.receipts.cases import attach_decisions, compose_cases
from axiom.extensions.builtins.receipts.db_models import Base, CaseVerdict
from axiom.extensions.builtins.receipts.verdicts import (
    OPTIONS,
    OUTCOME_RESOLVED,
    open_verdict,
    precedent_for,
    record_verdict,
    stamp_outcomes,
    verdict_payload,
    verdicts_for,
)

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


def _case(entity="node-a", status="failed", site="s"):
    items = [
        OversightItem(
            entity_kind="node",
            entity_id=entity,
            claim_kind="backup",
            status=status,
            evidence="runner artifact missing",
            next_action="check the artifact",
            site=site,
        )
    ]
    return compose_cases(items)[0]


def test_a_verdict_is_a_typed_decision_receipt(session):
    case = _case()
    v = record_verdict(session, case, chosen="hold", decider="@ben:local", now=NOW)
    session.commit()
    p = verdict_payload(v)
    assert p["decision_type"] == "choice"
    assert p["options"] == list(OPTIONS)  # what COULD have been chosen
    assert p["chosen"] == "hold"
    assert p["decider"] == "@ben:local" and p["decider_kind"] == "human"
    # The evidence recorded is what was in front of the decider.
    assert "backup is failed" in p["evidence"]
    # ADR-126 D1: a human verdict carries no stated confidence, and its
    # absence is information — the payload simply has no such key.
    assert "stated_confidence" not in p
    # The outcome has not happened yet, and says so.
    assert p["outcome"] is None and p["minutes_to_outcome"] is None


def test_an_unattributed_decision_is_refused(session):
    case = _case()
    with pytest.raises(ValueError, match="decider"):
        record_verdict(session, case, chosen="hold", decider="")
    with pytest.raises(ValueError, match="chosen"):
        record_verdict(session, case, chosen="delete-everything", decider="@ben")


def test_history_is_append_only_and_newest_first(session):
    case = _case()
    record_verdict(session, case, chosen="hold", decider="@a", now=NOW)
    record_verdict(
        session, case, chosen="acknowledge", decider="@b", now=NOW + timedelta(minutes=5)
    )
    session.commit()
    history = verdicts_for(session, case.case_id)
    assert [v.chosen for v in history] == ["acknowledge", "hold"]  # newest first
    assert session.query(CaseVerdict).count() == 2  # the first was never edited
    # The live decision is the most recent undecided-outcome one.
    assert open_verdict(session, case.case_id).chosen == "acknowledge"


def test_outcomes_are_observed_not_asserted(session):
    case = _case()
    record_verdict(session, case, chosen="hold", decider="@ben", now=NOW)
    session.commit()

    # The situation is still live → nothing is stamped.
    stamp_outcomes(session, {case.case_id}, now=NOW + timedelta(minutes=4))
    session.commit()
    assert open_verdict(session, case.case_id) is not None

    # The evaluator stops reporting it → resolved, with when.
    stamped = stamp_outcomes(session, set(), now=NOW + timedelta(minutes=11))
    session.commit()
    assert [v.outcome for v in stamped] == [OUTCOME_RESOLVED]
    assert open_verdict(session, case.case_id) is None
    p = verdict_payload(verdicts_for(session, case.case_id)[0])
    assert p["outcome"] == OUTCOME_RESOLVED
    assert p["minutes_to_outcome"] == 11.0  # the number precedent is made of


def test_precedent_is_the_same_situation_decided_before(session):
    case = _case()
    record_verdict(session, case, chosen="hold", decider="@ben", now=NOW)
    session.commit()
    stamp_outcomes(session, set(), now=NOW + timedelta(minutes=11))
    session.commit()

    # The same entity goes bad again → the SAME case id → precedent found.
    again = _case()
    assert again.case_id == case.case_id
    [decorated] = attach_decisions(session, [again])
    assert decorated.verdict is None  # nothing open right now
    assert decorated.precedent is not None
    assert decorated.precedent["chosen"] == "hold"
    assert decorated.precedent["minutes_to_outcome"] == 11.0

    # A different entity is a different case, with no borrowed history.
    other = _case(entity="node-b")
    [other_decorated] = attach_decisions(session, [other])
    assert other_decorated.precedent is None


def test_a_held_case_shows_its_live_decision(session):
    case = _case()
    record_verdict(session, case, chosen="hold", decider="@ben:local", now=NOW)
    session.commit()
    [decorated] = attach_decisions(session, [case])
    assert decorated.verdict["chosen"] == "hold"
    assert decorated.verdict["decider"] == "@ben:local"


def test_site_scopes_the_record(session):
    a = _case(site="site-a")
    b = _case(site="site-b")
    assert a.case_id != b.case_id  # site is part of the identity
    record_verdict(session, a, chosen="hold", decider="@ben", now=NOW)
    session.commit()
    assert open_verdict(session, a.case_id, site="site-a") is not None
    assert open_verdict(session, a.case_id, site="site-b") is None
    assert precedent_for(session, b.case_id, site="site-b") is None


def test_the_wire_tells_the_surface_what_it_may_decide(session):
    """A surface that hardcodes the options is guessing at the server's
    vocabulary. It renders what this list says, so the list must be the
    same OPTIONS the decide endpoint validates against."""
    from axiom.extensions.builtins.receipts.cases import case_payload

    case = _case()
    offered = {o["choice"]: o for o in case_payload(case)["options"]}
    # Without handling, only the options that are always honourable —
    # plus discuss, which records nothing and is therefore always safe.
    assert set(offered) == {"hold", "acknowledge", "discuss"}
    assert "fix" not in offered
    # Each one says what it will DO, not just what it is called.
    assert offered["hold"]["detail"] == "stay quiet for a day, then ask again"
    assert "the case record" in offered["acknowledge"]["detail"]
    assert offered["discuss"]["records"] is False
    assert offered["hold"]["records"] is True

    # Once decided, there is nothing further to decide HERE — a change of
    # mind is a new decision made from the record, not a second click.
    record_verdict(session, case, chosen="hold", decider="@ben", now=NOW)
    session.commit()
    [decorated] = attach_decisions(session, [case])
    # A decided case is offered nothing to DECIDE — a change of mind is a
    # new decision made from the record — but it can still be discussed.
    after = {o["choice"] for o in case_payload(decorated)["options"]}
    assert after == {"discuss"}
    assert case_payload(decorated)["verdict"]["chosen"] == "hold"


def test_the_record_keeps_the_name_as_it_stood_then(session):
    """The handle is the identity and is usually a provider GUID — right
    to key on, unreadable to show. The label is recorded WITH the
    decision, not looked up later: a directory answers who someone is
    called today; a decision record must say who they were called then."""
    case = _case()
    v = record_verdict(
        session,
        case,
        chosen="hold",
        decider="@78f6cda0-4068-4c73-b8f7-53210aee4379:site-a",
        decider_label="Lee, Robin",
        now=NOW,
    )
    session.commit()
    p = verdict_payload(v)
    assert p["decider"] == "@78f6cda0-4068-4c73-b8f7-53210aee4379:site-a"  # identity
    assert p["decider_label"] == "Lee, Robin"  # what to show


def test_an_uncaptured_label_is_null_not_blank(session):
    """A blank would render as a nameless decider. Null means 'not
    captured', and the surface falls back to the handle — which is what
    that row actually says."""
    case = _case()
    v = record_verdict(session, case, chosen="hold", decider="@ben:site-a", now=NOW)
    session.commit()
    assert verdict_payload(v)["decider_label"] is None


def test_a_hold_has_an_end_and_the_case_comes_back(session):
    """Founder: Hold should say its "consequence and revisit". It can only
    say that if holding actually ends — otherwise "leave this alone" is a
    way of never deciding, and the case simply vanishes."""
    from axiom.extensions.builtins.receipts.verdicts import HOLD_WINDOW, is_live

    case = _case()
    v = record_verdict(
        session,
        case,
        chosen="hold",
        decider="@ben:s",
        now=NOW,
        hold_until=NOW + HOLD_WINDOW,
    )
    session.commit()

    # Inside the window the hold stands and the case is not asked about.
    assert is_live(v, now=NOW + timedelta(hours=1))
    assert open_verdict(session, case.case_id, now=NOW + timedelta(hours=1)) is not None

    # After it, the case is open again — which is the revisit the button
    # promises, rather than the case being gone for good.
    assert not is_live(v, now=NOW + timedelta(hours=25))
    assert open_verdict(session, case.case_id, now=NOW + timedelta(hours=25)) is None


def test_an_old_hold_with_no_end_still_stands(session):
    """Rows written before holds had an end genuinely had none. Inventing
    an expiry for them would be putting a decision in somebody's mouth."""
    from axiom.extensions.builtins.receipts.verdicts import is_live

    case = _case()
    v = record_verdict(session, case, chosen="hold", decider="@ben:s", now=NOW)
    session.commit()
    assert v.hold_until is None
    assert is_live(v, now=NOW + timedelta(days=30))
