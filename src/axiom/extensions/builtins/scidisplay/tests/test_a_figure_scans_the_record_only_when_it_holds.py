# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A figure reads a channel's whole record only for a sweep that holds its axis (#1827).

On a deployed node every figure computed the value range over a channel's whole history to
set the axis a sweep holds: ~30 s on tens of millions of readings, on every request. Here:

* a figure without ``hold`` asks the door only for windowed reads of the readings table;
* a figure with ``hold`` asks for the whole-record extent once, and keeps it until the site's
  catalogue revision moves (a new reading can widen it; nothing else can).
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from axiom.extensions.builtins.scidisplay.node_figure_source import NodeFigureSource
from axiom.extensions.builtins.scidisplay.tests import (
    test_a_figure_is_drawn_from_a_nodes_gold as base,
)


class RecordDoor(base.FakeDoor):
    """The base door, which also answers a min/max over the readings table."""

    def _query_once(self, verb, params):  # noqa: ANN001
        if verb == "data.aggregate" and params["table"] == "signals":
            self.calls.append((verb, dict(params)))
            value = {"min": -5.0, "max": 95.0}[params["fn"]]
            return {"data": {"groups": [{"feed": "loop", "channel": "T1", "value": value}]}}
        return super()._query_once(verb, params)


@pytest.fixture
def source(tmp_path) -> NodeFigureSource:
    return NodeFigureSource(RecordDoor(), dates_cache_path=str(tmp_path / "days.json"))


def _readings(source) -> list[tuple[str, dict]]:
    return [(v, p) for v, p in source.door.calls if p.get("table") == "signals"]


def test_a_figure_without_a_sweep_reads_only_windows(source):
    source.figure("site-a", ["loop:T1"], last=timedelta(days=1))

    reads = _readings(source)
    assert reads, "the figure read nothing"
    unbounded = [(v, p) for v, p in reads
                 if not (p.get("window") or {}).get("start") or not (p.get("window") or {}).get("end")]
    assert not unbounded, f"a time-unbounded read of the readings table: {unbounded}"


def test_a_held_sweep_asks_for_the_record_extent_once_per_revision(source, monkeypatch):
    first = source.figure("site-a", ["loop:T1"], last=timedelta(days=1), hold=True)
    source.figure("site-a", ["loop:T1"], last=timedelta(hours=6), hold=True)

    record = [p for v, p in _readings(source) if v == "data.aggregate"]
    assert sorted(p["fn"] for p in record) == ["max", "min"], record
    assert first["held"] == {"degC": [-5.0, 95.0]}

    # A new reading moves the revision; the next held figure asks again.
    monkeypatch.setattr(source, "revision", lambda site: "moved")
    source.figure("site-a", ["loop:T1"], last=timedelta(days=1), hold=True)
    assert sum(1 for v, p in _readings(source) if v == "data.aggregate") == 4


def test_an_explicit_extent_is_used_as_given(source):
    out = source.figure("site-a", ["loop:T1"], last=timedelta(days=1), hold=True,
                        held_extent={"degC": (0.0, 50.0)})

    assert not [p for v, p in _readings(source) if v == "data.aggregate"]
    assert out["held"] == {"degC": [0.0, 50.0]}
