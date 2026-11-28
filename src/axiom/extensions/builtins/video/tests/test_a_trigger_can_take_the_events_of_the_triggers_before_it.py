# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""VID-027..030 (ADR-264): a trigger can take the events of the triggers before it.

An attachment names with ``on`` the attachments on its source whose events it takes.
After the runner writes an event to the journal, it gives a copy to every trigger on
the attachment that wrote it, in the order written, through ``on_event``, on that
trigger's own thread and merged with its frames in frame time. What ``on_event``
returns is journaled like any event, so triggers chain. An ``on`` that cannot work is
refused with its fix, and a trigger behind on its events drops its oldest and counts
them while nothing upstream waits.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime

import pytest

from axiom.extensions.builtins.video import ledger, runner
from axiom.extensions.builtins.video import triggers as vt
from axiom.extensions.builtins.video.bus import FrameBus

from .chain_fakes import BUILT, Listener
from .harness import until
from .scenes import frames, ts_at

SCRIPTED = "axiom.extensions.builtins.video.tests.trigger_fakes:ScriptedTrigger"
LISTENER = "axiom.extensions.builtins.video.tests.chain_fakes:Listener"
RELAY = "axiom.extensions.builtins.video.tests.chain_fakes:Relay"


@pytest.fixture
def catalog():
    BUILT.clear()
    return vt.TriggerCatalog(
        entries={
            "scripted": vt.TriggerEntry("scripted", SCRIPTED, "test"),
            "other": vt.TriggerEntry("other", SCRIPTED, "test"),
            "listener": vt.TriggerEntry("listener", LISTENER, "test"),
            "relay": vt.TriggerEntry("relay", RELAY, "test"),
            "motion": vt.TriggerEntry(
                "motion", "axiom.extensions.builtins.video.motion:MotionTrigger", "builtin"
            ),
        }
    )


@pytest.fixture
def bus():
    return FrameBus("cam-1")


@pytest.fixture
def triggers(state_dir, bus, catalog):
    found = runner.TriggerSet("cam-1", bus=bus, state_dir=state_dir, catalog=catalog)
    yield found
    found.stop()


def _publish(bus, n: int, *, pace_s: float = 0.002, start: int = 0) -> None:
    for frame in frames(n, start=start, noise=0.0):
        bus.publish(frame)
        if pace_s:
            time.sleep(pace_s)


def _trigger(triggers, name):
    return triggers.runners[name].trigger


def _health(triggers, name):
    return next(h for h in triggers.health() if h["name"] == name)


def _script(**seqs):
    """``_script(s3=[["started", "e1"]])`` → the ScriptedTrigger config for it."""
    return {k.lstrip("s"): v for k, v in seqs.items()}


def _when(ts: str) -> float:
    return datetime.fromisoformat(ts).timestamp()


# ---- VID-027: given each event an upstream wrote, after the write, in order --------


@pytest.mark.acceptance("VID-027")
def test_a_trigger_on_another_is_given_each_event_it_wrote_in_order(state_dir, bus, triggers):
    result = triggers.apply(
        [
            {"name": "scripted", "script": _script(s3=[["started", "e1"]], s9=[["ended", "e1"]])},
            {"name": "listener", "on": ["scripted"], "journal": str(state_dir)},
        ]
    )
    assert result["errors"] == [] and result["running"] == ["listener", "scripted"]
    _publish(bus, 15)
    listener = _trigger(triggers, "listener")
    until(lambda: len(listener.events) >= 2)
    written = [r for r in ledger.read(state_dir) if r["trigger"] == "scripted@1.0.0"]
    assert [e.as_dict() for e in listener.events] == written
    assert [e.kind for e in listener.events] == ["started", "ended"]
    assert listener.in_journal == [True, True], "an event is given after it is written"


@pytest.mark.acceptance("VID-027")
def test_a_kind_narrows_what_it_is_given(bus, triggers):
    triggers.apply(
        [
            {"name": "scripted", "script": _script(s3=[["started", "e1"]], s9=[["ended", "e1"]])},
            {"name": "listener", "on": ["scripted:ended"]},
        ]
    )
    _publish(bus, 15)
    listener = _trigger(triggers, "listener")
    until(lambda: len(listener.events) >= 1)
    _publish(bus, 5, start=15)
    until(lambda: _health(triggers, "listener")["frames"] >= 15)
    assert [e.kind for e in listener.events] == ["ended"]


@pytest.mark.acceptance("VID-027")
def test_what_on_event_returns_is_journaled_and_reaches_the_trigger_after_it(
    state_dir, bus, triggers
):
    result = triggers.apply(
        [
            {"name": "scripted", "script": _script(s2=[["occurred", "a"]], s6=[["occurred", "b"]])},
            {"name": "listener", "on": ["scripted"], "respond": "occurred"},
            {"name": "relay", "on": ["listener:occurred"]},
        ]
    )
    assert result["errors"] == []
    _publish(bus, 10)
    relay = _trigger(triggers, "relay")
    until(lambda: len(relay.events) >= 2)
    assert {e.trigger for e in relay.events} == {"listener@1.0.0"}
    causes = [e.detail["cause"] for e in relay.events]
    assert causes == ["cam-1:scripted@1.0.0:a", "cam-1:scripted@1.0.0:b"]
    journaled = [r for r in ledger.read(state_dir) if r["trigger"] == "listener@1.0.0"]
    assert [r["incident_key"] for r in journaled] == [e.incident_key for e in relay.events]


@pytest.mark.acceptance("VID-027")
def test_without_on_a_trigger_is_given_nothing(bus, triggers):
    triggers.apply(
        [
            {"name": "scripted", "script": _script(s2=[["occurred", "a"]])},
            {"name": "listener"},
        ]
    )
    _publish(bus, 8)
    until(lambda: _health(triggers, "listener")["frames"] >= 8)
    assert _trigger(triggers, "listener").events == []


@pytest.mark.acceptance("VID-027")
def test_on_event_runs_on_the_triggers_own_thread_and_never_beside_observe(
    state_dir, bus, triggers
):
    script = {str(i): [["occurred", f"o{i}"]] for i in range(0, 40, 2)}
    triggers.apply(
        [
            {"name": "scripted", "script": script},
            {"name": "listener", "on": ["scripted"], "sleep_s": 0.005},
        ]
    )
    _publish(bus, 40, pace_s=0.001)
    until(
        lambda: (
            _health(triggers, "scripted")["frames"] + _health(triggers, "scripted")["dropped"] >= 40
        )
    )
    written = len([r for r in ledger.read(state_dir) if r["trigger"] == "scripted@1.0.0"])
    assert written >= 5
    listener = _trigger(triggers, "listener")
    until(lambda: len(listener.events) == written)
    assert len(listener.threads) == 1
    assert threading.get_ident() not in listener.threads
    assert listener.overlaps == 0


@pytest.mark.acceptance("VID-027")
def test_each_trigger_is_given_its_own_copy(state_dir, bus, triggers):
    triggers.apply(
        [
            {"name": "scripted", "script": _script(s2=[["occurred", "a"]])},
            {"name": "listener", "on": ["scripted"], "mutate": True},
            {"name": "relay", "on": ["scripted"]},
        ]
    )
    _publish(bus, 6)
    listener, relay = _trigger(triggers, "listener"), _trigger(triggers, "relay")
    until(lambda: listener.events and relay.events)
    assert listener.events[0].detail.get("touched") == "listener"
    assert "touched" not in relay.events[0].detail
    assert all("touched" not in r["detail"] for r in ledger.read(state_dir))


@pytest.mark.acceptance("VID-027")
def test_the_ended_the_runner_writes_for_a_replaced_upstream_is_given(bus, triggers):
    upstream = {"name": "scripted", "script": _script(s3=[["started", "e1"]])}
    triggers.apply([upstream, {"name": "listener", "on": ["scripted"]}])
    _publish(bus, 6)
    listener = _trigger(triggers, "listener")
    until(lambda: len(listener.events) >= 1)
    result = triggers.apply(
        [{**upstream, "severity": "warning"}, {"name": "listener", "on": ["scripted"]}],
        atomic=True,
    )
    assert result["applied"] and _trigger(triggers, "listener") is listener, "listener kept"
    until(lambda: len(listener.events) >= 2, timeout=5)  # no frames follow: the poll gives it
    ended = listener.events[1]
    assert ended.kind == "ended" and ended.incident_key == listener.events[0].incident_key
    assert ended.detail["closed_by"] == runner.CLOSED_BY
    assert ended.detail["reason"] == runner.REASON_RECONFIGURED


@pytest.mark.acceptance("VID-027")
def test_the_factory_never_sees_on(triggers):
    triggers.apply([{"name": "scripted"}, {"name": "listener", "on": "scripted:ended"}])
    assert "on" not in _trigger(triggers, "listener").given
    assert triggers.runners["listener"].config["on"] == ["scripted:ended"]


@pytest.mark.acceptance("VID-027")
def test_an_on_event_that_raises_is_counted_and_the_trigger_goes_on(bus, triggers):
    triggers.apply(
        [
            {"name": "scripted", "script": _script(s1=[["occurred", "a"]], s3=[["occurred", "b"]])},
            {"name": "listener", "on": ["scripted"], "raise_on_event": True},
        ]
    )
    _publish(bus, 10)
    until(lambda: _health(triggers, "listener")["errors"] >= 2)
    _publish(bus, 5, start=10)
    until(lambda: _health(triggers, "listener")["frames"] >= 12)
    health = _health(triggers, "listener")
    assert health["state"] == "running" and "scripted failure" in health["last_error"]


@pytest.mark.acceptance("VID-027")
def test_a_config_error_from_on_event_fails_the_trigger_with_its_fix(bus, triggers):
    triggers.apply(
        [
            {"name": "scripted", "script": _script(s1=[["occurred", "a"]])},
            {"name": "listener", "on": ["scripted"], "config_error_on_event": True},
        ]
    )
    _publish(bus, 5)
    until(lambda: _health(triggers, "listener")["state"] == "failed")
    assert _health(triggers, "listener")["fix"] == "set a smaller region"
    assert _health(triggers, "scripted")["state"] == "running"


# ---- VID-028: events and frames in frame time -------------------------------------


def _runner(bus, trigger, *, on=("upstream",)):
    found = runner.TriggerRunner(
        "cam-1", "listener", {"on": list(on)}, trigger, bus=bus, record=lambda e: None
    )
    found.start()
    return found


def _given(ts: str, episode: str = "x") -> vt.TriggerEvent:
    return vt.TriggerEvent(
        stream="cam-1",
        trigger="upstream@1.0.0",
        ts=ts,
        kind="occurred",
        incident_key=vt.incident_key("cam-1", "upstream@1.0.0", episode),
    )


@pytest.mark.acceptance("VID-028")
def test_an_event_waits_for_the_first_frame_at_or_after_its_time(bus):
    listener = Listener({})
    found = _runner(bus, listener)
    try:
        assert found.give(_given(ts_at(5)))
        _publish(bus, 10, pace_s=0.01)
        until(lambda: len(listener.frames) >= 10)
        order = [x.seq if kind == "frame" else "event" for kind, x in listener.timeline]
        assert order == [0, 1, 2, 3, 4, "event", 5, 6, 7, 8, 9]
    finally:
        found.stop()


@pytest.mark.acceptance("VID-028")
def test_a_lagging_trigger_still_sees_an_event_before_the_frames_after_it(
    state_dir, bus, triggers, monkeypatch
):
    """A trigger that is behind is given each event before the first frame at or after it.

    The lag is held, not timed. The listener waits inside its first frame while the
    upstream sees every frame and writes its events, each frame published only once
    the upstream has finished the one before. Then the listener is let go, with
    events pending whose times fall before, between and after the frames it still
    has. A lag made of sleeps let the upstream's journal write lose a 20 ms race on
    a busy machine, and the event arrived after frames it should have preceded
    (#2125). The one-second cap on an event's wait has its own test below; here it
    is lifted, so a slow machine cannot deliver an event early.
    """
    monkeypatch.setattr(runner, "EVENT_WAIT_S", 3600.0)
    at, total, hold = (5, 12, 19, 26), 30, threading.Event()
    script = {str(i): [["occurred", f"o{i}"]] for i in at}
    triggers.apply(
        [
            {"name": "scripted", "script": script},
            {"name": "listener", "on": ["scripted"], "hold": hold},
        ]
    )
    listener = _trigger(triggers, "listener")
    try:
        for n, frame in enumerate(frames(total, noise=0.0)):
            bus.publish(frame)
            until(lambda n=n: _health(triggers, "scripted")["frames"] == n + 1, every=0.002)
            if n == 0:
                assert listener.holding.wait(timeout=20), "the listener never took its first frame"
        written = [r for r in ledger.read(state_dir) if r["trigger"] == "scripted@1.0.0"]
        assert len(written) == len(at), "the upstream wrote every event before the listener moved"
    finally:
        hold.set()
    until(lambda: len(listener.events) == len(written) and listener.frames[-1].seq == total - 1)
    assert [e.as_dict() for e in listener.events] == written

    # What the listener still had: its first frame, then the newest its queue kept.
    kept = [f.seq for f in listener.frames]
    assert kept == [0, *range(total - (len(kept) - 1), total)] and 1 < len(kept) < total, kept
    for i, (kind, item) in enumerate(listener.timeline):
        if kind != "event":
            continue
        before = [x for k, x in listener.timeline[:i] if k == "frame"]
        after = [x for k, x in listener.timeline[i + 1 :] if k == "frame"]
        assert all(_when(f.ts) < _when(item.ts) for f in before)
        assert not after or _when(after[0].ts) >= _when(item.ts)
    # Events before, between and after the frames it kept: each in its place.
    order = [
        x.seq if kind == "frame" else x.incident_key.rsplit(":", 1)[-1]
        for kind, x in listener.timeline
    ]
    expected: list = [0]
    pending = [f"o{i}" for i in at]
    for seq in kept[1:]:
        while pending and int(pending[0][1:]) <= seq:
            expected.append(pending.pop(0))
        expected.append(seq)
    assert not pending and order == expected, (order, expected)


@pytest.mark.acceptance("VID-028")
def test_with_no_frames_an_event_is_given_within_the_poll(bus):
    listener = Listener({})
    found = _runner(bus, listener)
    try:
        offered = time.monotonic()
        found.give(_given(ts_at(5)))
        until(lambda: listener.events, timeout=3)
        assert listener.given_at[0] - offered < 0.6
    finally:
        found.stop()


@pytest.mark.acceptance("VID-028")
def test_an_event_ahead_of_the_frames_is_not_held_past_a_second(bus):
    listener = Listener({})
    found = _runner(bus, listener)
    stop = threading.Event()

    def feed():
        i = 0
        while not stop.is_set():
            for frame in frames(1, start=i, noise=0.0):
                bus.publish(frame)
            i += 1
            time.sleep(0.02)

    feeder = threading.Thread(target=feed, daemon=True)
    feeder.start()
    try:
        until(lambda: len(listener.frames) >= 3)
        offered = time.monotonic()
        found.give(_given(ts_at(100_000)))  # hours ahead of every frame that will come
        until(lambda: listener.events, timeout=4)
        waited = listener.given_at[0] - offered
        assert runner.EVENT_WAIT_S - 0.05 <= waited < runner.EVENT_WAIT_S + 0.6
    finally:
        stop.set()
        feeder.join(timeout=2)
        found.stop()


# ---- VID-029: an on that cannot work is refused with its fix ----------------------


@pytest.mark.acceptance("VID-029")
@pytest.mark.parametrize(
    "on",
    [3, [""], ["scripted:middle"], ["scripted:"], [":ended"], [7], {"scripted": "ended"}, []],
    ids=["number", "empty", "unknown-kind", "no-kind", "no-name", "not-text", "table", "none"],
)
def test_a_malformed_on_is_refused_with_its_fix(on):
    with pytest.raises(vt.TriggerConfigError) as caught:
        vt.split_attachment({"name": "listener", "on": on})
    assert "on" in str(caught.value) and caught.value.fix


@pytest.mark.acceptance("VID-029")
def test_a_single_string_is_one_entry():
    _, config = vt.split_attachment({"name": "listener", "on": "scripted:ended"})
    assert config["on"] == ["scripted:ended"]


@pytest.mark.acceptance("VID-029")
@pytest.mark.parametrize(
    ("tables", "words"),
    [
        ([{"name": "listener", "on": ["scripted"]}], ["'scripted'", "not attached"]),
        ([{"name": "listener", "on": ["listener"]}], ["itself"]),
        (
            [{"name": "listener", "on": ["relay"]}, {"name": "relay", "on": ["listener"]}],
            ["cycle", "listener → relay → listener"],
        ),
        ([{"name": "scripted", "on": ["other"]}, {"name": "other"}], ["does not take events"]),
    ],
    ids=["not-on-this-source", "itself", "cycle", "takes-no-events"],
)
def test_an_on_that_cannot_work_is_refused_with_its_fix(catalog, tables, words):
    errors = runner.check_attachments(tables, catalog)
    assert len(errors) == 1, errors
    for word in words:
        assert word in errors[0]
    assert "Fix:" in errors[0]


@pytest.mark.acceptance("VID-029")
def test_a_reload_with_a_broken_on_changes_nothing(triggers):
    triggers.apply([{"name": "scripted"}])
    before = triggers.runners["scripted"]
    result = triggers.apply(
        [{"name": "scripted"}, {"name": "listener", "on": ["nobody"]}], atomic=True
    )
    assert result["applied"] is False and result["running"] == ["scripted"]
    assert any("'nobody'" in e for e in result["errors"])
    assert triggers.runners == {"scripted": before}


@pytest.mark.acceptance("VID-029")
def test_at_capture_start_a_broken_on_fails_that_attachment_and_the_rest_run(triggers):
    result = triggers.apply([{"name": "scripted"}, {"name": "listener", "on": ["nobody"]}])
    assert result["running"] == ["scripted"]
    failed = _health(triggers, "listener")
    assert failed["state"] == "failed" and failed["fix"]


# ---- VID-030: bounded, counted, shown ---------------------------------------------


@pytest.mark.acceptance("VID-030")
def test_a_trigger_behind_drops_its_oldest_events_and_counts_them(bus):
    gate = threading.Event()
    listener = Listener({"gate": gate})
    found = _runner(bus, listener)
    try:
        found.give(_given(ts_at(0), "first"))
        until(lambda: listener.given_at, timeout=3)  # now held inside on_event
        began = time.monotonic()
        extra = runner.EVENTS_MAXSIZE + 44
        for i in range(extra):
            found.give(_given(ts_at(1), f"e{i}"))
        assert time.monotonic() - began < 0.5, "giving never waits on the trigger"
        gate.set()
        until(lambda: len(listener.events) >= 1 + runner.EVENTS_MAXSIZE)
        health = found.health()
        assert health["events_dropped"] == 44
        assert health["events_in"] == 1 + runner.EVENTS_MAXSIZE
        kept = [e.incident_key.rsplit(":", 1)[1] for e in listener.events[1:]]
        assert kept == [f"e{i}" for i in range(44, extra)], "the oldest are dropped"
    finally:
        gate.set()
        found.stop()


@pytest.mark.acceptance("VID-030")
def test_status_shows_what_a_trigger_is_on_and_its_counts(bus, triggers, capsys):
    from axiom.extensions.builtins.video.commands.cli import _print_triggers_of

    triggers.apply(
        [
            {"name": "scripted", "script": _script(s1=[["occurred", "a"]], s2=[["occurred", "b"]])},
            {"name": "listener", "on": ["scripted:occurred"]},
        ]
    )
    _publish(bus, 5)
    until(lambda: _health(triggers, "listener")["events_in"] >= 2)
    rows = {h["name"]: h for h in triggers.health()}
    assert rows["listener"]["on"] == ["scripted:occurred"]
    assert rows["listener"]["events_dropped"] == 0
    assert rows["scripted"]["on"] == [] and rows["scripted"]["events_in"] == 0
    _print_triggers_of({"triggers": list(rows.values()), "attached": list(rows)})
    out = capsys.readouterr().out
    assert "on scripted:occurred (2 events given)" in out


@pytest.mark.acceptance("VID-027")
def test_takes_events_is_part_of_the_public_surface():
    from axiom.extensions.builtins.video import TakesEvents

    assert isinstance(Listener({}), TakesEvents)
    assert not isinstance(object(), TakesEvents)
