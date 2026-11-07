# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The flame kind (ADR-214): one hierarchy from three forms, drawn as flame or
icicle, zoomed, searched and inverted by controls, fast at fifty thousand frames,
and an imported Plotly icicle indistinguishable from its native twin."""

from __future__ import annotations

import random
import re
import time

import pytest

from axiom.extensions.builtins.scidisplay import flame_data as fd
from axiom.extensions.builtins.scidisplay import unified_spec as us
from axiom.extensions.builtins.scidisplay.chart_kinds import render_spec
from axiom.extensions.builtins.scidisplay.plotly_import import import_figure

FOLDED = """main;load;read 30
main;load;parse 20
main;draw;axes 15
main;draw;series;path 25
main;draw;series;labels 10
main 5
"""

CONTROLS = [
    {"id": "zoom", "type": "path", "label": "Zoom", "bind": {"to": "zoom"}},
    {"id": "find", "type": "text", "label": "Search", "bind": {"to": "search"}},
    {"id": "invert", "type": "toggle", "label": "Invert", "bind": {"to": "encoding", "key": "invert"}, "value": False},
    {"id": "direction", "type": "segmented", "label": "Direction", "bind": {"to": "encoding", "key": "direction"},
     "options": [{"value": "flame"}, {"value": "icicle"}]},
]


def _doc(tree, **kw):
    return {"schema_version": us.SCHEMA_VERSION, "kind": "flame", "title": "Profile",
            "data": {"tree": tree}, "controls": CONTROLS, **kw}


def _shape(node: fd.Node) -> dict:
    return {"name": node.name, "value": node.value, "self": node.self_value,
            "children": sorted((_shape(c) for c in node.children), key=lambda d: d["name"])}


def test_the_three_forms_are_one_tree() -> None:
    folded = fd.from_folded(FOLDED, root="main-root")
    tree = fd.from_tree({"name": "main-root", "children": [{"name": "main", "self": 5, "children": [
        {"name": "load", "children": [{"name": "read", "value": 30}, {"name": "parse", "value": 20}]},
        {"name": "draw", "children": [{"name": "axes", "value": 15}, {"name": "series", "children": [
            {"name": "path", "value": 25}, {"name": "labels", "value": 10}]}]}]}]})
    rows = fd.from_rows([
        {"id": 0, "name": "main-root"}, {"id": 1, "parent": 0, "name": "main", "self": 5},
        {"id": 2, "parent": 1, "name": "load"}, {"id": 3, "parent": 2, "name": "read", "value": 30},
        {"id": 4, "parent": 2, "name": "parse", "value": 20}, {"id": 5, "parent": 1, "name": "draw"},
        {"id": 6, "parent": 5, "name": "axes", "value": 15}, {"id": 7, "parent": 5, "name": "series"},
        {"id": 8, "parent": 7, "name": "path", "value": 25}, {"id": 9, "parent": 7, "name": "labels", "value": 10}])
    assert _shape(folded) == _shape(tree) == _shape(rows)
    assert folded.value == 105 and folded.children[0].self_value == 5


@pytest.mark.parametrize("bad, why", [
    ({"rows": [{"id": 1, "parent": 2, "name": "a"}, {"id": 2, "parent": 1, "name": "b"}]}, "cycle"),
    ({"rows": [{"id": 1, "parent": 9, "name": "a"}]}, "not a row"),
    ({"folded": "no counts here"}, "could not be read"),
    ({"root": {"value": 3}}, "every frame has a name"),
])
def test_a_hierarchy_that_cannot_be_read_says_why(bad, why) -> None:
    assert any(why in p for p in us.validate(_doc(bad)))


def test_invert_puts_callers_above_the_work() -> None:
    inv = fd.invert(fd.from_folded("a;x 3\nb;x 2\nb 1\n"))
    x = next(c for c in inv.children if c.name == "x")
    assert x.value == 5 and {c.name: c.value for c in x.children} == {"a": 3, "b": 2}


def test_search_counts_a_match_inside_a_match_once() -> None:
    root = fd.from_folded("f;g;f 10\nf 5\nh 5\n")
    hits, match = fd.search(root, "F")
    assert match.count == 2 and match.value == 15


def test_a_flame_draws_root_at_the_bottom_and_an_icicle_at_the_top() -> None:
    flame = render_spec(_doc({"folded": FOLDED})).svg
    icicle = render_spec(_doc({"folded": FOLDED}), state={"direction": "icicle"}).svg

    def root_y(svg: str) -> float:
        return float(re.search(r'aria-label="all: [^"]*"[^>]*>\s*<rect x="[^"]+" y="([\d.]+)"', svg).group(1))

    def leaf_y(svg: str) -> float:
        return float(re.search(r'aria-label="path: [^"]*"[^>]*>\s*<rect x="[^"]+" y="([\d.]+)"', svg).group(1))

    assert root_y(flame) > leaf_y(flame) and root_y(icicle) < leaf_y(icicle)


def test_hover_and_focus_say_name_value_and_both_shares() -> None:
    svg = render_spec(_doc({"folded": FOLDED, "unit": "samples"})).svg
    assert "series: 35 samples, 33.3% of total, 70.0% of its caller" in svg
    assert 'role="img"' in svg and 'tabindex="0"' in svg


def test_a_frame_is_the_same_colour_in_every_render() -> None:
    a = render_spec(_doc({"folded": FOLDED})).svg
    b = render_spec(_doc({"folded": "other;stuff 99\n" + FOLDED})).svg

    def fill_of(svg: str, name: str) -> str:
        return re.search(rf'aria-label="{name}: [^"]*"[^>]*>\s*<rect [^>]*fill="(#[0-9a-f]+)"', svg).group(1)

    assert fill_of(a, "parse") == fill_of(b, "parse")


def test_clicking_a_frame_zooms_and_the_breadcrumb_resets() -> None:
    whole = render_spec(_doc({"folded": FOLDED})).svg
    assert 'data-axk-control="zoom" data-axk-value="main;draw"' in whole
    zoomed = render_spec(_doc({"folded": FOLDED}), state={"zoom": "main;draw"})
    assert zoomed.state["zoom"] == "main;draw"
    assert 'data-axk-value="" tabindex="0" role="button" aria-label="Reset zoom"' in zoomed.svg
    # The zoomed frame is full width; its sibling is not drawn.
    assert re.search(r'aria-label="draw: [^"]*"[^>]*>\s*<rect x="12.00" y="[\d.]+" width="896.00"', zoomed.svg)
    assert 'aria-label="load:' not in zoomed.svg


def test_search_and_invert_are_controls() -> None:
    found = render_spec(_doc({"folded": FOLDED}), state={"find": "series"})
    assert "1 frame matches" in found.svg and "33.3% of the total" in found.svg
    inverted = render_spec(_doc({"folded": FOLDED}), state={"invert": True})
    assert "inverted" in inverted.svg and inverted.svg != render_spec(_doc({"folded": FOLDED})).svg


def _synthetic(n: int, seed: int = 3) -> str:
    """A profile-shaped tree of about *n* frames: skewed fan-out, a long tail of tiny ones."""
    rnd, lines, made = random.Random(seed), [], [0]
    queue = [(["main"], 10_000_000, 0)]
    while queue and made[0] < n:
        path, budget, depth = queue.pop(0)
        made[0] += 1
        k = rnd.choice([2, 3, 3, 4, 6, 8]) if depth < 25 else 0
        if not k or budget < 2:
            lines.append(";".join(path) + f" {max(1, budget)}")
            continue
        shares = [rnd.paretovariate(1.5) for _ in range(k)]
        tot = sum(shares)
        lines.append(";".join(path) + f" {max(1, budget // 20)}")
        for sh in shares:
            queue.append((path + [f"m{depth}.f{rnd.randint(0, 500)}"], int(budget * 0.95 * sh / tot), depth + 1))
    lines.extend(";".join(p) + f" {max(1, b)}" for p, b, _ in queue)
    return "\n".join(lines)


def _first_draw_seconds(doc: dict) -> float:
    fd._READ_CACHE.clear()
    start = time.process_time()
    render_spec(doc, state={"find": "f42"})
    return time.process_time() - start


def test_fifty_thousand_frames_render_quickly_and_fold_the_invisible() -> None:
    text = _synthetic(50_000)
    root = fd.from_folded(text)
    assert root.count() >= 50_000
    doc = _doc({"folded": text})
    fd._READ_CACHE.clear()
    rendered = render_spec(doc, state={"find": "f42"})
    # The size claim is structural: frames narrower than a pixel fold, so the
    # drawing stays bounded however large the profile.
    assert rendered.svg.count("<rect") < 20_000
    assert any("folded into" in n for n in rendered.notes)


def test_drawing_a_flame_graph_scales_with_its_frames_not_worse() -> None:
    """Speed, measured against this machine rather than a stopwatch.

    A fixed budget failed CI at 7.8 s for a draw that takes 0.5 s on a laptop:
    a shared runner under parallel tests is a different machine. What must hold
    everywhere is the shape: ten times the frames costs about ten times the
    work, never a quadratic blow-up. CPU time, not wall time, so other tests
    sharing the runner do not count against this one.
    """
    small = _doc({"folded": _synthetic(5_000)})
    large = _doc({"folded": _synthetic(50_000)})
    _first_draw_seconds(small)  # warm imports and caches outside the measurement
    t_small = min(_first_draw_seconds(small) for _ in range(3))
    t_large = min(_first_draw_seconds(large) for _ in range(2))
    assert t_large < 25 * t_small + 0.05, (
        f"50k frames took {t_large:.2f}s against {t_small:.3f}s for 5k: worse than linear"
    )


def test_an_imported_icicle_draws_exactly_as_its_native_twin() -> None:
    figure = {"data": [{"type": "icicle", "ids": ["r", "a", "b", "a1"], "labels": ["<b>root</b>", "a", "b", "a1"],
                        "parents": ["", "r", "r", "a"], "values": [10, 6, 4, 5], "branchvalues": "total",
                        "tiling": {"flip": "y"}, "marker": {"colors": ["red", "blue", "green", "gold"]}}],
              "layout": {"title": "Where the time went"}}
    native = {"schema_version": us.SCHEMA_VERSION, "kind": "flame", "title": "Where the time went",
              "encoding": {"direction": "flame"},
              "data": {"tree": {"rows": [{"id": "r", "name": "root", "value": 10},
                                         {"id": "a", "name": "a", "parent": "r", "value": 6},
                                         {"id": "b", "name": "b", "parent": "r", "value": 4},
                                         {"id": "a1", "name": "a1", "parent": "a", "value": 5}]}}}
    res = import_figure(figure, ref="test://icicle")
    assert res.native, res.refused
    assert "frame colours" in res.spec["source"]["replaced"]
    assert render_spec(res.spec).svg == render_spec(native).svg
    assert us.spec_id(res.spec) == us.spec_id(us.parse(native))


@pytest.mark.parametrize("trace", ["treemap", "sunburst"])
def test_a_treemap_or_sunburst_is_refused_with_the_way_forward(trace) -> None:
    res = import_figure({"data": [{"type": trace, "labels": ["a"], "parents": [""], "values": [1]}], "layout": {}})
    assert not res.native and any("re-export it as an icicle" in r for r in res.refused)
