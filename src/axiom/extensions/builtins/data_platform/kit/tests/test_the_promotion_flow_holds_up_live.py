# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""What stopped a live run of kit promotion, without a database.

A bound colleague ran the intended flow (kit-pull, kit-up, kit-materialize,
kit-scaffold starter --from-snapshot, kit-try, kit-check, kit-promote) against a
real node, and it stopped at several places that no unit test had reached:

- kit-pull filtered on the site alone and dropped other channels on the laptop.
  The signal index is (site, feed, channel, ts), so the read walked every row the
  site held and the door timed out at 30 s; with the feed named it took 0.95 s.
  A read timeout then reached the terminal as a ``TimeoutError`` traceback.
- the starter put channels of different units on one chart, which kit-try refuses.
- every new kit was refused at promote, because kit-init ships an example
  normalizer under conform/ and the bundle carried it.
- a submit to a node whose registry was never set up answered HTTP 500.

The node-side halves (a read-only serving login, a busy silver table, kit-try
keeping a materialized snapshot) are in ``test_kit_promotion_on_a_busy_node.py``.
"""

from __future__ import annotations

import json
import logging

import pytest

from axiom.extensions.builtins.data_platform import kit
from axiom.extensions.builtins.data_platform import read_target as rt
from axiom.extensions.builtins.data_platform.kit import host
from axiom.extensions.builtins.data_platform.kit.bundle import pack, unpack
from axiom.extensions.builtins.data_platform.skills import gold, kit_promotion
from axiom.extensions.builtins.data_platform.tests.fake_node import SITE, FakeNode, at
from axiom.infra.skills import SkillContext, SkillRegistry


@pytest.fixture
def ctx(tmp_path):
    return SkillContext(registry=SkillRegistry(), state_dir=tmp_path, logger=logging.getLogger("t"), surface="cli")


@pytest.fixture
def node(monkeypatch):
    """A bound laptop whose node is the fake. The fake times out a signals read that
    names a channel without its feed, as the real node did."""
    fake = FakeNode.standard()
    fake.add(SITE, "loop.main", "Valve_Pos", "percent", None, at("2026-03-10"), 48, base=40.0)
    monkeypatch.setattr(gold, "_GUARD", None)
    monkeypatch.setattr(rt, "ask", fake.ask)
    monkeypatch.setattr(rt, "resolve_read_target",
                        lambda: rt.ReadTarget("binding", site=SITE, face_url="https://node.example"))
    monkeypatch.setattr(rt, "current_binding", lambda: (SITE, "https://node.example"))
    return fake


@pytest.fixture
def project(tmp_path):
    kit.init(tmp_path / "site", SITE)
    return kit.load(tmp_path / "site")


def _pull(project, ctx, **extra):
    params = {"dir": str(project.root), "start": "2026-03-09T00:00:00Z", "end": "2026-03-12T00:00:00Z",
              "bucket": "1 hour", "id": "pulled", **extra}
    return kit_promotion.kit_pull(params, ctx)


# -- 1. kit-pull reads by the index, bounded ------------------------------------------


def test_a_pull_names_the_site_feed_and_channels_in_every_signals_read(node, project, ctx):
    r = _pull(project, ctx, channels="T1,Valve_Pos")
    assert r.ok, r.errors
    reads = node.signal_reads()
    assert reads, "the pull never read the signals"
    for read in reads:
        flt = read["filter"]
        assert "feed in ('loop.main')" in flt and "channel in ('T1', 'Valve_Pos')" in flt, flt
    snap = json.loads((project.root / "snapshots" / "pulled.json").read_text())
    assert snap["channels"] == ["T1", "Valve_Pos"], "channel names are kept exactly as the node holds them"
    assert {row["channel"] for row in snap["rows"]} == {"T1", "Valve_Pos"}
    assert {row["feed"] for row in snap["rows"]} == {"loop.main"}


def test_a_named_feed_skips_the_catalogue(node, project, ctx):
    r = _pull(project, ctx, channels="T1", feed="loop.main")
    assert r.ok, r.errors
    assert [p["table"] for _, p in node.calls] == ["signals"]


def test_no_channels_means_the_catalogued_ones_each_with_its_feed(node, project, ctx):
    r = _pull(project, ctx)
    assert r.ok, r.errors
    (read,) = node.signal_reads()
    assert "feed in ('loop.aux', 'loop.main')" in read["filter"]
    assert "'P1'" in read["filter"] and "'Valve_Pos'" in read["filter"]


def test_an_unknown_channel_is_named_with_the_nearest_real_one(node, project, ctx):
    r = _pull(project, ctx, channels="t1")
    assert not r.ok and "no channel 't1'" in r.errors[0] and "T1" in r.errors[0], r.errors
    assert node.signal_reads() == []


def test_a_pull_too_wide_is_refused_before_the_node_is_asked_for_signals(node, project, ctx):
    r = _pull(project, ctx, channels="T1", start="2020-01-01T00:00:00Z", end="2026-03-12T00:00:00Z",
              bucket="1 minute")
    assert not r.ok and "a pull keeps at most" in r.errors[0] and "--bucket" in r.errors[0], r.errors
    assert node.signal_reads() == []


def test_a_read_timeout_is_a_sentence_not_a_traceback(monkeypatch):
    import urllib.request

    def slow(*a, **k):
        raise TimeoutError("timed out")

    monkeypatch.setattr(urllib.request, "urlopen", slow)
    monkeypatch.setattr(rt, "_key", lambda url: "")
    target = rt.ReadTarget("binding", site=SITE, face_url="https://node.example")
    r = rt.ask(target, verb="data.series", params={})
    assert not r.ok and "did not answer `data.series` within 30 s" in r.errors[0], r.errors


# -- 3. the starter draws what it pulled ---------------------------------------------


def test_the_starter_writes_one_chart_per_unit_with_names_exact(node, project, ctx):
    assert _pull(project, ctx, channels="T1,P1,Valve_Pos").ok
    written = kit.scaffold_contribution(project, "starter", "loop_view", from_snapshot="pulled")
    charts = sorted(w for w in written if w.startswith("charts/"))
    assert charts == ["charts/loop_view_degc.json", "charts/loop_view_kpa.json", "charts/loop_view_percent.json"]
    by = {c: json.loads((project.root / c).read_text())["channels"] for c in charts}
    assert by == {"charts/loop_view_degc.json": ["T1"], "charts/loop_view_kpa.json": ["P1"],
                  "charts/loop_view_percent.json": ["Valve_Pos"]}
    sql = (project.root / "gold" / "loop_view.sql").read_text()
    assert "('T1', 'P1', 'Valve_Pos')" in sql
    report = kit.check_kit(kit.load(project.root), isolation=False)
    assert report["declarations"] == [], report


# -- 4. a new kit can be promoted ----------------------------------------------------


def test_a_new_kit_is_packed_without_its_example_normalizer(project, tmp_path):
    b = pack(project)
    assert not b.has_code and not any(f.startswith("conform/") for f in b.files)
    assert "conform/rig_frame.py" in b.left_behind
    unpacked = kit.load(unpack(b.data, tmp_path / "node-side"))
    assert host.static_problems(unpacked, bundle_has_code=False) == []
    assert pack(project, include_code=True).has_code, "code can still be packed, so the node's refusal is testable"


def test_promote_says_the_normalizer_stayed_behind(project, ctx, monkeypatch):
    sent = {}

    def fake_ask(target, *, verb, params=None):
        from axiom.infra.skills import SkillResult

        sent["bundle"] = params["bundle"]
        return SkillResult(ok=True, value={"id": 1, "site": SITE, "state": "checked", "problems": []})

    monkeypatch.setattr(rt, "ask", fake_ask)
    monkeypatch.setattr(rt, "resolve_read_target",
                        lambda: rt.ReadTarget("binding", site=SITE, face_url="https://node.example"))
    r = kit_promotion.kit_promote({"dir": str(project.root)}, ctx)
    assert r.ok and sent["bundle"]
    assert any("conform/ (" in a and "stayed on this machine" in a for a in r.actions_taken), r.actions_taken


# -- 5. a node that was never set up answers 503, naming the step --------------------


def test_a_submit_to_a_node_not_set_up_is_503_naming_the_setup_command(monkeypatch, project):
    import base64

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from axiom.extensions.builtins.data_platform import read_api

    def not_ready(*a, **k):
        raise host._not_ready("there is no kit_registry schema")

    monkeypatch.setattr(read_api, "holds_medallion", lambda: True)
    monkeypatch.setattr(host, "submit", not_ready)
    monkeypatch.setattr(kit_promotion, "_node_dsn", lambda: "postgresql://unused/")
    app = FastAPI()
    app.include_router(read_api.build_data_read_router())
    bundle = base64.b64encode(pack(project).data).decode()
    resp = TestClient(app, raise_server_exceptions=False).post(
        "/data/submit", json={"verb": "data.kit_submit", "params": {"bundle": bundle}})
    assert resp.status_code == 503, resp.text
    assert "kit-host-setup" in resp.json()["detail"]
