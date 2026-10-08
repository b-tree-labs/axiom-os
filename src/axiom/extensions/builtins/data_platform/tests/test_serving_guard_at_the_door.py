# Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The guard sits at the gold door, not beside it (ADR-157 wiring).

These tests stub the query layer and the connection, so what they prove is
the door itself: admission before the database, cheap refusal, the deployment
choosing the DSN, the served block leading the envelope, and the abusive
personas from the PRD's battery (unbounded range, hostile bucket, flood,
anonymous caller) meeting a bound instead of the serving tier.
"""

from __future__ import annotations

import logging
import sys
import types
from pathlib import Path

import pytest

from axiom.extensions.builtins.data_platform.skills import gold
from axiom.infra import serving_guard as sg
from axiom.infra.principal import PrincipalContext
from axiom.infra.skills import SkillContext, SkillRegistry


def _ctx(surface="mcp", handle="@tester", tmp_path=Path(".")):
    return SkillContext(
        registry=SkillRegistry(),
        state_dir=Path(tmp_path),
        logger=logging.getLogger("test"),
        principal=PrincipalContext(handle=handle),
        surface=surface,
    )


class _FakeConn:
    def set_session(self, **kw):
        pass

    def cursor(self):
        class _Cur:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        return _Cur()

    def close(self):
        pass


@pytest.fixture
def door(monkeypatch, tmp_path):
    """Fresh guard, stubbed query layer, stubbed connection, captured DSN."""
    monkeypatch.setattr(gold, "_GUARD", None)
    monkeypatch.setenv("AXIOM_CONFIG_DIR", str(tmp_path))  # no policy file

    captured: dict = {}

    def fake_series(cur, **kw):
        captured["series_kw"] = kw
        n = int(kw.get("_points", 5000) or 5000)
        return {
            "data": {
                "table": kw.get("table"),
                "column": kw.get("column"),
                "bucket": kw.get("bucket"),
                "fn": kw.get("fn"),
                "series": [{"t": f"2024-04-01T00:00:{i:02d}", "value": float(i)} for i in range(n)],
            },
            "provenance": {"source": "test", "method": "stub", "rows": n},
        }

    monkeypatch.setattr(gold.gq, "series", fake_series)

    import axiom.extensions.builtins.data_platform._dsn as dsn_mod

    def fake_resolve(*a, **kw):
        captured["resolve_args"] = (a, kw)
        return "dbname=deployment"

    monkeypatch.setattr(dsn_mod, "resolve_dsn", fake_resolve)

    fake_pg = types.ModuleType("psycopg2")

    def fake_connect(dsn, **kw):
        captured["connected_dsn"] = dsn
        return _FakeConn()

    fake_pg.connect = fake_connect
    monkeypatch.setitem(sys.modules, "psycopg2", fake_pg)
    return captured


BASE = {"table": "t", "column": "v", "bucket": "1 second", "time_column": "ts"}
DAY = {"column": "ts", "start": "2024-04-01T00:00:00", "end": "2024-04-02T00:00:00"}


def test_a_series_with_no_window_is_refused_before_any_connection(door, tmp_path):
    r = gold.series(dict(BASE), _ctx(tmp_path=tmp_path))
    assert not r.ok
    assert "window" in r.errors[0]
    assert "connected_dsn" not in door, "refusal must not open a connection"


def test_the_deployment_chooses_the_database_not_the_caller(door, tmp_path):
    params = dict(BASE, window=dict(DAY), dsn="postgres://evil@attacker/db")
    r = gold.series(params, _ctx(tmp_path=tmp_path))
    assert r.ok, r.errors
    args, kwargs = door["resolve_args"]
    assert not args and not kwargs, "resolve_dsn must not see request params"
    assert door["connected_dsn"] == "dbname=deployment"


def test_a_hostile_bucket_is_widened_across_the_whole_window(door, tmp_path):
    params = dict(BASE, bucket="1 microsecond", window=dict(DAY))
    r = gold.series(params, _ctx(tmp_path=tmp_path))
    assert r.ok, r.errors
    sent = door["series_kw"]["bucket"]
    assert sent != "1 microsecond"
    widened = sg.interval_seconds(sent)
    assert widened is not None and widened >= 86400.0 / 2000  # agent max_points
    assert r.value["served"]["reduced"] is True


def test_the_served_block_leads_the_envelope(door, tmp_path):
    r = gold.series(dict(BASE, window=dict(DAY)), _ctx(tmp_path=tmp_path))
    assert r.ok
    assert next(iter(r.value)) == "served", "what was done is stated first"
    served = r.value["served"]
    assert served["requested"]["window_seconds"] == 86400.0
    assert served["returned_points"] == len(r.value["data"]["series"])


def test_the_backstop_samples_evenly_within_the_point_budget(door, tmp_path):
    # The stub returns 5000 points however the bucket was shaped — the
    # grouped-series case an estimate cannot see. An agent caller holds 2000.
    r = gold.series(dict(BASE, window=dict(DAY)), _ctx(tmp_path=tmp_path))
    assert r.ok
    pts = r.value["data"]["series"]
    assert len(pts) <= 2000
    assert pts[0]["value"] == 0.0 and pts[-1]["value"] == 4999.0, (
        "sampling keeps both ends — never the oldest slice alone"
    )
    assert r.value["served"]["reduced"] is True


def test_an_anonymous_caller_gets_the_smallest_budget(door, tmp_path):
    r = gold.series(
        dict(BASE, window=dict(DAY)), _ctx(surface=None, tmp_path=tmp_path)
    )
    assert r.ok
    assert len(r.value["data"]["series"]) <= 200


def test_a_suspended_principal_is_refused_at_the_door(door, tmp_path, monkeypatch):
    guard = sg.ServingGuard(
        sg.ServingPolicy.shipped_defaults().replace(
            suspended_principals=frozenset({"@bad"})
        )
    )
    monkeypatch.setattr(gold, "_GUARD", guard)
    r = gold.series(
        dict(BASE, window=dict(DAY)), _ctx(handle="@bad", tmp_path=tmp_path)
    )
    assert not r.ok and "suspended" in r.errors[0]
    assert "connected_dsn" not in door


def test_a_flood_from_one_anonymous_principal_is_rate_limited(door, tmp_path):
    ctx = _ctx(surface=None, handle="@flood", tmp_path=tmp_path)
    outcomes = [
        gold.series(dict(BASE, window=dict(DAY)), ctx) for _ in range(30)
    ]
    assert outcomes[0].ok
    refused = [r for r in outcomes if not r.ok]
    assert refused and "rate" in refused[0].errors[0]


def test_every_gold_verb_declares_its_cost_class():
    # The PRD metric: 100 percent of gold verbs carry a declared cost class.
    from axiom.extensions.builtins.data_platform.skills import _GOLD_SKILLS, bind

    reg = SkillRegistry()
    bind(reg)
    for verb in _GOLD_SKILLS:
        spec = reg.spec(f"data.{verb}")
        assert spec is not None
        assert spec.cost_class in ("lookup", "aggregate", "series", "scan"), (
            f"data.{verb} declares no cost class; the guard will refuse it"
        )


def test_lookup_verbs_pass_the_door_too(door, tmp_path, monkeypatch):
    monkeypatch.setattr(
        gold.gq, "roles", lambda cur, **kw: {"data": {"roles": []}, "provenance": {}}
    )
    r = gold.roles({}, _ctx(tmp_path=tmp_path))
    assert r.ok, r.errors


# ------------------------------------------------------------------ phase 3


def _phase3_guard(monkeypatch, toml_text, tmp_path):
    f = tmp_path / "serving_policy.toml"
    f.write_text(toml_text, encoding="utf-8")
    monkeypatch.setattr(gold, "_GUARD", sg.ServingGuard(sg.load_policy(f)))


def test_a_declared_required_filter_refuses_the_expensive_shape(
    door, tmp_path, monkeypatch
):
    _phase3_guard(
        monkeypatch, '[tables."t"]\nrequired_filters = ["site"]\n', tmp_path
    )
    r = gold.series(dict(BASE, window=dict(DAY)), _ctx(tmp_path=tmp_path))
    assert not r.ok and "requires a filter on site" in r.errors[0]
    assert "connected_dsn" not in door, "the refusal is cheaper than the query"


def test_the_cheap_shape_passes_filter_or_grouping(door, tmp_path, monkeypatch):
    _phase3_guard(
        monkeypatch, '[tables."t"]\nrequired_filters = ["site"]\n', tmp_path
    )
    ok1 = gold.series(
        dict(BASE, window=dict(DAY), filter="site = 'a'"), _ctx(tmp_path=tmp_path)
    )
    assert ok1.ok, ok1.errors
    ok2 = gold.series(
        dict(BASE, window=dict(DAY), group_by=["site"]), _ctx(tmp_path=tmp_path)
    )
    assert ok2.ok, ok2.errors


def test_aggregate_enforces_the_same_requirement(door, tmp_path, monkeypatch):
    _phase3_guard(
        monkeypatch, '[tables."t"]\nrequired_filters = ["site"]\n', tmp_path
    )
    monkeypatch.setattr(
        gold.gq, "aggregate", lambda cur, **kw: {"data": {"value": 1}, "provenance": {}}
    )
    r = gold.aggregate(
        {"table": "t", "column": "v", "fn": "mean"}, _ctx(tmp_path=tmp_path)
    )
    assert not r.ok and "requires a filter" in r.errors[0]


def test_a_coarse_ask_is_served_from_the_declared_rollup(door, tmp_path, monkeypatch):
    _phase3_guard(
        monkeypatch,
        '[rollups."t"]\ntable = "t_1h"\nnative_bucket = "1 hour"\n',
        tmp_path,
    )
    r = gold.series(
        dict(BASE, bucket="1 day", window=dict(DAY)), _ctx(tmp_path=tmp_path)
    )
    assert r.ok, r.errors
    assert door["series_kw"]["table"] == "t_1h"
    assert r.value["served"]["source_table"] == "t_1h"


def test_a_fine_ask_stays_on_the_raw_table(door, tmp_path, monkeypatch):
    _phase3_guard(
        monkeypatch,
        '[rollups."t"]\ntable = "t_1h"\nnative_bucket = "1 hour"\n',
        tmp_path,
    )
    r = gold.series(
        dict(BASE, bucket="1 minute", window=dict(DAY)), _ctx(tmp_path=tmp_path)
    )
    assert r.ok, r.errors
    assert door["series_kw"]["table"] == "t"
    assert "source_table" not in r.value["served"]


def test_a_reshaped_bucket_can_newly_qualify_for_the_rollup(
    door, tmp_path, monkeypatch
):
    # 1-second buckets over a day blow the agent budget; the widened bucket
    # (>= 43s per 2000 points... widened to fit 2000 points of 86400s = 43s)
    # does NOT reach 1 hour — use a wider window so the reshape crosses it.
    _phase3_guard(
        monkeypatch,
        '[rollups."t"]\ntable = "t_1h"\nnative_bucket = "1 hour"\n',
        tmp_path,
    )
    year = {"column": "ts", "start": "2023-04-01T00:00:00", "end": "2024-04-01T00:00:00"}
    r = gold.series(
        dict(BASE, bucket="1 second", window=year), _ctx(tmp_path=tmp_path)
    )
    assert r.ok, r.errors
    # 31,536,000 s / 2,000 points = 15,768 s per bucket >= 1 hour
    assert door["series_kw"]["table"] == "t_1h"
    assert r.value["served"]["reduced"] is True
