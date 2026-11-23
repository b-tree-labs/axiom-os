# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""VID-019 and VID-020: a label is reported after its dwell, a change is one episode, and a
region is never silent for longer than its heartbeat.

spec-video §Design, ``reference-match``, over time: a label is reported once its run
of matched frames has lasted ``dwell_s``; a reported label is left once no matched
frame has agreed with it for ``leave_s``; leaving writes ``started`` and reaching the
next label writes ``ended`` under the same key, with ``from``, ``to``, ``since`` and
``returned``. Every ``heartbeat_s`` since its last event a region writes an
``occurred``, in every state. Times are the frames' own. A reconnect starts a new run
and keeps the rest.
"""

from __future__ import annotations

import pytest

from .matching import at, changes, kinds, run, trigger_for, watch
from .scenes import T0, Segment, door_reference_set, door_scene, seconds_after_t0


@pytest.fixture
def doors(tmp_path):
    return door_reference_set(tmp_path / "doors")


# ---- the dwell ----------------------------------------------------------------------


def test_a_label_is_reported_only_after_the_dwell(doors):
    trigger = trigger_for(doors, dwell_s=8.0)
    seen, events = watch(trigger, door_scene(Segment(12, "closed")))
    reported = [t for t, state, _ in seen if state == "closed"]
    assert reported[0] == 8.0, "first seen at 0 s, held for 8 s"
    assert all(state == "unknown" for t, state, _ in seen if t < 8.0)
    assert all(reasons == ("settling",) for t, state, reasons in seen if t < 8.0)
    (first,) = events
    assert at(first) == 8.0 and first.detail["since"] == T0.isoformat()


def test_a_label_interrupted_before_its_dwell_starts_again(doors):
    scene = door_scene(Segment(4, "closed"), Segment(2, "stranger"), Segment(10, "closed"))
    (first,) = run(trigger_for(doors), scene)
    assert at(first) == 11.0 and seconds_after_t0(first.detail["since"]) == 6.0


# ---- a change -----------------------------------------------------------------------


def test_one_change_is_one_started_and_one_ended_under_one_key(doors):
    trigger = trigger_for(doors)
    events = run(trigger, door_scene(Segment(20, "closed"), Segment(20, "open")))
    occurred, started, ended = events
    assert (occurred.kind, started.kind, ended.kind) == ("occurred", "started", "ended")
    assert started.incident_key == ended.incident_key
    assert started.incident_key.startswith("cam-1:reference-match@0.1.0:door-")
    assert started.incident_key != occurred.incident_key
    # Left once nothing agreed for leave_s (the last closed frame was at 19 s)...
    assert at(started) == 21.0
    assert started.detail["from"] == "closed" and started.detail["state"] == "changing"
    assert seconds_after_t0(started.detail["last_seen"]) == 19.0
    # ...and reached once open had held for dwell_s from when it was first seen.
    assert at(ended) == 25.0
    detail = ended.detail
    assert (detail["from"], detail["to"], detail["returned"]) == ("closed", "open", False)
    assert seconds_after_t0(detail["since"]) == 20.0
    assert detail["started"] == started.ts
    health = trigger.health()["regions"]["door"]
    assert health["changes"] == 1 and health["returned"] == 0 and health["open"] is None


def test_a_disagreement_shorter_than_leave_s_is_not_a_change(doors):
    scene = door_scene(Segment(10, "closed"), Segment(1, "stranger"), Segment(10, "closed"))
    seen, events = watch(trigger_for(doors), scene)
    assert kinds(events) == ["occurred"]
    assert {state for t, state, _ in seen if t >= 5.0} == {"closed"}


def test_leaving_and_returning_to_the_same_label_is_flagged_and_is_not_a_change(doors):
    scene = door_scene(Segment(10, "closed"), Segment(5, "stranger"), Segment(10, "closed"))
    trigger = trigger_for(doors)
    started, ended = changes(run(trigger, scene))
    assert started.detail["from"] == "closed"
    assert (ended.detail["from"], ended.detail["to"], ended.detail["returned"]) == (
        "closed",
        "closed",
        True,
    )
    health = trigger.health()["regions"]["door"]
    assert health["returned"] == 1 and health["changes"] == 0, "a return is not a change of label"


def test_two_changes_are_two_episodes_with_two_keys(doors):
    scene = door_scene(Segment(20, "closed"), Segment(20, "open"), Segment(20, "closed"))
    events = changes(run(trigger_for(doors), scene))
    assert kinds(events) == ["started", "ended", "started", "ended"]
    assert events[0].incident_key == events[1].incident_key
    assert events[2].incident_key == events[3].incident_key
    assert events[0].incident_key != events[2].incident_key
    assert [(e.detail["from"], e.detail["to"]) for e in events if e.kind == "ended"] == [
        ("closed", "open"),
        ("open", "closed"),
    ]


def test_a_dwell_shorter_than_leave_s_writes_started_and_ended_together(doors):
    trigger = trigger_for(doors, dwell_s=1.0, leave_s=4.0)
    started, ended = changes(run(trigger, door_scene(Segment(10, "closed"), Segment(10, "open"))))
    assert at(started) == at(ended) == 11.0
    assert (ended.detail["from"], ended.detail["to"]) == ("closed", "open")
    assert seconds_after_t0(started.detail["last_seen"]) == 9.0, "the last frame that read closed"


def test_the_times_are_the_frames_own_not_the_machines(doors):
    """Frames fed as fast as the machine can are still timed by their ``ts``."""
    scene = door_scene(Segment(20, "closed"), Segment(20, "open"), fps=10)
    events = run(trigger_for(doors), scene)
    assert [at(e) for e in events] == [5.0, 21.0, 25.0]


def test_while_changing_a_frame_that_reads_no_label_is_changing_not_unknown(doors):
    scene = door_scene(Segment(10, "closed"), Segment(10, "stranger"))
    seen, _ = watch(trigger_for(doors), scene)
    assert seen[-1][1:] == ("changing", ("neither",))


# ---- the heartbeat --------------------------------------------------------------------


def _heartbeats(events):
    return [e for e in events if e.kind == "occurred" and e.detail.get("heartbeat")]


def test_a_heartbeat_arrives_in_every_state_unknown_included(doors):
    scene = door_scene(
        Segment(30, "stranger"),  # unknown from the start
        Segment(30, "closed"),  # closed
        Segment(30, "stranger"),  # changing
        Segment(30, "closed", shift=(0.06, 0.04)),  # unknown: the camera was bumped
    )
    events = run(trigger_for(doors, heartbeat_s=10.0), scene)
    states = {e.detail["state"] for e in _heartbeats(events)}
    assert states == {"unknown", "closed", "changing"}
    unknown = [e for e in _heartbeats(events) if e.detail["state"] == "unknown"]
    assert {tuple(e.detail["reasons"]) for e in unknown} >= {("neither",), ("view",)}
    assert all(e.score is None for e in unknown)


def test_while_frames_arrive_a_region_is_never_silent_longer_than_its_heartbeat(doors):
    scene = door_scene(
        Segment(45, "stranger"), Segment(45, "closed"), Segment(45, "closed", gain=0.2)
    )
    events = run(trigger_for(doors, heartbeat_s=10.0), scene)
    times = [0.0, *(at(e) for e in events), 135.0]
    gaps = [b - a for a, b in zip(times, times[1:], strict=False)]
    assert max(gaps) <= 10.0 + 1.0, gaps  # to within one sample_s


def test_heartbeats_share_the_regions_status_key_and_a_change_has_its_own(doors):
    scene = door_scene(Segment(30, "closed"), Segment(30, "open"))
    events = run(trigger_for(doors, heartbeat_s=10.0), scene)
    first = events[0]
    assert first.detail["first"] is True
    status = {e.incident_key for e in events if e.kind == "occurred"}
    assert status == {first.incident_key}
    assert ":door-status-" in first.incident_key
    (change,) = {e.incident_key for e in changes(events)}
    assert change not in status


def test_a_heartbeat_says_since_when_the_state_has_held(doors):
    events = run(trigger_for(doors, heartbeat_s=10.0), door_scene(Segment(30, "closed")))
    beat = _heartbeats(events)[0]
    assert beat.detail["state"] == "closed" and beat.detail["since"] == T0.isoformat()
    assert at(beat) == 15.0, "ten seconds after the first report"


# ---- a reconnect and the frames' clock ----------------------------------------------


def test_a_reconnect_keeps_the_reported_label_and_writes_nothing(doors):
    scene = door_scene(Segment(10, "closed"), Segment(10, "closed", session="s2", gap_s=1.0))
    trigger = trigger_for(doors)
    assert kinds(run(trigger, scene)) == ["occurred"]
    assert trigger.health()["regions"]["door"]["state"] == "closed"


def test_after_an_outage_longer_than_leave_s_a_disagreeing_frame_leaves_the_label_at_once(doors):
    scene = door_scene(Segment(10, "closed"), Segment(10, "open", session="s2", gap_s=30.0))
    started, ended = changes(run(trigger_for(doors), scene))
    back = 10.0 + 30.0  # the new session's first frame
    assert at(started) == back and started.detail["from"] == "closed"
    assert seconds_after_t0(started.detail["last_seen"]) == 9.0
    assert at(ended) == back + 5.0 and ended.detail["to"] == "open"
    assert seconds_after_t0(ended.detail["since"]) == back


def test_a_label_seen_before_an_outage_does_not_complete_its_dwell_after_it(doors):
    scene = door_scene(Segment(3, "closed"), Segment(3, "closed", session="s2", gap_s=1.0))
    seen, events = watch(trigger_for(doors), scene)
    assert events == [], "3 s before and 3 s after is not 5 s seen"
    assert seen[-1][1] == "unknown"


def test_a_frame_earlier_than_the_last_matched_one_is_matched(doors):
    """A clock stepped back: the next frame is matched, and its run starts again."""
    trigger = trigger_for(doors)
    run(trigger, door_scene(Segment(4, "closed")))
    matched = trigger.health()["matched"]
    run(trigger, door_scene(Segment(1, "closed")))  # times start again at T0
    assert trigger.health()["matched"] == matched + 1
