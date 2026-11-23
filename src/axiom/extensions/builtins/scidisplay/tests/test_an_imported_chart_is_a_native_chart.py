# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The seamlessness claim, held per Plotly type (ADR-214).

For every trace type the importer supports, a Plotly figure and the chart a
person would author natively for the same data render to byte-identical SVG.
Not "similar": identical, because the renderer never reads ``source`` and the
importer emits the very document a person writes. If either drifts (the
importer adds a field, the renderer starts reading provenance) this fails.
"""

from __future__ import annotations

import json

import pytest

from axiom.extensions.builtins.scidisplay import unified_spec as us
from axiom.extensions.builtins.scidisplay.chart_kinds import normalise_svg, render_spec
from axiom.extensions.builtins.scidisplay.plotly_import import (
    import_figure,
    import_trame,
    reimport,
    sanitize_figure,
)

V = us.SCHEMA_VERSION
T = ["2026-09-21T00:00:00+00:00", "2026-09-21T01:00:00+00:00", "2026-09-21T02:00:00+00:00",
     "2026-09-21T03:00:00+00:00"]

# (plotly figure, native spec) per supported type.
PAIRS: dict[str, tuple[dict, dict]] = {
    "scatter-lines-time": (
        {"data": [{"type": "scatter", "mode": "lines", "name": "Output", "x": T, "y": [1, 2, 3, 2],
                   "meta": {"axiom": {"unit": "kW"}}, "line": {"color": "#ff00ff"}},
                  {"type": "scatter", "mode": "lines", "name": "Output model", "x": T, "y": [1.1, 2.2, 2.8, 2.1],
                   "meta": {"axiom": {"unit": "kW"}}, "line": {"dash": "dash"}}],
         "layout": {"title": {"text": "Output"}, "template": "plotly_dark"}},
        {"kind": "line", "title": "Output", "data": {"series": [
            {"name": "Output", "unit": "kW", "x": T, "y": [1, 2, 3, 2]},
            {"name": "Output model", "unit": "kW", "x": T, "y": [1.1, 2.2, 2.8, 2.1], "modelled": True}]}},
    ),
    "scatter-dual-axis": (
        {"data": [{"type": "scatter", "name": "A", "x": T, "y": [1, 2, 3, 4]},
                  {"type": "scatter", "name": "B", "x": T, "y": [40, 30, 20, 10], "yaxis": "y2"}],
         "layout": {"title": "Two units", "yaxis": {"title": "A (kW)"},
                    "yaxis2": {"title": "B (degC)", "overlaying": "y", "side": "right"}}},
        {"kind": "line", "title": "Two units", "data": {"series": [
            {"name": "A", "unit": "kW", "x": T, "y": [1, 2, 3, 4]},
            {"name": "B", "unit": "degC", "x": T, "y": [40, 30, 20, 10], "axis": "y2"}]}},
    ),
    "scatter-markers-fit": (
        {"data": [{"type": "scatter", "mode": "markers", "name": "Pairs", "x": [1, 2, 3, 4, 5],
                   "y": [2.1, 3.9, 6.2, 7.8, 10.1]}],
         "layout": {"title": "Pairs", "yaxis": {"title": "Response (V)"}, "xaxis": {"title": "Drive"}}},
        {"kind": "scatter", "title": "Pairs", "axes": {"x": {"title": "Drive"}}, "data": {"series": [
            {"name": "Pairs", "unit": "V", "x": [1, 2, 3, 4, 5], "y": [2.1, 3.9, 6.2, 7.8, 10.1]}]}},
    ),
    "scatter-step": (
        {"data": [{"type": "scatter", "name": "Setpoint", "x": T, "y": [0, 50, 50, 100],
                   "line": {"shape": "hv"}}], "layout": {"title": "Setpoint", "yaxis": {"title": "Level (percent)"}}},
        {"kind": "step", "title": "Setpoint", "data": {"series": [
            {"name": "Setpoint", "unit": "percent", "x": T, "y": [0, 50, 50, 100]}]}},
    ),
    "scatter-stacked-area": (
        {"data": [{"type": "scatter", "name": "A", "x": [0, 1, 2], "y": [1, 2, 3], "stackgroup": "one"},
                  {"type": "scatter", "name": "B", "x": [0, 1, 2], "y": [2, 2, 1], "stackgroup": "one"}],
         "layout": {"title": "Parts", "yaxis": {"title": "Count (readings)"}}},
        {"kind": "area", "title": "Parts", "encoding": {"stack": "stacked"}, "data": {"series": [
            {"name": "A", "unit": "readings", "x": [0, 1, 2], "y": [1, 2, 3]},
            {"name": "B", "unit": "readings", "x": [0, 1, 2], "y": [2, 2, 1]}]}},
    ),
    "scatter-log-log-spectrum": (
        {"data": [{"type": "scatter", "name": "Spectrum", "x": [0.01, 0.1, 1, 10, 100],
                   "y": [1e3, 1e5, 1e6, 1e5, 1e4], "line": {"shape": "hv"}, "fill": "tozeroy"}],
         "layout": {"title": "Spectrum", "xaxis": {"type": "log", "title": "Energy (eV)"},
                    "yaxis": {"type": "log", "title": "Intensity (1/s)"}}},
        {"kind": "area", "title": "Spectrum", "axes": {"x": {"type": "log", "title": "Energy (eV)"},
                                                       "y": {"type": "log"}},
         "encoding": {"curve": "step"}, "data": {"series": [
            {"name": "Spectrum", "unit": "1/s", "x": [0.01, 0.1, 1, 10, 100], "y": [1e3, 1e5, 1e6, 1e5, 1e4]}]}},
    ),
    "bar-grouped": (
        {"data": [{"type": "bar", "name": "Measured", "x": ["Mon", "Tue"], "y": [3, 4]},
                  {"type": "bar", "name": "Planned", "x": ["Mon", "Tue"], "y": [3.5, 3.8]}],
         "layout": {"title": "Daily", "barmode": "group", "yaxis": {"title": "Energy (MWh)"}}},
        {"kind": "bar", "title": "Daily", "data": {"series": [
            {"name": "Measured", "unit": "MWh", "x": ["Mon", "Tue"], "y": [3, 4]},
            {"name": "Planned", "unit": "MWh", "x": ["Mon", "Tue"], "y": [3.5, 3.8]}]}},
    ),
    "bar-horizontal-percent": (
        {"data": [{"type": "bar", "orientation": "h", "name": "May", "y": ["S1", "S2"], "x": [1, 3]},
                  {"type": "bar", "orientation": "h", "name": "June", "y": ["S1", "S2"], "x": [2, 1]}],
         "layout": {"title": "Share", "barmode": "stack", "barnorm": "percent"}},
        {"kind": "bar", "title": "Share", "encoding": {"stack": "percent", "orientation": "horizontal"},
         "data": {"series": [{"name": "May", "x": ["S1", "S2"], "y": [1, 3]},
                             {"name": "June", "x": ["S1", "S2"], "y": [2, 1]}]}},
    ),
    "pie": (
        {"data": [{"type": "pie", "labels": ["A", "B", "C"], "values": [5, 3, 2], "name": "share",
                   "marker": {"colors": ["red", "green", "blue"]}}], "layout": {"title": "Share"}},
        {"kind": "pie", "title": "Share", "data": {"series": [{"name": "share", "x": ["A", "B", "C"], "y": [5, 3, 2]}]}},
    ),
    "pie-hole-donut": (
        {"data": [{"type": "pie", "labels": ["A", "B"], "values": [7, 3], "hole": 0.5}], "layout": {"title": "Ring"}},
        {"kind": "donut", "title": "Ring", "encoding": {"hole": 0.5},
         "data": {"series": [{"name": "values", "x": ["A", "B"], "y": [7, 3]}]}},
    ),
    "heatmap": (
        {"data": [{"type": "heatmap", "z": [[1, 2], [3, None]], "x": [0, 1], "y": [0, 1], "colorscale": "Turbo",
                   "colorbar": {"title": {"text": "level (arb)"}}}],
         "layout": {"title": "Field", "xaxis": {"title": "X (cm)"}, "yaxis": {"title": "Y (cm)", "scaleanchor": "x"}}},
        {"kind": "heatmap", "title": "Field", "axes": {"x": {"title": "X (cm)"}, "y": {"title": "Y (cm)"}},
         "encoding": {"aspect": "equal"},
         "data": {"field": {"unit": "arb", "x": [0, 1], "y": [0, 1], "z": [[1, 2], [3, None]]}}},
    ),
    "histogram": (
        {"data": [{"type": "histogram", "name": "Temp", "x": [1, 2, 2, 3, 3, 3, 4], "nbinsx": 4}],
         "layout": {"title": "Spread", "xaxis": {"title": "Temperature (degC)"}}},
        {"kind": "histogram", "title": "Spread", "axes": {"x": {"title": "Temperature (degC)"}},
         "encoding": {"bins": 4}, "data": {"series": [{"name": "Temp", "unit": "degC", "y": [1, 2, 2, 3, 3, 3, 4]}]}},
    ),
    "box": (
        {"data": [{"type": "box", "name": "G1", "y": [1, 2, 3, 4, 5]}, {"type": "box", "name": "G2", "y": [2, 3, 3, 4, 9]}],
         "layout": {"title": "Groups", "yaxis": {"title": "Value (V)"}}},
        {"kind": "box", "title": "Groups", "data": {"series": [
            {"name": "G1", "unit": "V", "y": [1, 2, 3, 4, 5]}, {"name": "G2", "unit": "V", "y": [2, 3, 3, 4, 9]}]}},
    ),
    "indicator-number": (
        {"data": [{"type": "indicator", "mode": "number+delta", "value": 951, "title": {"text": "Output"},
                   "number": {"suffix": " kW"}, "delta": {"reference": 900}}], "layout": {}},
        {"kind": "kpi", "title": "Output", "data": {"value": {"value": 951, "unit": "kW", "label": "Output", "delta": 51}}},
    ),
    "indicator-gauge": (
        {"data": [{"type": "indicator", "mode": "gauge+number", "value": 84, "title": {"text": "Load"},
                   "number": {"suffix": "percent"}, "gauge": {"axis": {"range": [0, 120]},
                                                              "threshold": {"value": 100}}}], "layout": {}},
        {"kind": "gauge", "title": "Load", "data": {"value": {"value": 84, "unit": "percent", "label": "Load",
                                                               "min": 0, "max": 120, "target": 100}}},
    ),
    "indicator-bullet": (
        {"data": [{"type": "indicator", "mode": "gauge+number", "value": 42, "title": {"text": "Week"},
                   "number": {"suffix": " MWh"}, "gauge": {"shape": "bullet", "axis": {"range": [0, 60]},
                                                           "threshold": {"value": 50},
                                                           "steps": [{"range": [0, 30]}, {"range": [30, 45]}]}}],
         "layout": {}},
        {"kind": "bullet", "title": "Week", "data": {"value": {"value": 42, "unit": "MWh", "label": "Week",
                                                                "min": 0, "max": 60, "target": 50, "ranges": [30, 45]}}},
    ),
    "subplots-annotations-shapes": (
        {"data": [{"type": "scatter", "name": "A", "x": T, "y": [1, 2, 3, 4], "xaxis": "x", "yaxis": "y"},
                  {"type": "scatter", "name": "B", "x": T, "y": [4, 3, 2, 1], "xaxis": "x2", "yaxis": "y2"}],
         "layout": {"title": "Two panels",
                    "xaxis": {"domain": [0, 0.45]}, "xaxis2": {"domain": [0.55, 1]},
                    "yaxis": {"domain": [0, 1], "type": "log"}, "yaxis2": {"domain": [0, 1], "anchor": "x2"},
                    "annotations": [
                        {"text": "Left", "x": 0.225, "y": 1.0, "xref": "paper", "yref": "paper", "showarrow": False},
                        {"text": "Right", "x": 0.775, "y": 1.0, "xref": "paper", "yref": "paper", "showarrow": False},
                        {"text": "<b>start</b>", "x": T[1], "y": 0.5, "xref": "x", "yref": "y domain"}],
                    "shapes": [{"type": "line", "x0": T[1], "x1": T[1], "y0": 0, "y1": 1, "xref": "x", "yref": "y domain"},
                               {"type": "rect", "xref": "x2 domain", "yref": "y2", "x0": 0, "x1": 1, "y0": 3, "y1": 4,
                                "fillcolor": "orange"}]}},
        {"kind": "line", "title": "Two panels",
         "data": {"series": [{"name": "A", "x": T, "y": [1, 2, 3, 4], "panel": "p1"},
                             {"name": "B", "x": T, "y": [4, 3, 2, 1], "panel": "p2"}]},
         "marks": [{"type": "rule", "axis": "x", "value": T[1], "panel": "p1", "label": "start"},
                   {"type": "band", "axis": "y", "from": 3, "to": 4, "tone": "warn", "panel": "p2"}],
         "layout": {"panels": [{"id": "p1", "axes": {"y": {"type": "log"}}, "title": "Left"},
                               {"id": "p2", "title": "Right"}], "columns": 2}},
    ),
}


def _native(spec: dict) -> dict:
    return {"schema_version": V, **spec}


@pytest.mark.parametrize("name", sorted(PAIRS))
def test_an_imported_figure_draws_exactly_as_its_native_twin(name: str) -> None:
    figure, native = PAIRS[name]
    imported = import_figure(figure, ref=f"test://{name}")
    assert imported.native, imported.refused
    assert imported.spec["source"]["type"] == "imported"
    a = normalise_svg(render_spec(imported.spec).svg)
    b = normalise_svg(render_spec(_native(native)).svg)
    assert a == b, json.dumps(imported.spec, indent=1)[:3000]
    # The content ids agree too: provenance is not content.
    assert us.spec_id(imported.spec) == us.spec_id(us.parse(_native(native)))


def test_every_supported_plotly_type_is_covered_here() -> None:
    covered = {f["data"][0].get("type", "scatter") for f, _ in PAIRS.values()}
    assert covered >= {"scatter", "bar", "pie", "heatmap", "histogram", "box", "indicator"}


def test_an_import_keeps_the_original_by_reference_and_digest() -> None:
    figure, _ = PAIRS["bar-grouped"]
    res = import_figure(figure, ref="library://figures/daily.json", chart_id="daily")
    src = res.spec["source"]
    assert src["figure"] == "library://figures/daily.json"
    assert src["digest"] == us.figure_digest(figure)
    assert src["revision"] == 1 and src["importer_version"]


def test_a_reimport_updates_the_same_chart() -> None:
    figure, _ = PAIRS["bar-grouped"]
    first = import_figure(figure, ref="library://daily.json", chart_id="daily").spec
    changed = json.loads(json.dumps(figure))
    changed["data"][0]["y"] = [9, 9]
    second = reimport(first, changed).spec
    assert second["id"] == "daily"
    assert second["source"]["revision"] == 2
    assert second["source"]["figure"] == "library://daily.json"
    assert second["data"]["series"][0]["y"] == [9, 9]
    assert second["source"]["digest"] != first["source"]["digest"]


def test_what_the_author_styled_is_reported_not_refused() -> None:
    figure, _ = PAIRS["heatmap"]
    res = import_figure(figure)
    assert res.native
    assert any("Turbo" in r for r in res.spec["source"]["replaced"])


@pytest.mark.parametrize("trace", ["cone", "streamtube", "contour", "sankey", "violin", "candlestick"])
def test_an_unsupported_trace_is_refused_whole_and_goes_to_the_fallback(trace: str) -> None:
    res = import_figure({"data": [{"type": trace, "z": [[1]], "name": "t"}], "layout": {"title": "x"}})
    assert not res.native and res.spec is None
    assert any(trace in r for r in res.refused)
    assert res.fallback is not None


def test_two_trace_families_are_refused_rather_than_half_drawn() -> None:
    res = import_figure({"data": [{"type": "bar", "x": [1], "y": [1]}, {"type": "pie", "values": [1]}],
                         "layout": {}})
    assert not res.native and "mixes" in " ".join(res.refused)


def test_html_is_stripped_from_every_text() -> None:
    fig = {"data": [{"type": "scatter", "name": "<b>Out</b><script>x</script>", "x": T, "y": [1, 2, 3, 4]}],
           "layout": {"title": {"text": "<i>Main</i><br>a <a href='javascript:alert(1)'>sub</a>"}}}
    spec = import_figure(fig).spec
    assert spec["title"] == "Main" and spec["subtitle"] == "a sub"
    assert "<" not in json.dumps(spec["data"])


def test_the_fallback_carries_no_executable_content() -> None:
    fig = {"data": [{"type": "surface", "z": [[1]], "customdata": [1], "hovertemplate": "<a href='x'>%{z}</a>",
                     "meta": {"tracker": "x"}}],
           "layout": {"title": {"text": "<img src=x onerror=alert(1)>T"}}}
    clean = sanitize_figure(fig)
    text = json.dumps(clean)
    assert "customdata" not in text and "hovertemplate" not in text and "onerror" not in text
    assert "tracker" not in text


def test_update_menus_become_controls() -> None:
    """A Before/After restyle menu becomes a segmented control over groups; a
    Mode ON/OFF relayout menu becomes a toggle over a mark layer."""
    base = [{"text": "P", "x": 0.5, "y": 1.0, "xref": "paper", "yref": "paper"}]
    mode_ann = [{"text": "STEADY", "x": T[2], "y": 0.5, "xref": "x", "yref": "y domain", "textangle": 90}]
    mode_shapes = [{"type": "line", "x0": T[2], "x1": T[2], "y0": 0, "y1": 1, "xref": "x", "yref": "y domain"}]
    fig = {"data": [{"type": "scatter", "name": "A", "x": T, "y": [1, 2, 3, 4]},
                    {"type": "scatter", "name": "A", "x": T, "y": [1, 2, 2.5, 4]}],
           "layout": {"title": "Raw and final", "annotations": base, "updatemenus": [
               {"type": "buttons", "active": 1, "buttons": [
                   {"label": "Mode ON", "method": "relayout", "args": [{"annotations": base + mode_ann, "shapes": mode_shapes}]},
                   {"label": "Mode OFF", "method": "relayout", "args": [{"annotations": base, "shapes": []}]}]},
               {"type": "buttons", "active": 1, "buttons": [
                   {"label": "Before", "method": "restyle", "args": [{"visible": [True, False]}]},
                   {"label": "After", "method": "restyle", "args": [{"visible": [False, True]}]}]}]}}
    # Two series named alike would be one series twice; the menu separates them.
    fig["data"][1]["name"] = "A final"
    res = import_figure(fig)
    assert res.native, res.refused
    controls = {c["type"]: c for c in res.spec["controls"]}
    assert controls["toggle"]["value"] is False and controls["toggle"]["label"] == "Mode"
    assert controls["segmented"]["value"] == "After"
    rule = next(m for m in res.spec["marks"] if m["type"] == "rule")
    assert rule["label"] == "STEADY" and rule["layer"] == controls["toggle"]["bind"]["key"]
    on = render_spec(res.spec, state={controls["toggle"]["id"]: True}).svg
    off = render_spec(res.spec).svg
    assert "STEADY" in on and "STEADY" not in off


def test_frames_and_a_slider_become_a_frame_control() -> None:
    def field(k: float) -> dict:
        return {"type": "heatmap", "z": [[k, k + 1], [k + 2, k + 3]]}
    fig = {"data": [field(0)], "layout": {"title": "Planes", "sliders": [{"currentvalue": {"prefix": "Plane: "},
                                                                          "steps": []}]},
           "frames": [{"name": "0", "data": [field(0)]}, {"name": "10", "data": [field(10)]}]}
    res = import_figure(fig)
    assert res.native, res.refused
    ctl = res.spec["controls"][0]
    assert ctl["type"] == "slider" and ctl["bind"] == {"to": "frame"} and ctl["label"] == "Plane"
    assert render_spec(res.spec, state={"frame": 10}).svg != render_spec(res.spec, state={"frame": 0}).svg


def test_a_trame_widget_drives_a_sampled_figure_as_frames() -> None:
    def fig(z: int) -> dict:
        return {"data": [{"type": "heatmap", "z": [[z, 1], [2, 3]]}], "layout": {"title": f"Field at {z}"}}
    out = import_trame(
        {"fig_field": fig(0)},
        widgets=[{"type": "VSlider", "v_model": "z", "label": "Axial plane", "min": 0, "max": 20, "step": 10}],
        state={"z": 10},
        sweeps={"fig_field": {"var": "z", "values": {0: fig(0), 10: fig(10), 20: fig(20)}}},
    )
    spec = out["fig_field"].spec
    assert spec["source"]["format"] == "trame"
    ctl = spec["controls"][0]
    assert ctl["label"] == "Axial plane" and ctl["bind"] == {"to": "frame"} and ctl["value"] == 10
    assert len(spec["data"]["frames"]["items"]) == 3
