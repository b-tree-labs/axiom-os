# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""VID-017 and VID-018: a reference set or an attachment that cannot work is refused, with
its fix, when the attachment is built; a frame the set does not fit fails the trigger.

spec-video §Reference set: a directory named by an absolute path, holding
``reference-set.json`` (format ``reference-set/1``) and the pictures it names, each
checked against its SHA-256 and loaded without pickle. Any key the format does not
have is refused, at any level, and so is any attachment setting. A frame whose size
is not a whole multiple of the working size raises ``TriggerConfigError`` from
``observe``: the runner makes the trigger ``failed`` with the fix, not ``unknown``.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from axiom.extensions.builtins.video import reference_set as rs
from axiom.extensions.builtins.video import runner
from axiom.extensions.builtins.video import triggers as vt
from axiom.extensions.builtins.video.bus import FrameBus
from axiom.extensions.builtins.video.reference_match import ReferenceMatchTrigger
from axiom.extensions.builtins.video.triggers import TriggerConfigError

from .harness import until
from .matching import attach, trigger_for
from .scenes import Segment, door_reference_set, door_scene

FACTORY = "axiom.extensions.builtins.video.reference_match:ReferenceMatchTrigger"


@pytest.fixture
def doors(tmp_path):
    return door_reference_set(tmp_path / "doors")


def _refused(config) -> TriggerConfigError:
    with pytest.raises(TriggerConfigError) as caught:
        ReferenceMatchTrigger(config)
    assert caught.value.fix, "every refusal says how to fix it"
    return caught.value


def _region(manifest, **changes):
    (region,) = manifest["regions"]
    return [{**region, **changes}]


# ---- the attachment ------------------------------------------------------------------


def test_a_relative_path_is_refused_with_the_fix(doors):
    error = _refused({**attach(doors), "reference_set": "doors"})
    assert "absolute" in str(error) and "absolute" in error.fix


def test_no_reference_set_is_refused(doors):
    config = attach(doors)
    del config["reference_set"]
    assert "reference_set" in str(_refused(config))


def test_a_missing_set_is_refused(tmp_path):
    error = _refused({"reference_set": str(tmp_path / "nowhere")})
    assert str(tmp_path / "nowhere") in str(error)


def test_a_directory_without_a_manifest_is_refused(tmp_path):
    (tmp_path / "empty").mkdir()
    error = _refused({"reference_set": str(tmp_path / "empty")})
    assert "reference-set.json" in str(error)


@pytest.mark.parametrize(
    ("setting", "word"),
    [
        ({"dwel_s": 5.0}, "dwel_s"),
        ({"regions": ["door"]}, "regions"),
        ({"sample_s": -1}, "sample_s"),
        ({"dwell_s": "long"}, "dwell_s"),
        ({"leave_s": True}, "leave_s"),
        ({"heartbeat_s": 0}, "heartbeat_s"),
        ({"severity": "loud"}, "severity"),
    ],
)
def test_a_setting_it_does_not_have_or_cannot_use_is_refused(doors, setting, word):
    error = _refused({**attach(doors), **setting})
    assert word in str(error)


def test_an_unknown_setting_names_the_settings_it_has(doors):
    error = _refused({**attach(doors), "dwel_s": 5.0})
    for key in ("reference_set", "sample_s", "dwell_s", "leave_s", "heartbeat_s", "severity"):
        assert key in str(error)


# ---- the set ---------------------------------------------------------------------------


def test_a_picture_that_does_not_match_its_hash_is_refused(doors):
    picture = doors.path / "door-open.npy"
    np.save(picture, np.zeros((54, 32), dtype=np.float32), allow_pickle=False)
    error = _refused(attach(doors))
    assert "door-open.npy" in str(error) and "sha256" in str(error)


def test_a_missing_picture_is_refused(doors):
    (doors.path / "door-closed.npy").unlink()
    assert "door-closed.npy" in str(_refused(attach(doors)))


def test_a_pickled_picture_is_refused(doors):
    picture = doors.path / "door-open.npy"
    np.save(picture, np.array([{"a": 1}], dtype=object), allow_pickle=True)
    manifest = json.loads(doors.manifest_path.read_text())
    import hashlib

    digest = hashlib.sha256(picture.read_bytes()).hexdigest()
    for ref in manifest["regions"][0]["references"]:
        if ref["image"] == "door-open.npy":
            ref["sha256"] = digest
    doors.manifest_path.write_text(json.dumps(manifest))
    assert "door-open.npy" in str(_refused(attach(doors)))


@pytest.mark.parametrize("label", ["changing", "unknown"])
def test_a_reserved_label_is_refused(doors, label):
    regions = doors.manifest["regions"]
    refs = [dict(r) for r in regions[0]["references"]]
    refs[1]["label"] = label
    doors.rewrite(regions=_region(doors.manifest, references=refs))
    error = _refused(attach(doors))
    assert label in str(error) and "reserved" in str(error)


def test_a_label_repeated_in_a_region_is_refused(doors):
    refs = [dict(r) for r in doors.manifest["regions"][0]["references"]]
    refs[1]["label"] = refs[0]["label"]
    doors.rewrite(regions=_region(doors.manifest, references=refs))
    assert "closed" in str(_refused(attach(doors)))


def test_a_region_with_one_reference_is_refused(doors):
    refs = doors.manifest["regions"][0]["references"][:1]
    doors.rewrite(regions=_region(doors.manifest, references=refs))
    assert "two or more" in str(_refused(attach(doors)))


@pytest.mark.parametrize(
    ("change", "word"),
    [
        ({"format": "reference-set/9"}, "reference-set/1"),
        ({"gaurds": {}}, "gaurds"),
        ({"working_size": [160]}, "working_size"),
        ({"name": ""}, "name"),
        ({"version": 3}, "version"),
        ({"masks": [[0.5, 0.5, 0.4, 0.6]]}, "masks"),
        ({"regions": []}, "regions"),
        ({"guards": {"view": 0.9, "exposure": [200.0, 40.0], "clipped": 0.05}}, "exposure"),
        ({"guards": {"view": 0.9, "exposure": [40.0, 220.0]}}, "clipped"),
    ],
)
def test_a_manifest_that_cannot_work_is_refused_naming_the_field(doors, change, word):
    doors.rewrite(**change)
    assert word in str(_refused(attach(doors)))


@pytest.mark.parametrize(
    ("change", "word"),
    [
        ({"name": "Door 1"}, "Door 1"),
        ({"margin": -0.1}, "margin"),
        ({"bounds": [0.4, 0.2, 0.6]}, "bounds"),
        ({"colour": "red"}, "colour"),
    ],
)
def test_a_region_that_cannot_work_is_refused_naming_the_field(doors, change, word):
    doors.rewrite(regions=_region(doors.manifest, **change))
    assert word in str(_refused(attach(doors)))


def test_a_picture_named_outside_the_directory_is_refused(doors):
    refs = [dict(r) for r in doors.manifest["regions"][0]["references"]]
    refs[0]["image"] = "../door-closed.npy"
    doors.rewrite(regions=_region(doors.manifest, references=refs))
    assert "../door-closed.npy" in str(_refused(attach(doors)))


def test_a_picture_of_the_wrong_shape_is_refused(doors):
    """A picture is its box at the working size; anything else was taken at another size."""
    picture = doors.path / "door-open.npy"
    np.save(picture, np.full((10, 10), 50.0, dtype=np.float32), allow_pickle=False)
    import hashlib

    refs = [dict(r) for r in doors.manifest["regions"][0]["references"]]
    for ref in refs:
        if ref["image"] == "door-open.npy":
            ref["sha256"] = hashlib.sha256(picture.read_bytes()).hexdigest()
    doors.rewrite(regions=_region(doors.manifest, references=refs))
    error = _refused(attach(doors))
    assert "door-open.npy" in str(error) and "(54, 32)" in str(error)


def test_a_flat_picture_is_refused_because_nothing_can_match_it(doors):
    picture = doors.path / "door-open.npy"
    np.save(picture, np.full((54, 32), 80.0, dtype=np.float32), allow_pickle=False)
    import hashlib

    refs = [dict(r) for r in doors.manifest["regions"][0]["references"]]
    for ref in refs:
        if ref["image"] == "door-open.npy":
            ref["sha256"] = hashlib.sha256(picture.read_bytes()).hexdigest()
    doors.rewrite(regions=_region(doors.manifest, references=refs))
    assert "flat" in str(_refused(attach(doors)))


def test_a_refusal_is_a_reference_set_error_from_the_loader(doors):
    doors.rewrite(format="reference-set/9")
    with pytest.raises(rs.ReferenceSetError) as caught:
        rs.load_reference_set(doors.path)
    assert isinstance(caught.value, TriggerConfigError) and caught.value.fix


# ---- the loader and the writer ----------------------------------------------------------


def test_the_loader_reads_what_the_set_declares(doors):
    loaded = rs.load_reference_set(doors.path)
    assert (loaded.name, loaded.version) == ("test-room", "1")
    assert loaded.working_size == (160, 90)
    assert [r.name for r in loaded.regions] == ["door"]
    assert [ref.label for ref in loaded.regions[0].references] == ["closed", "open"]
    assert loaded.path == doors.path
    assert rs.FORMAT == "reference-set/1" and rs.MANIFEST == "reference-set.json"


def test_the_writer_writes_a_set_the_loader_reads(doors, tmp_path):
    manifest = json.loads(doors.manifest_path.read_text())
    images = {p.name: np.load(p) for p in doors.path.glob("*.npy")}
    for ref in manifest["regions"][0]["references"]:
        del ref["sha256"]
    del manifest["landmarks"]["sha256"]
    written = rs.write_reference_set(tmp_path / "copy", manifest, images)
    assert written == tmp_path / "copy" / "reference-set.json"
    loaded = rs.load_reference_set(tmp_path / "copy")
    assert [ref.label for ref in loaded.regions[0].references] == ["closed", "open"]
    trigger = trigger_for(door_reference_set(tmp_path / "again"))
    copy = ReferenceMatchTrigger({**attach(doors), "reference_set": str(tmp_path / "copy")})
    copy.start()
    scene = door_scene(Segment(8, "closed"))
    assert [e.detail["state"] for f in scene for e in copy.observe(f)] == [
        e.detail["state"] for f in scene for e in trigger.observe(f)
    ]


# ---- a frame the set does not fit --------------------------------------------------------


def test_a_frame_size_the_set_does_not_fit_raises_a_config_error_naming_both(doors):
    trigger = trigger_for(doors)
    frame = door_scene(Segment(0.5, "closed"), size=(300, 180))[0]
    with pytest.raises(TriggerConfigError) as caught:
        trigger.observe(frame)
    assert "300x180" in str(caught.value) and "160x90" in str(caught.value)
    assert caught.value.fix


def test_in_a_runner_a_frame_the_set_does_not_fit_fails_the_trigger_with_its_fix(state_dir, doors):
    catalog = vt.TriggerCatalog(
        entries={"reference-match": vt.TriggerEntry("reference-match", FACTORY, "builtin")}
    )
    bus = FrameBus("cam-1")
    triggers = runner.TriggerSet("cam-1", bus=bus, state_dir=state_dir, catalog=catalog)
    result = triggers.apply([{"name": "reference-match", **attach(doors)}])
    assert result["errors"] == []
    for frame in door_scene(Segment(1, "closed"), size=(300, 180)):
        bus.publish(frame)
    until(lambda: triggers.health()[0]["state"] == "failed")
    (health,) = triggers.health()
    assert "300x180" in health["last_error"] and health["fix"]
    triggers.stop()


def test_connector_reload_refuses_a_set_that_cannot_work_and_says_why(doors):
    catalog = vt.TriggerCatalog(
        entries={"reference-match": vt.TriggerEntry("reference-match", FACTORY, "builtin")}
    )
    (picture,) = [p for p in Path(doors.path).glob("door-open.npy")]
    picture.write_bytes(picture.read_bytes()[:-8])
    errors = runner.check_attachments([{"name": "reference-match", **attach(doors)}], catalog)
    assert len(errors) == 1 and "door-open.npy" in errors[0] and "Fix:" in errors[0]
