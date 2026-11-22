# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""VID-018: when the view, the exposure or a region cannot be trusted, the region reads
``unknown`` with the reason, and a change of light alone never gives another label.

spec-video §Design, ``reference-match``: the view guard compares the landmarks (the
parts of the view that never move) with their picture; the exposure guard bounds the
mean luma and the clipped fraction; a region without contrast is ``flat``. The
comparison ignores gain and offset, so a uniform change of light leaves the label
alone; an uneven one moves the landmarks too and fails the view guard, which is the
known limit the spec writes down.
"""

from __future__ import annotations

import pytest

from .matching import changes, kinds, run, trigger_for, watch
from .scenes import GUARDS, Segment, door_reference_set, door_scene


@pytest.fixture
def doors(tmp_path):
    return door_reference_set(tmp_path / "doors")


def _states(seen, after: float = 0.0) -> set[str]:
    return {state for t, state, _ in seen if t >= after}


def _reasons(seen, after: float = 0.0) -> set[tuple[str, ...]]:
    return {reasons for t, state, reasons in seen if t >= after and state == "unknown"}


def test_a_shifted_view_is_unknown_with_reason_view(doors):
    """The camera was bumped: the door's picture may even still match, but the view does not."""
    seen, events = watch(trigger_for(doors), door_scene(Segment(20, "closed", shift=(0.06, 0.04))))
    assert _states(seen) == {"unknown"} and _reasons(seen) == {("view",)}
    assert events == []


@pytest.mark.parametrize(
    ("gain", "offset", "what"),
    [(0.2, 0.0, "the light went out"), (1.6, 30.0, "the picture is burned out")],
)
def test_bad_exposure_is_unknown_with_reason_exposure(doors, gain, offset, what):
    seen, _ = watch(trigger_for(doors), door_scene(Segment(20, "closed", gain=gain, offset=offset)))
    assert _states(seen) == {"unknown"}, what
    assert all("exposure" in reasons for reasons in _reasons(seen)), what


def test_a_flat_region_is_unknown_with_reason_flat(tmp_path):
    """A lamp shining at the lens over the door leaves nothing to compare. The guard allows
    the clipping here, so ``flat`` is the only reason."""
    lenient = door_reference_set(tmp_path / "lenient", guards={**GUARDS, "clipped": 0.2})
    trigger = trigger_for(lenient)
    seen, _ = watch(trigger, door_scene(Segment(20, "flat")))
    assert _states(seen) == {"unknown"} and _reasons(seen) == {("flat",)}
    assert trigger.health()["regions"]["door"]["scores"] == {"closed": None, "open": None}


def test_the_reasons_come_in_one_order_view_exposure_flat(doors):
    """Light from one side moves the landmarks, and the lamp over the door clips it white."""
    seen, _ = watch(trigger_for(doors), door_scene(Segment(3, "flat", ramp=0.5)))
    assert ("view", "exposure", "flat") in _reasons(seen)


@pytest.mark.parametrize(
    ("gain", "offset"),
    [(0.5, 0.0), (0.7, 30.0), (0.8, -10.0), (1.15, 0.0), (1.3, 0.0), (0.3, 0.0), (0.15, 0.0)],
)
def test_a_change_of_light_alone_never_produces_a_different_label(doors, gain, offset):
    """Brighter, dimmer, or so far either way that the exposure guard fails: closed or unknown,
    never open."""
    scene = door_scene(Segment(20, "closed"), Segment(30, "closed", gain=gain, offset=offset))
    trigger = trigger_for(doors)
    seen, events = watch(trigger, scene)
    assert _states(seen) <= {"closed", "unknown", "changing"}
    assert all(e.detail.get("to") in (None, "closed") for e in events)
    assert trigger.health()["regions"]["door"]["counts"].get("open", 0) == 0


@pytest.mark.parametrize("ramp", [0.1, 0.3, 0.5])
def test_an_uneven_change_of_light_is_unknown_at_worst_never_another_label(doors, ramp):
    """The known limit: a lamp on one side moves the landmarks, the view guard fails, and the
    region reads unknown until the light returns. It is never read as open."""
    scene = door_scene(
        Segment(20, "closed"), Segment(30, "closed", ramp=ramp), Segment(20, "closed")
    )
    trigger = trigger_for(doors)
    seen, events = watch(trigger, scene)
    assert _states(seen) <= {"closed", "unknown", "changing"}
    assert all(e.detail.get("to") in (None, "closed") for e in events)
    if ramp >= 0.3:
        assert ("view",) in _reasons(seen, after=20.0)
        assert [e.detail["returned"] for e in events if e.kind == "ended"] == [True]


def test_after_leaving_a_failed_guard_is_unknown_not_changing(doors):
    scene = door_scene(Segment(10, "closed"), Segment(10, "closed", gain=0.2))
    seen, events = watch(trigger_for(doors), scene)
    assert seen[-1][1] == "unknown" and "exposure" in seen[-1][2]
    (started,) = changes(events)
    assert started.detail["state"] == "unknown" and "exposure" in started.detail["reasons"]


def test_when_the_guard_passes_again_the_label_returns(doors):
    scene = door_scene(
        Segment(10, "closed"), Segment(10, "closed", shift=(0.06, 0.04)), Segment(10, "closed")
    )
    events = run(trigger_for(doors), scene)
    assert kinds(events) == ["occurred", "started", "ended"]
    assert events[-1].detail["returned"] is True


def test_health_gives_the_last_view_score(doors):
    trigger = trigger_for(doors)
    run(trigger, door_scene(Segment(2, "closed", shift=(0.06, 0.04))))
    view = trigger.health()["regions"]["door"]["view"]
    assert view is not None and view < GUARDS["view"]
