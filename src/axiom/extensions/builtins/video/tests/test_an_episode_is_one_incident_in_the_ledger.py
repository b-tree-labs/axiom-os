# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""VID-011, VID-012, spec-video §Trigger runner and §Events.

One thread per attached trigger, each with its own subscription. Every event is
appended to ``<state>/video/events.jsonl`` with the locked append. An episode is
one incident: ``started`` then ``ended`` under one key, never a stream of
repeats, and never a ``started`` left open: stopping capture closes it, and a
copy that takes a source over closes what a crashed holder left open. A trigger
that raises is counted and does not stop capture or another trigger.
"""

from __future__ import annotations

import json
import time

import pytest

from axiom.extensions.builtins.video import ledger, runner
from axiom.extensions.builtins.video import state as vs
from axiom.extensions.builtins.video import triggers as vt
from axiom.extensions.builtins.video.bus import FrameBus
from axiom.extensions.builtins.video.motion import MotionTrigger

from .harness import until
from .scenes import crossing, frames

SCRIPTED = "axiom.extensions.builtins.video.tests.trigger_fakes:ScriptedTrigger"


@pytest.fixture
def catalog():
    return vt.TriggerCatalog(
        entries={
            "motion": vt.TriggerEntry(
                "motion", "axiom.extensions.builtins.video.motion:MotionTrigger", "builtin"
            ),
            "scripted": vt.TriggerEntry("scripted", SCRIPTED, "test"),
            "other": vt.TriggerEntry("other", SCRIPTED, "test"),
        }
    )


@pytest.fixture
def bus():
    return FrameBus("cam-1")


def _publish(bus, scene, *, pace_s: float = 0.0):
    for frame in scene:
        bus.publish(frame)
        if pace_s:
            time.sleep(pace_s)


def _health(triggers, name):
    return next(h for h in triggers.health() if h["name"] == name)


def _observed(triggers, name, n):
    """Wait until ``name`` has had ``n`` frames, observed or dropped."""
    return until(
        lambda: _health(triggers, name)["frames"] + _health(triggers, name)["dropped"] >= n
    )


# ---- the ledger --------------------------------------------------------------------


def _event(
    key="cam-1:motion@0.1.0:e1", kind="started", ts="2026-11-10T12:00:00+00:00", stream="cam-1"
):
    return vt.TriggerEvent(
        stream=stream,
        trigger="motion@0.1.0",
        ts=ts,
        kind=kind,
        incident_key=key.replace("cam-1", stream, 1),
    )


def test_the_ledger_is_one_jsonl_file_in_the_video_state(state_dir):
    assert ledger.ledger_path(state_dir) == state_dir / "video" / "events.jsonl"
    ledger.append(_event(), state_dir)
    (line,) = ledger.ledger_path(state_dir).read_text().splitlines()
    assert json.loads(line)["incident_key"] == "cam-1:motion@0.1.0:e1"


def test_the_ledger_write_is_the_locked_append(state_dir, monkeypatch):
    from axiom.infra import state as infra_state

    calls = []
    real = infra_state.locked_append_jsonl
    monkeypatch.setattr(
        infra_state, "locked_append_jsonl", lambda path, rec: (calls.append(path), real(path, rec))
    )
    ledger.append(_event(), state_dir)
    assert calls == [ledger.ledger_path(state_dir)]


def test_reading_filters_by_source_and_since(state_dir):
    ledger.append(_event(ts="2026-11-10T12:00:00+00:00"), state_dir)
    ledger.append(_event(kind="ended", ts="2026-11-10T12:00:09+00:00"), state_dir)
    ledger.append(
        _event(key="cam-1:motion@0.1.0:e2", ts="2026-11-10T12:05:00+00:00", stream="cam-2"),
        state_dir,
    )
    assert [r["kind"] for r in ledger.read(state_dir)] == ["started", "ended", "started"]
    assert [r["stream"] for r in ledger.read(state_dir, stream="cam-1")] == ["cam-1", "cam-1"]
    assert [r["ts"] for r in ledger.read(state_dir, since="2026-11-10T12:00:05+00:00")] == [
        "2026-11-10T12:00:09+00:00",
        "2026-11-10T12:05:00+00:00",
    ]


def test_a_torn_line_is_skipped_not_fatal(state_dir):
    ledger.append(_event(), state_dir)
    with ledger.ledger_path(state_dir).open("a") as fh:
        fh.write('{"stream": "cam-1", "kin')
    assert len(ledger.read(state_dir)) == 1


def test_since_must_be_a_time_and_the_error_shows_one(state_dir):
    with pytest.raises(ValueError) as caught:
        ledger.read(state_dir, since="yesterday-ish")
    assert "2026-" in str(caught.value) or "ISO" in str(caught.value)


def test_a_since_without_an_offset_is_local_time():
    from datetime import datetime

    assert (
        ledger.parse_since("2026-11-10T08:00")
        == datetime.fromisoformat("2026-11-10T08:00").astimezone()
    )


def test_no_ledger_reads_as_no_events(state_dir):
    assert ledger.read(state_dir) == []


# ---- the runner -------------------------------------------------------------------


def test_motion_on_the_bus_writes_one_episode_to_the_ledger(state_dir, bus, catalog):
    triggers = runner.TriggerSet("cam-1", bus=bus, state_dir=state_dir, catalog=catalog)
    result = triggers.apply([{"name": "motion"}])
    assert result["errors"] == [] and result["running"] == ["motion"]
    scene = crossing()
    _publish(bus, scene, pace_s=0.002)
    _observed(triggers, "motion", len(scene))
    triggers.stop()
    rows = ledger.read(state_dir)
    assert [r["kind"] for r in rows] == ["started", "ended"]
    assert len({r["incident_key"] for r in rows}) == 1


def test_each_trigger_has_its_own_subscription(state_dir, bus, catalog):
    triggers = runner.TriggerSet("cam-1", bus=bus, state_dir=state_dir, catalog=catalog)
    triggers.apply([{"name": "motion"}, {"name": "scripted"}])
    assert sorted(s.name for s in bus.subscriptions()) == ["trigger:motion", "trigger:scripted"]
    triggers.stop()
    assert bus.subscriptions() == []


def test_stopping_capture_closes_an_open_episode(state_dir, bus, catalog):
    triggers = runner.TriggerSet("cam-1", bus=bus, state_dir=state_dir, catalog=catalog)
    triggers.apply([{"name": "scripted", "script": {"2": [["started", "e1"]]}}])
    _publish(bus, frames(5))
    _observed(triggers, "scripted", 5)
    assert vs.read_open_episodes("cam-1", state_dir), "an open episode is on disk while it is open"
    triggers.stop()
    started, ended = ledger.read(state_dir)
    assert (started["kind"], ended["kind"]) == ("started", "ended")
    assert ended["incident_key"] == started["incident_key"]
    assert (
        ended["detail"]["reason"] == runner.REASON_STOPPED
        and ended["detail"]["closed_by"] == "capture"
    )
    assert vs.read_open_episodes("cam-1", state_dir) == {}


def test_a_copy_that_takes_over_closes_what_a_crashed_holder_left_open(state_dir, bus, catalog):
    crashed = runner.TriggerSet("cam-1", bus=bus, state_dir=state_dir, catalog=catalog)
    crashed.apply([{"name": "scripted", "script": {"1": [["started", "e1"]]}}])
    _publish(bus, frames(3))
    _observed(crashed, "scripted", 3)
    crashed.stop(reason=None)  # a process that died: nothing closed, nothing written
    assert [r["kind"] for r in ledger.read(state_dir)] == ["started"]

    successor = runner.TriggerSet(
        "cam-1", bus=FrameBus("cam-1"), state_dir=state_dir, catalog=catalog
    )
    closed = successor.recover()
    assert [e.kind for e in closed] == ["ended"]
    rows = ledger.read(state_dir)
    assert [r["kind"] for r in rows] == ["started", "ended"]
    assert rows[1]["incident_key"] == rows[0]["incident_key"]
    assert rows[1]["detail"]["reason"] == runner.REASON_LOST
    assert successor.recover() == [], "closed once"


def test_a_trigger_that_raises_is_counted_and_capture_and_the_others_go_on(state_dir, bus, catalog):
    triggers = runner.TriggerSet("cam-1", bus=bus, state_dir=state_dir, catalog=catalog)
    triggers.apply(
        [
            {"name": "scripted", "raise_at": [1, 2, 3]},
            {"name": "other", "script": {"4": [["occurred", "x1"]]}},
        ]
    )
    _publish(bus, frames(6))
    _observed(triggers, "scripted", 6)
    _observed(triggers, "other", 6)
    health = {h["name"]: h for h in triggers.health()}
    assert health["scripted"]["errors"] == 3 and health["scripted"]["state"] == "running"
    assert "scripted failure at 3" in health["scripted"]["last_error"]
    assert health["other"]["errors"] == 0 and health["other"]["events"] == 1
    triggers.stop()
    assert [r["kind"] for r in ledger.read(state_dir)] == ["occurred"]


def test_a_config_error_while_observing_fails_the_trigger_with_its_fix(state_dir, bus, catalog):
    triggers = runner.TriggerSet("cam-1", bus=bus, state_dir=state_dir, catalog=catalog)
    triggers.apply([{"name": "scripted", "config_error_at": [0]}])
    _publish(bus, frames(4))
    until(lambda: triggers.health()[0]["state"] == "failed")
    (health,) = triggers.health()
    assert "outside the frame" in health["last_error"] and health["fix"] == "set a smaller region"
    assert bus.subscriptions() == [], "a failed trigger is no longer fed"
    triggers.stop()


@pytest.mark.parametrize(
    ("script", "why"),
    [
        ({"1": [["ended", "never"]]}, "never started"),
        ({"1": [["started", "e1"]], "2": [["started", "e1"]]}, "already open"),
    ],
)
def test_an_episode_out_of_order_is_dropped_and_counted(state_dir, bus, catalog, script, why):
    triggers = runner.TriggerSet("cam-1", bus=bus, state_dir=state_dir, catalog=catalog)
    triggers.apply([{"name": "scripted", "script": script}])
    _publish(bus, frames(4))
    _observed(triggers, "scripted", 4)
    (health,) = triggers.health()
    assert health["errors"] == 1 and why in health["last_error"]
    triggers.stop(reason=None)
    assert len([r for r in ledger.read(state_dir) if r["kind"] == "started"]) == (
        1 if "open" in why else 0
    )


def test_an_event_for_another_stream_is_refused(state_dir, bus, catalog):
    triggers = runner.TriggerSet("cam-1", bus=bus, state_dir=state_dir, catalog=catalog)
    triggers.apply(
        [{"name": "scripted", "stream_override": "cam-9", "script": {"1": [["occurred", "x"]]}}]
    )
    _publish(bus, frames(3))
    _observed(triggers, "scripted", 3)
    (health,) = triggers.health()
    assert health["errors"] == 1 and "cam-9" in health["last_error"]
    triggers.stop()
    assert ledger.read(state_dir) == []


def test_a_slow_trigger_drops_its_oldest_frames_and_nobody_else_waits(state_dir, bus, catalog):
    triggers = runner.TriggerSet("cam-1", bus=bus, state_dir=state_dir, catalog=catalog)
    triggers.apply([{"name": "scripted", "sleep_s": 0.05}, {"name": "other"}])
    t0 = time.monotonic()
    _publish(bus, frames(60), pace_s=0.005)
    assert time.monotonic() - t0 < 1.0, "publishing never waits for a trigger"
    _observed(triggers, "other", 60)
    health = {h["name"]: h for h in triggers.health()}
    assert health["scripted"]["dropped"] > 0 and health["other"]["dropped"] == 0
    triggers.stop()


def test_health_merges_the_triggers_own_counters(state_dir, bus, catalog):
    triggers = runner.TriggerSet("cam-1", bus=bus, state_dir=state_dir, catalog=catalog)
    triggers.apply([{"name": "scripted", "severity": "warning"}])
    _publish(bus, frames(3))
    _observed(triggers, "scripted", 3)
    (health,) = triggers.health()
    assert health["trigger"] == "scripted@1.0.0" and health["health"] == {"seen": 3}
    assert health["config"] == {"severity": "warning"}
    triggers.stop()
    assert health["state"] == "running"


def test_a_stopped_trigger_is_told_to_stop(state_dir, bus, catalog):
    triggers = runner.TriggerSet("cam-1", bus=bus, state_dir=state_dir, catalog=catalog)
    triggers.apply([{"name": "scripted"}])
    trigger = triggers.runners["scripted"].trigger
    assert trigger.started
    triggers.stop()
    assert trigger.stopped


def test_motion_runs_in_a_runner_like_any_other_trigger(state_dir, bus, catalog):
    triggers = runner.TriggerSet("cam-1", bus=bus, state_dir=state_dir, catalog=catalog)
    triggers.apply([{"name": "motion"}])
    assert isinstance(triggers.runners["motion"].trigger, MotionTrigger)
    triggers.stop()
