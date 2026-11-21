# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""3D kinds (ADR-214 D10): one document for native and imported scenes, the
same resolved scene for the browser's WebGL view and the static fallback, and
controls that cut, explode, recolour and turn it."""

from __future__ import annotations

import math
import re

import pytest

from axiom.extensions.builtins.scidisplay import unified_spec as us
from axiom.extensions.builtins.scidisplay.chart_kinds import render_spec
from axiom.extensions.builtins.scidisplay.plotly_import import import_figure, import_vtk_scene

V = us.SCHEMA_VERSION


def _prism_mesh(n_el: int = 3, segments: int = 4):
    """Square columns side by side, cut into axial segments: a small 'core'."""
    points, cells, elements, power, flux = [], [], [], [], []
    for e in range(n_el):
        x0 = e * 2.0
        for k in range(segments):
            z0, z1 = k * 1.0, (k + 1) * 1.0
            base = len(points)
            for z in (z0, z1):
                points += [[x0, 0, z], [x0 + 1, 0, z], [x0 + 1, 1, z], [x0, 1, z]]
            for a, b in ((0, 1), (1, 2), (2, 3), (3, 0)):
                cells.append([base + a, base + b, base + 4 + b, base + 4 + a])
                elements.append(f"E{e}")
                shape = math.sin(math.pi * (k + 0.5) / segments) * (1 + 0.1 * e)
                power.append(round(shape, 4))
                flux.append(round(shape * 2, 4))
    return {"mesh": {"points": points, "cells": cells}, "elements": elements, "length_unit": "cm",
            "scalars": {"power": {"unit": "relative", "association": "cell", "values": power},
                        "flux": {"unit": "arb", "association": "cell", "values": flux}}}


CONTROLS = [
    {"id": "camera", "type": "segmented", "label": "View", "bind": {"to": "encoding", "key": "camera"},
     "options": [{"value": "iso"}, {"value": "top"}, {"value": "side"}], "value": "iso"},
    {"id": "plane", "type": "slider", "label": "Axial cut", "bind": {"to": "encoding", "key": "slice_at"},
     "min": 0, "max": 4, "step": 1, "value": 4},
    {"id": "scalar", "type": "dropdown", "label": "Colour by", "bind": {"to": "encoding", "key": "scalar"},
     "options": [{"value": "power"}, {"value": "flux"}]},
    {"id": "opacity", "type": "slider", "label": "Opacity", "bind": {"to": "encoding", "key": "opacity"},
     "min": 0.2, "max": 1, "step": 0.1, "value": 1},
    {"id": "explode", "type": "slider", "label": "Explode", "bind": {"to": "encoding", "key": "explode"},
     "min": 0, "max": 1, "step": 0.25, "value": 0},
]


def _doc(**kw):
    return {"schema_version": V, "kind": "mesh3d", "title": "Columns", "data": {"scene": _prism_mesh()},
            "encoding": {"cutaway": True, "slice_axis": "z"}, "controls": CONTROLS, **kw}


def test_a_scene_is_chrome_around_a_box_and_a_resolved_payload() -> None:
    r = render_spec(_doc())
    assert 'data-axk-scene="' in r.svg and 'class="axk-scene-fallback"' in r.svg
    assert r.scene and r.scene["box"] and len(r.scene["polys"]) == 3 * 4 * 4
    assert r.scene["stops"][0] == "#00224e" and r.scene["scalar"] == "power"
    assert r.scene["camera"]["azimuth"] == 45.0


def test_each_element_is_reachable_by_hover_and_keyboard() -> None:
    svg = render_spec(_doc()).svg
    labels = re.findall(r'data-axk-mark="" tabindex="0" aria-label="(E\d: [^"]+)"', svg)
    assert sorted(lab.split(":")[0] for lab in labels) == ["E0", "E1", "E2"]
    assert "relative (power)" in labels[0]


def test_the_cut_slider_removes_what_is_above_the_plane() -> None:
    whole = render_spec(_doc()).scene
    half = render_spec(_doc(), state={"plane": 2}).scene
    assert len(half["polys"]) == len(whole["polys"]) // 2
    assert any("cut away" in n for n in render_spec(_doc(), state={"plane": 2}).notes)


def test_the_scalar_picker_recolours_and_the_camera_turns() -> None:
    power = render_spec(_doc())
    flux = render_spec(_doc(), state={"scalar": "flux"})
    assert flux.scene["scalar"] == "flux" and flux.scene["range"][1] == pytest.approx(2 * power.scene["range"][1], rel=1e-3)
    top = render_spec(_doc(), state={"camera": "top"})
    assert top.scene["camera"]["elevation"] == 90.0 and top.svg != power.svg


def test_explode_moves_elements_apart() -> None:
    a = render_spec(_doc()).scene
    b = render_spec(_doc(), state={"explode": 1}).scene
    span = lambda s: max(p[0] for p in s["points"]) - min(p[0] for p in s["points"])  # noqa: E731
    assert span(b) > span(a)


def test_inline_geometry_is_bounded() -> None:
    doc = {"schema_version": V, "kind": "scatter3d", "title": "Big",
           "data": {"scene": {"points": [[i, 0, 0] for i in range(260_000)]}}}
    assert any("carry it as a ref" in p for p in us.validate(doc))


def test_an_imported_plotly_mesh_is_the_native_mesh() -> None:
    mesh = _prism_mesh(2, 2)
    pts, tris, intensity = mesh["mesh"]["points"], [], []
    for c, v in zip(mesh["mesh"]["cells"], mesh["scalars"]["power"]["values"], strict=True):
        tris += [(c[0], c[1], c[2]), (c[0], c[2], c[3])]
        intensity += [v, v]
    fig = {"data": [{"type": "mesh3d", "name": "power", "x": [p[0] for p in pts], "y": [p[1] for p in pts],
                     "z": [p[2] for p in pts], "i": [t[0] for t in tris], "j": [t[1] for t in tris],
                     "k": [t[2] for t in tris], "intensity": intensity, "intensitymode": "cell",
                     "colorscale": "Jet", "colorbar": {"title": {"text": "power (relative)"}}}],
           "layout": {"title": "Columns", "scene": {"camera": {"eye": {"x": 1.25, "y": -1.25, "z": 1.25}}}}}
    native = {"schema_version": V, "kind": "mesh3d", "title": "Columns",
              "encoding": {"camera": {"azimuth": 45.0, "elevation": 35.26}},
              "data": {"scene": {"mesh": {"points": pts, "cells": [list(t) for t in tris]},
                                 "scalars": {"power": {"unit": "relative", "association": "cell",
                                                       "values": intensity}}}}}
    res = import_figure(fig, ref="test://mesh3d")
    assert res.native, res.refused
    assert "colour scale Jet (drawn cividis)" in res.spec["source"]["replaced"]
    assert us.spec_id(res.spec) == us.spec_id(us.parse(native))
    a, b = render_spec(res.spec), render_spec(native)
    assert a.svg == b.svg and a.scene == b.scene


def test_a_plotly_surface_and_volume_import() -> None:
    surf = import_figure({"data": [{"type": "surface", "z": [[1, 2], [3, 4]], "x": [0, 1], "y": [0, 1]}],
                          "layout": {"title": "S"}})
    assert surf.native and surf.spec["kind"] == "surface"
    xs, ys, zs, vs = [], [], [], []
    for z in (0, 1):
        for y in (0, 1):
            for x in (0, 1):
                xs.append(x), ys.append(y), zs.append(z), vs.append(x + y + z)
    vol = import_figure({"data": [{"type": "isosurface", "x": xs, "y": ys, "z": zs, "value": vs,
                                   "isomin": 1, "isomax": 2}], "layout": {"title": "V"}})
    assert vol.native and vol.spec["kind"] == "volume" and vol.spec["encoding"]["iso"] == [1, 2]
    assert render_spec(vol.spec).scene["polys"]


def test_a_trame_vtk_scene_imports_as_a_mesh_with_its_original_kept() -> None:
    mesh = _prism_mesh(1, 2)
    scene = {"points": mesh["mesh"]["points"], "polys": mesh["mesh"]["cells"],
             "cell_data": {"power": mesh["scalars"]["power"]["values"]}, "units": {"power": "relative"}}
    res = import_vtk_scene(scene, ref="trame://app/state/scene", title="Columns")
    assert res.native and res.spec["source"]["format"] == "trame"
    assert res.spec["source"]["digest"].startswith("sha256:")
    assert render_spec(res.spec).scene["scalar"] == "power"


def test_a_cone_is_refused_with_a_reason() -> None:
    res = import_figure({"data": [{"type": "cone", "x": [0], "y": [0], "z": [0], "u": [1], "v": [0], "w": [0]}],
                         "layout": {}})
    assert not res.native and any("vector field" in r for r in res.refused)


def test_a_single_precision_vtk_scene_matches_its_native_twin() -> None:
    import struct

    mesh = _prism_mesh(1, 2)
    f32 = lambda v: struct.unpack("<f", struct.pack("<f", v + 0.1))[0]  # noqa: E731
    scene = {"points": [[f32(c) for c in p] for p in mesh["mesh"]["points"]], "polys": mesh["mesh"]["cells"],
             "cell_data": {"power": mesh["scalars"]["power"]["values"]}}
    native = {"schema_version": V, "kind": "mesh3d", "title": "Columns", "data": {"scene": {
        "mesh": {"points": [[round(c + 0.1, 4) for c in p] for p in mesh["mesh"]["points"]],
                 "cells": mesh["mesh"]["cells"]},
        "scalars": {"power": {"association": "cell", "values": mesh["scalars"]["power"]["values"]}}}}}
    res = import_vtk_scene(scene, ref="trame://x", title="Columns")
    assert us.spec_id(res.spec) == us.spec_id(us.parse(native))


def test_a_surface_keeps_its_value_name_and_unit() -> None:
    res = import_figure({"data": [{"type": "surface", "name": "power", "z": [[1, 2], [3, 4]],
                                   "colorbar": {"title": {"text": "power (relative)"}}}], "layout": {"title": "S"}})
    r = render_spec(res.spec)
    assert r.scene["scalar"] == "power" and r.scene["unit"] == "relative"
    assert ">relative</text>" in r.svg


@pytest.mark.parametrize("name", ["cividis", "viridis", "greys"])
@pytest.mark.parametrize("theme", ["light", "dark"])
def test_every_3d_colour_stands_off_its_paper(name, theme) -> None:
    from axiom.extensions.builtins.scidisplay import palettes

    for stop in palettes.scene_stops(name, theme):
        assert palettes.contrast(stop, palettes.PAPERS[theme]) >= palettes.SCENE_MIN_CONTRAST - 0.02, (name, theme, stop)


def test_a_scene_carries_both_themes_and_the_static_view_follows_the_page() -> None:
    r = render_spec(_doc())
    assert r.scene["stops_dark"][0] != r.scene["stops"][0]
    assert "light-dark(" in r.svg


def test_a_flat_surface_is_drawn_at_a_readable_aspect_and_says_so() -> None:
    grid = {"x": [0, 10, 20, 30], "y": [0, 10, 20, 30],
            "z": [[0.1 * (i + j) for i in range(4)] for j in range(4)]}
    doc = {"schema_version": V, "kind": "surface", "title": "Flat", "data": {"scene": {"grid": grid}}}
    auto = render_spec(doc)
    zs = [p[2] for p in auto.scene["points"]]
    assert max(zs) - min(zs) == pytest.approx(15, rel=0.01)
    assert any("height drawn at" in n for n in auto.notes)
    assert auto.scene["point_values"] and len(auto.scene["point_values"]) == len(auto.scene["points"])
    true = render_spec({**doc, "encoding": {"aspect": "data"}})
    assert max(p[2] for p in true.scene["points"]) == pytest.approx(0.6)
