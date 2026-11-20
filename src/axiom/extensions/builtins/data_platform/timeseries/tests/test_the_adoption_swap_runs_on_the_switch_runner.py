# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""ADR-197 P1b: the adoption's swap, rollback and drop are hooks of the switch runner.

Against a real TimescaleDB container (never the developer's database). The
existing adoption tests pass unchanged; these show what the runner adds: a change
record written first, a ledger row written in the swap's own transaction, the
measured hold against the budget, a recorded approver on the drop, and a rollback
that is itself a recorded change.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from axiom.extensions.builtins.data_platform.timeseries import adopt
from axiom.extensions.builtins.data_platform.timeseries.contract import TimeseriesContract
from axiom.infra import change_intent
from axiom.infra.switchplan import PgLedger

from .conftest import SCHEMA

APPROVER = "@ben.booth:axiom"


def _c() -> TimeseriesContract:
    return TimeseriesContract(schema=SCHEMA, table="readings", identity=("site", "feed", "channel"),
                              chunk_interval=timedelta(hours=12))


@pytest.fixture
def copied(db):
    db.execute("DROP TABLE IF EXISTS data_platform.switch_records")
    db.execute("DROP TABLE IF EXISTS data_platform.timeseries_adoption_windows")
    db.execute("DROP TABLE IF EXISTS data_platform.timeseries_adoptions")
    db.execute(f"CREATE TABLE {SCHEMA}.readings (site text NOT NULL, feed text NOT NULL, channel text NOT NULL, "
               "ts timestamptz NOT NULL, value double precision, UNIQUE (site, feed, channel, ts))")
    db.execute(f"CREATE INDEX readings_ts ON {SCHEMA}.readings (ts)")
    db.execute(f"INSERT INTO {SCHEMA}.readings SELECT 'a', 'f', 'c', now() - interval '3 days' - make_interval(secs => i), 1 "
               "FROM generate_series(0, 20000) i")
    db.execute(f"ANALYZE {SCHEMA}.readings")
    adopt.run(db, _c())
    return db


def _record(db):
    rid = adopt._read_adoption(db.cursor(), _c().qualified)["notes"]["switch_id"]
    return PgLedger(db, adopt.LEDGER_SCHEMA).load(rid)


def test_a_swap_writes_its_change_first_and_its_ledger_row_in_the_swap_transaction(copied, tmp_path):
    steps = adopt.swap(copied, _c(), principal=APPROVER)
    assert "swapped" in steps[0]
    rec = _record(copied)
    assert rec.kind == "timeseries-adoption" and rec.phase == "committed" and rec.state["swapped"] is True
    hold = rec.holds[-1]
    assert hold["hold_s"] <= hold["budget_s"] == 2.0 and hold["breach"] is False
    assert rec.retire_task["owner"] == APPROVER and rec.retire_task["status"] == "open" and rec.retire_task["due_at"]
    changes = change_intent.changes()
    assert [(c.kind, c.outcome) for c in changes] == [("migration", "switched")]
    assert change_intent.has_intent(changes[0].id)


def test_a_swap_that_leaves_readers_on_the_old_table_is_a_failed_recorded_change_and_leaves_no_committed_row(copied, monkeypatch):
    monkeypatch.setattr(adopt, "check_reads_land_on_new",
                        lambda *a, **k: (_ for _ in ()).throw(adopt.SwapUnsafe("swap rolled back: readers on the old table")))
    with pytest.raises(adopt.SwapUnsafe, match="readers on the old table"):
        adopt.swap(copied, _c())
    assert adopt.progress(copied, _c()).phase == "copying"
    assert [(c.kind, c.outcome) for c in change_intent.changes()] == [("migration", "failed")]
    rows = PgLedger(copied, adopt.LEDGER_SCHEMA).records()
    assert [r.phase for r in rows] == ["rolled_back"]


def test_dropping_the_old_table_is_a_retire_with_a_recorded_approver(copied):
    adopt.swap(copied, _c(), principal=APPROVER)
    v = adopt.verify(copied, _c())
    assert v.ok
    adopt.drop_old(copied, _c(), reverify=False, principal=APPROVER)
    rec = _record(copied)
    assert rec.phase == "retired" and rec.approver == APPROVER and rec.retire_task["status"] == "done"
    assert copied.execute(f"SELECT to_regclass('{SCHEMA}.readings__old')").fetchone()[0] is None
    assert [c.outcome for c in change_intent.changes()][-1] == "retired"


def test_rollback_after_a_runner_swap_is_a_recorded_change(copied):
    adopt.swap(copied, _c(), principal=APPROVER)
    adopt.rollback(copied, _c())
    rec = _record(copied)
    assert rec.phase == "rolled_back" and rec.retire_task["status"] == "cancelled"
    outcomes = [(c.kind, c.outcome) for c in change_intent.changes()]
    assert outcomes[-1] == ("migration", "rolled_back")
    assert adopt.progress(copied, _c()).phase == "rolled_back"


def test_a_swap_made_before_the_runner_existed_still_rolls_back_and_drops_directly(copied):
    adopt.swap(copied, _c(), principal=APPROVER)
    copied.execute(f"UPDATE {adopt.ADOPTIONS} SET notes = notes - 'switch_id'")  # no runner record, as on an older node
    adopt.rollback(copied, _c())
    assert adopt.progress(copied, _c()).phase == "rolled_back"


def test_two_swaps_at_once_the_second_is_refused_as_busy(copied, timescale_dsn):
    import psycopg

    from axiom.infra.switchplan import SingleFlight

    with psycopg.connect(timescale_dsn, autocommit=True) as other:
        held = SingleFlight(adopt._lock_name(_c().qualified), conn=other)
        assert held.acquire()
        with pytest.raises(adopt.AdoptionBusy):
            adopt.swap(copied, _c())
        held.release()
    adopt.swap(copied, _c())
