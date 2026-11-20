"""Tests for the example normalizer. They need nothing installed.

Run them with `pytest data/conform/tests`. The second test is the one that
proves the first can fail: break `rig_frame` (make it `return []`) and watch
both go red.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location("rig_frame", Path(__file__).parents[1] / "rig_frame.py")
rig_frame = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rig_frame)

TS = "2026-09-01T12:00:00+00:00"


def test_one_row_per_channel_with_units():
    rows = list(rig_frame.rig_frame({"row": {"ts": TS, "t_in": 20.0, "t_out": 31.5, "flow": 12.0}}))
    assert [r["channel"] for r in rows] == ["temperature.inlet", "temperature.outlet", "flow.primary"]
    assert all(r["unit"] for r in rows)


def test_an_absent_channel_keeps_the_others():
    rows = list(rig_frame.rig_frame({"row": {"ts": TS, "t_in": 20.0}}))
    assert [r["channel"] for r in rows] == ["temperature.inlet"]
