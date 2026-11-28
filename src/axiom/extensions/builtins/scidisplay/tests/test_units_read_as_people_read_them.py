# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A figure says a unit the way a person reads it, says each name once, and draws a state as a band.

``degC`` is how a channel map spells it and ``°C`` how a reader does; a log axis over ten
decades of watts reads ``1m``, ``1``, ``1k``, ``1M`` with the unit once above it, not
``1000000``; a value that rounds to nothing is ``0``, never ``-0``.
"""

from __future__ import annotations

import re

from axiom.extensions.builtins.scidisplay import units
from axiom.extensions.builtins.scidisplay.chart_kinds import render_spec

T = [f"2026-09-21T{h:02d}:00:00+00:00" for h in range(0, 24, 2)]


def _line(title: str, series: list[dict], **extra) -> str:
    doc = {"schema_version": "2.0", "kind": "line", "title": title, "data": {"series": series}, **extra}
    return render_spec(doc, width=640).svg


def _texts(svg: str) -> list[str]:
    """The text a reader sees: what is inside <text> and <tspan>, not the accessible <title>."""
    out = []
    for body in re.findall(r"<text\b[^>]*>(.*?)</text>", svg, re.S):
        out.extend(t.strip() for t in re.findall(r"(?:^|>)([^<>]+)(?:<|$)", body) if t.strip())
    return out


def test_a_unit_is_spelled_the_way_a_reader_spells_it() -> None:
    assert [units.pretty(u) for u in ("degC", "percent", "pct", "console_units", "W", "pcm")] == [
        "°C", "%", "%", "units", "W", "pcm"]
    assert units.label(units.pretty("")) == units.UNDECLARED


def test_axes_and_end_labels_read_degrees_and_percent_not_their_spellings() -> None:
    svg = _line("T", [{"name": "Fuel", "unit": "degC", "x": T, "y": [20 + i for i in range(12)]},
                      {"name": "Rod", "unit": "console_units", "x": T, "y": [float(i) for i in range(12)]}])
    texts = " ".join(_texts(svg))
    assert "°C" in texts and "units" in texts
    assert "degC" not in texts and "console_units" not in texts


def test_a_log_power_axis_reads_si_prefixes_on_its_ticks() -> None:
    ys = [10.0 ** e for e in range(-4, 8)][:12]
    svg = _line("Power", [{"name": "Power", "unit": "W", "x": T, "y": ys}], axes={"y": {"type": "log"}})
    ticks = set(_texts(svg))
    assert {"1m", "1", "1k", "1M"} <= ticks and "100µ" in ticks
    assert "1000000" not in ticks


def test_a_reading_that_rounds_to_nothing_is_zero_not_minus_zero() -> None:
    svg = _line("R", [{"name": "Xenon", "unit": "pcm", "x": T, "y": [-12.0] * 11 + [-0.0001]},
                      {"name": "Total", "unit": "pcm", "x": T, "y": [-8.0] * 12}])
    assert not any(re.fullmatch(r"-0(\.0+)?\s*pcm", t) for t in _texts(svg))


def test_a_single_series_legend_does_not_repeat_the_title() -> None:
    svg = _line("Power", [{"name": "Power", "unit": "W", "x": T, "y": [float(i) for i in range(12)]}])
    assert _texts(svg).count("Power") == 1



def test_every_kind_s_axis_reads_the_unit_as_a_reader_spells_it() -> None:
    doc = {"schema_version": "2.0", "kind": "bar", "title": "Rods",
           "data": {"series": [{"name": "At peak", "unit": "console_units", "x": ["A", "B"], "y": [972.0, 658.0]}]}}
    texts = _texts(render_spec(doc, width=640).svg)
    assert "units" in texts and "console_units" not in " ".join(texts)

