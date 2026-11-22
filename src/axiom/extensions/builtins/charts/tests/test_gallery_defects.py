# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Four defects seen on the 21-kind gallery, held so they stay fixed.

1. Power read 8.00x10^-4 W beside a 1.1 limit while the area drew 800,000 W: the recipe
   now draws a channel in a unit of the site's choosing (``scale``), thresholds and all.
2. The replay opened on the last step of a day that ends shut down, so every "now" read zero:
   ``open_frame`` opens it on a named step.
3. The donut's total (1,881,550 readings) was drawn as 1.88x10^6 in a hole 40px wide.
4. A histogram over 2,198 readings within 5 degrees, plus one stray at 20, drew one bar.
"""

from __future__ import annotations

import re

from axiom.extensions.builtins.charts import kinds_group as kg
from axiom.extensions.builtins.charts.tests.test_mvp_03_kinds_group import CONFIG, FakeGold
from axiom.extensions.builtins.scidisplay import unified_spec as us
from axiom.extensions.builtins.scidisplay.chart_kinds import _compact, _with_unit, render_spec


def _spec(kind: str, **data):
    return {"schema_version": us.SCHEMA_VERSION, "id": f"t-{kind}", "kind": kind, "title": "T", "data": data}


def _bars(svg: str) -> int:
    return len(re.findall(r'<rect[^>]*fill-opacity="0.85"', svg))


def test_a_count_is_compact_so_it_fits_a_donut_hole():
    assert _compact(1_881_550) == "1.88M"
    assert _compact(12_400) == "12.4k"
    assert _compact(640) == "640"


def test_the_donut_centre_is_drawn_compact_and_inside_its_hole():
    svg = render_spec(_spec("donut", series=[{"name": "Readings", "unit": "", "x": ["good", "bad"],
                                              "y": [1_877_776, 3_774]}])).svg
    assert ">1.88M</text>" in svg
    assert "×10" not in re.sub(r"<desc[^>]*>.*?</desc>", "", svg, flags=re.S), "no count in scientific notation"
    r = float(re.search(r"A([\d.]+),[\d.]+ 0", svg).group(1))
    size = float(re.search(r'font-size="([\d.]+)"[^>]*font-weight="600">1\.88M', svg).group(1))
    assert size * 0.6 * len("1.88M") < 2 * r * 0.58, "the text must be narrower than the hole it sits in"


def test_a_stray_reading_does_not_set_the_histogram_range():
    near = [361 + (i % 50) / 10 for i in range(2000)]
    spec = _spec("histogram", series=[{"name": "T", "unit": "degC", "y": [20.0, *near]}])
    out = render_spec(spec)
    assert _bars(out.svg) >= 8, "the distribution must be drawn as a distribution, not one bar"
    assert any("outside the range shown" in n for n in out.notes)


def test_a_histogram_with_no_strays_says_nothing_extra():
    out = render_spec(_spec("histogram", series=[{"name": "T", "unit": "degC", "y": [float(i % 40) for i in range(400)]}]))
    assert not any("outside the range" in n for n in out.notes)
    assert _bars(out.svg) >= 5


def _site(tmp_path, monkeypatch, extra=""):
    kg.clear_cache()
    cfg_text = CONFIG.replace('frames = 6', f'frames = 6\n{extra}')
    monkeypatch.setattr(kg, "open_source", lambda spec, site_id, *, base=None: FakeGold())
    (tmp_path / "scene.json").write_text("{}")
    cfg = kg.KindsConfig.from_mapping(__import__("tomllib").loads(cfg_text), base=tmp_path)
    return cfg


def test_a_channel_is_drawn_in_the_unit_the_recipe_names(tmp_path, monkeypatch):
    cfg = _site(tmp_path, monkeypatch)
    raw = kg._kind_data(cfg, "sparkline", kg.date(2026, 9, 10), {})["series"][0]["y"]
    cfg.kinds["sparkline"]["channel"] = {"ref": "loop:LEVEL", "label": "Level", "unit": "cL", "scale": 0.01}
    scaled = kg._kind_data(cfg, "sparkline", kg.date(2026, 9, 10), {})["series"][0]
    assert scaled["unit"] == "cL"
    assert max(scaled["y"]) == max(raw) * 0.01
    # Another recipe reading the same channel unscaled is not served the scaled readings.
    again = kg._kind_data(cfg, "step", kg.date(2026, 9, 10), {})["series"][0]["y"]
    assert max(again) == max(raw)


def test_the_twin_estimate_band_is_scaled_with_its_estimate(tmp_path, monkeypatch):
    cfg = _site(tmp_path, monkeypatch)
    cfg.kinds["area"]["channels"][0]["scale"] = 0.01
    cfg.kinds["area"]["channels"][1]["scale"] = 0.01
    s = kg._kind_data(cfg, "area", kg.date(2026, 9, 10), {})["series"][1]
    spread = max(h - lo for h, lo in zip(s["high"], s["low"], strict=True) if h is not None)
    assert abs(spread - 2 * 2.0 * 0.01) < 1e-3


def test_the_replay_opens_on_the_step_the_site_names(tmp_path, monkeypatch):
    cfg = _site(tmp_path, monkeypatch, "open_frame = 3")
    assert cfg.open_frame == 3
    area = kg.build_group(cfg)["graphs"][1]["spec"]
    assert area["controls"][0]["value"] == 3
    assert kg.KindsConfig.from_mapping(cfg.as_mapping(), base=tmp_path).open_frame == 3


def test_the_replay_opens_on_the_last_step_unless_told_otherwise(tmp_path, monkeypatch):
    cfg = _site(tmp_path, monkeypatch)
    assert kg.build_group(cfg)["graphs"][1]["spec"]["controls"][0]["value"] == 5


def test_the_group_carries_no_gallery_special_case(tmp_path, monkeypatch):
    # Found under Prebuilt like any group: nothing in the document marks it as a feature.
    assert "featured" not in kg.build_group(_site(tmp_path, monkeypatch))


def test_a_power_in_megawatts_reads_in_megawatts():
    assert _with_unit(0.8, "MW") == "0.8 MW"
    assert _with_unit(1.1, "MW") == "1.1 MW"


# --- Held from the demo gallery's beauty pass (2026-11-13) -------------------------------------


def test_a_state_chart_names_its_states_rather_than_numbering_them():
    spec = _spec("state", series=[{"name": "State", "unit": "", "levels": ["Off", "Changing", "At level"],
                                   "x": ["2026-01-14T00:00:00+00:00", "2026-01-14T08:00:00+00:00",
                                         "2026-01-14T09:00:00+00:00", "2026-01-14T18:00:00+00:00"],
                                   "y": [0, 1, 2, 0]}])
    svg = render_spec(spec, width=440).svg
    assert ">At level</text>" in svg and ">Off</text>" in svg
    assert "(no unit)" not in svg, "a state is a name, not a number with no unit"


def test_a_model_wears_its_measured_counterparts_hue_for_a_share_too():
    series = [{"name": "Level", "unit": "percent", "x": [0, 1, 2], "y": [1, 2, 3]},
              {"name": "Level model", "unit": "percent", "role": "simulated", "counterpart_of": "Level",
               "x": [0, 1, 2], "y": [1, 2, 3]}]
    svg = render_spec(_spec("area", series=series), width=440).svg
    strokes = re.findall(r'fill="none" stroke="(#[0-9a-fA-F]{6}|var\([^)]*\)|light-dark\([^)]*\))"[^>]*stroke-width="1.6"', svg)
    assert len(strokes) == 2 and strokes[0] == strokes[1], strokes


def test_passing_a_goal_is_not_an_alarm_but_passing_a_limit_is():
    from axiom.extensions.builtins.scidisplay import chart_marks

    alarm = chart_marks.tone("alarm")
    goal = render_spec(_spec("bullet", value={"value": 50, "unit": "h", "min": 0, "max": 60, "target": 40, "label": "H"})).svg
    limit = render_spec(_spec("gauge", value={"value": 110, "unit": "percent", "min": 0, "max": 120, "target": 105})).svg
    assert alarm not in goal and alarm in limit


def test_a_zero_bar_is_a_value_not_none():
    svg = render_spec(_spec("bar", series=[{"name": "H", "unit": "h", "x": ["a", "b"], "y": [0, 3]}])).svg
    assert ">none</text>" not in svg


# --- The colour system and the second beauty pass (2026-11-13, review) --------------------------


def test_every_single_series_kind_wears_the_one_default_series_colour():
    from axiom.extensions.builtins.scidisplay import palettes

    want = palettes.series_colour(palettes.DEFAULT_SERIES)
    t = [f"2026-01-14T{h:02d}:00:00+00:00" for h in range(6)]
    one = {"name": "A", "unit": "°C", "x": t, "y": [1, 2, 3, 2, 4, 3]}
    cases = {
        "line": {"series": [one]}, "area": {"series": [one]}, "step": {"series": [one]},
        "sparkline": {"series": [one]},
        "bar": {"series": [{"name": "H", "unit": "h", "x": ["a", "b"], "y": [1, 3]}]},
        "histogram": {"series": [{"name": "T", "unit": "°C", "y": [float(i % 9) for i in range(90)]}]},
        "box": {"series": [{"name": f"A {d}", "unit": "°C", "group": "A", "y": [1, 2, 3, 4, d]} for d in range(3)]},
        "gauge": {"value": {"value": 40, "unit": "%", "min": 0, "max": 120, "target": 105}},
        "bullet": {"value": {"value": 50, "unit": "h", "min": 0, "max": 60, "target": 40, "label": "H"}},
    }
    for kind, data in cases.items():
        svg = render_spec(_spec(kind, **data), width=440).svg
        assert want in svg, f"{kind} is not drawn in the default series colour"


def test_the_categorical_order_is_the_design_systems_and_has_no_brand_orange():
    import colorsys

    from axiom.extensions.builtins.scidisplay import palettes

    assert palettes.AXIOM[0] == palettes.DEFAULT_SERIES
    for c in palettes.AXIOM:
        r, g, b = (int(c[i:i + 2], 16) / 255 for i in (1, 3, 5))
        h, _, s = colorsys.rgb_to_hls(r, g, b)
        assert not (15 <= h * 360 <= 40 and s > 0.5), f"{c} reads as the brand's identity orange"
    assert set(palettes.AXIOM_DARK) == set(palettes.AXIOM), "every slot has its selected dark step"


def test_a_status_breakdown_wears_status_tones_with_a_glyph_and_a_word():
    from axiom.extensions.builtins.scidisplay import chart_marks

    spec = _spec("donut", series=[{"name": "Readings", "unit": "readings", "x": ["good", "stale", "bad"],
                                   "y": [5700, 40, 20]}])
    spec["encoding"] = {"colour_by": "status"}
    svg = render_spec(spec).svg
    assert chart_marks.tone("alarm") in svg and chart_marks.tone("ok") in svg
    for word in ("✓ good", "— stale", "✕ bad"):
        assert word in svg, word


def test_a_heatmap_tells_no_data_apart_from_zero():
    svg = render_spec(_spec("heatmap", field={"x": [0, 1, 2], "y": ["a", "b"], "z": [[0, 0, None], [1, 2, 3]],
                                              "unit": "%"}), width=600).svg
    assert 'class="axk-no-data"' in svg and "url(#" in svg, "an empty cell is hatched, not left as paper"
    assert "hatched: no data" in svg


def test_a_kpi_change_is_given_to_the_values_resolution():
    spec = _spec("kpi", series=[{"name": "Level", "unit": "%", "y": [95.7415, 95.8, 95.62, 95.8]}])
    svg = render_spec(spec).svg
    # Shown as 95.8, so its change is given to a tenth: +0.1, not +0.0585.
    assert "+0.1" in svg and "0.0585" not in svg and "0.06" not in svg


def test_several_series_take_the_categorical_order_on_every_kind():
    from axiom.extensions.builtins.scidisplay import palettes

    first, second = (palettes.series_colour(c) for c in palettes.AXIOM[:2])
    t = [f"2026-01-14T{h:02d}:00:00+00:00" for h in range(6)]
    two = [{"name": "A", "unit": "°C", "x": t, "y": [1, 2, 3, 2, 4, 3]},
           {"name": "B", "unit": "°C", "x": t, "y": [2, 3, 1, 2, 2, 3]}]
    for kind in ("line", "step", "area"):
        svg = render_spec(_spec(kind, series=two), width=440).svg
        assert first in svg and second in svg, f"{kind}: two series are slots 1 and 2, in order"


def test_a_3d_scene_names_its_axes(tmp_path):
    spec = _spec("surface", scene={"grid": {"x": [0, 1, 2], "y": [0, 1], "z": [[0, 1, 2], [1, 2, 3]]}})
    spec["axes"] = {"x": {"title": "hour of the day, UTC"}, "y": {"title": "day"}, "z": {"title": "Level, %"}}
    assert us.validate(spec) == []
    svg = render_spec(spec, width=900).svg
    assert 'class="axk-scene-axes"' in svg
    for words in ("x: hour of the day, UTC", "y: day", "z: Level, %"):
        assert words in svg, words


def test_every_demo_3d_scene_names_its_axes(tmp_path):
    from axiom.extensions.builtins.charts import demo

    for g in demo.groups(tmp_path)[0]["graphs"]:
        if g["kind"] in ("mesh3d", "surface", "scatter3d", "volume"):
            assert set((g["spec"].get("axes") or {})) == {"x", "y", "z"}, g["kind"]


def test_an_axis_unit_and_the_footnote_under_it_never_share_a_line():
    near = [80 + (i % 50) / 10 for i in range(500)]
    svg = render_spec(_spec("histogram", series=[{"name": "T", "unit": "°C", "y": [20.0, *near]}]), width=400).svg
    unit_y = float(re.search(r'<text[^>]*y="([\d.]+)"[^>]*>°C</text>', svg).group(1))
    note_y = float(re.search(r'<text[^>]*y="([\d.]+)"[^>]*font-style="italic">[^<]*outside the range', svg).group(1))
    assert note_y - unit_y >= 12, (unit_y, note_y)
