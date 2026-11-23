# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""VID-021: several regions ride one attachment, report independently, and each event names
its region; ``health()`` gives each region's state, since when, its scores and its counts.

A source takes one attachment per trigger name (spec-video §Trigger runner), so a set
that watches two things in one view declares two regions, and one ``reference-match``
attachment watches both.
"""

from __future__ import annotations

import pytest

from .matching import at, changes, run, trigger_for
from .scenes import TWO_DOORS, Segment, T0, door_reference_set, door_scene

REGIONS = {
    "door": {"closed": "closed", "open": "open"},
    "side-door": {"shut": "closed", "wide": "open"},
}


@pytest.fixture
def two(tmp_path):
    return door_reference_set(tmp_path / "two", regions=REGIONS, doors=TWO_DOORS)


def _scene(*segments):
    return door_scene(*segments, doors=TWO_DOORS)


def _of(events, region):
    return [e for e in events if e.detail["region"] == region]


def test_two_regions_in_one_attachment_report_independently(two):
    scene = _scene(
        Segment(20, {"door": "closed", "side-door": "closed"}),
        Segment(20, {"door": "open", "side-door": "closed"}),
        Segment(20, {"door": "open", "side-door": "open"}),
    )
    events = run(trigger_for(two), scene)
    door, side = _of(events, "door"), _of(events, "side-door")
    assert [e.detail["state"] for e in door if e.kind == "occurred"] == ["closed"]
    assert [e.detail["state"] for e in side if e.kind == "occurred"] == ["shut"]
    assert [(e.detail["from"], e.detail["to"]) for e in door if e.kind == "ended"] == [
        ("closed", "open")
    ]
    assert [(e.detail["from"], e.detail["to"]) for e in side if e.kind == "ended"] == [
        ("shut", "wide")
    ]
    assert [at(e) for e in changes(door)] == [21.0, 25.0]
    assert [at(e) for e in changes(side)] == [41.0, 45.0]
    assert not {e.incident_key for e in door} & {e.incident_key for e in side}


def test_every_event_names_its_region_and_its_key_does_too(two):
    scene = _scene(Segment(20, "closed"), Segment(20, "open"))
    events = run(trigger_for(two, heartbeat_s=10.0), scene)
    assert {e.detail["region"] for e in events} == {"door", "side-door"}
    for event in events:
        episode = event.incident_key.rsplit(":", 1)[1]
        assert episode.startswith(event.detail["region"] + "-")
        assert episode.startswith("side-door-") == (event.detail["region"] == "side-door")


def test_a_failed_guard_makes_every_region_unknown(two):
    """The guards are the frame's: a bumped camera can see no region."""
    trigger = trigger_for(two)
    run(trigger, _scene(Segment(10, "closed", shift=(0.06, 0.04))))
    regions = trigger.health()["regions"]
    assert {r["state"] for r in regions.values()} == {"unknown"}
    assert {tuple(r["reasons"]) for r in regions.values()} == {("view",)}


def test_health_gives_each_regions_state_since_scores_and_counts_and_the_set(two):
    trigger = trigger_for(two)
    run(
        trigger,
        _scene(
            Segment(10, {"door": "closed", "side-door": "closed"}),
            Segment(10, {"door": "stranger", "side-door": "closed"}),
        ),
    )
    health = trigger.health()
    assert health["reference_set"] == {"name": "test-room", "version": "1", "path": str(two.path)}
    assert health["heuristic"] is True
    assert health["frames"] == 40 and health["matched"] == 20
    door, side = health["regions"]["door"], health["regions"]["side-door"]
    assert (side["state"], side["label"], side["since"]) == ("shut", "shut", T0.isoformat())
    assert side["counts"] == {"shut": 20, "wide": 0, "unknown": 0}
    assert set(side["scores"]) == {"shut", "wide"} and side["scores"]["shut"] > 0.9
    assert door["state"] == "changing" and door["label"] == "closed"
    assert door["reasons"] == ["neither"]
    assert door["counts"] == {"closed": 10, "open": 0, "unknown": 10}
    assert door["open"] is not None and door["open"].startswith("cam-1:reference-match@0.1.0:door-")
    assert side["open"] is None
    for row in (door, side):
        assert {"changes", "returned", "view"} <= set(row)


def test_health_before_any_frame_names_every_region(two):
    health = trigger_for(two).health()
    assert set(health["regions"]) == {"door", "side-door"}
    assert {r["state"] for r in health["regions"].values()} == {"unknown"}
    assert health["matched"] == 0 and health["analysis"] is None
