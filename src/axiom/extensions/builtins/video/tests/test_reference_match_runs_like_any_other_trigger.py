# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""VID-016 with VID-010 and VID-015: ``reference-match`` is a built-in registered exactly as a
consumer's trigger would be, and runs in the runner like any other.

It is a ``[[video_triggers]]`` block in this extension's manifest, so discovery finds
it as ``builtin`` and ``list --triggers`` shows it beside ``motion``. In a runner its
events reach the ledger, capture closes a change it leaves open, and ``connector
reload`` replaces a changed attachment and keeps an unchanged one. Its events carry
no frame data (ADR-256 D7). The skills that describe the triggers are declared once,
and the manifest's generated blocks stay in step with them.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import tomllib
from pathlib import Path

import pytest

from axiom.extensions.builtins.video import ledger, runner
from axiom.extensions.builtins.video import triggers as vt
from axiom.extensions.builtins.video.bus import FrameBus
from axiom.extensions.builtins.video.commands import cli
from axiom.extensions.builtins.video.reference_match import ReferenceMatchTrigger

from .harness import SRC, envelope, run_cli, until
from .matching import attach, run, trigger_for
from .scenes import Segment, door_reference_set, door_scene

FACTORY = "axiom.extensions.builtins.video.reference_match:ReferenceMatchTrigger"
MANIFEST = Path(vt.__file__).parent / "axiom-extension.toml"


@pytest.fixture
def doors(tmp_path):
    return door_reference_set(tmp_path / "doors")


@pytest.fixture
def catalog():
    return vt.TriggerCatalog(
        entries={"reference-match": vt.TriggerEntry("reference-match", FACTORY, "builtin")}
    )


@pytest.fixture
def bus():
    return FrameBus("cam-1")


def _publish(bus, scene, triggers=None):
    """Publish each frame once the trigger has taken the last, so a loaded machine drops none
    and the events are the scene's, not the scheduler's."""
    for i, frame in enumerate(scene, start=1):
        bus.publish(frame)
        deadline = time.monotonic() + 10.0
        while triggers is not None and triggers.health()[0]["frames"] < i:
            assert time.monotonic() < deadline, f"frame {i} was not observed"
            time.sleep(0.001)


def _seen(triggers, n):
    def health():
        (row,) = triggers.health()
        return row

    until(lambda: health()["frames"] + health()["dropped"] >= n)
    return health()


# ---- registered like a consumer's ----------------------------------------------------


def test_the_manifest_declares_reference_match_as_a_block_beside_motion():
    blocks = tomllib.loads(MANIFEST.read_text(encoding="utf-8"))["video_triggers"]
    assert {"name": "reference-match", "factory": FACTORY} in blocks
    assert [b["name"] for b in blocks] == ["motion", "reference-match"]


def test_discovery_finds_it_as_a_builtin_that_says_it_is_a_heuristic():
    found = vt.discover_triggers()
    assert found.entry("reference-match").origin == "builtin"
    row = next(r for r in found.rows() if r["name"] == "reference-match")
    assert (row["state"], row["version"]) == ("registered", "0.1.0")
    assert "heuristic" in row["description"]
    assert found.entry("reference-match").load() is ReferenceMatchTrigger


def test_list_triggers_shows_reference_match_beside_motion(state_dir):
    done = run_cli(["list", "--triggers", "--json"], state_dir=state_dir)
    assert done.returncode == 0, done.stderr
    rows = {t["name"]: t for t in envelope(done)["value"]["triggers"]}
    assert rows["reference-match"]["origin"] == "builtin"
    assert rows["reference-match"]["state"] == "registered"
    assert rows["motion"]["state"] == "registered"
    text = run_cli(["list", "--triggers"], state_dir=state_dir)
    assert "reference-match" in text.stdout and "0.1.0" in text.stdout


def test_the_skills_name_what_it_answers():
    from axiom.extensions.builtins.video.skills import SPECS

    spec = {s.name: s for s in SPECS}
    assert "reference-match" in spec["video.list"].description
    assert "region" in spec["video.events"].description


def test_the_manifest_and_the_skills_generated_blocks_stay_in_step():
    done = subprocess.run(
        [sys.executable, "-m", "axiom", "skills", "emit-md", "--check", "--ext", "video"],
        capture_output=True,
        text=True,
        timeout=120,
        env={
            **os.environ,
            "PYTHONPATH": os.pathsep.join([str(SRC), os.environ.get("PYTHONPATH", "")]),
        },
    )
    assert done.returncode == 0, f"stdout={done.stdout!r}\nstderr={done.stderr!r}"


# ---- in the runner ----------------------------------------------------------------------


def test_in_a_runner_its_events_reach_the_ledger(state_dir, bus, catalog, doors):
    triggers = runner.TriggerSet("cam-1", bus=bus, state_dir=state_dir, catalog=catalog)
    result = triggers.apply([{"name": "reference-match", **attach(doors)}])
    assert result["errors"] == [] and result["running"] == ["reference-match"]
    scene = door_scene(Segment(20, "closed"), Segment(20, "open"))
    _publish(bus, scene, triggers)
    health = _seen(triggers, len(scene))
    assert health["dropped"] == 0 and health["errors"] == 0
    triggers.stop()
    rows = ledger.read(state_dir)
    assert [r["kind"] for r in rows] == ["occurred", "started", "ended"]
    assert rows[1]["incident_key"] == rows[2]["incident_key"]
    assert health["health"]["regions"]["door"]["state"] == "open"


def test_stopping_capture_closes_an_open_change(state_dir, bus, catalog, doors):
    triggers = runner.TriggerSet("cam-1", bus=bus, state_dir=state_dir, catalog=catalog)
    triggers.apply([{"name": "reference-match", **attach(doors)}])
    scene = door_scene(Segment(10, "closed"), Segment(10, "stranger"))
    _publish(bus, scene, triggers)
    _seen(triggers, len(scene))
    triggers.stop()
    rows = ledger.read(state_dir)
    assert [r["kind"] for r in rows] == ["occurred", "started", "ended"]
    ended = rows[-1]
    assert ended["incident_key"] == rows[1]["incident_key"]
    assert ended["detail"]["closed_by"] == runner.CLOSED_BY
    assert ended["detail"]["reason"] == runner.REASON_STOPPED
    assert "to" not in ended["detail"], "an interrupted change names no label it reached"


def test_reload_replaces_a_changed_attachment_and_closes_its_open_change(
    state_dir, bus, catalog, doors
):
    triggers = runner.TriggerSet("cam-1", bus=bus, state_dir=state_dir, catalog=catalog)
    triggers.apply([{"name": "reference-match", **attach(doors)}])
    before = triggers.runners["reference-match"].trigger
    scene = door_scene(Segment(10, "closed"), Segment(10, "stranger"))
    _publish(bus, scene, triggers)
    _seen(triggers, len(scene))
    result = triggers.apply(
        [{"name": "reference-match", **attach(doors, dwell_s=2.0)}], atomic=True
    )
    assert result["applied"] is True and result["errors"] == []
    assert triggers.runners["reference-match"].trigger is not before
    triggers.stop()
    rows = ledger.read(state_dir)
    assert [r["kind"] for r in rows] == ["occurred", "started", "ended"]
    assert rows[-1]["detail"]["reason"] == runner.REASON_RECONFIGURED


def test_an_unchanged_attachment_keeps_running_across_a_reload(state_dir, bus, catalog, doors):
    triggers = runner.TriggerSet("cam-1", bus=bus, state_dir=state_dir, catalog=catalog)
    triggers.apply([{"name": "reference-match", **attach(doors)}])
    before = triggers.runners["reference-match"].trigger
    triggers.apply([{"name": "reference-match", **attach(doors)}], atomic=True)
    assert triggers.runners["reference-match"].trigger is before
    triggers.stop()


def test_a_reload_with_a_broken_set_keeps_the_running_one(state_dir, bus, catalog, doors, tmp_path):
    triggers = runner.TriggerSet("cam-1", bus=bus, state_dir=state_dir, catalog=catalog)
    triggers.apply([{"name": "reference-match", **attach(doors)}])
    before = triggers.runners["reference-match"].trigger
    broken = {"name": "reference-match", **attach(doors), "reference_set": str(tmp_path / "gone")}
    result = triggers.apply([broken], atomic=True)
    assert result["applied"] is False and any("gone" in e for e in result["errors"])
    assert triggers.runners["reference-match"].trigger is before
    triggers.stop()


def test_its_events_carry_no_frame_data(doors):
    events = run(trigger_for(doors), door_scene(Segment(20, "closed"), Segment(20, "open")))
    assert events
    for event in events:
        text = json.dumps(event.as_dict())  # raises on an array or any non-JSON value
        assert "ndarray" not in text


# ---- what status says ---------------------------------------------------------------------


def test_status_text_shows_each_region_with_its_state_and_since(capsys, doors):
    trigger = trigger_for(doors)
    run(trigger, door_scene(Segment(10, "closed"), Segment(10, "stranger")))
    cli._print_triggers_of(
        {
            "attached": ["reference-match"],
            "triggers": [
                {
                    "name": "reference-match",
                    "trigger": "reference-match@0.1.0",
                    "state": "running",
                    "frames": 40,
                    "events": 2,
                    "errors": 0,
                    "dropped": 0,
                    "health": trigger.health(),
                }
            ],
        }
    )
    out = capsys.readouterr().out
    line = next(line for line in out.splitlines() if "region door" in line)
    assert "changing" in line and "since" in line and "neither" in line
    assert "test-room" in out
