# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``channels``, ``latest`` and ``show``: the questions a newcomer asks, in the order they ask them.

The node here serves only ``data.aggregate``, ``data.series`` and ``data.roles``, which is
all a node served before these verbs existed. So every test below is also a proof that the
verbs compose from what a bound reader's node already answers.
"""

from __future__ import annotations

import logging
import pytest

from axiom.extensions.builtins.data_platform import read_target as rt
from axiom.extensions.builtins.data_platform.skills import friendly, gold
from axiom.infra.skills import SkillContext, SkillRegistry

from .fake_node import OTHER, SERVED, SITE, FakeNode


@pytest.fixture
def node(monkeypatch, tmp_path):
    """A bound laptop whose node is the fake, bound to ``example-loop``."""
    fake = FakeNode.standard()
    monkeypatch.setattr(gold, "_GUARD", None)
    monkeypatch.setattr(rt, "ask", fake.ask)
    monkeypatch.setattr(
        rt, "resolve_read_target",
        lambda: rt.ReadTarget("binding", site=SITE, face_url="https://node.example"),
    )
    monkeypatch.setattr(rt, "current_binding", lambda: (SITE, "https://node.example"))
    return fake


@pytest.fixture
def ctx(tmp_path):
    return SkillContext(registry=SkillRegistry(), state_dir=tmp_path,
                        logger=logging.getLogger("test"), surface="cli")


def _ok(result):
    assert result.ok, result.errors
    return result.value["data"]


# --------------------------------------------------------------------------- channels


def test_channels_lists_each_channel_with_feed_unit_role_and_span(node, ctx):
    data = _ok(friendly.channels({}, ctx))
    by = {(c["feed"], c["channel"]): c for c in data["channels"]}
    t1 = by[("loop.main", "T1")]
    assert (t1["unit"], t1["role"]) == ("degC", "fluid_temperature")
    assert t1["first_ts"] == "2026-03-10T00:00:00Z"
    assert t1["last_ts"] == "2026-03-11T23:50:00Z"
    assert by[("loop.main", "P1")]["role"] == "line_pressure"
    assert by[("loop.main", "Old1")]["role"] is None
    assert data["site"] == SITE


def test_channels_can_be_narrowed_to_a_role(node, ctx):
    data = _ok(friendly.channels({"role": "fluid_temperature"}, ctx))
    assert sorted(c["channel"] for c in data["channels"]) == ["T1", "T2"]


def test_channels_never_asks_for_anything_a_node_does_not_already_serve(node, ctx):
    friendly.channels({}, ctx)
    friendly.channels({"role": "line_pressure"}, ctx)
    assert {verb for verb, _ in node.calls} <= set(SERVED)


def test_an_unknown_role_is_answered_with_the_nearest_real_one(node, ctx):
    result = friendly.channels({"role": "fluid_temp"}, ctx)
    assert not result.ok
    assert result.errors == [f"no role 'fluid_temp' at {SITE}; did you mean fluid_temperature?"]


def test_an_unknown_site_is_answered_with_the_nearest_real_one(node, ctx):
    result = friendly.channels({"site": "exmple-loop"}, ctx)
    assert not result.ok
    assert result.errors == ["no site 'exmple-loop'; did you mean example-loop?"]


def test_a_site_is_the_bindings_unless_told(node, ctx):
    assert _ok(friendly.channels({}, ctx))["site"] == SITE
    assert _ok(friendly.channels({"site": OTHER}, ctx))["site"] == OTHER


def test_with_no_binding_and_two_sites_it_asks_which(node, ctx, monkeypatch):
    monkeypatch.setattr(rt, "current_binding", lambda: None)
    result = friendly.channels({}, ctx)
    assert not result.ok
    assert f"which site? pass --site; sites here: {SITE}, {OTHER}" in result.errors[0]


def test_with_no_binding_and_one_site_that_site_is_the_one(node, ctx, monkeypatch):
    monkeypatch.setattr(rt, "current_binding", lambda: None)
    node.signals = [r for r in node.signals if r[0] == SITE]
    assert _ok(friendly.channels({}, ctx))["site"] == SITE


# --------------------------------------------------------------------------- latest


def test_latest_is_each_channels_own_newest_reading(node, ctx):
    data = _ok(friendly.latest({}, ctx))
    by = {r["channel"]: r for r in data["readings"] if r["feed"] == "loop.main"}
    assert by["T1"]["ts"] == "2026-03-11T23:50:00Z"
    # A channel that stopped three days before the others still answers, with its own time.
    assert by["Old1"]["ts"] == "2026-03-01T04:50:00Z"
    assert by["Old1"]["value"] is not None
    assert by["T1"]["role"] == "fluid_temperature"


def test_latest_by_role_and_by_channel(node, ctx):
    by_role = _ok(friendly.latest({"role": "fluid_temperature"}, ctx))
    assert sorted(r["channel"] for r in by_role["readings"]) == ["T1", "T2"]
    by_channel = _ok(friendly.latest({"channel": "P1"}, ctx))
    assert [r["channel"] for r in by_channel["readings"]] == ["P1"]
    assert by_channel["readings"][0]["unit"] == "kPa"


def test_latest_unknown_channel_is_answered_with_the_nearest_real_one(node, ctx):
    result = friendly.latest({"channel": "T11"}, ctx)
    assert not result.ok
    assert "did you mean T1" in result.errors[0]


def test_latest_reads_a_feed_once_not_once_per_channel(node, ctx):
    friendly.latest({"role": "fluid_temperature"}, ctx)
    reads = [p for p in node.signal_reads() if p.get("column") == "value"]
    assert len(reads) == 1


# --------------------------------------------------------------------------- show


def test_show_a_channel_resolves_its_feed_so_the_read_cannot_scan_every_feed(node, ctx):
    data = _ok(friendly.show({"target": "T1"}, ctx))
    assert data["kind"] == "channel" and data["feeds"] == ["loop.main"]
    # The fake node times out a channel filter that does not name its feed, as the real one did.
    for read in node.signal_reads():
        assert "feed = 'loop.main'" in read["filter"]


def test_show_defaults_to_the_latest_day_with_readings_not_now(node, ctx):
    data = _ok(friendly.show({"target": "T1"}, ctx))
    assert data["day"] == "2026-03-11"
    assert data["window"] == {"start": "2026-03-11T00:00:00Z", "end": "2026-03-12T00:00:00Z"}
    assert data["defaulted_window"] is True


def test_show_picks_a_bucket_from_the_window(node, ctx):
    assert _ok(friendly.show({"target": "T1"}, ctx))["bucket"] == "30 minutes"
    week = _ok(friendly.show({"target": "T1", "start": "2026-03-04", "end": "2026-03-12"}, ctx))
    assert week["bucket"] == "6 hours"
    hour = _ok(friendly.show({"target": "T1", "day": "2026-03-10", "every": "1h"}, ctx))
    assert hour["bucket"] == "1 hour"
    assert len(hour["series"]) == 24


def test_show_a_role_filters_by_role_and_keeps_each_channel_apart(node, ctx):
    data = _ok(friendly.show({"target": "fluid_temperature"}, ctx))
    assert data["kind"] == "role" and data["channels"] == ["T1", "T2"]
    assert {p["channel"] for p in data["series"]} == {"T1", "T2"}
    assert data["units"] == ["degC"]
    for read in node.signal_reads():
        assert "role = 'fluid_temperature'" in read["filter"]
        assert read["window"]["start"] and read["window"]["end"]  # bounded: a role read with no window times out


def test_show_a_role_reads_a_bounded_window_before_it_reads_the_series(node, ctx):
    friendly.show({"target": "line_pressure"}, ctx)
    first = node.signal_reads()[0]
    assert first["window"]["column"] == "ts"
    assert first["window"]["end"] > first["window"]["start"]


def test_show_window_flags_are_the_same_window(node, ctx):
    day = _ok(friendly.show({"target": "T1", "day": "2026-03-10"}, ctx))
    span = _ok(friendly.show({"target": "T1", "start": "2026-03-10", "end": "2026-03-11"}, ctx))
    assert day["window"] == span["window"]
    assert day["series"] == span["series"]


def test_show_an_empty_window_says_when_the_last_reading_was_and_how_to_reach_it(node, ctx):
    result = friendly.show({"target": "T1", "day": "2026-03-20"}, ctx)
    assert not result.ok
    assert result.errors == [
        f"no readings at {SITE} for channel T1 between 2026-03-20T00:00:00Z and 2026-03-21T00:00:00Z; "
        "its last reading was 2026-03-11; try --day 2026-03-11"
    ]


def test_show_an_empty_role_window_says_the_same(node, ctx):
    result = friendly.show({"target": "fluid_temperature", "yesterday": True}, ctx)
    assert not result.ok
    assert "its last reading was 2026-03-11; try --day 2026-03-11" in result.errors[0]
    assert "for role fluid_temperature" in result.errors[0]


def test_show_an_unknown_name_is_answered_with_the_nearest_real_one(node, ctx):
    result = friendly.show({"target": "fluid_temp"}, ctx)
    assert not result.ok
    assert result.errors == [f"no role or channel 'fluid_temp' at {SITE}; did you mean fluid_temperature?"]
    typo = friendly.show({"target": "Old2"}, ctx)
    assert "did you mean Old1" in typo.errors[0]


def test_show_a_channel_in_two_feeds_needs_the_feed(node, ctx):
    result = friendly.show({"target": "Dup"}, ctx)
    assert not result.ok and "pass --feed" in result.errors[0]
    assert _ok(friendly.show({"target": "Dup", "feed": "loop.aux"}, ctx))["feeds"] == ["loop.aux"]


def test_show_needs_a_target_and_says_where_to_find_one(node, ctx):
    result = friendly.show({}, ctx)
    assert not result.ok and "data roles" in result.errors[0]


def test_show_refuses_two_windows(node, ctx):
    result = friendly.show({"target": "T1", "day": "2026-03-10", "last": "7d"}, ctx)
    assert not result.ok and "each pick a window" in result.errors[0]


def test_show_only_ever_asks_for_verbs_the_node_serves(node, ctx):
    friendly.show({"target": "fluid_temperature"}, ctx)
    friendly.show({"target": "P1"}, ctx)
    friendly.show({"target": "nope"}, ctx)
    assert {verb for verb, _ in node.calls} <= set(SERVED)
