# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The chart-kinds group: one chart of every kind from a site's own gold (MVP-03-A2..A5).

A site hands over channels and thresholds; the group is one saved group on the
Charts page with one graph per registered ChartSpec kind, each drawn from gold at
draw time by the platform renderer. SQLite and temp-dir fixtures only: nothing
here reads or writes a developer's database or state directory.
"""

from __future__ import annotations

import contextlib
import json
import logging
import math
import textwrap
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from axiom.extensions.builtins.charts import kinds_group as kg
from axiom.extensions.builtins.charts import kinds_sources as ks
from axiom.extensions.builtins.scidisplay import unified_spec as us
from axiom.extensions.builtins.scidisplay.chart_kinds import render_spec

DAY = date(2026, 9, 10)
WHO = "@pat:site-a"


class FakeGold:
    """A day and a week of three channels on one site, with quality verdicts.

    ``loop:LEVEL`` ramps 0 -> 100 over the morning and holds; ``loop:T1`` follows it;
    ``model:LEVEL_EST`` is a modelled estimate of LEVEL. T1's readings 10:00-10:05 are
    verdict ``bad`` (withheld), so a held span exists to draw.
    """

    def __init__(self, quality: bool = True) -> None:
        self.quality = quality

    def _value(self, channel: str, t: float) -> float:
        h = (t % 86400) / 3600.0
        level = 0.0 if h < 6 else min(100.0, (h - 6) * 50) if h < 20 else 0.0
        if channel == "LEVEL":
            return level
        if channel == "LEVEL_EST":
            return level * 0.98 + 1
        if channel == "SIGMA":
            return 2.0
        if channel == "T1":
            return 20 + level * 3
        return 20 + level * 2.5 + math.sin(t / 900)

    def read(self, feed, channel, start, end, step):  # noqa: ANN001
        raw = []
        t = start.timestamp()
        while t < end.timestamp():
            q = "good"
            if channel == "T1" and 10 * 3600 <= t % 86400 < 10 * 3600 + 300:
                q = "bad"
            raw.append((t, self._value(channel, t), q if self.quality else None))
            t += 30
        return ks.bucket(raw, start.timestamp(), end.timestamp(), step)

    def unit(self, feed, channel):  # noqa: ANN001
        return {"LEVEL": "percent", "T1": "degC", "T2": "degC"}.get(channel, "")

    def lineage(self):
        return "gold.signals (fake)"

    def carries_quality(self):
        return self.quality


CONFIG = textwrap.dedent(
    """
    [kinds_group]
    site = "site-a"
    label = "Site A"
    replay = "2026-09-10"
    delay_hours = 24
    frames = 6
    week = ["2026-09-08", "2026-09-10"]
    span = ["2026-09-08", "2026-09-10"]

    [sources.day]
    fake = "day"
    [sources.history]
    fake = "history"

    [kinds.line]
    channels = [
      { ref = "loop:T1", label = "T1", unit = "degC" },
      { ref = "loop:T2", label = "T2", unit = "degC" },
    ]
    [kinds.area]
    channels = [
      { ref = "loop:LEVEL", label = "Level", unit = "percent" },
      { ref = "model:LEVEL_EST", label = "Level estimate", unit = "percent", role = "simulated", counterpart_of = "Level", sigma = "model:SIGMA" },
    ]
    [kinds.step]
    channels = ["loop:LEVEL"]
    [kinds.sparkline]
    channel = "loop:LEVEL"
    [kinds.kpi]
    channels = ["loop:LEVEL", "loop:T1"]
    [kinds.gauge]
    channel = { ref = "loop:LEVEL", label = "Level", unit = "percent" }
    max = 120
    limit = { value = 110 }
    [kinds.bullet]
    channel = "loop:LEVEL"
    at_least = 90
    target = { value = 20, sample = true }
    [kinds.progress]
    rows = [ { label = "Hours at level", channel = "loop:LEVEL", at_least = 90, plan = 40, plan_sample = true } ]
    [kinds.state]
    channel = "loop:LEVEL"
    states = [ { name = "Off", below = 1 }, { name = "Changing", below = 90 }, { name = "At level" } ]
    [kinds.pie]
    [kinds.donut]
    channels = ["loop:T1", "loop:T2"]
    [kinds.scatter]
    x = "loop:LEVEL"
    y = { ref = "loop:T1", label = "T1", unit = "degC", valid_above = 0 }
    [kinds.histogram]
    channel = "loop:T1"
    step_s = 60
    where = { ref = "loop:LEVEL", at_least = 90 }
    [kinds.box]
    channels = ["loop:T1", "loop:T2"]
    [kinds.heatmap]
    channel = "loop:LEVEL"
    [kinds.bar]
    channel = "loop:LEVEL"
    at_least = 90
    [kinds.flame]
    days = 7
    [kinds.mesh3d]
    scene = "scene.json"
    [kinds.surface]
    channel = "loop:LEVEL"
    [kinds.scatter3d]
    channels = ["loop:LEVEL", "loop:T1", "loop:T2"]
    [kinds.volume]
    sample = true
    """
)

SCENE = {"mesh": {"points": [[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1]], "cells": [[0, 1, 2], [0, 1, 3], [0, 2, 3]]},
         "elements": ["a", "b", "b"], "scalars": {"holds": {"unit": "", "association": "cell", "values": [1, 2, 2]}}}


@pytest.fixture
def site(tmp_path, monkeypatch):
    """A registered config over fake gold, platform telemetry and a scene, in a temp state dir."""
    kg.clear_cache()
    (tmp_path / "scene.json").write_text(json.dumps(SCENE))
    (tmp_path / "kinds.toml").write_text(CONFIG)
    telemetry = tmp_path / "state" / "telemetry"
    telemetry.mkdir(parents=True)
    now = datetime.now(UTC).timestamp()
    rows = [{"tool_name": n, "surface": s, "latency_ms": ms, "ts": now - 60, "ok": True}
            for n, s, ms in (("charts.render", "web", 40), ("charts.render", "web", 60),
                             ("data.series", "mcp", 120), ("charts.suggest", "cli", 5))]
    (telemetry / "capabilities.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    def _open(spec, site_id, *, base=None):  # noqa: ANN001
        assert "fake" in spec, "the test names only fake sources"
        return FakeGold()

    monkeypatch.setattr(kg, "open_source", _open)
    cfg = kg.KindsConfig.load(tmp_path / "kinds.toml")
    kg.register(cfg, tmp_path / "state")
    yield cfg, tmp_path / "state"
    kg.clear_cache()


@pytest.fixture
def views(monkeypatch):
    from axiom.extensions.builtins.webapp.catalog import store
    from axiom.extensions.builtins.webapp.views import models as view_models

    engine = create_engine("sqlite://", poolclass=StaticPool)
    view_models.Base.metadata.create_all(engine)
    s = Session(engine)

    @contextlib.contextmanager
    def _provider():
        yield s

    monkeypatch.setattr(store, "_provider", _provider)
    yield s
    s.close()


def _ctx(state_dir: Path, principal: str = ""):
    from axiom.infra.principal import PrincipalContext
    from axiom.infra.skills import SkillContext, SkillRegistry

    extra = {"principal": PrincipalContext(handle=principal)} if principal else {}
    return SkillContext(registry=SkillRegistry(), state_dir=state_dir, logger=logging.getLogger("t"), **extra)


def _draw(spec, state_dir, state=None):  # noqa: ANN001
    return render_spec(spec, state=state, width=600, resolve=kg.resolver(state_dir))


# --- MVP-03-A5: a reference group of every kind --------------------------------------------


def test_mvp_03_a5_the_group_has_one_valid_chart_per_registered_kind(site) -> None:
    cfg, _ = site
    group = kg.build_group(cfg)
    kinds = [g["kind"] for g in group["graphs"]]
    assert sorted(kinds) == sorted(us.registered_kinds())
    for g in group["graphs"]:
        assert us.validate(g["spec"]) == [], g["kind"]
        assert g["spec"]["data"]["verb"]["name"] == kg.VERB
    # The group references its data, so it fits a saved view however much it draws.
    assert len(json.dumps(group)) < 24 * 1024


def test_mvp_03_a5_every_chart_draws_from_gold_with_no_refusal(site) -> None:
    cfg, state = site
    for g in kg.build_group(cfg)["graphs"]:
        drawn = _draw(g["spec"], state)
        assert "<svg" in drawn.svg
        assert not any("no figure source" in n or "cannot" in n for n in drawn.notes), (g["kind"], drawn.notes)
        assert g["spec"]["catalog"]["origin"], g["kind"]


def test_a_kind_with_no_real_source_says_sample_everywhere(site) -> None:
    cfg, _ = site
    vol = next(g["spec"] for g in kg.build_group(cfg)["graphs"] if g["kind"] == "volume")
    assert vol["title"].startswith("SAMPLE")
    assert vol["catalog"]["notes"] == "SAMPLE" and vol["catalog"]["origin"].startswith("SAMPLE")
    measured = next(g["spec"] for g in kg.build_group(cfg)["graphs"] if g["kind"] == "line")
    assert "SAMPLE" not in measured["title"] and measured["catalog"]["provenance"] == "measured"


# --- the replay ------------------------------------------------------------------------------


def test_a_replayed_chart_steps_through_the_day_and_loops(site) -> None:
    cfg, state = site
    spec = next(g["spec"] for g in kg.build_group(cfg)["graphs"] if g["kind"] == "line")
    play = spec["controls"][0]
    assert play["type"] == "playback" and play["loop"] is True and play["max"] == cfg.frames - 1
    assert "replay of 2026-09-10" in spec["subtitle"]
    early = kg.resolve_data({**spec["data"]["verb"]["params"], "at": 1}, state_dir=state)
    whole = kg.resolve_data(spec["data"]["verb"]["params"], state_dir=state)
    last = datetime.fromisoformat(early["series"][0]["x"][-1])
    assert last < datetime(2026, 9, 10, 8, tzinfo=UTC)
    assert len(whole["series"][0]["x"]) > len(early["series"][0]["x"])
    assert any("at 08:00 UTC" in n for n in _draw(spec, state, {"replay": 1}).notes)
    assert any("the whole day" in n for n in _draw(spec, state).notes)


def test_a_day_younger_than_the_delay_is_not_replayed(site) -> None:
    cfg, _ = site
    with pytest.raises(kg.KindsError, match="not 24 h old"):
        kg.build_group(cfg, replay=datetime.now(UTC).date())


# --- MVP-03-A3 / A4: the twin is dashed, held data is hatched --------------------------------


def test_mvp_03_a3_the_estimate_is_its_measurement_s_hue_dashed_and_labelled(site) -> None:
    cfg, state = site
    spec = next(g["spec"] for g in kg.build_group(cfg)["graphs"] if g["kind"] == "area")
    data = kg.resolve_data(spec["data"]["verb"]["params"], state_dir=state)
    est = next(s for s in data["series"] if s["name"] == "Level estimate")
    assert est["role"] == "simulated" and est["counterpart_of"] == "Level"
    assert len(est["low"]) == len(est["y"]) and est["high"][-1] - est["low"][-1] == pytest.approx(4.0)
    assert spec["catalog"]["provenance"] == "hybrid"
    assert "(simulated)" in _draw(spec, state).svg


def test_mvp_03_a4_withheld_readings_draw_as_the_neutral_hatch(site) -> None:
    cfg, state = site
    spec = next(g["spec"] for g in kg.build_group(cfg)["graphs"] if g["kind"] == "line")
    data = kg.resolve_data(spec["data"]["verb"]["params"], state_dir=state)
    hatches = [m for m in data["marks"] if m["type"] == "hatch"]
    assert hatches and all(m["label"] == "—" for m in hatches)
    assert hatches[0]["from"].startswith("2026-09-10T10:00")
    assert 'class="axk-hatch"' in _draw(spec, state).svg


def test_the_quality_donut_counts_verdicts_and_a_snapshot_says_it_has_none(site, monkeypatch) -> None:
    cfg, state = site
    spec = next(g["spec"] for g in kg.build_group(cfg)["graphs"] if g["kind"] == "donut")
    data = kg.resolve_data(spec["data"]["verb"]["params"], state_dir=state)
    counts = dict(zip(data["series"][0]["x"], data["series"][0]["y"], strict=True))
    assert counts["bad"] == 10 and counts["good"] == 2 * 2880 - 10
    kg.clear_cache()
    monkeypatch.setattr(kg, "open_source", lambda spec, site_id, base=None: FakeGold(quality=False))
    assert "carries no quality" in " ".join(_draw(spec, state).notes)


# --- MVP-03-A2: saved for the caller, one id on every surface --------------------------------


def test_mvp_03_a2_the_group_is_saved_for_the_caller_and_drawn_alike_by_id(site, views) -> None:
    from axiom.extensions.builtins.charts import saved as catalog
    from axiom.extensions.builtins.charts.skills import kinds_group, render, saved

    _, state = site
    # By default nothing is saved for anybody: the registered group is shared (see test_prebuilt_groups).
    shared = kinds_group({"site": "site-a"}, _ctx(state, WHO))
    assert shared.ok and shared.value["saved"] is False and shared.value["shared"] is True
    assert catalog.listing(WHO) == []
    got = kinds_group({"site": "site-a", "copy": True}, _ctx(state, WHO))
    assert got.ok, got.errors
    assert got.value["saved"] and got.value["principal"] == WHO
    listed = {c["kind"]: c for c in catalog.listing(WHO)}
    assert set(listed) == set(us.registered_kinds())
    one = listed["gauge"]
    by_id = saved({"id": one["id"], "width": 600}, _ctx(state, WHO))
    direct = render({"spec": catalog.spec_for(WHO, one["id"]), "width": 600}, _ctx(state))
    assert by_id.ok and direct.ok and by_id.value["svg"] == direct.value["svg"]
    # Refreshing replaces the group by name rather than adding a second.
    assert kinds_group({"site": "site-a", "copy": True}, _ctx(state, WHO)).ok
    from axiom.extensions.builtins.webapp.views import read_view

    names = [g["name"] for g in read_view(WHO, "groups")["groups"]]
    assert names.count("Chart kinds · Site A") == 1


def test_mvp_03_a2_the_group_needs_somebody_to_belong_to(site) -> None:
    from axiom.extensions.builtins.charts.skills import kinds_group
    from axiom.infra.principal import PrincipalContext

    _, state = site
    import dataclasses

    nobody = dataclasses.replace(_ctx(state), principal=PrincipalContext(handle=""))
    got = kinds_group({"site": "site-a", "copy": True}, nobody)
    assert not got.ok and "belongs to somebody" in got.errors[0]
    dry = kinds_group({"site": "site-a", "dry_run": True}, _ctx(state))
    assert dry.ok and dry.value["saved"] is False and dry.value["sample"] == ["volume"]


def test_the_cli_registers_a_config_and_says_what_it_built(site, tmp_path, monkeypatch, capsys) -> None:
    from axiom.extensions.builtins.charts import cli

    _, state = site
    monkeypatch.setattr("axiom.infra.paths.get_user_state_dir", lambda: state)
    code = cli.main(["kinds-group", "--site", "site-a", "--config", str(tmp_path / "kinds.toml"),
                     "--dry-run", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["site"] == "site-a" and len(out["kinds"]) == len(us.registered_kinds())
    assert out["ids"]["line"] == "kinds-site-a-line"


def test_merge_keeps_the_person_s_other_groups() -> None:
    mine = {"groups": [{"id": "k1", "name": "Mine", "graphs": []}]}
    group = {"id": "x", "name": "Chart kinds · A", "graphs": [{"id": "g1"}]}
    once = kg.merge_into(mine, group)
    twice = kg.merge_into(once, {**group, "graphs": []})
    assert [g["name"] for g in twice["groups"]] == ["Mine", "Chart kinds · A"]
    assert twice["groups"][1]["id"] == "k2" and twice["groups"][1]["graphs"] == [] and twice["display"] == "k2"


# --- the platform's own timing ---------------------------------------------------------------


def test_the_flame_is_the_platform_s_own_capability_latency(site) -> None:
    _, state = site
    tree = kg.platform_timing(state)
    assert tree["value"] == pytest.approx(225.0)
    web = next(c for c in tree["children"] if c["name"] == "web")
    assert web["children"][0]["children"][0] == {"name": "charts.render", "value": 100.0, "category": "web"}
    with pytest.raises(kg.KindsError, match="no capability telemetry"):
        kg.platform_timing(state, now=datetime.now(UTC).timestamp() + 30 * 86400)


# --- the sources -----------------------------------------------------------------------------


def test_bucketing_never_averages_a_withheld_reading_in() -> None:
    rows = [(0.0, 10.0, "good"), (10.0, 999.0, "bad"), (20.0, 20.0, "good"), (60.0, 5.0, "bad")]
    assert ks.bucket(rows, 0, 120, 60) == [(0.0, 15.0, "suspect"), (60.0, None, "bad")]


def test_a_snapshot_is_read_and_must_be_of_the_site(tmp_path) -> None:
    t0 = datetime(2026, 9, 10, tzinfo=UTC)
    doc = {"site": "site-a", "channels": [{"feed": "loop", "channel": "T1", "unit": "degC"}],
           "series": {"loop|T1": [[int((t0 + timedelta(minutes=5 * i)).timestamp() * 1000), float(i)] for i in range(12)]}}
    (tmp_path / "s.json").write_text(json.dumps(doc))
    src = ks.open_source({"snapshot": "s.json"}, "site-a", base=tmp_path)
    got = src.read("loop", "T1", t0, t0 + timedelta(hours=1), 900)
    assert [v for _, v, _ in got] == [1.0, 4.0, 7.0, 10.0] and src.unit("loop", "T1") == "degC"
    assert not src.carries_quality()
    with pytest.raises(ks.SourceError, match="not 'site-b'"):
        ks.SnapshotSource(tmp_path / "s.json", "site-b").read("loop", "T1", t0, t0, 60)


def test_a_gold_export_is_read_at_full_rate_with_its_verdicts(tmp_path) -> None:
    pa = pytest.importorskip("pyarrow")
    pq = pytest.importorskip("pyarrow.parquet")
    win = tmp_path / "site-a_2026-09-10"
    win.mkdir()
    t0 = datetime(2026, 9, 10, tzinfo=UTC)
    ts = [t0 + timedelta(seconds=s) for s in range(0, 120, 2)]
    pq.write_table(pa.table({
        "channel": ["T1"] * len(ts), "ts": pa.array(ts, type=pa.timestamp("ns", tz="UTC")),
        "value": [float(i) for i in range(len(ts))], "unit": ["degC"] * len(ts),
        "quality": ["bad" if i == 3 else "good" for i in range(len(ts))],
    }), win / "measured.parquet")
    (win / "window.json").write_text(json.dumps({
        "source": "site-a", "start": t0.isoformat(), "end": (t0 + timedelta(days=1)).isoformat(),
        "files": {"measured": {"feed": "loop"}}, "channels": {"measured": {"T1": "degC"}},
        "exported_from": "gold.signals"}))
    src = ks.open_source({"export": str(tmp_path)}, "site-a")
    got = src.read("loop", "T1", t0, t0 + timedelta(minutes=2), 60)
    assert got[0][2] == "suspect" and got[1][2] == "good" and len(got) == 2
    assert src.unit("loop", "T1") == "degC" and "2026-09-10" in src.lineage()


# --- the spec additions ----------------------------------------------------------------------


def test_a_hatch_mark_and_a_looping_playback_are_part_of_the_spec() -> None:
    base = {"schema_version": "2.0", "kind": "line", "title": "L",
            "data": {"series": [{"name": "a", "x": [0, 1], "y": [1, 2]}]}}
    assert us.validate({**base, "marks": [{"type": "hatch", "from": 0, "to": 1}]}) == []
    assert us.validate({**base, "marks": [{"type": "hatch", "axis": "y", "from": 0, "to": 1}]})
    play = {"id": "p", "type": "playback", "label": "P", "bind": {"to": "param", "key": "at"}, "min": 0, "max": 3}
    assert us.validate({**base, "controls": [{**play, "loop": True}]}) == []
    assert us.validate({**base, "controls": [{**play, "type": "slider", "loop": True}]})


def test_a_verb_may_answer_any_kind_and_a_refused_read_is_said_quietly() -> None:
    doc = {"schema_version": "2.0", "kind": "gauge", "title": "G", "data": {"verb": {"name": "x.y"}}}
    assert us.validate(doc) == []

    def refuse(*_a):  # noqa: ANN002
        raise ks.SourceError("the store is away")

    drawn = render_spec(doc, resolve=refuse)
    assert drawn.notes == ["the store is away"] and "<svg" in drawn.svg
