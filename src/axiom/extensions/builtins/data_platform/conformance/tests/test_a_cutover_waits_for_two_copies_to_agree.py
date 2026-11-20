# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A platform move cuts over only after the old and new copies have agreed long enough.

While a data platform moves to a new home, old and new run in parallel and
both receive the same readings. Each day the two copies of silver are compared
with reconcile in dry-run mode: nothing is filled and nothing is resolved. The
gate then says "safe to cut over" only when:

* the most recent run of clean days is long enough, with no day missing from it;
* enough of those days actually carried data from the feeds that matter (a
  clean day with nothing in it proves nothing);
* every required event (an outage with catch-up, an upgrade of both sides)
  happened inside that run, with a clean day after it;
* the copied history was checked clean.

One disagreement makes the day unclean and starts the count again. The verdict
always says why it is not yet safe.
"""

from __future__ import annotations

import hashlib

import psycopg
import pytest

from axiom.extensions.builtins.data_platform.conformance import SILVER_SIGNALS_DDL, pg_upsert
from axiom.extensions.builtins.data_platform.conformance.cutover_gate import (
    DayCheck,
    GateLedger,
    Rule,
    check_day,
    verdict,
)
from axiom.extensions.builtins.data_platform.conformance.reconcile import PgSide

DAY = "2026-10-08"


# -- the daily comparison, on two real databases -------------------------------


@pytest.fixture
def copies(timescale_dsn):
    made = []
    for name in ("gate_old", "gate_new"):
        with psycopg.connect(timescale_dsn, autocommit=True) as admin:
            admin.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
            admin.execute(f"CREATE DATABASE {name}")
        c = psycopg.connect(timescale_dsn.rsplit("/", 1)[0] + "/" + name, autocommit=True)
        for stmt in SILVER_SIGNALS_DDL:
            c.execute(stmt)
        made.append(c)
    yield made[0], made[1]
    for c in made:
        c.close()


def _row(i: int, *, value=None, feed="site-a.live", channel="TC1") -> dict:
    ts = f"{DAY}T10:{i // 60:02d}:{i % 60:02d}+00:00"
    return {
        "site": "site-a",
        "feed": feed,
        "channel": channel,
        "ts": ts,
        "value": float(i) if value is None else value,
        "unit": "degC",
        "quality": "good",
        "source_class": "measured",
        "schema_ref": "site-a/v1",
        "row_hash": hashlib.sha256(f"{feed}|{ts}|{channel}".encode()).hexdigest(),
    }


def _load(conn, rows):
    up = pg_upsert(conn.cursor())
    for r in rows:
        up(r)


def _count(conn) -> int:
    return conn.execute("SELECT count(*) FROM silver.signals").fetchone()[0]


def test_identical_copies_make_a_clean_day_that_names_the_feeds_it_saw(copies):
    old, new = copies
    rows = [_row(i) for i in range(30)] + [_row(i, feed="site-a.model") for i in range(5)]
    _load(old, rows)
    _load(new, rows)
    day = check_day(PgSide(old), PgSide(new), sites=["site-a"], day=DAY)
    assert day.clean and day.missing_on_new == day.extra_on_new == day.conflicts == 0
    assert day.feeds == ["site-a.live", "site-a.model"]


def test_a_missing_an_extra_and_a_changed_reading_each_make_the_day_unclean_and_change_nothing(
    copies,
):
    old, new = copies
    _load(old, [_row(i) for i in range(10)])
    _load(new, [_row(i) for i in range(1, 10)] + [_row(99)])  # 0 missing, 99 extra
    new.execute("UPDATE silver.signals SET value = -1 WHERE ts = %s", (f"{DAY}T10:00:05+00:00",))
    before = (_count(old), _count(new))

    day = check_day(PgSide(old), PgSide(new), sites=["site-a"], day=DAY)
    assert not day.clean
    assert (day.missing_on_new, day.extra_on_new, day.conflicts) == (1, 1, 1)
    assert day.examples, "an unclean day names where to look"
    assert (_count(old), _count(new)) == before  # a comparison, never a repair


# -- the verdict ---------------------------------------------------------------


def _days(n: int, *, start: int = 1, clean=True, feeds=("site-a.live",)) -> list[DayCheck]:
    return [
        DayCheck(day=f"2026-11-{start + i:02d}", clean=clean, feeds=list(feeds)) for i in range(n)
    ]


RULE = Rule(
    min_clean_days=14,
    min_active_days=5,
    active_feeds=("site-a.model",),
    required_events=("outage", "upgrade"),
)
EVENTS = [{"kind": "outage", "day": "2026-11-03"}, {"kind": "upgrade", "day": "2026-11-05"}]


def _active(days, which):
    for d in days:
        if d.day in which:
            d.feeds = ["site-a.live", "site-a.model"]
    return days


def test_safe_only_when_every_condition_holds():
    days = _active(_days(14), {f"2026-11-{d:02d}" for d in (2, 4, 6, 8, 10)})
    v = verdict(days, rule=RULE, events=EVENTS, history_clean=True)
    assert v.safe, v.reasons
    assert v.clean_streak == 14 and v.active_days == 5


def test_one_unclean_day_restarts_the_count():
    days = _active(_days(14), {f"2026-11-{d:02d}" for d in (2, 4, 6, 8, 10)})
    days[12].clean = False
    v = verdict(days, rule=RULE, events=EVENTS, history_clean=True)
    assert not v.safe and v.clean_streak == 1
    assert any("clean" in r for r in v.reasons)


def test_a_day_with_no_comparison_breaks_the_run():
    days = _active(_days(15), {f"2026-11-{d:02d}" for d in (9, 10, 11, 12, 13)})
    del days[7]  # 2026-11-08 was never compared
    v = verdict(
        days,
        rule=RULE,
        events=[{"kind": "outage", "day": "2026-11-10"}, {"kind": "upgrade", "day": "2026-11-11"}],
        history_clean=True,
    )
    assert not v.safe and v.clean_streak == 7


def test_clean_days_without_the_feeds_that_matter_prove_nothing():
    v = verdict(_days(20), rule=RULE, events=EVENTS, history_clean=True)
    assert not v.safe and v.active_days == 0
    assert any("site-a.model" in r for r in v.reasons)


def test_a_required_event_must_fall_inside_the_run_and_be_followed_by_a_clean_day():
    days = _active(_days(14), {f"2026-11-{d:02d}" for d in (2, 4, 6, 8, 10)})
    late = [{"kind": "outage", "day": "2026-11-03"}, {"kind": "upgrade", "day": "2026-11-14"}]
    v = verdict(days, rule=RULE, events=late, history_clean=True)
    assert not v.safe and any("upgrade" in r for r in v.reasons)
    before = [{"kind": "outage", "day": "2026-10-20"}, {"kind": "upgrade", "day": "2026-11-05"}]
    v = verdict(days, rule=RULE, events=before, history_clean=True)
    assert not v.safe and any("outage" in r for r in v.reasons)


def test_history_must_have_been_checked_not_just_be_unknown():
    days = _active(_days(14), {f"2026-11-{d:02d}" for d in (2, 4, 6, 8, 10)})
    for history in (None, False):
        v = verdict(days, rule=RULE, events=EVENTS, history_clean=history)
        assert not v.safe and any("history" in r for r in v.reasons)


def test_the_ledger_keeps_days_and_events_and_a_rerun_replaces_a_day(tmp_path):
    ledger = GateLedger(tmp_path / "gate.jsonl")
    ledger.record_day(DayCheck(day="2026-11-01", clean=False, feeds=["f"]))
    ledger.record_day(DayCheck(day="2026-11-01", clean=True, feeds=["f"]))  # re-run after a fix
    ledger.record_event("outage", "2026-11-01", note="edge down 2 h")
    reopened = GateLedger(tmp_path / "gate.jsonl")
    assert [(d.day, d.clean) for d in reopened.days()] == [("2026-11-01", True)]
    assert reopened.events() == [{"kind": "outage", "day": "2026-11-01", "note": "edge down 2 h"}]
