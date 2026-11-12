# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""ChartSpec 2.0: one document, every kind drawn, controls that drive it, and
the two older vocabularies (and saved views) carried onto it without loss."""

from __future__ import annotations

import math
import re

import pytest

from axiom.extensions.builtins.data_platform.charts import CHART_KINDS
from axiom.extensions.builtins.data_platform.charts import ChartSpec as CatalogEntry
from axiom.extensions.builtins.scidisplay import palettes
from axiom.extensions.builtins.scidisplay import unified_spec as us
from axiom.extensions.builtins.scidisplay.chart_kinds import FOCUSABLE_MAX, render_spec
from axiom.extensions.builtins.scidisplay.unified_migrate import (
    CATALOG_KINDS,
    from_catalog,
    from_saved_graph,
    from_v1,
    migrate_saved_view,
)

V = us.SCHEMA_VERSION
T = [f"2026-09-21T0{h}:00:00+00:00" for h in range(6)]


def _s(**kw):
    return {"schema_version": V, **kw}


EXAMPLES = {
    "line": _s(kind="line", title="L", data={"series": [{"name": "A", "unit": "kW", "x": T, "y": [1, 2, 3, 2, 1, 2]}]}),
    "line-numeric": _s(kind="line", title="L", axes={"x": {"type": "log"}},
                       data={"series": [{"name": "A", "x": [1, 10, 100], "y": [3, 2, 1]}]}),
    "area": _s(kind="area", title="A", encoding={"stack": "stacked"}, data={"series": [
        {"name": "A", "x": [0, 1], "y": [1, 2]}, {"name": "B", "x": [0, 1], "y": [1, 1]}]}),
    "step": _s(kind="step", title="S", data={"series": [{"name": "A", "x": T, "y": [0, 1, 1, 2, 2, 0]}]}),
    "sparkline": _s(kind="sparkline", title="S", data={"series": [{"name": "A", "y": [1, 3, 2]}]}),
    "bar": _s(kind="bar", title="B", data={"series": [{"name": "A", "unit": "MWh", "x": ["a", "b"], "y": [1, 2]}]}),
    "bar-stacked-h": _s(kind="bar", title="B", encoding={"stack": "stacked", "orientation": "horizontal"},
                        data={"series": [{"name": "A", "x": ["a"], "y": [1]}, {"name": "B", "x": ["a"], "y": [2]}]}),
    "bullet": _s(kind="bullet", title="B", data={"value": {"value": 3, "min": 0, "max": 5, "target": 4}}),
    "pie": _s(kind="pie", title="P", data={"series": [{"name": "A", "x": ["a", "b"], "y": [1, 3]}]}),
    "donut": _s(kind="donut", title="D", data={"series": [{"name": "A", "x": ["a", "b"], "y": [1, 3]}]}),
    "progress": _s(kind="progress", title="P", data={"value": {"value": 30, "unit": "percent", "max": 100}}),
    "gauge": _s(kind="gauge", title="G", data={"value": {"value": 30, "unit": "percent", "max": 100, "target": 80}}),
    "kpi": _s(kind="kpi", title="K", data={"value": {"value": 30.5, "unit": "kW", "trend": [28, 29, 30.5]}}),
    "histogram": _s(kind="histogram", title="H", data={"series": [{"name": "A", "unit": "V", "y": list(range(50))}]}),
    "box": _s(kind="box", title="X", data={"series": [{"name": "A", "y": [1, 2, 3, 4, 50]}]}),
    "scatter": _s(kind="scatter", title="S", encoding={"fit": "linear"},
                  data={"series": [{"name": "A", "x": [1, 2, 3, 4], "y": [2, 4, 6, 8.5]}]}),
    "heatmap": _s(kind="heatmap", title="F", data={"field": {"z": [[1, 2], [3, None]]}}),
    "mesh3d": _s(kind="mesh3d", title="M", data={"scene": {
        "mesh": {"points": [[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1]], "cells": [[0, 1, 2], [0, 1, 3], [0, 2, 3]]},
        "scalars": {"heat": {"unit": "W", "association": "cell", "values": [1, 2, 3]}}, "elements": ["a", "b", "b"]}}),
    "surface": _s(kind="surface", title="S", data={"scene": {"grid": {"z": [[1, 2], [3, 4]]}}}),
    "scatter3d": _s(kind="scatter3d", title="P", data={"scene": {"points": [[0, 0, 0], [1, 1, 1]],
                    "scalars": {"v": {"association": "point", "values": [1, 2]}}}}),
    "volume": _s(kind="volume", title="V", data={"scene": {"grid": {"x": [0, 1], "y": [0, 1], "z": [0, 1],
                 "values": [1, 2, 3, 4, 5, 6, 7, 8]}}}),
    "flame": _s(kind="flame", title="F", data={"tree": {"folded": "a;b 3\na;c 2\nd 1"}}),
    "state": _s(kind="state", title="S", data={"series": [{"name": "A", "x": T, "y": [0, 1, 1, 2, 2, 0]}]}),
}


def test_every_registered_kind_has_an_example_here() -> None:
    assert set(us.registered_kinds()) <= {k.split("-")[0] for k in EXAMPLES}


@pytest.mark.parametrize("name", sorted(EXAMPLES))
def test_every_kind_draws_an_accessible_figure(name: str) -> None:
    svg = render_spec(EXAMPLES[name]).svg
    assert svg.startswith("<svg") and 'role="img"' in svg.split(">", 1)[0]
    assert "<title id=" in svg and "<desc id=" in svg
    assert "data-axk-mark" in svg, "no mark a pointer or a keyboard can reach"
    labels = re.findall(r'data-axk-mark=""[^>]*aria-label="([^"]+)"', svg)
    assert labels, "marks carry their value"
    # Chrome themes with the page; literals are the light figure for export.
    assert "var(--axk-plot-paper," in svg


@pytest.mark.parametrize("name", sorted(EXAMPLES))
def test_the_renderer_is_deterministic(name: str) -> None:
    assert render_spec(EXAMPLES[name]).svg == render_spec(EXAMPLES[name]).svg


def test_a_big_field_takes_one_tab_stop_not_thousands() -> None:
    big = _s(kind="heatmap", title="F", data={"field": {"z": [[i * j for i in range(40)] for j in range(40)]}})
    svg = render_spec(big).svg
    assert svg.count('tabindex="0"') == 1 and FOCUSABLE_MAX < 40 * 40


def test_a_value_says_its_unit() -> None:
    svg = render_spec(EXAMPLES["kpi"]).svg
    assert "30.5 kW" in svg


def test_a_heatmap_is_never_a_rainbow() -> None:
    assert palettes.sequential_name("turbo") == "cividis"
    assert palettes.sequential_name("jet") == "cividis"
    svg = render_spec(EXAMPLES["heatmap"]).svg
    assert palettes.SEQUENTIAL["cividis"][0] in svg or "#00224e" in svg


def test_the_palette_registry_serves_every_sort() -> None:
    reg = palettes.registry()
    assert set(reg) == {"categorical", "sequential", "diverging"}
    assert reg["sequential"]["cividis"][0] == "#00224e"


# ---- the document ----------------------------------------------------------


def test_a_document_says_every_problem_at_once() -> None:
    problems = us.validate(_s(kind="pie", axes={"x": {}}, data={"series": []}, marks=[{"type": "band"}]))
    joined = " ".join(problems)
    for expected in ("title", "a non-empty list", "has no axes", "no axes to mark"):
        assert expected in joined


def test_a_newer_or_older_major_is_refused_with_the_way_forward() -> None:
    assert "from_v1" in us.validate({"schema_version": "1.1", "kind": "timeseries"})[0]
    assert us.validate({"schema_version": "2.9", "kind": "line"})


def test_a_measured_chart_may_not_carry_a_model() -> None:
    doc = _s(kind="line", title="x", catalog={"provenance": "measured"},
             data={"series": [{"name": "m", "y": [1], "modelled": True}]})
    assert any("never render as a measurement" in p for p in us.validate(doc))


def test_an_import_must_pin_its_original() -> None:
    doc = dict(EXAMPLES["bar"], source={"type": "imported", "format": "plotly", "figure": "x",
                                        "importer_version": "2"})
    assert any("digest" in p for p in us.validate(doc))


def test_a_control_must_bind_to_something_that_exists() -> None:
    doc = dict(EXAMPLES["line"], controls=[{"id": "m", "type": "toggle", "label": "Modes",
                                            "bind": {"to": "layer", "key": "modes"}}])
    assert any("no mark carries" in p for p in us.validate(doc))


def test_the_json_schema_names_every_kind_and_control() -> None:
    schema = us.json_schema()
    assert set(schema["properties"]["kind"]["enum"]) == set(us.registered_kinds())
    assert set(schema["properties"]["controls"]["items"]["properties"]["type"]["enum"]) == set(us.CONTROL_TYPES)


# ---- state -----------------------------------------------------------------

FRAMED = _s(kind="heatmap", title="F",
            data={"frames": {"key": "z", "items": [{"value": z, "data": {"field": {"z": [[z, 1], [2, 3]]}}}
                                                   for z in (0, 10, 20)]}},
            controls=[{"id": "z", "type": "slider", "label": "Plane", "bind": {"to": "frame"}}])


def test_a_control_starts_at_its_first_value() -> None:
    assert us.resolve_state(FRAMED).values == {"z": 0}


def test_a_value_a_control_cannot_take_is_answered_not_clamped() -> None:
    r = us.resolve_state(FRAMED, {"z": 15, "nope": 1})
    assert r.values == {"z": 0}
    assert any("15 is not one of its values" in n for n in r.notes)
    assert any("no control 'nope'" in n for n in r.notes)


def test_moving_a_control_changes_the_figure() -> None:
    assert render_spec(FRAMED, state={"z": 20}).svg != render_spec(FRAMED, state={"z": 0}).svg


def test_a_range_control_windows_the_x_axis() -> None:
    doc = dict(EXAMPLES["step"], controls=[{"id": "w", "type": "range", "label": "Window",
                                            "bind": {"to": "window"}, "min": 0, "max": 1}])
    full = render_spec(doc).svg
    part = render_spec(doc, state={"w": [T[1], T[3]]}).svg
    assert full != part


def test_a_group_control_shows_one_group() -> None:
    doc = _s(kind="bar", title="B", data={"series": [{"name": "A", "group": "before", "x": ["a"], "y": [1]},
                                                     {"name": "B", "group": "after", "x": ["a"], "y": [2]}]},
             controls=[{"id": "g", "type": "segmented", "label": "Data", "bind": {"to": "group"},
                        "options": [{"value": "before"}, {"value": "after"}], "value": "after"}])
    data = us.select_data(doc, us.resolve_state(doc))
    assert [s["name"] for s in data["series"]] == ["B"]


# ---- migration -------------------------------------------------------------


def test_a_v1_document_migrates_onto_a_query() -> None:
    v1 = {"schema_version": "1.1", "kind": "comparison", "site": "s1", "channels": ["f:a", "f:b"],
          "window": {"from": "2026-01-01T00:00:00Z", "to": "2026-01-02T00:00:00Z"},
          "transform": {"bucket": "5m", "agg": "mean"}, "title": "A vs B", "display_units": {"W": "kW"}}
    doc = from_v1(v1)
    assert doc["kind"] == "line" and doc["encoding"] == {"compare": True}
    assert doc["data"]["query"]["transform"] == {"bucket": "5m", "agg": "mean"}
    assert doc["display_units"] == {"W": "kW"}


def test_every_catalogue_kind_has_a_unified_kind() -> None:
    assert set(CHART_KINDS) == set(CATALOG_KINDS)


def test_a_catalogue_entry_keeps_its_registry_fields() -> None:
    entry = CatalogEntry(chart_id="ops.power", name="Power", kind="dual_axis_timeseries",
                         provenance="measured", roles=("power", "temp"), unit="W", unit_authority="map",
                         min_series=2, group="operations", order=5, default=True, verb="data.series")
    doc = from_catalog(entry)
    assert doc["id"] == "ops.power" and doc["kind"] == "line"
    assert doc["catalog"]["verb"] == "data.series" and doc["catalog"]["roles"] == ["power", "temp"]
    assert doc["catalog"]["unit_authority"] == "map" and doc["catalog"]["default"] is True
    assert doc["data"]["verb"]["name"] == "data.series"


def test_a_saved_view_keeps_its_state_and_gains_specs() -> None:
    view = {"groups": [{"id": "k1", "syncDates": True,
                        "window": {"from": "2025-03-20T00:00:00Z", "to": "2025-03-21T00:00:00Z"},
                        "graphs": [{"id": "g1", "site": "s", "series": ["f:a", "f:b"], "log": True},
                                   {"id": "g2", "site": "s", "series": []}]}],
            "display": None, "expanded": "k1/g1"}
    out = migrate_saved_view(view)
    g1 = out["groups"][0]["graphs"][0]
    assert out["expanded"] == "k1/g1" and g1["id"] == "g1"
    assert g1["spec"]["data"]["query"]["window"]["from"] == "2025-03-20T00:00:00Z"
    assert g1["spec"]["axes"] == {"y": {"type": "log"}}
    assert "spec" not in out["groups"][0]["graphs"][1]


def test_a_query_chart_is_drawn_through_the_resolver_it_is_given() -> None:
    doc = from_saved_graph({"site": "s", "series": ["f:a"], "window": {"from": T[0], "to": T[-1]}})
    blank = render_spec(doc)
    assert "no figure source" in " ".join(blank.notes)

    def resolve(_doc, data, _state):
        return {"series": [{"name": "a", "unit": "kW", "x": T, "y": [1, 2, 3, 4, 5, 6]}]}

    drawn = render_spec(doc, resolve=resolve)
    assert "kW" in drawn.svg and drawn.readout


def test_a_small_map_can_print_its_values_in_its_cells() -> None:
    doc = _s(kind="heatmap", title="Map", encoding={"labels": True},
             data={"field": {"z": [[0.91, 1.02], [1.10, None]]}})
    svg = render_spec(doc, width=500, height=420).svg
    assert "axk-cell-labels" in svg and ">1.1<" in svg or ">1.10<" in svg


def test_two_controls_on_one_target_are_one_value() -> None:
    doc = dict(FRAMED, controls=[*FRAMED["controls"], {"id": "play", "type": "playback", "label": "Play",
                                                        "bind": {"to": "frame"}}])
    assert us.resolve_state(doc).values == {"z": 0, "play": 0}
    moved = us.resolve_state(doc, {"play": 20})
    assert moved.values == {"z": 20, "play": 20} and moved.frame == 20


def test_the_published_json_schema_is_the_one_the_code_holds() -> None:
    import json
    from pathlib import Path

    path = next(p for p in Path(__file__).resolve().parents if (p / "docs" / "reference").is_dir())
    published = json.loads((path / "docs/reference/chart-spec-2.0.schema.json").read_text())
    assert published == us.json_schema(), "regenerate docs/reference/chart-spec-2.0.schema.json"


# ---- every series colour reads in both themes ---------------------------------------------


def _both(colour: str) -> tuple[str, str]:
    """(light, dark) of a colour as the renderer writes it."""
    written = palettes.series_colour(colour)
    m = re.match(r"light-dark\((#[0-9a-f]{6}),(#[0-9a-f]{6})\)", written)
    return (m.group(1), m.group(2)) if m else (written, written)


def _all_series_colours() -> list[tuple[str, str]]:
    from axiom.extensions.builtins.scidisplay.chart_colour import FAMILIES, assign_colours

    out = [(f"categorical {name}", c) for name, pal in palettes.CATEGORICAL.items() for c in pal]
    units = {"temperature": "degC", "pressure": "kPa", "flow": "kg/s", "radiation": "Sv/h", "power": "W",
             "electrical": "V", "length": "mm", "fraction": "percent", "undeclared": ""}
    for family in FAMILIES:
        for n in range(1, 7):
            colours, _ = assign_colours([(f"{family}-{i}", units[family]) for i in range(n)])
            out.extend((f"{family} x{n}", c) for c in colours)
    return out


@pytest.mark.parametrize("where, colour", _all_series_colours())
def test_every_series_colour_reads_on_both_papers(where: str, colour: str) -> None:
    light, dark = _both(colour)
    assert palettes.contrast(light, palettes.PAPERS["light"]) >= palettes.MIN_CONTRAST - 0.05, where
    assert palettes.contrast(dark, palettes.PAPERS["dark"]) >= palettes.MIN_CONTRAST, where


def test_the_dark_twin_keeps_the_hue_and_export_keeps_the_light_colour() -> None:
    from axiom.extensions.builtins.scidisplay.chart_colour import _channels, rgb_to_lab
    from axiom.extensions.builtins.scidisplay.chart_svg import flatten_theme

    light, dark = _both("#7a1f1f")
    assert dark != light
    hue = lambda c: math.degrees(math.atan2(*reversed(rgb_to_lab(_channels(c))[1:])))  # noqa: E731
    assert abs(hue(light) - hue(dark)) < 12
    assert flatten_theme(f'<line stroke="light-dark({light},{dark})"/>') == f'<line stroke="{light}"/>'


def test_an_empty_category_is_said_not_left_blank() -> None:
    doc = _s(kind="bar", title="Share", encoding={"stack": "percent", "orientation": "horizontal"},
             data={"series": [{"name": "May", "x": ["a", "b"], "y": [3, 0]},
                              {"name": "June", "x": ["a", "b"], "y": [1, 0]}]})
    assert "no values in this view" in render_spec(doc).svg
