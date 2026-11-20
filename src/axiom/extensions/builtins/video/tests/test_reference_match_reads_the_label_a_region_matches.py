# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""VID-016 and VID-018: a region reads the label of the reference picture it matches, or none.

spec-video §Design, ``reference-match``: a matched frame is reduced to the set's
working size by a whole factor, compared with the landmarks (view) and checked for
exposure, and each region is correlated with each of its references. The best
wins if it clears its ``min_match`` and beats the runner-up by the region's
``margin``; otherwise the region reads ``unknown``, reason ``neither``. The labels
are the set's: the trigger has none of its own but ``changing`` and ``unknown``.
"""

from __future__ import annotations

import json

import pytest

from axiom.extensions.builtins.video import reference_match
from axiom.extensions.builtins.video.reference_match import ReferenceMatchTrigger

from .matching import attach, kinds, run, trigger_for, watch
from .scenes import WORKING, Segment, door_reference_set, door_scene


@pytest.fixture
def doors(tmp_path):
    return door_reference_set(tmp_path / "doors")


def test_the_trigger_says_what_it_is():
    assert (ReferenceMatchTrigger.name, ReferenceMatchTrigger.version) == (
        "reference-match",
        "0.1.0",
    )
    text = ReferenceMatchTrigger.description
    assert "heuristic" in text and "reference" in text
    assert "confidence" in text, "it says its numbers are not one"
    assert reference_match.METHOD == "reference_correlation"
    assert reference_match.RESERVED_LABELS == ("changing", "unknown")
    assert reference_match.DEFAULTS == {
        "sample_s": 1.0,
        "dwell_s": 15.0,
        "leave_s": 3.0,
        "heartbeat_s": 300.0,
    }
    assert reference_match.KEYS == {"reference_set", "severity", *reference_match.DEFAULTS}


def test_a_region_reads_the_label_of_the_reference_it_matches(doors):
    trigger = trigger_for(doors)
    events = run(trigger, door_scene(Segment(20, "closed")))
    (first,) = events
    assert first.kind == "occurred" and first.detail["first"] is True
    assert first.detail["state"] == "closed" and first.detail["region"] == "door"
    assert first.score is not None and first.score > 0.9
    assert trigger.health()["regions"]["door"]["state"] == "closed"


def test_the_other_reference_reads_its_own_label(doors):
    trigger = trigger_for(doors)
    (first,) = run(trigger, door_scene(Segment(20, "open")))
    assert first.detail["state"] == "open"


def test_the_labels_are_the_sets_and_none_is_built_in(tmp_path):
    """Nothing about doors is in the trigger: call the looks alpha and beta and it reads those."""
    named = door_reference_set(
        tmp_path / "ab", regions={"door": {"alpha": "closed", "beta": "open"}}
    )
    trigger = trigger_for(named)
    events = run(trigger, door_scene(Segment(20, "closed"), Segment(20, "open")))
    said = [e.detail.get("to") or e.detail["state"] for e in events if e.kind != "started"]
    assert said == ["alpha", "beta"]
    health = trigger.health()["regions"]["door"]
    assert set(health["scores"]) == {"alpha", "beta"}


def test_a_region_can_have_more_than_two_references(tmp_path):
    three = door_reference_set(
        tmp_path / "three", regions={"door": {"closed": "closed", "open": "open", "ajar": "ajar"}}
    )
    trigger = trigger_for(three)
    events = run(trigger, door_scene(Segment(20, "ajar")))
    assert [e.detail["state"] for e in events] == ["ajar"]


def test_a_region_that_matches_no_reference_reads_unknown_never_a_label(doors):
    """Someone standing in the doorway is neither closed nor open, and is not called either."""
    trigger = trigger_for(doors)
    seen, events = watch(trigger, door_scene(Segment(40, "stranger")))
    assert {state for _, state, _ in seen} == {"unknown"}
    assert seen[-1][2] == ("neither",)
    assert trigger.health()["regions"]["door"]["label"] is None
    assert events == [], "nothing is reported, and the heartbeat is ten minutes away"


def test_a_match_below_its_minimum_is_unknown(tmp_path):
    strict = door_reference_set(tmp_path / "strict", min_match=0.9999)
    seen, _ = watch(trigger_for(strict), door_scene(Segment(20, "closed")))
    assert {state for _, state, _ in seen} == {"unknown"} and seen[-1][2] == ("neither",)


def test_a_match_without_the_margin_over_the_runner_up_is_unknown(tmp_path):
    close_call = door_reference_set(tmp_path / "margin", margin=0.9)
    seen, _ = watch(trigger_for(close_call), door_scene(Segment(20, "closed")))
    assert {state for _, state, _ in seen} == {"unknown"} and seen[-1][2] == ("neither",)


def test_text_burned_into_the_picture_is_masked_out(tmp_path, doors):
    """The scene's clock changes every frame. Masked, it moves no score; unmasked, it does."""
    masked = trigger_for(doors)
    unmasked = trigger_for(door_reference_set(tmp_path / "unmasked", masks=()))
    scene = door_scene(Segment(3, "closed"))
    run(masked, scene)
    run(unmasked, scene)
    assert masked.health()["regions"]["door"]["view"] > 0.99
    assert unmasked.health()["regions"]["door"]["view"] < masked.health()["regions"]["door"]["view"]


def test_about_one_frame_per_sample_s_is_matched_and_the_rest_skipped(doors):
    trigger = trigger_for(doors, sample_s=2.0)
    scene = door_scene(Segment(20, "closed"))  # 40 frames, 2 a second
    run(trigger, scene)
    health = trigger.health()
    assert health["frames"] == 40 and health["matched"] == 10


def test_a_4k_frame_is_matched_at_the_working_size(tmp_path):
    big = door_reference_set(tmp_path / "4k", size=(3840, 2160), working=(960, 540))
    trigger = trigger_for(big, dwell_s=0.0)
    events = run(trigger, door_scene(Segment(1, "closed"), size=(3840, 2160), noise=0))
    assert [e.detail["state"] for e in events] == ["closed"]
    assert trigger.health()["analysis"] == {"working_size": [960, 540], "factor": 4}


def test_a_frame_at_twice_the_working_size_is_reduced_by_two(doors):
    trigger = trigger_for(doors)
    run(trigger, door_scene(Segment(1, "closed")))
    assert trigger.health()["analysis"] == {"working_size": list(WORKING), "factor": 2}


def test_every_event_says_it_is_a_heuristic_and_names_the_set_and_its_region(doors):
    trigger = trigger_for(doors)
    events = run(trigger, door_scene(Segment(20, "closed"), Segment(20, "open")))
    assert kinds(events) == ["occurred", "started", "ended"]
    for event in events:
        detail = event.detail
        assert detail["region"] == "door"
        assert detail["method"] == "reference_correlation" and detail["heuristic"] is True
        assert (detail["reference_set"], detail["reference_version"]) == ("test-room", "1")
        assert set(detail["scores"]) == {"closed", "open"}
        assert "view" in detail and "reasons" in detail and "state" in detail
        text = json.dumps(event.as_dict())
        assert "confidence" not in text, "no number is called a confidence"
        assert event.trigger == "reference-match@0.1.0"


def test_an_events_score_is_the_correlation_of_the_label_it_names(doors):
    trigger = trigger_for(doors)
    occurred, started, ended = run(trigger, door_scene(Segment(20, "closed"), Segment(20, "open")))
    assert occurred.score == occurred.detail["scores"]["closed"]
    assert started.score is None, "a started names no label: the region is changing"
    assert ended.score == ended.detail["scores"]["open"]


def test_a_configured_severity_is_carried_by_its_events(doors):
    events = run(trigger_for(doors, severity="warning"), door_scene(Segment(20, "closed")))
    assert {e.severity for e in events} == {"warning"}


def test_the_factory_is_given_only_its_table(doors):
    """Built from the attachment alone, like every trigger (ADR-240 D3)."""
    trigger = ReferenceMatchTrigger(attach(doors))
    assert trigger.config["reference_set"] == str(doors.path)


def test_importing_reference_match_does_not_import_numpy():
    import subprocess
    import sys

    from .harness import SRC

    code = (
        "import sys; import axiom.extensions.builtins.video.reference_match; "
        "import axiom.extensions.builtins.video.reference_set; print('numpy' in sys.modules)"
    )
    done = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env={"PYTHONPATH": str(SRC), "PATH": "/usr/bin:/bin"},
        timeout=60,
    )
    assert done.stdout.strip() == "False", done.stderr
