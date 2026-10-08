# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""RRULE cadence: parse/serialize (iCalendar RFC 5545) + real firing through
the engine via dateutil.rrule. The lossless calendar recurrence path."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from axiom.extensions.builtins.schedule import chaos, formats, store
from axiom.extensions.builtins.schedule.api import Cadence, register, status
from axiom.extensions.builtins.schedule.cadence import compute_next_fire_at
from axiom.extensions.builtins.schedule.engine import EngineContext, tick
from axiom.extensions.builtins.schedule.lease import LeaseManager
from axiom.extensions.builtins.schedule.store import SqlFireLog

# A Monday.
MON = datetime(2026, 6, 8, 9, 0, 0, tzinfo=UTC)


def _n(dt):
    return dt.replace(tzinfo=None) if dt is not None and dt.tzinfo else dt


def _ctx(clock, executor):
    return EngineContext(
        session=store.session_scope,
        authz=type("A", (), {"decide": lambda self, e: True})(),
        fire_log=SqlFireLog(),
        executor=executor,
        lease=LeaseManager(node_id="n", ttl_seconds=30),
        now_fn=clock,
        window_seconds=60,
    )


def test_parse_and_serialize_rrule_round_trip():
    c = formats.parse("RRULE:FREQ=WEEKLY;BYDAY=MO,WE,FR")
    assert c.kind == "rrule"
    assert formats.serialize(c, dialect="rrule") == "RRULE:FREQ=WEEKLY;BYDAY=MO,WE,FR"
    # bare FREQ= is accepted and normalized on the way out
    assert formats.parse("FREQ=DAILY").kind == "rrule"
    assert formats.serialize(formats.parse("FREQ=DAILY"), dialect="rrule") == "RRULE:FREQ=DAILY"


def test_interval_to_rrule():
    assert formats.to_rrule(Cadence(kind="interval", interval=timedelta(hours=1))) == "RRULE:FREQ=HOURLY"
    assert formats.to_rrule(Cadence(kind="interval", interval=timedelta(hours=2))) == "RRULE:FREQ=HOURLY;INTERVAL=2"
    assert formats.to_rrule(Cadence(kind="interval", interval=timedelta(days=1))) == "RRULE:FREQ=DAILY"


def test_compute_next_for_weekly_rrule_lands_on_the_right_day():
    # Every Wednesday at 09:00; from a Monday the next is two days out.
    cad = Cadence(kind="rrule", rrule="FREQ=WEEKLY;BYDAY=WE;BYHOUR=9;BYMINUTE=0;BYSECOND=0",
                  not_before=MON)
    nxt = compute_next_fire_at(cad, None, MON)
    assert nxt.weekday() == 2  # Wednesday
    assert _n(nxt) == _n(MON + timedelta(days=2))


def test_rrule_count_exhausts():
    # Fire daily, but only twice (COUNT=2). After two, the recurrence is done.
    cad = Cadence(kind="rrule", rrule="FREQ=DAILY;COUNT=2", not_before=MON)
    first = compute_next_fire_at(cad, None, MON - timedelta(minutes=1))
    assert _n(first) == _n(MON)
    second = compute_next_fire_at(cad, first, first)
    assert _n(second) == _n(MON + timedelta(days=1))
    third = compute_next_fire_at(cad, second, second)
    assert third is None  # exhausted


def test_rrule_schedule_fires_through_the_engine(sqlite_store):
    sched = register(envelope={"cap": "x"},
                     cadence=Cadence(kind="rrule", rrule="FREQ=DAILY;BYHOUR=9;BYMINUTE=0;BYSECOND=0",
                                     not_before=MON),
                     action="demo.daily", now=MON)
    # First fire is the same day at 09:00 = MON.
    assert _n(status(sched).summary.next_fire_at) == _n(MON)
    ex = chaos.CountingExecutor()
    assert tick(_ctx(chaos.JumpClock(MON), ex)).fired == 1
    # Advances to the next day's 09:00.
    assert _n(status(sched).summary.next_fire_at) == _n(MON + timedelta(days=1))
