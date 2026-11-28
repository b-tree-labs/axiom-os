# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""ADR-265 D7: the chart readout keeps each point's served bound.

The node serves every series point with its own uncertainty, and the figure source used to
keep only ``(t, value)``, so the crosshair could never say "± 0.5 K". The readout now carries
an ``uncertainty`` list aligned 1:1 with its points, like ``quality``: ``[low, high]`` where the
node could claim a bound, ``null`` where it could not. Null is "not reported", never 0.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from axiom.extensions.builtins.scidisplay.node_figure_source import NodeFigureSource
from axiom.uncertainty.serving import Served

from .test_a_figure_is_drawn_from_a_nodes_gold import T0, FakeDoor

BOUND = Served(fn="mean", low=0.2, high=0.5, quantified=3, premise="independent").payload()
NONE = Served(fn="mean", unquantified=3).payload()


class BoundedDoor(FakeDoor):
    """T1's even buckets report a bound; odd ones report none; bucket 1 has no key at all."""

    def _query_once(self, verb, params):  # noqa: ANN001
        out = super()._query_once(verb, params)
        if verb != "data.series":
            return out
        for group in out["data"]["series"]:
            pts = []
            for i, p in enumerate(group["points"]):
                if i != 1:
                    p = {**p, "uncertainty": BOUND if i % 2 == 0 else NONE}
                pts.append(p)
            group["points"] = pts
        return out


@pytest.fixture
def series(tmp_path):
    source = NodeFigureSource(BoundedDoor(), dates_cache_path=str(tmp_path / "days.json"))
    fig = source.figure("site-a", ["loop:T1"], t_from=T0, t_to=T0 + timedelta(hours=2))
    [s] = fig["readout"]["series"]
    return s


def test_every_readout_point_has_its_bound_aligned(series) -> None:
    assert len(series["uncertainty"]) == len(series["points"])
    assert series["uncertainty"][0] == [0.2, 0.5]


def test_a_point_with_no_claimable_bound_is_null_never_zero(series) -> None:
    assert series["uncertainty"][3] is None  # the node said it could claim none
    assert series["uncertainty"][1] is None  # the node's answer had no bound at all
