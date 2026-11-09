# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Bronze → silver → gold flow is proven per connector, and each way it can fail is told apart.

Real TimescaleDB, real bronze files, a real conform pass. A connector added
later (the "second kit" below) is held to the same proof with no change to
this module: it is read from the configured connectors, not listed here.

The five outcomes that matter are told apart because a different person fixes
each: a producer that stopped, a pipeline that is stuck, one catching up, a
connector that never flowed, and gold serving something silver does not hold.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from axiom.extensions.builtins.data_platform import pipeline_proof as pp
from axiom.extensions.builtins.data_platform.agents.plinth.connectors import (
    ConnectorConfig,
    list_connectors,
    save_connector,
)
from axiom.extensions.builtins.data_platform.conformance import NormalizerRegistry
from axiom.extensions.builtins.data_platform.conformance.runner import run_conform
from axiom.extensions.builtins.data_platform.skills import ensure_schema


@pytest.fixture
def db(timescale_dsn):
    with psycopg.connect(timescale_dsn, autocommit=True) as c:
        c.execute("DROP SCHEMA IF EXISTS silver CASCADE")
        c.execute("DROP SCHEMA IF EXISTS gold CASCADE")
        c.execute("DROP SCHEMA IF EXISTS data_platform CASCADE")
    out = ensure_schema.run({"dsn": timescale_dsn}, None)
    assert out.ok, out.errors
    return timescale_dsn


def _write(root, name, site, schema_ref, *, start, n=30, step_s=60, fname="a.jsonl", append=False):
    rows_dir = root / name / "_rows" / start.strftime("%Y-%m-%d")
    rows_dir.mkdir(parents=True, exist_ok=True)
    with (rows_dir / fname).open("a" if append else "w") as fh:
        for i in range(n):
            ts = (start + timedelta(seconds=i * step_s)).isoformat()
            fh.write(json.dumps({"item_id": f"{name}{ts}", "row_hash": f"{name}{i}{ts}",
                                 "schema_ref": schema_ref,
                                 "row": {"ts": ts, "t": float(i)}}) + "\n")


def _register(tmp_path, name, site, schema_ref):
    save_connector(ConnectorConfig(name=name, kind="push", site=site,
                                   bronze_root=str(tmp_path / "bronze"),
                                   params={"schema_ref": schema_ref}), state_dir=tmp_path)


def _registry(*refs):
    reg = NormalizerRegistry()
    for ref in refs:
        reg.register(ref, lambda rec, _r=ref: iter([{
            "feed": _r.split("/")[0], "channel": "temp", "ts": rec["row"]["ts"],
            "value": rec["row"]["t"], "unit": "degC", "source_class": "measured"}]))
    return reg


def _conform(tmp_path, dsn, reg):
    return run_conform(bronze_root=tmp_path / "bronze", dsn=dsn, state_dir=tmp_path, registry=reg)


def _prove(dsn, tmp_path, **kw):
    with psycopg.connect(dsn, autocommit=True) as c, c.cursor() as cur:
        return pp.prove(cur, list_connectors(state_dir=tmp_path), **kw)


def _by(report, connector):
    return next(f for f in report["feeds"] if f["connector"] == connector)


def _recent(minutes=30):
    return datetime.now(UTC) - timedelta(minutes=minutes)


def test_two_connectors_flow_and_a_third_added_later_is_held_to_the_same_proof(db, tmp_path):
    bronze = tmp_path / "bronze"
    for name, site, ref in (("loop", "s1", "loop/v1"), ("kit2", "s2", "kit2/v1")):
        _write(bronze, name, site, ref, start=_recent())
        _register(tmp_path, name, site, ref)
    reg = _registry("loop/v1", "kit2/v1", "kit3/v1")
    _conform(tmp_path, db, reg)

    report = _prove(db, tmp_path)
    assert _by(report, "loop")["verdict"] == pp.FLOWING, report
    assert _by(report, "kit2")["verdict"] == pp.FLOWING, report
    assert report["overall"] == "FLOWING"
    assert report["schema_current"] is True

    # A kit added afterwards: configured, bronze written, not yet conformed.
    _write(bronze, "kit3", "s3", "kit3/v1", start=_recent())
    _register(tmp_path, "kit3", "s3", "kit3/v1")
    assert _by(_prove(db, tmp_path), "kit3")["verdict"] == pp.NEVER_FLOWED
    _conform(tmp_path, db, reg)
    after = _prove(db, tmp_path)
    assert _by(after, "kit3")["verdict"] == pp.FLOWING, after
    assert after["overall"] == "FLOWING"


def test_a_producer_that_stopped_is_not_a_pipeline_fault(db, tmp_path):
    _write(tmp_path / "bronze", "loop", "s1", "loop/v1", start=_recent(minutes=300))
    _register(tmp_path, "loop", "s1", "loop/v1")
    _conform(tmp_path, db, _registry("loop/v1"))
    f = _by(_prove(db, tmp_path), "loop")
    assert f["verdict"] == pp.SOURCE_SILENT, f
    assert f["backlog_bytes"] == 0


def test_bytes_the_conform_pass_has_not_read_are_a_backlog_then_stuck(db, tmp_path):
    _write(tmp_path / "bronze", "loop", "s1", "loop/v1", start=_recent())
    _register(tmp_path, "loop", "s1", "loop/v1")
    reg = _registry("loop/v1")
    _conform(tmp_path, db, reg)
    # New bytes arrive; nothing reads them.
    _write(tmp_path / "bronze", "loop", "s1", "loop/v1", start=_recent(minutes=1), n=10,
           append=True)
    behind = _by(_prove(db, tmp_path), "loop")
    assert behind["backlog_bytes"] > 0
    assert behind["verdict"] == pp.BACKLOG, behind          # the pass moved a moment ago
    stuck = _by(_prove(db, tmp_path, stuck_after_s=0.0), "loop")
    assert stuck["verdict"] == pp.PIPELINE_STUCK, stuck      # control: the same state, judged stuck
    _conform(tmp_path, db, reg)
    assert _by(_prove(db, tmp_path), "loop")["backlog_bytes"] == 0


def test_flow_is_proven_while_a_writer_holds_a_transaction_and_the_schema_step_runs(db, tmp_path):
    """The deploy scenario end to end: nothing blocks, nothing is lost, and the idle session is named."""
    _write(tmp_path / "bronze", "loop", "s1", "loop/v1", start=_recent())
    _register(tmp_path, "loop", "s1", "loop/v1")
    reg = _registry("loop/v1")
    _conform(tmp_path, db, reg)

    writer = psycopg.connect(db, autocommit=False)
    writer.execute(
        "INSERT INTO silver.signals (site, feed, channel, ts, value, unit, source_class, "
        "schema_ref, row_hash) VALUES ('w','w','c', now(), 1.0, 'degC', 'measured', 'x/v1', "
        "md5(random()::text))"
    )
    try:
        out = ensure_schema.run({"dsn": db, "lock_timeout": "1s", "wait_s": 0}, None)
        assert out.ok, out.errors
        _write(tmp_path / "bronze", "loop", "s1", "loop/v1", start=_recent(minutes=1), n=5,
               append=True)
        _conform(tmp_path, db, reg)
        import time as _t
        _t.sleep(1.1)
        old = pp.IDLE_TXN_REPORT_S
        pp.IDLE_TXN_REPORT_S = 1
        try:
            report = _prove(db, tmp_path)
        finally:
            pp.IDLE_TXN_REPORT_S = old
    finally:
        writer.rollback()
        writer.close()
    assert _by(report, "loop")["verdict"] == pp.FLOWING, report
    assert report["schema_current"] is True
    assert any(h["state"] == "idle in transaction" for h in report["idle_in_transaction"]), report


def test_a_connector_with_no_silver_rows_and_no_bronze_is_never_flowed(db, tmp_path):
    _register(tmp_path, "empty", "s9", "empty/v1")
    f = _by(_prove(db, tmp_path), "empty")
    assert f["verdict"] == pp.NEVER_FLOWED
    assert _prove(db, tmp_path)["overall"] == "ATTENTION"


def test_gold_serving_something_silver_does_not_hold_is_called_out():
    proof = pp.FeedProof(connector="c", site="s", feed="f", verdict="", silver_points=10,
                         silver_last_ts="2026-10-22 10:00:00+00",
                         gold_last_ts="2026-10-22 09:00:00+00")
    pp._judge(proof, stuck_after_s=60)
    assert proof.verdict == pp.GOLD_DISAGREES


def test_the_skill_exits_clean_when_flowing_and_names_the_fault_when_not(db, tmp_path, monkeypatch):
    """The agent/CLI door: ok only when no connector is in a pipeline fault."""
    from axiom.extensions.builtins.data_platform.agents.plinth import connectors as conn_mod
    from axiom.extensions.builtins.data_platform.skills import pipeline_proof as skill

    monkeypatch.setattr(conn_mod, "list_connectors",
                        lambda state_dir=None: list_connectors(state_dir=tmp_path))
    _write(tmp_path / "bronze", "loop", "s1", "loop/v1", start=_recent())
    _register(tmp_path, "loop", "s1", "loop/v1")
    reg = _registry("loop/v1")
    _conform(tmp_path, db, reg)
    good = skill.run({"dsn": db}, None)
    assert good.ok, (good.errors, good.actions_taken)
    assert good.value["overall"] == "FLOWING"

    _register(tmp_path, "ghost", "s9", "ghost/v1")          # configured, never flowed
    bad = skill.run({"dsn": db}, None)
    assert not bad.ok
    assert any("ghost: NEVER_FLOWED" in e for e in bad.errors), bad.errors
    one = skill.run({"dsn": db, "connector": "loop"}, None)
    assert one.ok
