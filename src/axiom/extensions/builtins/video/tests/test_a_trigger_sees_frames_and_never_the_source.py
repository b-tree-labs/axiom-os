# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""ADR-240 D3, VID-010, VID-013: ``StreamTrigger`` and ``TriggerEvent`` are the only seam.

A trigger is built from its attachment table alone and sees ``Frame``s, never the
source kind. An event carries an incident key that is stable for one episode, a
severity from the trigger's own configuration (default ``info``), and nothing
that names who should hear about it (ADR-238 D2: routing decides).
"""

from __future__ import annotations

import dataclasses

import pytest

from axiom.extensions.builtins.video import triggers as vt
from axiom.extensions.builtins.video.motion import MotionTrigger

from .scenes import frames
from .trigger_fakes import ScriptedTrigger


def _event(**over):
    base = dict(
        stream="cam-1",
        trigger="motion@0.1.0",
        ts="2026-11-10T12:00:00+00:00",
        kind="started",
        incident_key="cam-1:motion@0.1.0:abc",
        severity="info",
    )
    return vt.TriggerEvent(**{**base, **over})


def test_an_event_carries_exactly_the_contract():
    assert [f.name for f in dataclasses.fields(vt.TriggerEvent)] == [
        "stream",
        "trigger",
        "ts",
        "kind",
        "incident_key",
        "severity",
        "score",
        "detail",
    ]


def test_an_event_has_no_field_that_names_a_recipient():
    fields = {f.name for f in dataclasses.fields(vt.TriggerEvent)}
    assert vt.ADDRESSING_KEYS, "the guard needs words to look for"
    assert not fields & set(vt.ADDRESSING_KEYS)


def test_the_vocabularies():
    assert vt.EVENT_KINDS == ("started", "ended", "occurred")
    assert vt.SEVERITIES == ("info", "warning", "critical")
    assert vt.DEFAULT_SEVERITY == "info"
    assert vt.ENTRY_POINT_GROUP == "axiom.video_triggers"
    assert vt.MANIFEST_BLOCK == "video_triggers"


def test_the_incident_key_is_stream_trigger_episode():
    assert vt.incident_key("cam-1", "motion@0.1.0", "e7") == "cam-1:motion@0.1.0:e7"


def test_severity_defaults_to_info():
    event = vt.TriggerEvent(
        stream="cam-1",
        trigger="motion@0.1.0",
        ts="2026-11-10T12:00:00+00:00",
        kind="occurred",
        incident_key="cam-1:motion@0.1.0:e1",
    )
    assert event.severity == "info" and event.score is None and dict(event.detail) == {}


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("kind", "repeated"),
        ("severity", "urgent"),
        ("incident_key", "cam-2:motion@0.1.0:abc"),
        ("incident_key", "cam-1:other@1.0.0:abc"),
        ("incident_key", "cam-1:motion@0.1.0:"),
        ("stream", ""),
    ],
)
def test_an_event_outside_the_contract_is_refused(field, value):
    with pytest.raises(ValueError):
        _event(**{field: value})


def test_an_event_round_trips_through_its_dict():
    event = _event(score=0.05, detail={"method": "frame_diff", "roi": "full"})
    data = event.as_dict()
    assert data["incident_key"] == "cam-1:motion@0.1.0:abc" and data["score"] == 0.05
    assert vt.TriggerEvent.from_dict(data) == event


def test_the_base_trigger_fills_in_stream_trigger_severity_and_key_from_the_frame():
    class Probe(vt.BaseTrigger):
        name = "probe"
        version = "2.0.0"

        def observe(self, frame):
            return [self.event(frame, "occurred", "e1", score=1.0, detail={"x": 1})]

    (frame,) = list(frames(1))
    (event,) = Probe({"severity": "warning"}).observe(frame)
    assert (event.stream, event.trigger, event.ts) == ("cam-1", "probe@2.0.0", frame.ts)
    assert event.incident_key == "cam-1:probe@2.0.0:e1"
    assert (event.severity, event.score, dict(event.detail)) == ("warning", 1.0, {"x": 1})
    assert Probe({}).severity == "info"


def test_a_severity_outside_the_vocabulary_is_refused_with_the_choices():
    with pytest.raises(vt.TriggerConfigError) as caught:
        ScriptedTrigger({"severity": "loud"})
    assert "info" in str(caught.value) and "critical" in str(caught.value)


def test_motion_is_a_stream_trigger_and_says_what_it_is():
    trigger = MotionTrigger({})
    assert isinstance(trigger, vt.StreamTrigger)
    assert (trigger.name, trigger.version) == ("motion", "0.1.0")
    assert "heuristic" in MotionTrigger.description


def test_an_attachment_table_splits_into_its_name_and_the_config_a_factory_is_given():
    name, config = vt.split_attachment({"name": "motion", "min_area": 0.1, "roi": "0,0,10,10"})
    assert name == "motion"
    assert config == {"severity": "info", "min_area": 0.1, "roi": "0,0,10,10"}


def test_an_attachment_without_a_name_is_refused():
    with pytest.raises(vt.TriggerConfigError) as caught:
        vt.split_attachment({"min_area": 0.1})
    assert "name" in str(caught.value)


@pytest.mark.parametrize("key", ["notify", "recipient", "to"])
def test_an_attachment_that_names_a_recipient_is_refused(key):
    """VID-013, ADR-238 D2: the trigger never names a recipient; routing decides."""
    with pytest.raises(vt.TriggerConfigError) as caught:
        vt.split_attachment({"name": "motion", key: "@operator"})
    assert key in str(caught.value) and "ADR-238" in str(caught.value)


def test_a_factory_is_given_the_attachment_and_nothing_about_the_source():
    seen = []

    def spy(config):
        seen.append(dict(config))
        return ScriptedTrigger(config)

    catalog = vt.TriggerCatalog(entries={"spy": vt.TriggerEntry("spy", spy, "test")})
    name, config, trigger = catalog.build({"name": "spy", "severity": "warning", "sleep_s": 0})
    assert name == "spy" and isinstance(trigger, ScriptedTrigger)
    assert seen == [{"severity": "warning", "sleep_s": 0}]
