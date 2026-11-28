# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""VID-014 and the phase 2 acceptance: ``motion`` opens and closes ONE episode on a moving
object, stays silent on static noise, and says it is a heuristic.

spec-video §Design, ``motion``: crop to the region, grayscale, downscale so the
longer side is at most 320 px, difference against a running-average background
(α = 0.05), threshold at 25/255, ``score = changed / total``. ``score ≥ min_area``
for ``min_frames`` consecutive frames opens an episode; ``score < min_area`` for
``cooldown_s`` closes it. The background resets when the session does, and a new
session first LEARNS it, as a plain mean over ``LEARN_FRAMES`` = 1/α frames that are
not scored: a background that starts as one noisy frame carries the noise twice, so
a dim camera at night would open an episode after every reconnect.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from axiom.extensions.builtins.video import motion
from axiom.extensions.builtins.video.motion import MotionTrigger
from axiom.extensions.builtins.video.triggers import TriggerConfigError

from .scenes import Square, crossing, frames


def _run(trigger, scene):
    trigger.start()
    out = []
    for frame in scene:
        out.extend(trigger.observe(frame))
    return out


def _seconds(a: str, b: str) -> float:
    return (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds()


def test_the_algorithm_constants_are_the_specs():
    assert (motion.ALPHA, motion.THRESHOLD, motion.MAX_SIDE) == (0.05, 25, 320)
    assert motion.LEARN_FRAMES == round(1 / motion.ALPHA) == 20
    assert motion.DEFAULTS == {"roi": "full", "min_area": 0.02, "min_frames": 3, "cooldown_s": 5.0}


def test_a_square_crossing_the_room_is_one_episode_with_one_incident_key():
    events = _run(MotionTrigger({}), crossing())
    assert [e.kind for e in events] == ["started", "ended"]
    started, ended = events
    assert started.incident_key == ended.incident_key
    assert started.incident_key.startswith("cam-1:motion@0.1.0:")
    assert started.trigger == "motion@0.1.0" and started.severity == "info"
    assert started.score is not None and started.score >= 0.02
    # It opens within min_frames of the square appearing (frame 20 at 10 fps)...
    assert 0.2 <= _seconds("2026-11-10T12:00:00+00:00", started.ts) <= 2.6
    # ...and closes cooldown_s after the room has been quiet again.
    assert 5.0 <= _seconds(started.ts, ended.ts) <= 11.0
    for event in events:
        assert event.detail["method"] == "frame_diff" and event.detail["heuristic"] is True
        assert event.detail["roi"] == "full"
    assert ended.detail["score_peak"] >= started.score


@pytest.mark.parametrize("noise", [4.0, 8.0, 12.0, 15.0])
def test_static_noise_is_silent(noise):
    """σ = 12 per channel fired at the start of every session before the background was learned."""
    assert _run(MotionTrigger({}), frames(300, noise=noise)) == []


def test_a_pause_shorter_than_the_cooldown_is_still_one_episode():
    scene = frames(220, squares=(Square(20, 40), Square(60, 80, x0=150)))
    assert [e.kind for e in _run(MotionTrigger({}), scene)] == ["started", "ended"]


def test_two_crossings_far_apart_are_two_episodes_with_two_keys():
    scene = frames(300, squares=(Square(20, 40), Square(170, 190)))
    events = _run(MotionTrigger({}), scene)
    assert [e.kind for e in events] == ["started", "ended", "started", "ended"]
    assert events[0].incident_key == events[1].incident_key
    assert events[2].incident_key == events[3].incident_key
    assert events[0].incident_key != events[2].incident_key


def test_a_flicker_shorter_than_min_frames_is_silent():
    assert _run(MotionTrigger({}), frames(120, squares=(Square(30, 32, speed=0),))) == []


def test_min_frames_and_cooldown_come_from_the_config():
    scene = list(frames(200, squares=(Square(20, 50),)))
    eager = _run(MotionTrigger({"min_frames": 1, "cooldown_s": 0.5}), scene)
    patient = _run(MotionTrigger({"min_frames": 1, "cooldown_s": 8}), scene)
    assert [e.kind for e in eager] == [e.kind for e in patient] == ["started", "ended"]
    assert _seconds(eager[0].ts, eager[1].ts) < _seconds(patient[0].ts, patient[1].ts)


def test_a_change_of_light_counts_because_motion_is_a_heuristic():
    """Frame differencing cannot tell a person from a lamp. The trigger says so, and does not pretend."""
    events = _run(MotionTrigger({}), frames(200, light_step=(30, 60)))
    assert [e.kind for e in events] == ["started", "ended"]
    assert "light" in MotionTrigger.description


def test_only_the_region_is_watched():
    scene = list(frames(180, squares=(Square(20, 50, y=20, speed=2),)))  # stays in the left half
    assert _run(MotionTrigger({"roi": "180,0,140,180"}), scene) == []
    events = _run(MotionTrigger({"roi": "0,0,160,180"}), scene)
    assert [e.kind for e in events] == ["started", "ended"]
    assert events[0].detail["roi"] == "0,0,160,180"


def test_the_background_resets_with_the_session():
    """A camera that comes back brighter (new session) is not motion."""
    before = list(frames(40, background=100, session="s1"))
    after = list(frames(80, background=180, session="s2", start=40))
    assert _run(MotionTrigger({}), before + after) == []


def test_the_same_change_inside_one_session_is_motion():
    """The guard can fail: without the session reset, the same jump does fire."""
    before = list(frames(40, background=100, session="s1"))
    after = list(frames(100, background=180, session="s1", start=40))
    assert [e.kind for e in _run(MotionTrigger({}), before + after)] == ["started", "ended"]


def test_a_4k_frame_is_analysed_at_320_px():
    scene = frames(
        30, size=(3840, 2160), noise=0, squares=(Square(22, 30, side=500, speed=150, y=800),)
    )
    trigger = MotionTrigger({"min_frames": 1})
    events = _run(trigger, scene)
    assert [e.kind for e in events] == ["started"]
    assert trigger.health()["analysis"] == [320, 180]


def test_the_first_frames_of_a_session_are_learned_not_scored():
    trigger = MotionTrigger({"min_frames": 1})
    scene = list(frames(motion.LEARN_FRAMES, squares=(Square(5, 6, speed=0),)))
    assert _run(trigger, scene) == []
    assert trigger.health()["learning"] is False
    fresh = MotionTrigger({})
    fresh.start()
    fresh.observe(next(frames(1)))
    assert fresh.health()["learning"] is True


def test_health_counts_frames_and_episodes_and_names_the_open_one():
    trigger = MotionTrigger({})
    events = _run(trigger, crossing(60))  # stops while the episode is still open
    health = trigger.health()
    assert health["frames"] == 60 and health["episodes"] == 1
    assert health["open"] == events[0].incident_key
    assert 0.0 <= health["last_score"] <= 1.0


@pytest.mark.parametrize(
    ("config", "word"),
    [
        ({"min_aera": 0.1}, "min_aera"),
        ({"min_area": 0}, "min_area"),
        ({"min_area": 1.5}, "min_area"),
        ({"min_frames": 0}, "min_frames"),
        ({"min_frames": 2.5}, "min_frames"),
        ({"cooldown_s": -1}, "cooldown_s"),
        ({"roi": "10,10,0,5"}, "roi"),
        ({"roi": "left half"}, "roi"),
        ({"severity": "loud"}, "severity"),
    ],
)
def test_a_bad_config_is_refused_with_the_key_and_a_fix(config, word):
    with pytest.raises(TriggerConfigError) as caught:
        MotionTrigger(config)
    assert word in str(caught.value) and caught.value.fix


def test_a_region_outside_the_frame_is_a_config_error_naming_the_frame_size():
    trigger = MotionTrigger({"roi": "300,100,100,100"})
    trigger.start()
    with pytest.raises(TriggerConfigError) as caught:
        trigger.observe(next(frames(1)))
    assert "320x180" in str(caught.value)


def test_a_configured_severity_is_carried_by_its_events():
    events = _run(MotionTrigger({"severity": "warning"}), crossing())
    assert {e.severity for e in events} == {"warning"}


def test_importing_motion_does_not_import_numpy():
    import subprocess
    import sys

    from .harness import SRC

    code = (
        "import sys; import axiom.extensions.builtins.video.motion; print('numpy' in sys.modules)"
    )
    done = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env={"PYTHONPATH": str(SRC), "PATH": "/usr/bin:/bin"},
        timeout=60,
    )
    assert done.stdout.strip() == "False", done.stderr
