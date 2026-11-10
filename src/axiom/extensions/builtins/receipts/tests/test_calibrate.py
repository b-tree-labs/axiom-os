# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The measurement has to recover a reliability it was not told about.

This is the differentiating claim of the whole construct, so it gets the
strictest test available: plant two solvers with known clear rates, write
their decisions to the REAL record, and measure. Nothing in `calibrate`
reads the generator — it reads `case_verdict` rows, the same table the
surface writes.

A measurement validated only by running is not validated.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from axiom.extensions.builtins.receipts.calibrate import calibrate
from axiom.extensions.builtins.receipts.db_models import Base
from axiom.extensions.builtins.receipts.simulation import robot_fleet_at_scale, seed_record

NOW = datetime(2026, 9, 26, 9, 0, tzinfo=UTC)
#: What the generator was told to produce. `calibrate` never sees these.
PLANTED = {"@sam:dc-3": 0.82, "@fleet-warden:dc-3": 0.54}


@pytest.fixture()
def recorded():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    with contextlib.closing(session):
        seed_record(session, robot_fleet_at_scale(now=NOW), now=NOW)
        session.commit()
        yield session
    engine.dispose()


def test_it_recovers_the_planted_reliability(recorded):
    """The claim, tested. Two solvers, known rates, measured from the
    record alone."""
    report = calibrate(recorded)
    for solver, planted in PLANTED.items():
        rows = report.for_solver(solver)
        assert rows, f"no decisions measured for {solver}"
        resolved = sum(r.resolved for r in rows)
        decided = sum(r.decided for r in rows)
        measured = resolved / decided
        assert abs(measured - planted) < 0.10, (
            f"{solver}: planted {planted:.0%}, measured {measured:.0%}"
        )


def test_it_distinguishes_the_two_solvers(recorded):
    """The useful form of the claim: not a number, an ORDERING. The agent
    clears less often than the human, and the record says so without being
    told."""
    report = calibrate(recorded)

    def rate(solver: str) -> float:
        rows = report.for_solver(solver)
        return sum(r.resolved for r in rows) / sum(r.decided for r in rows)

    assert rate("@sam:dc-3") > rate("@fleet-warden:dc-3")


def test_an_unresolved_decision_is_never_counted_as_a_failure(recorded):
    """`outcome IS NULL` means two things — still open, or open for ever —
    and the record cannot tell them apart. Treating null as failure would
    make the measurement an artifact of how recently somebody decided."""
    report = calibrate(recorded)
    with_open = [r for r in report.rows if r.open]
    assert with_open, "fixture produced no censored decisions to test against"
    for row in with_open:
        assert row.high > row.low, "a censored row collapsed to a point"
        assert row.low <= (row.resolved / row.decided) <= row.high


def test_the_bound_contains_the_truth(recorded):
    """Weaker than a point estimate and honest: whatever the real rate is,
    it is inside."""
    report = calibrate(recorded)
    for solver, planted in PLANTED.items():
        rows = report.for_solver(solver)
        low = sum(r.resolved for r in rows) / sum(r.decided for r in rows)
        high = 1.0 if any(r.open for r in rows) else low
        assert low - 0.10 <= planted <= high + 0.10, f"{solver} outside the bound"


def test_the_bound_is_too_wide_to_be_useful_per_condition(recorded):
    """A finding, pinned so it is not mistaken for success.

    Per (solver, condition, choice) the buckets here hold three to five
    decisions, so the bound runs to 100% whenever anything is unresolved
    and cannot separate a good solver from a bad one. The measurement is
    correct and the SAMPLE is too small — which tells us what volume the
    claim actually needs, and is the sort of thing a demo would otherwise
    hide by aggregating.
    """
    report = calibrate(recorded)
    censored = [r for r in report.rows if r.open]
    assert censored
    assert all(r.high == 1.0 for r in censored), (
        "if this now fails, the fixture generates enough volume for the "
        "per-condition bound to be informative — say so rather than deleting "
        "this test"
    )
    assert all(r.decided < 10 for r in censored)


def test_rows_without_a_condition_are_excluded_and_counted(recorded):
    """A measurement that silently drops half its input is a worse lie
    than no measurement."""
    from axiom.extensions.builtins.receipts.db_models import CaseVerdict

    recorded.add(
        CaseVerdict(
            site="dc-3",
            case_id="c-legacy01",
            condition=None,
            chosen="hold",
            decider="@sam:dc-3",
            decider_kind="human",
        )
    )
    recorded.commit()
    assert calibrate(recorded).excluded_no_condition == 1
