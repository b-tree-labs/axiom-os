# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A table declared as time series gets its storage shape from the declaration.

Against a real TimescaleDB (a ``tsdb-*`` container; never the developer's
database). Every test reads the state back from the catalogue afterwards,
because "contract applied" is the kind of statement that was true for a
year while a 466 GB plain table sat next to an idle TimescaleDB.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from axiom.extensions.builtins.data_platform.timeseries.contract import (
    ContractRefused,
    TimeseriesContract,
    apply,
    release_retention,
    status,
)

from .conftest import SCHEMA

D, H = timedelta(days=1), timedelta(hours=1)


def _create_readings(db, table="readings", unique=("site", "feed", "channel", "ts")):
    db.execute(
        f"CREATE TABLE {SCHEMA}.{table} (site text NOT NULL, feed text NOT NULL, "
        f"channel text NOT NULL, ts timestamptz NOT NULL, value double precision, "
        f"UNIQUE ({', '.join(unique)}))"
    )


def _contract(**over) -> TimeseriesContract:
    base = dict(schema=SCHEMA, table="readings", identity=("site", "feed", "channel"),
                rows_per_day=25_000_000)
    base.update(over)
    return TimeseriesContract(**base)


def _fill(db, rows: int, table="readings", day="2026-01-01"):
    db.execute(
        f"INSERT INTO {SCHEMA}.{table} SELECT 'a', 'f', ch, "
        f"timestamptz '{day}' + make_interval(secs => i), 20.0 + (i %% 50) * 0.01 "
        f"FROM generate_series(0, %s) i, unnest(array['c1','c2']) ch",
        (rows // 2 - 1,),
    )


def _jobs(db, table="readings") -> dict[str, dict]:
    rows = db.execute(
        "SELECT proc_name, scheduled, config FROM timescaledb_information.jobs "
        "WHERE hypertable_schema = %s AND hypertable_name = %s", (SCHEMA, table),
    ).fetchall()
    return {proc: {"scheduled": sched, "config": cfg} for proc, sched, cfg in rows}


def test_a_declared_table_becomes_a_hypertable_with_compression_and_a_policy(db):
    _create_readings(db)
    steps = apply(db, _contract())
    st = status(db, _contract())
    assert st.is_hypertable
    assert st.chunk_interval == 6 * H, "derived from 25M rows/day, not a fixed day"
    assert st.compression_enabled
    assert st.segmentby == "site,feed,channel" and st.orderby == "ts DESC"
    assert st.compress_after == 7 * D
    assert st.retain is None
    jobs = _jobs(db)
    assert "policy_compression" in jobs and "policy_retention" not in jobs
    assert any("hypertable" in s for s in steps), steps


def test_applying_twice_changes_nothing(db):
    _create_readings(db)
    apply(db, _contract())
    before = status(db, _contract())
    assert apply(db, _contract()) == []
    assert status(db, _contract()) == before


def test_a_changed_declaration_converges_and_says_what_moved(db):
    _create_readings(db)
    apply(db, _contract())
    steps = apply(db, _contract(compress_after=2 * D, retain=400 * D))
    st = status(db, _contract())
    assert st.compress_after == 2 * D and st.retain == 400 * D
    assert _jobs(db)["policy_retention"]["scheduled"] is True
    assert any("2 days" in s for s in steps) and any("400 days" in s for s in steps), steps
    # Retention withdrawn from the declaration is withdrawn from the database,
    # and reported, because a policy nobody declared is one nobody checks.
    steps = apply(db, _contract(compress_after=2 * D))
    assert status(db, _contract()).retain is None
    assert any("retention" in s and "removed" in s for s in steps), steps


def test_a_wider_chunk_in_the_declaration_reaches_new_chunks(db):
    _create_readings(db)
    apply(db, _contract())
    steps = apply(db, _contract(chunk_interval=D))
    assert status(db, _contract()).chunk_interval == D
    assert any("chunk" in s for s in steps), steps


def test_compression_settings_change_even_after_chunks_are_compressed(db):
    _create_readings(db)
    apply(db, _contract())
    _fill(db, 20_000)
    db.execute(f"SELECT compress_chunk(c) FROM show_chunks('{SCHEMA}.readings') c")
    apply(db, _contract(identity=("site", "channel")))
    st = status(db, _contract())
    assert st.segmentby == "site,channel"
    assert db.execute(f"SELECT count(*) FROM {SCHEMA}.readings").fetchone()[0] == 20_000


def test_existing_rows_survive_conversion_and_compressed_chunks_still_answer(db):
    _create_readings(db)
    _fill(db, 40_000)
    apply(db, _contract(chunk_interval=H))
    db.execute(f"SELECT compress_chunk(c, if_not_compressed => true) FROM show_chunks('{SCHEMA}.readings') c")
    before, after = db.execute(
        "SELECT before_compression_total_bytes, after_compression_total_bytes "
        f"FROM hypertable_compression_stats('{SCHEMA}.readings')"
    ).fetchone()
    assert after < before / 3, (before, after)
    assert db.execute(
        f"SELECT count(*) FROM {SCHEMA}.readings WHERE channel = 'c1'").fetchone()[0] == 20_000


def test_a_unique_index_without_the_time_column_is_refused_with_the_fix(db):
    db.execute(f"CREATE TABLE {SCHEMA}.readings (id bigserial PRIMARY KEY, ts timestamptz NOT NULL, v double precision)")
    with pytest.raises(ContractRefused) as exc:
        apply(db, _contract(identity=()))
    msg = str(exc.value)
    assert "readings_pkey" in msg and "ts" in msg
    assert not status(db, _contract()).is_hypertable


def test_a_large_plain_table_is_not_converted_in_place(db):
    """``migrate_data`` locks the table for the length of a full copy. Above
    the in-place limit the contract refuses and names the adoption verb, so a
    deploy can never stall a live writer behind a conversion."""
    _create_readings(db)
    _fill(db, 1_100_000)
    db.execute(f"ANALYZE {SCHEMA}.readings")
    with pytest.raises(ContractRefused) as exc:
        apply(db, _contract())
    assert "adopt-timeseries" in str(exc.value)
    assert not status(db, _contract()).is_hypertable


def test_a_missing_table_is_an_error_not_a_silent_skip(db):
    with pytest.raises(ContractRefused):
        apply(db, _contract(table="never_created"))


def test_guarded_retention_runs_only_once_delivery_is_confirmed(db):
    """The #1250 rule, for a table that is this node's forwarded copy: nothing
    older than the window is dropped while a batch still awaits delivery
    upstream. The retention job is installed paused and a pass that knows the
    forwarder's position releases it."""
    guarded = _contract(retain=400 * D, forward_guard=True)
    _create_readings(db)
    apply(db, guarded)
    assert _jobs(db)["policy_retention"]["scheduled"] is False
    # Installed paused, so a pass that finds undelivered batches changes
    # nothing and says nothing: it reports a change of state, not the state.
    assert release_retention(db, guarded, undelivered=2) is None
    assert _jobs(db)["policy_retention"]["scheduled"] is False
    released = release_retention(db, guarded, undelivered=0)
    assert _jobs(db)["policy_retention"]["scheduled"] is True
    assert released and "resumed" in released
    assert release_retention(db, guarded, undelivered=0) is None
    # The forwarder falls behind again: paused again, and said.
    held = release_retention(db, guarded, undelivered=3)
    assert _jobs(db)["policy_retention"]["scheduled"] is False
    assert held and "3" in held and "paused" in held


def test_unguarded_retention_runs_from_the_start(db):
    """Negative control for the guard."""
    _create_readings(db)
    apply(db, _contract(retain=400 * D))
    assert _jobs(db)["policy_retention"]["scheduled"] is True
    assert release_retention(db, _contract(retain=400 * D), undelivered=5) is None
    assert _jobs(db)["policy_retention"]["scheduled"] is True


def test_ensure_schema_applies_every_declared_contract(timescale_dsn, db, monkeypatch):
    from axiom.extensions.builtins.data_platform.skills import ensure_schema
    from axiom.extensions.builtins.data_platform.timeseries import config as ts_config

    _create_readings(db)
    _create_readings(db, table="events")
    declared = [_contract(), _contract(table="events", identity=("site",), rows_per_day=1000)]
    monkeypatch.setattr(ts_config, "configured_contracts", lambda: declared)

    class _Ctx:
        state_dir = None

    r = ensure_schema.run({"dsn": timescale_dsn}, _Ctx())
    assert r.ok, r.errors
    assert status(db, declared[0]).is_hypertable and status(db, declared[1]).is_hypertable
    assert status(db, declared[1]).chunk_interval == 30 * D
    assert any(f"{SCHEMA}.events" in a for a in r.actions_taken), r.actions_taken
    assert r.value["timeseries"], r.value
    # Idempotent through the skill too.
    again = ensure_schema.run({"dsn": timescale_dsn}, _Ctx())
    assert again.ok and again.value["timeseries"] == []


def test_ensure_schema_with_nothing_declared_touches_no_table(timescale_dsn, db, monkeypatch):
    """Negative control: declared, never discovered."""
    from axiom.extensions.builtins.data_platform.skills import ensure_schema
    from axiom.extensions.builtins.data_platform.timeseries import config as ts_config

    _create_readings(db)
    monkeypatch.setattr(ts_config, "configured_contracts", list)

    class _Ctx:
        state_dir = None

    r = ensure_schema.run({"dsn": timescale_dsn}, _Ctx())
    assert r.ok, r.errors
    assert not status(db, _contract()).is_hypertable


def test_ensure_schema_fails_loudly_when_a_declared_contract_cannot_be_applied(
    timescale_dsn, db, monkeypatch
):
    """A release whose declared storage shape silently does not happen is the
    failure class ensure-schema exists to close."""
    from axiom.extensions.builtins.data_platform.skills import ensure_schema
    from axiom.extensions.builtins.data_platform.timeseries import config as ts_config

    monkeypatch.setattr(ts_config, "configured_contracts", lambda: [_contract(table="absent")])

    class _Ctx:
        state_dir = None

    r = ensure_schema.run({"dsn": timescale_dsn}, _Ctx())
    assert not r.ok
    assert "absent" in " ".join(r.errors)


def test_the_test_database_runs_no_background_jobs(db):
    """The guard for the deadlock this suite hit on CI (2026-10-16): a policy
    job installed by one test ran while the next test's fixture dropped the
    schema, and the two took the job's advisory lock and the hypertable's
    lock in opposite orders. The test containers start with no background
    workers, so no job can run, and every test here reads the job catalogue
    and drives compression or retention itself."""
    assert db.execute("SHOW timescaledb.max_background_workers").fetchone()[0] == "0"
    running = db.execute(
        "SELECT count(*) FROM pg_stat_activity WHERE backend_type LIKE 'TimescaleDB%'"
    ).fetchone()[0]
    assert running == 0
