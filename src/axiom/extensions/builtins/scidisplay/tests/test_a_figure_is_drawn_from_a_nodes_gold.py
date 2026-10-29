# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A figure source answers the Charts contract from a node's data door.

The door is faked: these tests pin what is asked of it (gold tables only,
bucketed, chunked, under budget) and what comes back (the Figure JSON the
appkit ``FigureSource`` draws).
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta

import pytest

from axiom.extensions.builtins.scidisplay.node_figure_source import (
    DataDoor,
    FigureNote,
    NodeFigureSource,
    NodeReadError,
    choose_bucket,
)

T0 = datetime(2026, 3, 1, tzinfo=UTC)

CATALOGUE = [
    # site, feed, channel, unit, rows, first, last
    ("site-a", "loop", "T1", "degC", 86_400, T0, T0 + timedelta(days=10)),
    ("site-a", "loop", "T2", "degC", 86_400, T0, T0 + timedelta(days=10)),
    ("site-a", "loop", "P1", "kPa", 86_400, T0, T0 + timedelta(days=10)),
    ("site-a", "dev:bus", "PV:A:B", "degC", 1_000, T0, T0 + timedelta(days=1)),
    ("site-b", "daq", "flow", "", 10, T0, T0 + timedelta(hours=2)),
]


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _seconds(bucket: str) -> int:
    n, unit = bucket.split()
    return int(n) * {"second": 1, "minute": 60, "hour": 3600, "day": 86400}[unit.rstrip("s")]


class FakeDoor(DataDoor):
    def __init__(self) -> None:
        super().__init__(node_url="https://node.example", key=lambda: "k", rate=1000, burst=1000)
        self.calls: list[tuple[str, dict]] = []

    def _query_once(self, verb, params):  # noqa: ANN001
        self.calls.append((verb, json.loads(json.dumps(params))))
        if verb == "data.aggregate":
            assert params["table"] == "signal_catalogue"
            col = params["column"]
            groups = []
            for site, feed, ch, unit, rows, first, last in CATALOGUE:
                value = {"rows": str(rows), "first_ts": _iso(first), "last_ts": _iso(last)}[col]
                groups.append({"site": site, "feed": feed, "channel": ch, "unit": unit or None,
                               "value": value})
            return {"data": {"groups": groups}}
        assert verb == "data.series"
        assert params["table"] == "signals"
        w = params["window"]
        lo = datetime.fromisoformat(w["start"].replace("Z", "+00:00"))
        hi = datetime.fromisoformat(w["end"].replace("Z", "+00:00"))
        step = timedelta(seconds=_seconds(params["bucket"]))
        names = re.findall(r"'([^']*)'", params["filter"].split("channel", 1)[1])
        feeds = re.findall(r"'([^']*)'", params["filter"].split("feed", 1)[1].split("channel", 1)[0])
        out = []
        for site, feed, ch, *_rest in CATALOGUE:
            if ch not in names or feed not in feeds:
                continue
            pts, t = [], lo
            while t < hi:
                pts.append({"t": _iso(t), "value": 1.0 if params["fn"] == "count" else 20.0})
                t += step
            out.append({"key": {"feed": feed, "channel": ch}, "points": pts})
        return {"data": {"series": out}}


@pytest.fixture
def source(tmp_path) -> NodeFigureSource:
    return NodeFigureSource(FakeDoor(), labels={"site-a": "Site A"},
                            dates_cache_path=str(tmp_path / "days.json"))


def test_the_sites_are_the_catalogues_sites_with_their_labels(source):
    sites = source.sites()
    assert [s["id"] for s in sites] == ["site-a", "site-b"]
    assert sites[0]["label"] == "Site A" and sites[1]["label"] == "site-b"
    assert sites[0]["rows"] == 86_400 * 3 + 1_000


def test_the_catalogue_is_read_once_and_kept(source):
    source.sites()
    source.channels("site-a")
    source.revision("site-a")
    assert sum(1 for v, _ in source.door.calls if v == "data.aggregate") == 3


def test_channels_carry_feed_unit_and_range(source):
    chans = {c["channel"]: c for c in source.channels("site-a")}
    assert chans["T1"] == {"feed": "loop", "channel": "T1", "unit": "degC", "rows": 86_400,
                           "first": "2026-03-01T00:00:00Z", "last": "2026-03-11T00:00:00Z"}


def test_a_channel_name_with_colons_is_not_split_into_a_feed(source):
    hits = source.resolve("site-a", ["dev:bus:PV:A:B", "PV:A:B", "loop:T1", "loop|T2", "T1"])
    assert [(c.feed, c.channel) for c in hits] == [("dev:bus", "PV:A:B"), ("loop", "T1"), ("loop", "T2")]


def test_only_gold_tables_are_read(source):
    source.figure("site-a", ["loop:T1"])
    source.dates("site-a", ["loop:T1"])
    tables = {p["table"] for _, p in source.door.calls}
    assert tables == {"signal_catalogue", "signals"}


def test_with_no_window_the_figure_opens_on_the_last_day(source):
    fig = source.figure("site-a", ["loop:T1", "loop:T2"])
    assert fig["window"]["to"].startswith("2026-03-11T00:00:00")
    assert fig["window"]["from"].startswith("2026-03-10T00:00:00")
    assert fig["bounds"] == {"from": "2026-03-01T00:00:00+00:00", "to": "2026-03-11T00:00:00+00:00"}
    assert [s["name"] for s in fig["readout"]["series"]] == ["T1", "T2"]
    assert fig["svg"].startswith("<svg") or "<svg" in fig["svg"][:200]
    assert fig["served"]["bucket"] == "2 minutes"
    assert len(fig["readout"]["series"][0]["points"]) == 720


def test_a_second_unit_takes_the_second_axis_rather_than_vanishing(source):
    # Measured beside estimated is the twin's core view, and they rarely share a unit.
    fig = source.figure("site-a", ["loop:T1", "loop:P1"])
    assert [s["name"] for s in fig["readout"]["series"]] == ["T1", "P1"]
    assert sorted(fig["units"]) == ["degC", "kPa"]


def test_channels_from_two_feeds_draw_in_one_graph(source):
    fig = source.figure("site-a", ["loop:T1", "dev:bus|PV:A:B"], t_from=T0, t_to=T0 + timedelta(hours=6))
    assert [s["name"] for s in fig["readout"]["series"]] == ["T1", "PV:A:B"]


def test_the_bucket_keeps_a_series_near_its_point_target():
    assert choose_bucket(86400, 900) == 120
    assert choose_bucket(7 * 86400, 900) == 900
    assert choose_bucket(3600, 900) == 5


def test_a_wide_window_is_read_in_bounded_chunks(source):
    source.row_budget = 10**9
    source.figure("site-a", ["loop:T1"], t_from=T0, t_to=T0 + timedelta(days=8))
    series = [p for v, p in source.door.calls if v == "data.series"]
    assert 1 < len(series) <= source.max_chunks
    spans = sorted((p["window"]["start"], p["window"]["end"]) for p in series)
    assert spans[0][0] == "2026-03-01T00:00:00Z" and spans[-1][1] == "2026-03-09T00:00:00Z"
    assert all(a[1] == b[0] for a, b in zip(spans, spans[1:]))


def test_a_window_past_the_row_budget_is_clamped_and_says_so(source):
    source.row_budget = 8_640 * 2  # two days of T1
    fig = source.figure("site-a", ["loop:T1"], t_from=T0, t_to=T0 + timedelta(days=10))
    lo = datetime.fromisoformat(fig["window"]["from"])
    assert lo == T0 + timedelta(days=8)
    assert any("narrow the window" in n for n in fig["notes"])


def test_an_unknown_channel_is_a_note_not_a_fault(source):
    with pytest.raises(FigureNote):
        source.figure("site-a", ["loop:nope"])


def test_a_channel_named_in_a_feed_is_not_found_in_another(source):
    # site-b's daq:flow exists; asking for loop:flow must not draw it under that name.
    found, missing = source.resolve_report("site-a", ["loop:T1", "loop:T"])
    assert [c.channel for c in found] == ["T1"]
    assert missing[0]["ref"] == "loop:T" and missing[0]["closest"][0].startswith("loop:T")


def test_an_unknown_channel_beside_a_known_one_is_said_with_the_closest_names(source):
    fig = source.figure("site-a", ["loop:T1", "loop:T3"])
    assert [s["name"] for s in fig["readout"]["series"]] == ["T1"]
    assert fig["missing"][0]["ref"] == "loop:T3"
    assert any("not found at site-a: loop:T3 (closest: loop:T" in n for n in fig["notes"]), fig["notes"]


def test_a_link_naming_only_unknown_channels_says_which_and_what_is_close(source):
    with pytest.raises(FigureNote, match=r"not found at site-a: loop:T9 \(closest: loop:T"):
        source.figure("site-a", ["loop:T9"])


def test_dates_are_counted_per_day_and_kept_on_disk(source, tmp_path):
    days = source.dates("site-a", ["loop:T1"])
    assert days[0] == {"date": "2026-03-01", "rows": 1} and len(days) == 11
    reads = sum(1 for v, _ in source.door.calls if v == "data.series")
    again = NodeFigureSource(FakeDoor(), dates_cache_path=str(tmp_path / "days.json"))
    assert again.dates("site-a", ["loop:T1"]) == days
    assert not [v for v, _ in again.door.calls if v == "data.series"]
    assert reads >= 1


def test_a_refusal_from_the_door_is_raised_with_its_reason():
    class Refusing(FakeDoor):
        def _query_once(self, verb, params):  # noqa: ANN001
            raise NodeReadError(f"{verb}: refused: not a data read")

    with pytest.raises(NodeReadError, match="not a data read"):
        NodeFigureSource(Refusing()).sites()


def test_a_rate_limit_is_waited_out_then_answered():
    class Limited(FakeDoor):
        tries = 0

        def _query_once(self, verb, params):  # noqa: ANN001
            Limited.tries += 1
            if Limited.tries == 1:
                raise NodeReadError(f"{verb}: refused: rate limit: 5.0/s (burst 20)")
            return super()._query_once(verb, params)

    door = Limited()
    assert door.query("data.aggregate", {"table": "signal_catalogue", "column": "rows"})
    assert Limited.tries == 2


class TenantDoor(FakeDoor):
    """A key bound to site-a, as the node holds it: its own site, and a refusal for any other."""

    def _query_once(self, verb, params):  # noqa: ANN001
        text = params.get("filter") or ""
        if "site = " in text and "'site-a'" not in text:
            other = text.split("'")[1]
            raise NodeReadError(f"{verb}: this credential is bound to site 'site-a', which reads "
                                f"only 'site-a', not {other!r}")
        got = super()._query_once(verb, params)
        if verb == "data.aggregate" and "groups" in got.get("data", {}):
            got["data"]["groups"] = [g for g in got["data"]["groups"] if g["site"] == "site-a"]
        return got


def test_a_tenant_key_lists_its_own_site_only():
    assert [s["id"] for s in NodeFigureSource(TenantDoor()).sites()] == ["site-a"]


@pytest.mark.parametrize("ask", [
    lambda src: src.channels("site-b"),
    lambda src: src.revision("site-b"),
    lambda src: src.dates("site-b", ["flow"]),
    lambda src: src.figure("site-b", ["flow"]),
    lambda src: src.channels("no-such-site"),
])
def test_another_sites_channels_are_refused_not_empty(ask):
    from axiom.extensions.builtins.scidisplay.node_figure_source import SiteRefused

    with pytest.raises(SiteRefused, match="reads only 'site-a'"):
        ask(NodeFigureSource(TenantDoor()))


def test_for_a_key_that_reads_every_site_an_absent_site_is_a_note(source):
    with pytest.raises(FigureNote, match="no site 'site-z'"):
        source.channels("site-z")


class ClassGuardDoor(FakeDoor):
    """A node that refuses to summarise a measurement and an estimate in one group."""

    def _query_once(self, verb, params):  # noqa: ANN001
        if verb == "data.series" and "source_class" not in params["group_by"]:
            raise NodeReadError("data.series: refusing to summarise across 2 source_class values")
        return super()._query_once(verb, params)


def test_measured_beside_estimated_is_grouped_by_class_not_refused():
    fig = NodeFigureSource(ClassGuardDoor()).figure("site-a", ["loop:T1", "loop:T2"])
    assert [s["name"] for s in fig["readout"]["series"]] == ["T1", "T2"]


def test_the_same_name_in_another_feed_is_the_closest_match(source):
    _found, missing = source.resolve_report("site-a", ["dev:bus|T1"])
    assert missing[0]["closest"][0] == "loop:T1"
