# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""MVP-03-A3: measured and simulated counterparts share a hue; simulated is dashed and labelled.

ChartSpec 2.0 side. A series says ``role: "simulated"`` and ``counterpart_of: <measured>``
(the appkit mirror, ``figure/spec.ts``, uses the same two names).
"""

from __future__ import annotations

import re

from axiom.extensions.builtins.scidisplay import unified_spec as us
from axiom.extensions.builtins.scidisplay.chart_kinds import render_spec

_X = [f"2026-10-30T00:{m:02d}:00Z" for m in range(6)]


def _doc(**sim) -> dict:
    return {
        "schema_version": "2.0",
        "kind": "line",
        "title": "Power",
        "data": {
            "series": [
                {
                    "name": "Power",
                    "unit": "kW",
                    "x": _X,
                    "y": [900, 901, 903, 902, 904, 905],
                    "role": "measured",
                },
                {
                    "name": "Power ROM",
                    "unit": "kW",
                    "x": _X,
                    "y": [899, 902, 902, 903, 903, 906],
                    "role": "simulated",
                    "counterpart_of": "Power",
                    **sim,
                },
            ]
        },
    }


def test_mvp_03_a3_the_spec_accepts_role_and_counterpart_of() -> None:
    assert us.validate(_doc()) == []
    assert "counterpart_of" in us.SERIES_KEYS and "role" in us.SERIES_KEYS
    assert us.SERIES_ROLES == ("measured", "simulated")


def test_mvp_03_a3_a_counterpart_must_name_a_series_in_the_block() -> None:
    doc = _doc()
    doc["data"]["series"][1]["counterpart_of"] = "Nope"
    assert any("counterpart_of" in p for p in us.validate(doc))
    doc = _doc()
    doc["data"]["series"][1]["role"] = "measured"
    assert any("only a simulated series" in p for p in us.validate(doc))


def test_mvp_03_a3_the_resolver_shares_the_hue_dashes_and_labels() -> None:
    measured, simulated = us.resolve_counterparts(
        _doc()["data"]["series"], colours={"Power": "#0072b2"}
    )
    assert simulated["colour"] == "#0072b2"
    assert simulated["dash"] == "dashed" and measured["dash"] == "solid"
    assert simulated["label_suffix"] == " (simulated)"
    assert simulated["modelled"] is True and simulated["against"] == "Power"


def test_mvp_03_a3_a_declared_counterpart_colour_is_inherited() -> None:
    series = _doc()["data"]["series"]
    series[0]["colour"] = "#d55e00"
    _, simulated = us.resolve_counterparts(series)
    assert simulated["colour"] == "#d55e00"


def test_mvp_03_a3_a_series_without_the_fields_is_unchanged() -> None:
    plain = [{"name": "a", "y": [1, 2]}, {"name": "b", "y": [2, 3]}]
    assert us.resolve_counterparts(plain) == plain


def test_mvp_03_a3_the_svg_draws_the_counterpart_dashed_in_the_same_hue_and_labelled() -> None:
    svg = render_spec(_doc()).as_dict()["svg"]
    assert "(simulated)" in svg
    strokes = [p for p in re.findall(r"<polyline [^>]*>", svg) if "stroke=" in p]
    dashed = [p for p in strokes if "stroke-dasharray" in p]
    solid = [p for p in strokes if "stroke-dasharray" not in p]
    assert len(dashed) == 1 and len(solid) == 1, strokes

    def colour(p: str) -> str:
        return re.search(r'stroke="([^"]+)"', p).group(1)

    assert colour(dashed[0]) == colour(solid[0])
