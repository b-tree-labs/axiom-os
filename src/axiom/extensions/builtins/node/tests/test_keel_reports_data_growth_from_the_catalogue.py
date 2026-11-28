# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""KEEL's data-growth report: tables that grow without a bound, judged from the catalogue.

The fake catalogue below is shaped like a real node's database: a large plain
readings table with no compression and three overlapping indexes, a ledger
rewritten far more often than it is appended to, a compressed hypertable, and
an audit table with no limit.
"""

from __future__ import annotations

import logging
import os
import re
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from axiom.extensions.builtins.node import data_growth as dg
from axiom.extensions.builtins.node.skills import bind
from axiom.extensions.builtins.node.skills import data_growth as skill
from axiom.infra.skill_chain import plan_digest
from axiom.infra.skills import SkillContext, SkillRegistry, SkillResult, SkillSpec

GB = dg.GB
DAY = dg.DAY
MANIFEST = Path(dg.__file__).parent / "axiom-extension.toml"
#: Read at import: the test environment fixture clears the process env per test.
_PG_DSN = os.environ.get("AXIOM_TEST_PG_DSN", "")


class FakeCatalog:
    def __init__(self, tables, indexes=(), db_bytes=325 * GB, data_dir="/data/pg"):
        self._t, self._i, self._db, self._dir = list(tables), list(indexes), db_bytes, data_dir

    def tables(self):
        return [dg.TableStat(**vars(t)) for t in self._t]

    def indexes(self):
        return list(self._i)

    def database_bytes(self):
        return self._db

    def data_directory(self):
        return self._dir

    def stats_reset(self):
        return "2026-01-01T00:00:00+00:00"


def _node():
    readings = dg.TableStat("store", "readings", total_bytes=201 * GB, table_bytes=86 * GB,
                            index_bytes=115 * GB, live_rows=290_000_000, inserts=290_000_000)
    ledger = dg.TableStat("store", "file_ledger", total_bytes=9 * GB, table_bytes=6 * GB,
                          index_bytes=3 * GB, live_rows=21_800_000, dead_rows=900_000,
                          inserts=21_800_000, updates=4_100_000_000, hot_updates=1_000_000_000)
    hyper = dg.TableStat("store", "events", total_bytes=20 * GB, table_bytes=15 * GB,
                         index_bytes=5 * GB, hypertable=True, compression=True,
                         compress_after="7d", chunk_bytes_per_day=0.2 * GB)
    audit = dg.TableStat("auth", "identity_audit", total_bytes=18 * GB, table_bytes=14 * GB,
                         index_bytes=4 * GB, inserts=50_000_000)
    small = dg.TableStat("store", "lookup", total_bytes=10_000_000, inserts=10)
    idx = [
        dg.IndexStat("store", "readings", "readings_pkey", ("id",), unique=True, primary=True,
                     bytes=10 * GB, scans=5),
        dg.IndexStat("store", "readings", "readings_ch_ts", ("channel", "ts"), bytes=40 * GB,
                     scans=100),
        dg.IndexStat("store", "readings", "readings_ch", ("channel",), bytes=30 * GB, scans=0),
        dg.IndexStat("store", "readings", "readings_ch_src", ("channel", "source"),
                     bytes=35 * GB, scans=3),
    ]
    return [readings, ledger, hyper, audit, small], idx


def _usage(free):
    return lambda path: SimpleNamespace(total=2000 * GB, used=2000 * GB - free, free=free)


def _report(tmp_path, limits=None, free=1300 * GB, store=True, **kw):
    tables, idx = _node()
    return dg.report(FakeCatalog(tables, idx),
                     dg.SampleStore.under(tmp_path) if store else None,
                     limits or {}, disk_usage=_usage(free), **kw)


def _codes(rep):
    return {(f["code"], f["table"]) for f in rep["findings"]}


def test_every_table_is_reported_with_its_metadata(tmp_path):
    rep = _report(tmp_path)
    by = {t["table"]: t for t in rep["tables"]}
    assert set(by) == {"store.readings", "store.file_ledger", "store.events",
                       "auth.identity_audit", "store.lookup"}
    r = by["store.readings"]
    assert (r["total_bytes"], r["index_bytes"], r["hypertable"], r["compression"]) == (
        201 * GB, 115 * GB, False, False)
    assert by["store.events"]["compress_after"] == "7d"
    assert by["store.events"]["growth_from"] == "chunks"
    assert rep["volume"]["path"] == "/data/pg" and rep["volume"]["free_bytes"] == 1300 * GB
    assert rep["stats_since"]


def test_a_growing_table_with_no_bound_breaks_the_rule(tmp_path):
    codes = _codes(_report(tmp_path))
    assert ("unbounded-growth", "store.readings") in codes
    assert ("unbounded-growth", "auth.identity_audit") in codes
    # a compressed hypertable is bounded; a small table is not judged
    assert ("unbounded-growth", "store.events") not in codes
    assert not any(t == "store.lookup" for _, t in codes)


def test_a_declared_bound_or_a_static_table_satisfies_the_rule(tmp_path):
    limits = {
        "auth.identity_audit": dg.Limit("auth.identity_audit", retain="365d"),
        "store.readings": dg.Limit("store.readings", grows=False),
    }
    codes = _codes(_report(tmp_path, limits))
    assert ("unbounded-growth", "auth.identity_audit") not in codes
    assert ("unbounded-growth", "store.readings") not in codes


def test_a_table_over_its_declared_row_limit_is_a_finding(tmp_path):
    limits = {"store.file_ledger": dg.Limit("store.file_ledger", max_rows=1_000_000)}
    assert ("over-row-limit", "store.file_ledger") in _codes(_report(tmp_path, limits))


def test_the_fix_is_a_plan_bound_to_its_capability(tmp_path):
    f = next(f for f in _report(tmp_path)["findings"]
             if f["code"] == "unbounded-growth" and f["table"] == "store.readings")
    fix = f["fix"]
    assert fix["capability"] == "data.adopt_timeseries"
    assert fix["plan_hash"] == plan_digest("data.adopt_timeseries", {"table": "store.readings"})
    assert fix["steps"] and "never" in fix["applies"]


def test_update_churn_index_weight_and_overlaps_are_flagged(tmp_path):
    rep = _report(tmp_path)
    codes = _codes(rep)
    assert ("update-churn", "store.file_ledger") in codes
    assert ("update-churn", "store.readings") not in codes
    assert ("index-heavy", "store.readings") in codes
    overlaps = [f for f in rep["findings"] if f["code"] == "overlapping-indexes"]
    assert len(overlaps) == 3  # three indexes on (channel, ...): three pairs
    prefix = next(f for f in overlaps if "readings_ch " in f["message"] + " "
                  and "readings_ch_ts" in f["message"])
    assert prefix["fix"]["sql"] == ["DROP INDEX CONCURRENTLY store.readings_ch"]
    other = next(f for f in overlaps if "readings_ch_src" in f["message"]
                 and "readings_ch_ts" in f["message"])
    assert other["fix"]["sql"] == []  # neither is a prefix of the other: review, not drop
    heavy = next(f for f in rep["findings"] if f["code"] == "index-heavy")
    assert heavy["fix"]["sql"] == ["DROP INDEX CONCURRENTLY store.readings_ch"]  # never scanned


def test_growth_per_day_comes_from_stored_samples(tmp_path):
    tables, idx = _node()
    store = dg.SampleStore.under(tmp_path)
    t0 = 1_800_000_000.0
    dg.report(FakeCatalog(tables, idx), store, {}, disk_usage=_usage(1300 * GB), now=t0)
    tables[0].total_bytes += 2 * GB
    rep = dg.report(FakeCatalog(tables, idx, db_bytes=327 * GB), store, {},
                    disk_usage=_usage(1300 * GB), now=t0 + 2 * DAY)
    r = next(t for t in rep["tables"] if t["table"] == "store.readings")
    assert r["growth_from"] == "samples" and r["bytes_per_day"] == pytest.approx(1 * GB)
    assert rep["volume"]["bytes_per_day"] == pytest.approx(1 * GB)
    assert rep["volume"]["days_to_full"] == pytest.approx(1300)


def test_samples_closer_than_twelve_hours_give_no_rate(tmp_path):
    store = dg.SampleStore.under(tmp_path)
    store.record(1000.0, {"a.b": 100})
    assert store.rate("a.b", 1000.0 + 3600, 200) is None


def test_a_volume_near_full_is_a_critical_finding(tmp_path):
    tables, idx = _node()
    store = dg.SampleStore.under(tmp_path)
    t0 = 1_800_000_000.0
    dg.report(FakeCatalog(tables, idx), store, {}, disk_usage=_usage(10 * GB), now=t0)
    rep = dg.report(FakeCatalog(tables, idx, db_bytes=327 * GB), store, {},
                    disk_usage=_usage(10 * GB), now=t0 + 2 * DAY)
    vol = rep["findings"][0]
    assert vol["code"] == "volume-runway" and vol["severity"] == "critical"


def test_an_invisible_volume_is_said_not_guessed(tmp_path):
    tables, idx = _node()

    def gone(path):
        raise OSError("no such path")

    rep = dg.report(FakeCatalog(tables, idx), None, {}, disk_usage=gone)
    assert rep["volume"] is None and rep["notes"]


def test_one_alert_per_cause_not_per_table(tmp_path):
    alerts = dg.alerts(_report(tmp_path)["findings"])
    codes = [a["code"] for a in alerts]
    assert sorted(codes) == ["unbounded-growth", "update-churn"]  # info findings do not page
    unbounded = next(a for a in alerts if a["code"] == "unbounded-growth")
    assert unbounded["dedup_key"] == "node.data_growth:unbounded-growth"
    assert set(unbounded["tables"]) == {"store.readings", "store.file_ledger", "auth.identity_audit"}


def test_the_skill_alerts_through_herald_and_never_acts(tmp_path):
    sent: list[dict] = []
    reg = SkillRegistry()
    bind(reg)
    reg.register_skill(
        SkillSpec(name="notifications.alert", fn=lambda p, c: sent.append(p) or SkillResult(
            ok=True, value={}), description="alert", surfaces=("cli",),
            risk="reversible-write", side_effects=True),
        mutating=True,
    )
    ctx = SkillContext(registry=reg, state_dir=tmp_path, logger=logging.getLogger("t"))
    tables, idx = _node()
    res = skill.run({"_gateway": None, "alert": True, "_catalog": FakeCatalog(tables, idx), "_limits": {},
                     "_disk_usage": _usage(1300 * GB)}, ctx)
    assert res.ok
    assert len(sent) == 2 and {s["dedup_key"] for s in sent} == {
        "node.data_growth:unbounded-growth", "node.data_growth:update-churn"}
    assert all(s["actor"] == "@keel:local" for s in sent)


def test_the_postgres_catalogue_never_reads_table_rows():
    queries = dg.PostgresCatalog.queries()
    assert len(queries) >= 10
    for sql in queries:
        low = sql.lower()
        assert "count(" not in low and "min(" not in low and "max(" not in low
        for rel in re.findall(r"\b(?:from|join)\s+([a-z_.]+)", low):
            assert rel.startswith((
                "pg_", "timescaledb_information.", "unnest", "hypertable_detailed_size",
                "chunks_detailed_size",
            )), rel


def test_keel_does_not_import_another_agents_code():
    """Declarations come from PLINTH through the registry, not by import."""
    for path in (Path(dg.__file__), Path(skill.__file__)):
        assert "builtins.data_platform" not in path.read_text(), path


def _backup(**kw):
    return {"policy": True, "enabled": True, "exclude_table_data": [], **kw}


def test_a_table_left_out_of_the_dump_and_not_rederivable_is_flagged(tmp_path):
    rep = _report(tmp_path, backup=_backup(exclude_table_data=["store.read*"]))
    by = {t["table"]: t["backup"] for t in rep["tables"]}
    assert by["store.readings"] == "excluded" and by["store.events"] == "dumped"
    assert ("not-backed-up", "store.readings") in _codes(rep)
    assert ("not-backed-up", "store.events") not in _codes(rep)


def test_a_rederivable_table_may_be_left_out_of_the_dump(tmp_path):
    limits = {"store.readings": dg.Limit("store.readings", rederivable=True)}
    rep = _report(tmp_path, limits, backup=_backup(exclude_table_data=["store.readings"]))
    assert ("not-backed-up", "store.readings") not in _codes(rep)


def test_no_enabled_backup_flags_every_large_table_in_one_alert(tmp_path):
    rep = _report(tmp_path, backup={"policy": False, "enabled": False})
    alerts = [a for a in dg.alerts(rep["findings"]) if a["code"] == "not-backed-up"]
    assert len(alerts) == 1 and len(alerts[0]["tables"]) == 4


def test_keel_reads_plinths_rules_through_the_registry(tmp_path):
    calls: list[str] = []
    reg = SkillRegistry()
    bind(reg)

    def rules(params, ctx):
        calls.append("data.storage_rules")
        return SkillResult(ok=True, value={
            "rules": [{"table": "store.readings", "retain": "30d", "rederivable": True}],
            "backup": _backup(exclude_table_data=["store.readings"]),
        })

    reg.register_skill(SkillSpec(name="data.storage_rules", fn=rules, description="rules",
                                 surfaces=("cli",), risk="read", side_effects=False))
    ctx = SkillContext(registry=reg, state_dir=tmp_path, logger=logging.getLogger("t"))
    tables, idx = _node()
    res = skill.run({"_gateway": None, "_catalog": FakeCatalog(tables, idx), "_disk_usage": _usage(1300 * GB)}, ctx)
    assert calls == ["data.storage_rules"]
    codes = _codes(res.value)
    assert ("unbounded-growth", "store.readings") not in codes
    assert ("not-backed-up", "store.readings") not in codes


def test_without_plinth_keel_says_so_and_judges_everything_undeclared(tmp_path):
    reg = SkillRegistry()
    bind(reg)
    ctx = SkillContext(registry=reg, state_dir=tmp_path, logger=logging.getLogger("t"))
    tables, idx = _node()
    res = skill.run({"_gateway": None, "_catalog": FakeCatalog(tables, idx), "_disk_usage": _usage(1300 * GB)}, ctx)
    assert res.ok and any("PLINTH" in n for n in res.value["notes"])


def test_declared_limits_parse_and_default():
    got = dg.limits_from_entries([
        {"schema": "auth", "table": "identity_audit", "retain": "365d"},
        {"table": "state", "grows": False},
        {"table": "ledger", "max_rows": "1000"},
        {"schema": "x"},  # malformed: no table
    ])
    assert got["auth.identity_audit"].bounded
    assert not got["public.state"].grows
    assert got["public.ledger"].max_rows == 1000
    assert len(got) == 3


def test_the_daily_schedule_runs_the_report_with_alerts():
    sched = tomllib.loads(MANIFEST.read_text())["extension"]["schedule"]
    s = next(s for s in sched if s["action"] == "node.data_growth")
    assert s["cadence"]["kind"] == "cron" and s["params"] == {"alert": True}


@pytest.mark.integration
@pytest.mark.skipif(not _PG_DSN, reason="needs AXIOM_TEST_PG_DSN")
def test_the_report_runs_against_a_real_postgres(tmp_path):  # pragma: no cover - Postgres only
    """A disposable schema in the lane's own database: a growing table with overlapping
    indexes and a churning table, read back from the real catalogue."""
    import time as _time

    import psycopg

    from axiom.extensions.builtins.node.skills import data_retention

    with psycopg.connect(_PG_DSN, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS keel_dg_test CASCADE")
        conn.execute("CREATE SCHEMA keel_dg_test")
        try:
            conn.execute("CREATE TABLE keel_dg_test.readings (id bigserial PRIMARY KEY, "
                         "channel int, source int, ts timestamptz default now(), v float8)")
            conn.execute("INSERT INTO keel_dg_test.readings (channel, source, v) "
                         "SELECT g % 50, g % 7, random() FROM generate_series(1, 20000) g")
            conn.execute("CREATE INDEX readings_ch ON keel_dg_test.readings (channel)")
            conn.execute("CREATE INDEX readings_ch_ts ON keel_dg_test.readings (channel, ts)")
            conn.execute("CREATE TABLE keel_dg_test.ledger (id int PRIMARY KEY, state int)")
            conn.execute("INSERT INTO keel_dg_test.ledger SELECT g, 0 FROM generate_series(1, 100) g")
            for _ in range(30):
                conn.execute("UPDATE keel_dg_test.ledger SET state = state + 1")
            _time.sleep(1.0)  # let the statistics collector flush
            conn.execute("SELECT pg_stat_force_next_flush()") if conn.info.server_version >= 150000 else None
            cat = dg.PostgresCatalog(conn.cursor())
            th = dg.Thresholds(min_bytes=0, churn_min_updates=1000, churn_ratio=20)
            rep = dg.report(cat, dg.SampleStore.under(tmp_path), {}, th=th)
            by = {t["table"]: t for t in rep["tables"]}
            r = by["keel_dg_test.readings"]
            assert r["total_bytes"] > 0 and r["index_bytes"] > 0 and r["inserts"] == 20000
            assert r["hypertable"] is False and rep["database_bytes"] > 0
            codes = {(f["code"], f["table"]) for f in rep["findings"]}
            assert ("overlapping-indexes", "keel_dg_test.readings") in codes
            assert ("unbounded-growth", "keel_dg_test.readings") in codes
            assert ("update-churn", "keel_dg_test.ledger") in codes
            # an approved churn fix applies on a real connection (allowlisted statement)
            fix = next(f for f in rep["findings"] if f["code"] == "update-churn")["fix"]
            res = data_retention.apply({**fix["params"], "_conn": conn}, None)
            assert res.ok
            opts = conn.execute("SELECT reloptions FROM pg_class WHERE oid = "
                                "'keel_dg_test.ledger'::regclass").fetchone()[0]
            assert opts == ["fillfactor=80"]
        finally:
            conn.execute("DROP SCHEMA keel_dg_test CASCADE")
