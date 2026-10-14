# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A site's local medallion loses its database, or fills its disk, mid-pass.

Two different disk-full failures, measured 2026-10-08 on TimescaleDB 2.17.2:

- WAL cannot be written (data and WAL on one volume, the archive role's
  layout): PostgreSQL PANICs and the server exits. That is the database-down
  case, below, once space is freed and it restarts.
- Table files cannot be extended while WAL still has room: the statement
  fails and the server stays up. Tested here with WAL on its own volume.
  Sessions already open keep reading; a NEW session may be refused until
  space is freed, because starting one can need to write a file.

ADR-180's lane-3 failure modes. Ingest does not depend on the database (the
edge writes bronze files), so what is at stake is the conform pass and the
readers:

- the pass FAILS, loudly, rather than reporting a clean funnel;
- everything committed before the failure stays, and stays readable;
- the next pass completes it, with every reading exactly once.

The prefix survives because a pass commits every ``checkpoint_rows`` and the
upsert is idempotent; a rerun skips what is there.
"""

from __future__ import annotations

import json

import psycopg
import pytest

from axiom.extensions.builtins.data_platform.conformance import NormalizerRegistry
from axiom.extensions.builtins.data_platform.conformance.runner import run_conform

N = 3000
CHECKPOINT = 500


def _bronze(tmp_path, n):
    from axiom.extensions.builtins.data_platform.agents.plinth.connectors import (
        ConnectorConfig,
        save_connector,
    )

    rows_dir = tmp_path / "bronze" / "loop" / "_rows" / "2026-10-08"
    rows_dir.mkdir(parents=True, exist_ok=True)
    with (rows_dir / "a.jsonl").open("w") as fh:
        for i in range(n):
            ts = f"2026-10-08T{(i // 3600) % 24:02d}:{(i // 60) % 60:02d}:{i % 60:02d}+00:00"
            fh.write(json.dumps({"item_id": str(i), "row_hash": f"rh{i}", "schema_ref": "loop/v1",
                                 "row": {"ts": ts, "t": float(i), "i": i}}) + "\n")
    save_connector(ConnectorConfig(name="loop", kind="push", site="s",
                                   bronze_root=str(tmp_path / "bronze"),
                                   params={"schema_ref": "loop/v1"}), state_dir=tmp_path)


def _registry(on_row=None):
    reg = NormalizerRegistry()

    def norm(rec):
        if on_row is not None:
            on_row(rec["row"]["i"])
        yield {"feed": "loop", "channel": "temp", "ts": rec["row"]["ts"],
               "value": rec["row"]["t"], "unit": "degC", "source_class": "measured"}

    reg.register("loop/v1", norm)
    return reg


def _run(tmp_path, dsn, reg):
    return run_conform(bronze_root=tmp_path / "bronze", dsn=dsn, state_dir=tmp_path,
                       registry=reg, batch_size=100, checkpoint_rows=CHECKPOINT)


def _counts(dsn):
    with psycopg.connect(dsn) as c:
        return c.execute(
            "SELECT count(*), count(DISTINCT row_hash) FROM silver.signals").fetchone()


def _fresh(dsn):
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute("DROP SCHEMA IF EXISTS silver CASCADE")
        c.execute("DROP SCHEMA IF EXISTS gold CASCADE")


def test_losing_the_database_mid_pass_fails_keeps_the_prefix_and_resumes(timescale_dsn, tmp_path):
    _fresh(timescale_dsn)
    _bronze(tmp_path, N)

    def kill_at(i):
        if i == 1750:
            with psycopg.connect(timescale_dsn, autocommit=True) as other:
                other.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND pid <> pg_backend_pid()")

    with pytest.raises(psycopg.Error):
        _run(tmp_path, timescale_dsn, _registry(kill_at))
    total, distinct = _counts(timescale_dsn)
    assert total == distinct
    assert total % CHECKPOINT == 0 and 0 < total <= 1750  # a committed prefix, nothing torn

    stats = _run(tmp_path, timescale_dsn, _registry())
    assert stats["rows_in"] == N
    assert _counts(timescale_dsn) == (N, N)
    assert stats["rows_changed"] == N - total  # the rerun wrote only what was missing


def test_a_full_data_disk_fails_the_pass_and_readers_keep_reading(small_disk_timescale_dsn, tmp_path):
    dsn = small_disk_timescale_dsn
    _fresh(dsn)
    _bronze(tmp_path, 1)
    _run(tmp_path, dsn, _registry())  # schema and gold views exist before the disk fills
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute("TRUNCATE silver.signals CASCADE")
        # Fill the disk with ~4.6 MB tables until it refuses, then free one:
        # room for a few thousand readings, far fewer than the pass brings.
        made = 0
        with pytest.raises(psycopg.Error, match="(?i)space"):
            while made < 200:
                c.execute(f"CREATE TABLE public.fill_{made} AS "
                          "SELECT repeat('x', 1000) AS pad FROM generate_series(1, 4000)")
                made += 1
        c.execute("DROP TABLE IF EXISTS public.fill_0")
        c.execute("CHECKPOINT")

    # A reader with a session already open, as a dashboard or the SQL role has.
    # On a FULL data disk PostgreSQL may refuse to start a NEW session ("could
    # not write init file", seen on CI 2026-10-08), so readers that connect
    # per query wait for space; a session that is open keeps answering.
    reader = psycopg.connect(dsn, autocommit=True)
    _bronze(tmp_path, 40_000)
    with pytest.raises(psycopg.Error, match="(?i)space|disk"):
        _run(tmp_path, dsn, _registry())

    # what was committed is still there and still readable
    total, distinct = reader.execute(
        "SELECT count(*), count(DISTINCT row_hash) FROM silver.signals").fetchone()
    assert total == distinct and total % CHECKPOINT == 0 and 0 < total < 40_000

    # space comes back (through the open session: a new one may be refused);
    # the next pass completes the day exactly once
    for k in range(1, made):
        reader.execute(f"DROP TABLE IF EXISTS public.fill_{k}")
    reader.execute("CHECKPOINT")
    reader.close()
    stats = _run(tmp_path, dsn, _registry())
    assert stats["rows_in"] == 40_000
    assert _counts(dsn) == (40_000, 40_000)


def test_the_conform_verb_reports_a_lost_database_as_a_failure(timescale_dsn, tmp_path):
    """Through the skill a site's nightly runs: not ok, and the cause named."""
    from axiom.extensions.builtins.data_platform.skills import conform_run

    _bronze(tmp_path, 10)

    class Ctx:
        state_dir = tmp_path

    r = conform_run.run({"dsn": "postgresql://nobody:x@127.0.0.1:1/none",
                         "bronze_root": str(tmp_path / "bronze")}, Ctx())
    assert not r.ok
    assert "conform pass failed" in " ".join(r.errors)
