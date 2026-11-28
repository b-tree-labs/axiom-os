# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Role groups: a product's chart template, filled by a site's map from roles to its channels.

A product ships a template that names ROLES (what a channel is for), never a channel. A site
hands over which of its channels play each role, where its gold is, and its time zone. The
result is a prebuilt group on the Charts page with a day picker: every chart is drawn for the
chosen day, read from gold at draw time. Temp-dir fixtures only; nothing reads a developer's
state or database.
"""

from __future__ import annotations

import json
import textwrap
from datetime import UTC, date, datetime, timedelta

import pytest

from axiom.extensions.builtins.charts import kinds_sources as ks
from axiom.extensions.builtins.charts import prebuilt
from axiom.extensions.builtins.charts import role_groups as rg
from axiom.extensions.builtins.scidisplay import unified_spec as us
from axiom.extensions.builtins.scidisplay.chart_kinds import render_spec

DAY = date(2026, 9, 10)
QUIET = date(2026, 9, 9)
NOW = datetime(2026, 9, 20, tzinfo=UTC)


class FakeGold:
    """Two days of one site. On DAY the level ramps 0 -> 100 from 06:00 to 08:00 site time, holds
    to 12:00 and drops to 0; QUIET has readings and no level. Every other day has none.

    ``LEVEL`` is percent; ``POS_A`` / ``POS_B`` are positions that follow the level; ``T1``
    and ``T2`` temperatures; ``EST`` a modelled level. The site is UTC-6, so its day runs
    06:00 UTC to 06:00 UTC.
    """

    OFFSET = 6 * 3600

    def _value(self, channel: str, t: float) -> float | None:
        local = t - self.OFFSET
        day = datetime.fromtimestamp(local, UTC).date()
        if day not in (DAY, QUIET):
            return None
        h = (local % 86400) / 3600.0
        level = 0.0
        if day == DAY:
            level = 0.0 if h < 6 else min(100.0, (h - 6) * 50) if h < 12 else 0.0
        return {
            "LEVEL": level,
            "EST": level * 0.9,
            "POS_A": 300 + level * 2,
            "POS_B": 320 + level * 2,
            "T1": 20 + level * 0.3,
            "T2": 21 + level * 0.3,
        }[channel]

    def read(self, feed, channel, start, end, step):  # noqa: ANN001
        raw = []
        t = start.timestamp()
        while t < end.timestamp():
            v = self._value(channel, t)
            if v is not None:
                raw.append((t, v, "good"))
            t += 60
        return ks.bucket(raw, start.timestamp(), end.timestamp(), step)

    def unit(self, feed, channel):  # noqa: ANN001
        return {"LEVEL": "percent", "EST": "percent", "T1": "degC", "T2": "degC"}.get(channel, "steps")

    def lineage(self):
        return "gold.signals (fake)"

    def carries_quality(self):
        return True


TEMPLATE = textwrap.dedent(
    """
    [template]
    id = "loop-day"
    name = "Loop day"
    day_picker = true
    layout = "2"

    [template.roles]
    level = "the loop's level"
    position = "an actuator's position"
    temperature = "a temperature"
    level_estimate = "a model's estimate of the level"

    [[chart]]
    id = "level"
    title = "Level"
    roles = ["level"]
    y = "log"

    [[chart]]
    id = "positions"
    title = "Positions"
    kind = "step"
    roles = ["position"]

    [[chart]]
    id = "temperatures"
    title = "Temperatures"
    roles = ["temperature"]

    [[chart]]
    id = "estimate"
    title = "Level and its estimate"
    roles = ["level"]
    estimates = ["level_estimate"]

    [[chart]]
    id = "total"
    title = "Level-hours"
    kind = "kpi"
    recipe = "total"
    roles = ["level"]
    unit = "percent h"

    [[chart]]
    id = "peak"
    title = "Peak level"
    kind = "kpi"
    recipe = "peak"
    roles = ["level"]

    [[chart]]
    id = "state"
    title = "Operating state"
    kind = "state"
    recipe = "state"
    roles = ["level"]
    states = [ { name = "Off", below = 1 }, { name = "Changing", below = 90 }, { name = "At level" } ]

    [[chart]]
    id = "at-peak"
    title = "Positions at peak level"
    kind = "bar"
    recipe = "at_peak"
    roles = ["position"]
    of = "level"

    [[chart]]
    id = "plateau"
    title = "Mean position on the low plateau"
    kind = "kpi"
    recipe = "median_where"
    roles = ["position"]
    where = { role = "level", between = [10, 60] }
    hours = [6, 12]
    min_points = 5

    [[chart]]
    id = "unmapped"
    title = "Something this site does not have"
    roles = ["pressure"]
    """
)

SITE = textwrap.dedent(
    """
    [role_group]
    template = "loop-day"
    site = "site-a"
    label = "Site A"
    timezone = "Etc/GMT+6"
    visibility = "shown"
    delay_hours = 24
    days_back = 30

    [sources.day]
    fake = "day"

    [roles]
    level = [ { ref = "loop:LEVEL", label = "Level" } ]
    position = [ { ref = "loop:POS_A", label = "A" }, { ref = "loop:POS_B", label = "B" } ]
    temperature = [ "loop:T1", "loop:T2" ]
    level_estimate = [ { ref = "model:EST", label = "Level estimate" } ]
    """
)


@pytest.fixture
def site(tmp_path, monkeypatch):
    """A product extension dir holding the template, and a site config registered in a temp state dir."""
    rg.clear_cache()
    ext = tmp_path / "extensions" / "loops"
    (ext / "charts" / "templates").mkdir(parents=True)
    (ext / "charts" / "templates" / "loop-day.toml").write_text(TEMPLATE)
    (tmp_path / "site.toml").write_text(SITE)
    monkeypatch.setattr(rg, "template_dirs", lambda: [tmp_path / "extensions"])

    def _open(spec, site_id, *, base=None):  # noqa: ANN001
        assert "fake" in spec, "the test names only fake sources"
        return FakeGold()

    monkeypatch.setattr(rg, "open_source", _open)
    monkeypatch.setattr(rg, "_now", lambda: NOW)
    cfg = rg.RoleGroupConfig.load(tmp_path / "site.toml")
    rg.register(cfg, tmp_path / "state")
    yield cfg, tmp_path / "state"
    rg.clear_cache()


def _graph(group, chart_id):
    return next(g for g in group["graphs"] if g["spec"]["id"].endswith(f"-{chart_id}"))


def _draw(spec, state_dir, day=None):  # noqa: ANN001
    if day:
        spec = {**spec, "data": {"verb": {**spec["data"]["verb"],
                                          "params": {**spec["data"]["verb"]["params"], "day": day}}}}
    return render_spec(spec, width=600, resolve=rg.resolver(state_dir))


def _data(spec, state_dir, day=None):  # noqa: ANN001
    params = dict(spec["data"]["verb"]["params"])
    if day:
        params["day"] = day
    return rg.resolve_data(params, state_dir=state_dir)


def test_the_site_map_fills_the_template_as_a_group_with_a_day_picker(site) -> None:
    cfg, state = site
    group = rg.build_group(cfg)
    assert group["id"] == "loop-day-site-a" and group["name"] == "Loop day · Site A"
    assert group["day_picker"] is True and group["day"] == DAY.isoformat()
    assert group["kind"] == "site" and group["visibility"] == "shown"
    verbs = [g["spec"]["data"]["verb"] for g in group["graphs"]]
    assert [v["params"]["chart"] for v in verbs] == [
        "level", "positions", "temperatures", "estimate", "total", "peak", "state", "at-peak", "plateau"]
    for g, v in zip(group["graphs"], verbs, strict=True):
        assert us.validate(g["spec"]) == [], g["spec"]["id"]
        assert v["name"] == rg.VERB
        assert {k: v["params"][k] for k in ("site", "template", "day")} == {
            "site": "site-a", "template": "loop-day", "day": DAY.isoformat()}


def test_a_chart_whose_roles_the_site_does_not_map_is_left_out_and_named(site) -> None:
    cfg, _ = site
    group = rg.build_group(cfg)
    assert not any(g["spec"]["id"].endswith("-unmapped") for g in group["graphs"])
    assert group["omitted"] == [{"chart": "unmapped", "roles": ["pressure"]}]


def test_a_chart_draws_every_channel_the_site_maps_to_its_role_with_its_unit_and_axis(site) -> None:
    cfg, state = site
    group = rg.build_group(cfg)
    level, positions = _graph(group, "level")["spec"], _graph(group, "positions")["spec"]
    assert level["axes"]["y"]["type"] == "log"
    got = _data(positions, state)
    assert [s["name"] for s in got["series"]] == ["A", "B"]
    assert {s["unit"] for s in got["series"]} == {"steps"}
    assert "<svg" in _draw(level, state).svg


def test_the_day_is_the_site_s_own_day_and_a_new_day_re_scopes_the_chart(site) -> None:
    cfg, state = site
    level = _graph(rg.build_group(cfg), "level")["spec"]
    on = _data(level, state)["series"][0]
    # Site midnight (06:00 UTC), stamped in the site's own offset so the axis reads its clock.
    assert on["x"][0] == "2026-09-10T00:00:00-06:00" and max(v for v in on["y"] if v is not None) == 100.0
    quiet = _data(level, state, day=QUIET.isoformat())["series"][0]
    assert quiet["x"][0] == "2026-09-09T00:00:00-06:00" and max(quiet["y"]) == 0.0


def test_a_time_chart_spans_the_whole_day_as_the_legacy_figure_does(site) -> None:
    cfg, state = site
    for chart in ("positions", "state"):
        got = _data(_graph(rg.build_group(cfg), chart)["spec"], state)
        assert got["x_range"] == ["2026-09-10T00:00:00-06:00", "2026-09-11T00:00:00-06:00"], chart


def test_the_day_picker_offers_only_days_with_readings_and_old_enough(site, monkeypatch) -> None:
    cfg, _ = site
    assert [d["date"] for d in rg.build_group(cfg)["days"]] == [QUIET.isoformat(), DAY.isoformat()]
    assert all(d["rows"] > 0 for d in rg.build_group(cfg)["days"])
    monkeypatch.setattr(rg, "_now", lambda: datetime(2026, 9, 11, 12, tzinfo=UTC))
    rg.clear_cache()
    assert [d["date"] for d in rg.build_group(cfg)["days"]] == [QUIET.isoformat()]


def test_a_day_younger_than_the_delay_is_refused_at_draw_time(site) -> None:
    cfg, state = site
    level = _graph(rg.build_group(cfg), "level")["spec"]
    with pytest.raises(rg.RoleGroupError, match="not 24 h old"):
        _data(level, state, day=(NOW.date()).isoformat())


def test_an_estimate_is_its_measurement_s_counterpart(site) -> None:
    cfg, state = site
    got = _data(_graph(rg.build_group(cfg), "estimate")["spec"], state)["series"]
    assert [s["name"] for s in got] == ["Level", "Level estimate"]
    assert got[1]["role"] == "simulated" and got[1]["counterpart_of"] == "Level"


def test_total_integrates_over_hours_and_skips_gaps(site) -> None:
    cfg, state = site
    row = _data(_graph(rg.build_group(cfg), "total")["spec"], state)["series"][0]
    # Ramp 0->100 over 2 h (100 percent h), hold 100 for 4 h (400), then off.
    assert row["unit"] == "percent h" and row["y"] == [pytest.approx(500.0, rel=0.01)]


def test_peak_names_its_value_and_when(site) -> None:
    cfg, state = site
    row = _data(_graph(rg.build_group(cfg), "peak")["spec"], state)["series"][0]
    assert row["y"] == [100.0] and row["unit"] == "percent" and "08:00" in row["name"]


def test_state_buckets_the_role_into_named_states(site) -> None:
    cfg, state = site
    spec = _graph(rg.build_group(cfg), "state")["spec"]
    assert spec["encoding"]["states"] == ["Off", "Changing", "At level"] and "subtitle" not in spec
    s = _data(spec, state)["series"][0]
    assert set(v for v in s["y"] if v is not None) == {0, 1, 2}
    svg = _draw(spec, state).svg
    assert all(name in svg for name in ("Off", "Changing", "At level"))


def test_at_peak_reads_each_channel_when_another_role_peaked(site) -> None:
    cfg, state = site
    s = _data(_graph(rg.build_group(cfg), "at-peak")["spec"], state)["series"][0]
    assert s["x"] == ["A", "B"] and s["y"] == [500.0, 520.0]


def test_median_where_keeps_only_the_moments_the_condition_holds(site) -> None:
    cfg, state = site
    row = _data(_graph(rg.build_group(cfg), "plateau")["spec"], state)["series"][0]
    # Level between 10 and 60 is 06:12..07:12 site time: positions 320..420 and 340..440, mean 380.
    assert row["y"][0] == pytest.approx(380.0, abs=3)


def test_median_where_on_a_day_the_condition_never_held_says_so_and_draws_nothing_else(site) -> None:
    cfg, state = site
    spec = _graph(rg.build_group(cfg), "plateau")["spec"]
    with pytest.raises(rg.RoleGroupError, match="not measured on this day"):
        _data(spec, state, day=QUIET.isoformat())
    drawn = _draw(spec, state, day=QUIET.isoformat())
    assert "not measured on this day" in drawn.svg and "<line" not in drawn.svg


def test_a_registered_role_group_is_a_prebuilt_group(site) -> None:
    _, state = site
    got = [g for g in prebuilt.listing(state) if g["id"] == "loop-day-site-a"]
    assert got and got[0]["prebuilt"] is True and got[0]["day_picker"] is True


def test_the_skill_registers_a_config_and_the_cli_says_what_it_built(site, tmp_path, monkeypatch, capsys) -> None:
    from axiom.extensions.builtins.charts import cli

    _, state = site
    monkeypatch.setattr("axiom.infra.paths.get_user_state_dir", lambda: state)
    code = cli.main(["role-group", "--config", str(tmp_path / "site.toml"), "--dry-run", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["group"] == "Loop day · Site A" and out["day"] == DAY.isoformat()
    assert out["omitted"] == ["unmapped"] and "level" in out["charts"]


def test_a_kit_snapshot_is_a_gold_source(tmp_path) -> None:
    folder = tmp_path / "data" / "snapshots"
    folder.mkdir(parents=True)
    t0 = datetime(2026, 9, 10, tzinfo=UTC)
    rows = [{"site": "site-a", "feed": "loop", "channel": "T1", "unit": "degC",
             "ts": (t0 + timedelta(minutes=i)).strftime("%Y-%m-%dT%H:%M:%SZ"), "value": float(i)} for i in range(30)]
    (folder / "one.json").write_text(json.dumps({"id": "one", "site": "site-a", "rows": rows}))
    (folder / "other.json").write_text(json.dumps({"id": "other", "site": "site-b", "rows": rows}))
    src = ks.open_source({"kit": str(tmp_path)}, "site-a")
    got = src.read("loop", "T1", t0, t0 + timedelta(minutes=30), 600)
    assert [v for _, v, _ in got] == [4.5, 14.5, 24.5] and src.unit("loop", "T1") == "degC"
    assert "kit snapshot" in src.lineage() and not src.carries_quality()


def test_a_site_map_without_a_gold_source_is_refused() -> None:
    with pytest.raises(rg.RoleGroupError, match="sources.day"):
        rg.RoleGroupConfig.from_mapping({"role_group": {"template": "loop-day", "site": "s"}, "roles": {}})
