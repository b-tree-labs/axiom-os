# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A series that states a range beside each point (``low``, ``high``) has it drawn (ADR-136).

The chart spec accepted ``low`` and ``high`` on a series and the time-line renderer ignored
them, so a source that stated how well a value was known had that left off the figure.
"""

from __future__ import annotations

import re

from axiom.extensions.builtins.scidisplay.chart_kinds import render_spec

X = [f"2026-03-04T14:{m:02d}:00Z" for m in range(6)]


def _spec(**series):
    return {"schema_version": "2.0", "kind": "line", "title": "Flow",
            "data": {"series": [{"name": "flow", "unit": "kg/s", "x": X,
                                 "y": [10.0, 10.5, 11.0, 10.8, 10.2, 10.0], **series}]}}


def _svg(spec):
    out = render_spec(spec)
    return out["svg"] if isinstance(out, dict) else out.svg


def _bands(svg):
    return re.findall(r'<polygon class="axk-band"[^>]*points="([^"]+)"', svg)


def test_low_and_high_are_drawn_as_a_band_under_the_line():
    svg = _svg(_spec(low=[9.5, 10.0, 10.4, 10.3, 9.8, 9.6], high=[10.5, 11.0, 11.6, 11.3, 10.6, 10.4]))
    bands = _bands(svg)
    assert len(bands) == 1 and 'data-series="flow"' in svg
    assert len(bands[0].split()) == 12  # six points along the top, six back along the bottom
    assert svg.index("axk-band") < svg.index("<polyline")  # under the line, never over it


def test_a_series_with_no_range_has_no_band():
    assert _bands(_svg(_spec())) == []


def test_the_band_breaks_where_a_point_has_no_range():
    svg = _svg(_spec(low=[9.5, 10.0, None, 10.3, 9.8, 9.6], high=[10.5, 11.0, None, 11.3, 10.6, 10.4]))
    # Two pieces, either side of the point nobody bounded: a band across it would claim a bound.
    assert [len(b.split()) for b in _bands(svg)] == [4, 6]


def test_the_axis_holds_the_band_as_well_as_the_line():
    narrow = _svg(_spec())
    wide = _svg(_spec(low=[0.0] * 6, high=[40.0] * 6))
    ys = lambda svg: [float(v) for v in re.findall(r"<polyline[^>]*points=\"([^\"]+)\"", svg)[0].replace(",", " ").split()[1::2]]  # noqa: E731
    # The same readings sit in a smaller part of the plot when the range is wide.
    assert max(ys(wide)) - min(ys(wide)) < max(ys(narrow)) - min(ys(narrow))


def test_the_readout_carries_the_range_for_the_crosshair():
    out = render_spec(_spec(low=[9.5] * 6, high=[10.5] * 6))
    readout = out["readout"] if isinstance(out, dict) else out.readout
    assert readout["series"][0]["band"][0] == [9.5, 10.5]
    plain = render_spec(_spec())
    plain = plain["readout"] if isinstance(plain, dict) else plain.readout
    assert "band" not in plain["series"][0]
