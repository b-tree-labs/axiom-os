# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The demo gallery: every chart kind on a bare node, drawn from sample data shipped with the platform.

A node with no site config and no gold still has a Charts page worth opening: a prebuilt group of
kind ``demo`` with one chart of every registered kind, drawn by the real renderer through the same
chart-data verb a site's group uses, and every chart says SAMPLE. Whether it is shown in the Charts
navigation is the ``charts_demo_gallery`` flag; hidden, it is still listed (search and a direct link
open it). Temp state dirs and SQLite only.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

import pytest

from axiom.extensions.builtins.charts import demo, prebuilt
from axiom.extensions.builtins.charts import kinds_group as kg
from axiom.extensions.builtins.charts import kinds_sources as ks
from axiom.extensions.builtins.charts.tests.test_mvp_03_kinds_group import (  # noqa: F401
    _ctx,
    site,
    views,
)
from axiom.extensions.builtins.scidisplay import unified_spec as us
from axiom.extensions.builtins.scidisplay.chart_kinds import render_spec


@pytest.fixture
def bare(tmp_path, monkeypatch):
    """A bare node: an empty state dir, no registered config, the flag at its default."""
    monkeypatch.delenv("AXIOM_FLAG_CHARTS_DEMO_GALLERY", raising=False)
    kg.clear_cache()
    yield tmp_path / "state"
    kg.clear_cache()


def _demo_group(state):
    return next(g for g in prebuilt.listing(state) if g["kind"] == "demo")


def test_sample_data_is_deterministic_domain_free_and_carries_verdicts() -> None:
    a, b = ks.SampleSource("7"), ks.SampleSource("7")
    day = datetime(2026, 1, 14, tzinfo=UTC)
    end = datetime(2026, 1, 15, tzinfo=UTC)
    for ch in ks.SAMPLE_CHANNELS:
        assert a.read("sample", ch, day, end, 300) == b.read("sample", ch, day, end, 300), ch
    level = [v for _, v, _ in a.read("sample", "level", day, end, 300) if v is not None]
    assert max(level) > 50 and min(level) == 0, "a day with a shape: off, ramp, hold"
    verdicts = {q for _, _, q in a.read("sample", "temp_a", day, end, 60)}
    assert "bad" in verdicts and a.carries_quality()
    assert a.lineage().startswith("SAMPLE")


def test_a_bare_node_lists_the_demo_group_shown_with_every_kind(bare) -> None:
    g = _demo_group(bare)
    assert g["visibility"] == "shown" and g["prebuilt"] is True
    assert sorted(x["kind"] for x in g["graphs"]) == sorted(us.registered_kinds())
    for x in g["graphs"]:
        assert us.validate(x["spec"]) == [], x["kind"]


def test_every_demo_chart_draws_through_the_chart_data_verb_and_says_sample(bare) -> None:
    for x in _demo_group(bare)["graphs"]:
        spec = x["spec"]
        assert spec["data"]["verb"]["name"] == kg.VERB, "the same chart-data path a site's group uses"
        assert spec["catalog"]["notes"] == kg.SAMPLE and spec["catalog"]["origin"].startswith(kg.SAMPLE)
        drawn = render_spec(spec, width=600, resolve=kg.resolver(bare))
        assert not any("no figure source" in n or "cannot" in n or "no chart-kinds" in n for n in drawn.notes), (
            x["kind"], drawn.notes)
        assert re.search(r'class="axk-sample-tag"[^>]*>.*?SAMPLE', drawn.svg, re.S), f"{x['kind']}: no SAMPLE tag"


def test_the_flag_hides_the_demo_group_but_keeps_it_listed(bare, monkeypatch) -> None:
    monkeypatch.setenv("AXIOM_FLAG_CHARTS_DEMO_GALLERY", "off")
    g = _demo_group(bare)
    assert g["visibility"] == "hidden", "hidden from the navigation, still listed for search and links"


def test_a_site_group_is_kind_site_and_its_config_sets_its_visibility(site) -> None:  # noqa: F811
    cfg, state = site
    got = next(g for g in prebuilt.listing(state) if g["id"] == "kinds-site-a")
    assert (got["kind"], got["visibility"]) == ("site", "shown")
    kg.register(kg.KindsConfig.from_mapping({**cfg.as_mapping(), "kinds_group": {
        **cfg.as_mapping()["kinds_group"], "visibility": "hidden"}}, base=cfg.base), state)
    got = next(g for g in prebuilt.listing(state) if g["id"] == "kinds-site-a")
    assert got["visibility"] == "hidden"


def test_group_visibility_writes_where_each_kind_of_group_lives(site, views) -> None:  # noqa: F811
    from axiom.extensions.builtins.charts.skills import group_visibility
    from axiom.extensions.builtins.webapp.views import read_view, write_view

    _, state = site
    who = "probe@local"
    write_view(who, "groups", {"groups": [{"id": "k1", "name": "Mine A", "graphs": []}]})
    ctx = _ctx(state, who)
    # Personal: the account's own saved view.
    assert group_visibility({"id": "Mine A", "visibility": "hidden"}, ctx).ok
    assert read_view(who, "groups")["groups"][0]["visibility"] == "hidden"
    # Site: the registered config.
    assert group_visibility({"id": "kinds-site-a", "visibility": "hidden"}, ctx).ok
    assert kg.registered("site-a", state).visibility == "hidden"
    # Demo: its one switch is the flag, and the refusal names it.
    refused = group_visibility({"id": demo.GROUP_ID, "visibility": "hidden"}, ctx)
    assert not refused.ok and demo.FLAG in " ".join(refused.errors)
    assert not group_visibility({"id": "Mine A", "visibility": "sideways"}, ctx).ok


def test_search_finds_a_hidden_prebuilt_group_by_name_and_links_to_it(bare, monkeypatch, views) -> None:  # noqa: F811
    from axiom.extensions.builtins.search import engine

    monkeypatch.setenv("AXIOM_FLAG_CHARTS_DEMO_GALLERY", "off")
    monkeypatch.setattr("axiom.infra.paths.get_user_state_dir", lambda: bare)
    out = engine.search("chart kinds", engine.Scope(principal="probe@local"), kinds=["chart"])
    row = next(r for r in out["results"] if r["id"] == demo.GROUP_ID)
    assert row["matched"] == "name" and row["url"] == f"#/chart?open={demo.GROUP_ID}"


def test_wide_kinds_span_the_row_and_no_title_names_its_kind(bare) -> None:
    g = _demo_group(bare)
    wide = {x["kind"] for x in g["graphs"] if x.get("span") == "full"}
    assert wide == {"heatmap", "flame", "mesh3d", "surface", "scatter3d", "volume"}
    spans = [x.get("span") == "full" for x in g["graphs"]]
    assert spans == sorted(spans), "the row-wide cards follow the grid's full rows, leaving no half-empty row"
    for x in g["graphs"]:
        words = re.findall(r"[a-z0-9]+", x["spec"]["title"].lower())
        assert x["kind"].lower() not in words and not (x["kind"] == "mesh3d" and "mesh" in " ".join(words)), (
            f"{x['kind']}: the card names the kind; the title names the chart")


def test_the_demo_heatmap_has_an_empty_span_to_show(bare) -> None:
    heat = next(x["spec"] for x in _demo_group(bare)["graphs"] if x["kind"] == "heatmap")
    data = kg.resolver(bare)(heat, heat["data"], type("S", (), {"params": {}})())
    cells = [v for row in data["field"]["z"] for v in row]
    assert None in cells and 0 in [round(v) for v in cells if v is not None], "zero and no data, both present"


def test_the_replay_day_is_named_by_its_control_alone(bare) -> None:
    for x in _demo_group(bare)["graphs"]:
        spec = x["spec"]
        assert "replay of" not in str(spec.get("subtitle") or ""), x["kind"]
        if x["kind"] in kg.REPLAYED:
            play = spec["controls"][0]
            labels = [o["label"] for o in play["options"]]
            assert play["label"] == "Replay 2026-01-14" and labels[-1] == "24:00 UTC" and len(labels) == 48
            drawn = render_spec(spec, width=440, resolve=kg.resolver(bare))
            assert not any("replay of" in n for n in drawn.notes), "the moment is the control's readout"
