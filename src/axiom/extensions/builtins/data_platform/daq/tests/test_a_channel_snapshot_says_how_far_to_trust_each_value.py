# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Every channel on a monitor shows its value and how far to trust it.

Monitoring practice (OPC UA status classes, ISA-101 displays) shows a value
beside a quality of Good, Uncertain or Bad and the reason. The platform already
has a closed quality vocabulary (ADR-132: good, suspect, bad, saturated, stale)
and declared fault rules (``company``). This snapshot keeps the latest reading
and a short trend per channel, assesses each against staleness, declared range,
a missing unit and the declared rules, and reports the ADR-132 quality with its
OPC UA-style class for display.
"""

from __future__ import annotations

from axiom.extensions.builtins.data_platform.company import ZeroWhileCompanionAbove
from axiom.extensions.builtins.data_platform.daq.snapshot import (
    ChannelDecl,
    ChannelSnapshot,
    display_class,
)

T0 = 1_000_000.0


def _snap(**kw):
    decls = {
        "TC1": ChannelDecl(unit="degC", description="inlet", low=-20, high=900),
        "TC2": ChannelDecl(unit="degC", description="outlet"),
        "FLOW": ChannelDecl(unit=None, description="flow, unit not declared yet"),
    }
    return ChannelSnapshot(decls=decls, expected_interval_s=1.0, **kw)


def test_a_fresh_in_range_value_is_good_and_trends():
    s = _snap()
    for i in range(5):
        s.observe("loop", T0 + i, {"TC1": 20.0 + i, "TC2": 21.0}, received_at=T0 + i)
    row = s.view(now=T0 + 5)["channels"]["TC1"]
    assert row["value"] == 24.0 and row["unit"] == "degC" and row["description"] == "inlet"
    assert row["quality"] == "good" and display_class(row["quality"]) == "Good"
    assert [p[1] for p in row["trend"]] == [20.0, 21.0, 22.0, 23.0, 24.0]
    assert row["age_s"] == 1.0


def test_a_value_that_stopped_arriving_is_stale():
    s = _snap()
    s.observe("loop", T0, {"TC1": 20.0}, received_at=T0)
    row = s.view(now=T0 + 600)["channels"]["TC1"]
    assert row["quality"] == "stale" and display_class("stale") == "Uncertain"
    assert "no reading for" in row["reason"]


def test_a_value_outside_its_declared_range_is_bad():
    s = _snap()
    s.observe("loop", T0, {"TC1": 5000.0}, received_at=T0)
    row = s.view(now=T0)["channels"]["TC1"]
    assert row["quality"] == "bad" and "outside its declared range" in row["reason"]


def test_a_value_with_no_unit_is_suspect_not_good():
    s = _snap()
    s.observe("loop", T0, {"FLOW": 3.2}, received_at=T0)
    row = s.view(now=T0)["channels"]["FLOW"]
    assert row["quality"] == "suspect" and "no unit" in row["reason"]


def test_a_declared_fault_rule_marks_the_zeroed_sensor_bad():
    rule = ZeroWhileCompanionAbove(zero=["TC1"], companion=["TC2"], above=15.0,
                                   subject_reason="company.zero_while_bath_warm")
    s = _snap(rules=[rule])
    s.observe("loop", T0, {"TC1": 0.0, "TC2": 23.0}, received_at=T0)
    ch = s.view(now=T0)["channels"]
    assert ch["TC1"]["quality"] == "bad" and "exactly 0" in ch["TC1"]["reason"]
    assert ch["TC2"]["quality"] == "suspect"


def test_a_non_numeric_value_is_bad_and_does_not_enter_the_trend():
    s = _snap()
    s.observe("loop", T0, {"TC1": "OPEN"}, received_at=T0)
    row = s.view(now=T0)["channels"]["TC1"]
    assert row["quality"] == "bad" and row["trend"] == []


def test_a_declared_channel_never_read_is_listed_as_bad_with_why():
    s = _snap()
    row = s.view(now=T0)["channels"]["TC2"]
    assert row["value"] is None and row["quality"] == "bad" and "no reading yet" in row["reason"]


def test_the_trend_is_bounded_and_thinned_to_one_point_per_interval():
    s = _snap(trend_points=10, trend_step_s=1.0)
    for i in range(100):
        s.observe("loop", T0 + i * 0.25, {"TC1": float(i)}, received_at=T0 + i * 0.25)
    trend = s.view(now=T0 + 25)["channels"]["TC1"]["trend"]
    assert len(trend) == 10
    assert all(b[0] - a[0] >= 1.0 for a, b in zip(trend, trend[1:]))


def test_the_view_round_trips_through_json(tmp_path):
    import json

    s = _snap()
    s.observe("loop", T0, {"TC1": 20.0}, received_at=T0)
    path = tmp_path / "snap.json"
    s.save(path, now=T0)
    doc = json.loads(path.read_text())
    assert doc["channels"]["TC1"]["value"] == 20.0
