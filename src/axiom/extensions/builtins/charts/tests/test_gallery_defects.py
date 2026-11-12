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
