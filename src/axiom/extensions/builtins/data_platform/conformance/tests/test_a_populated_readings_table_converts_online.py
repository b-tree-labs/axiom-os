# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A populated plain silver.signals converts to a compressed hypertable online.

The shape a long-running install has: key ``(row_hash, channel)`` without
``ts``, the reading-identity index, the redundant ``(site, feed, channel, ts)``
index, ``signal_uncertainty`` with a foreign key on ``(row_hash, channel)``,
and views (and views on views) reading the table. The migration must refuse
without confirmation and without a backup, change the key, replace the
foreign key with a ts-carrying key and a checked join, keep every dependent
view answering, and end with compression declared and a policy.

Also: a fresh install on TimescaleDB gets the hypertable straight away, and
the redundant index is not created where the identity index exists.
"""

from __future__ import annotations

import os
import time

import pytest

from axiom.extensions.builtins.data_platform.conformance import SILVER_DDL, pg_upsert
from axiom.extensions.builtins.data_platform.conformance.readings_migration import (
    MigrationRefused,
    backup_refusal,
    facts,
    migrate,
    orphan_terms,
)
from axiom.extensions.builtins.data_platform.conformance.timeseries import (
    ensure_readings_storage,
    is_time_partitioned,
)


def _row(i: int, *, day: int, channel="TC1", terms=None) -> dict:
    import hashlib

    ts = f"2026-09-{day:02d}T13:{(i // 60) % 60:02d}:{i % 60:02d}+00:00"
    return {
        "site": "site-a", "feed": "site-a.live", "channel": channel, "ts": ts,
        "value": float(i), "unit": "degC", "quality": "ok", "source_class": "measured",
        "schema_ref": "site-a/v1", "row_hash": hashlib.sha256(f"{ts}|{channel}".encode()).hexdigest(),
        **({"uncertainty_terms": terms} if terms else {}),
    }


@pytest.fixture
def legacy(timescale_dsn):
    import psycopg

    with psycopg.connect(timescale_dsn, autocommit=True) as c:
        c.execute("DROP SCHEMA IF EXISTS silver CASCADE")
        c.execute("DROP TABLE IF EXISTS data_platform.timeseries_adoptions CASCADE")
        c.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
        for stmt in SILVER_DDL:
            c.execute(stmt)
        c.execute("CREATE INDEX signals_site_feed_channel_ts ON silver.signals (site, feed, channel, ts)")
        up = pg_upsert(c.cursor())
        for day in (10, 11, 12):
            for i in range(40):
                up(_row(i, day=day, terms={"cal": 0.1} if i % 4 == 0 else None))
        c.execute("CREATE VIEW silver.v_latest AS SELECT channel, max(ts) AS ts FROM silver.signals GROUP BY channel")
        c.execute("CREATE VIEW silver.v_on_view AS SELECT count(*) AS n FROM silver.v_latest")
        c.execute("ANALYZE silver.signals")
        yield c
        # The container is shared: leave no hypertable behind for other suites.
        c.execute("DROP SCHEMA IF EXISTS silver CASCADE")


def test_the_live_shape_is_what_this_starts_from(legacy):
    f = facts(legacy)
    assert f.key_columns == ["row_hash", "channel"] and f.terms_fk and f.redundant_index
    assert f.identity_valid and not f.hypertable and f.dependent_views >= 1
    assert f.rows_estimate == 120, "row counts come from the table's own statistics"


def test_every_changing_phase_refuses_without_confirmation_and_a_backup(legacy, tmp_path):
    with pytest.raises(MigrationRefused, match="confirm"):
        migrate(legacy, phase="prepare")
    with pytest.raises(MigrationRefused, match="pg_dump --format=custom"):
        migrate(legacy, phase="prepare", confirm=True)
    stale = tmp_path / "old.dump"
    stale.write_bytes(b"x")
    os.utime(stale, (time.time() - 5 * 86400,) * 2)
    assert "old" in (backup_refusal(facts(legacy), str(stale), False) or "")
    assert facts(legacy).key_columns == ["row_hash", "channel"], "nothing changed"


def test_it_converts_online_keeps_every_view_and_compresses(legacy):
    before_views = legacy.execute("SELECT n FROM silver.v_on_view").fetchone()[0]
    est = migrate(legacy)  # estimate: writes nothing
    assert est["blockers"] == [] and "pg_dump" in est["dump"]

    kw = dict(confirm=True, accept_rederive=True)
    migrate(legacy, phase="prepare", **kw)
    f = facts(legacy)
    assert f.key_columns == ["row_hash", "channel", "ts"] and not f.terms_fk and not f.redundant_index
    # The conform writer still works between phases (identity key, terms).
    pg_upsert(legacy.cursor())(_row(50, day=12, terms={"cal": 0.1}))
    migrate(legacy, phase="prepare", **kw)  # idempotent

    copied = migrate(legacy, phase="copy", **kw)
    assert copied["progress"]["windows_done"] == copied["progress"]["windows_total"] > 1, "per-window copy"
    # A row written during the copy, after its window: the swap's tail catches it.
    pg_upsert(legacy.cursor())(_row(55, day=12, terms={"cal": 0.2}))

    with pytest.raises(MigrationRefused, match="pause the conform writer"):
        migrate(legacy, phase="swap", **kw)
    swapped = migrate(legacy, phase="swap", conform_paused=True, **kw)
    assert is_time_partitioned(legacy.cursor())
    assert swapped["orphan_terms"] == 0
    assert legacy.execute("SELECT count(*) FROM silver.signals").fetchone()[0] == 122
    assert legacy.execute("SELECT n FROM silver.v_on_view").fetchone()[0] == before_views
    assert legacy.execute(
        "SELECT count(*) FROM silver.signal_uncertainty WHERE ts IS NULL").fetchone()[0] == 0
    assert legacy.execute(
        "SELECT compression_enabled FROM timescaledb_information.hypertables "
        "WHERE hypertable_schema = 'silver' AND hypertable_name = 'signals'").fetchone()[0] is True
    after = legacy.execute(
        "SELECT (config->>'compress_after')::interval FROM timescaledb_information.jobs "
        "WHERE proc_name = 'policy_compression' AND hypertable_name = 'signals'").fetchone()[0]
    assert after.days == 7
    # The writer still works after the swap, terms included.
    pg_upsert(legacy.cursor())(_row(56, day=12, terms={"cal": 0.3}))
    assert orphan_terms(legacy) == 0
    migrate(legacy, phase="finish", **kw)  # idempotent


def test_orphan_terms_is_the_check_that_replaces_the_foreign_key(legacy):
    migrate(legacy, phase="prepare", confirm=True, accept_rederive=True)
    legacy.execute("DELETE FROM silver.signals WHERE ts < '2026-09-11'")
    assert orphan_terms(legacy) > 0


def test_a_fresh_install_on_timescale_is_a_compressed_hypertable(timescale_dsn):
    import psycopg

    with psycopg.connect(timescale_dsn, autocommit=True) as c:
        c.execute("DROP SCHEMA IF EXISTS silver CASCADE")
        c.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
        for stmt in SILVER_DDL:
            c.execute(stmt)
        assert c.execute("SELECT to_regclass('silver.signals_site_feed_channel_ts')").fetchone()[0] is None, \
            "the identity index exists, so its prefix is not built"
        ensure_readings_storage(c)
        assert is_time_partitioned(c.cursor())
        ensure_readings_storage(c)  # idempotent
        c.execute("DROP SCHEMA IF EXISTS silver CASCADE")


def test_a_populated_plain_table_is_left_for_the_operator(legacy):
    steps = ensure_readings_storage(legacy)
    assert not is_time_partitioned(legacy.cursor()) and "readings-hypertable" in steps[0]
