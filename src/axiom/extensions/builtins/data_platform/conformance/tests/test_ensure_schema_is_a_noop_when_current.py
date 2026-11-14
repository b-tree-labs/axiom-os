# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A deploy's schema step takes no lock when the schema is already current.

Found 2026-10-22 deploying site 1.6.119 to a node whose conform loops keep a
transaction open on ``silver.signals``: ``axi data ensure-schema`` failed twice
with ``canceling statement due to lock timeout``. The statements it ran were all
no-ops (``ADD COLUMN IF NOT EXISTS``, ``CREATE OR REPLACE VIEW``), but Postgres
takes ACCESS EXCLUSIVE on the table before it checks, so any writer with an
uncommitted INSERT defeated a deploy that had nothing to change. The same class
failed v1.6.80 twice.

The rule these tests hold: **ensure-schema is a read unless the code's DDL
changed.** When it must change something it waits for a gap between the
writer's batches instead of failing on the first lock, and when it still cannot
it names the session in the way, with how long it has been there.

Real server, a real second connection with a real uncommitted INSERT.
"""

from __future__ import annotations

import threading
import time

import psycopg
import pytest

from axiom.extensions.builtins.data_platform.conformance import (
    GOLD_SIGNALS_DDL,
    SILVER_DDL,
    schema_state,
)
from axiom.extensions.builtins.data_platform.skills import ensure_schema

STATEMENTS = [s for s in (*SILVER_DDL, *GOLD_SIGNALS_DDL) if "CREATE INDEX" not in s.upper()]


@pytest.fixture
def current(timescale_dsn):
    """A database brought up by ensure-schema once, so it is current."""
    with psycopg.connect(timescale_dsn, autocommit=True) as c:
        c.execute("DROP SCHEMA IF EXISTS silver CASCADE")
        c.execute("DROP SCHEMA IF EXISTS gold CASCADE")
    out = ensure_schema.run({"dsn": timescale_dsn, "lock_timeout": "5s"}, None)
    assert out.ok, out.errors
    return timescale_dsn


def _open_writer(dsn):
    """A session with an uncommitted INSERT: what a conform batch looks like between statements."""
    w = psycopg.connect(dsn, autocommit=False)
    w.execute(
        "INSERT INTO silver.signals (site, feed, channel, ts, value, unit, source_class, "
        "schema_ref, row_hash) VALUES ('s','f','c', now(), 1.0, 'degC', 'measured', 'x/v1', "
        "md5(random()::text))"
    )
    return w


def test_a_current_schema_is_a_read_not_a_lock(current):
    writer = _open_writer(current)
    try:
        t0 = time.monotonic()
        out = ensure_schema.run({"dsn": current, "lock_timeout": "1s", "wait_s": 0}, None)
        took = time.monotonic() - t0
    finally:
        writer.rollback()
        writer.close()
    assert out.ok, out.errors
    assert took < 1.0, f"took {took:.2f}s: it waited on a lock it should never have asked for"
    assert any("already current" in a for a in out.actions_taken), out.actions_taken
    assert out.value["columns_added"] == []


def test_the_first_run_on_an_empty_database_still_builds_everything(timescale_dsn):
    with psycopg.connect(timescale_dsn, autocommit=True) as c:
        c.execute("DROP SCHEMA IF EXISTS silver CASCADE")
        c.execute("DROP SCHEMA IF EXISTS gold CASCADE")
    out = ensure_schema.run({"dsn": timescale_dsn}, None)
    assert out.ok, out.errors
    with psycopg.connect(timescale_dsn) as c:
        assert c.execute("SELECT to_regclass('silver.signals')").fetchone()[0]
        assert c.execute("SELECT to_regclass('gold.signals')").fetchone()[0]
        assert c.execute("SELECT count(*) FROM silver.schema_state").fetchone()[0] == 1


def test_a_missing_column_is_drift_even_when_the_stamp_matches(current):
    """The stamp says 'applied' but someone dropped a column: it must repair, not trust."""
    with psycopg.connect(current, autocommit=True) as c:
        c.execute("DROP VIEW IF EXISTS gold.ingest_stale, gold.ingest_freshness, gold.signals_latest, "
                  "gold.signals_uncertainty, gold.uncertainty_coverage, gold.uncertainty_sources, "
                  "gold.uncertainty_budget, gold.signals")
        c.execute("ALTER TABLE silver.signals DROP COLUMN derivation")
    out = ensure_schema.run({"dsn": current}, None)
    assert out.ok, out.errors
    assert "derivation" in out.value["columns_added"]


def test_a_changed_ddl_waits_for_a_gap_between_the_writers_batches(current, monkeypatch):
    """Drift + a writer that commits after 2s: the step waits, then succeeds."""
    monkeypatch.setattr(
        schema_state, "fingerprint", lambda statements: "forced-drift-" + str(time.time())
    )
    writer = _open_writer(current)
    threading.Timer(2.0, lambda: (writer.commit(), writer.close())).start()
    t0 = time.monotonic()
    out = ensure_schema.run({"dsn": current, "lock_timeout": "1s", "wait_s": 30}, None)
    took = time.monotonic() - t0
    assert out.ok, out.errors
    assert 1.5 <= took < 25, f"took {took:.2f}s"


def test_a_writer_that_never_commits_is_named_not_just_timed_out(current, monkeypatch):
    monkeypatch.setattr(schema_state, "fingerprint", lambda statements: "forced-drift-2")
    writer = _open_writer(current)
    try:
        out = ensure_schema.run({"dsn": current, "lock_timeout": "1s", "wait_s": 4}, None)
    finally:
        writer.rollback()
        writer.close()
    assert not out.ok
    msg = " ".join(out.errors)
    assert "idle in transaction" in msg, msg
    assert "pid" in msg, msg
    assert "silver.signals" in msg, msg


def test_the_fingerprint_moves_when_the_ddl_moves():
    a = schema_state.fingerprint(STATEMENTS)
    assert a == schema_state.fingerprint(list(STATEMENTS))
    assert a != schema_state.fingerprint([*STATEMENTS, "SELECT 1"])


def test_control_the_unguarded_ddl_does_fail_against_the_same_writer(current):
    """Negative control: without the stamp check the same writer defeats the same no-op DDL.

    This is the failure of 2026-10-22 reproduced on a real server. If this test
    ever passes without an error, Postgres stopped taking the lock early and the
    guard above is no longer what makes the schema step safe.
    """
    from axiom.extensions.builtins.data_platform.conformance import apply_conformance_ddl

    writer = _open_writer(current)
    try:
        with psycopg.connect(current, autocommit=True) as c:
            c.execute("SET lock_timeout = '1s'")
            with pytest.raises(psycopg.errors.LockNotAvailable):
                apply_conformance_ddl(c, STATEMENTS)
    finally:
        writer.rollback()
        writer.close()


def _bronze_rows(tmp_path, n=40):
    import json

    from axiom.extensions.builtins.data_platform.agents.plinth.connectors import (
        ConnectorConfig,
        save_connector,
    )

    rows_dir = tmp_path / "bronze" / "loop" / "_rows" / "2026-10-22"
    rows_dir.mkdir(parents=True)
    with (rows_dir / "a.jsonl").open("w") as fh:
        for i in range(n):
            ts = f"2026-10-22T10:00:{i % 60:02d}+00:00"
            fh.write(json.dumps({"item_id": str(i), "row_hash": f"rh{i}", "schema_ref": "loop/v1",
                                 "row": {"ts": ts, "t": float(i)}}) + "\n")
    save_connector(ConnectorConfig(name="loop", kind="push", site="s",
                                   bronze_root=str(tmp_path / "bronze"),
                                   params={"schema_ref": "loop/v1"}), state_dir=tmp_path)


def test_a_conform_pass_does_not_wait_on_a_writer_when_the_schema_is_current(current, tmp_path):
    """The conform loops ran the same DDL at the top of every pass; so they queued on each other."""
    from axiom.extensions.builtins.data_platform.conformance import NormalizerRegistry
    from axiom.extensions.builtins.data_platform.conformance.runner import run_conform

    _bronze_rows(tmp_path)
    reg = NormalizerRegistry()
    reg.register("loop/v1", lambda rec: iter([{
        "feed": "loop", "channel": "temp", "ts": rec["row"]["ts"],
        "value": rec["row"]["t"], "unit": "degC", "source_class": "measured"}]))

    def connect(dsn):
        c = psycopg.connect(dsn)
        c.execute("SET lock_timeout = '1500ms'")
        return c

    # One pass first so the stamp exists, then hold a writer open and pass again.
    run_conform(bronze_root=tmp_path / "bronze", dsn=current, state_dir=tmp_path,
                registry=reg, connect=connect)
    writer = _open_writer(current)
    try:
        t0 = time.monotonic()
        stats = run_conform(bronze_root=tmp_path / "bronze", dsn=current, state_dir=tmp_path,
                            registry=reg, connect=connect)
        took = time.monotonic() - t0
    finally:
        writer.rollback()
        writer.close()
    assert stats["applied"] is True
    assert took < 10, f"the pass took {took:.1f}s"
