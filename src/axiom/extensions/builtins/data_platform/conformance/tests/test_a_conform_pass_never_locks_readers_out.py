# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A conform pass never locks readers out of silver, and commits as it goes.

Found 2026-10-08 running the archive role on a producing site's real day: the
nightly conform ran the schema DDL inside the same transaction as the whole
pass. Even a no-op ``ALTER TABLE .. ADD COLUMN IF NOT EXISTS`` takes an ACCESS
EXCLUSIVE lock, held until the single commit at the end, so for the whole
pass (18 minutes and counting on the low-power profile) every reader queued:
the read-only SQL role, the CLI, MCP, a plain ``count(*)``. And
``--checkpoint-rows`` / ``--batch-size`` were parsed and then dropped.

Each test reads silver from a SECOND connection in the middle of a real pass,
with a short lock_timeout, so a held lock fails the test instead of hanging it.
"""

from __future__ import annotations

import json

import psycopg
import pytest

from axiom.extensions.builtins.data_platform.conformance import NormalizerRegistry
from axiom.extensions.builtins.data_platform.conformance.runner import run_conform


@pytest.fixture
def fresh(timescale_dsn):
    with psycopg.connect(timescale_dsn, autocommit=True) as c:
        c.execute("DROP SCHEMA IF EXISTS silver CASCADE")
        c.execute("DROP SCHEMA IF EXISTS gold CASCADE")
    return timescale_dsn


def _bronze(tmp_path, n):
    from axiom.extensions.builtins.data_platform.agents.plinth.connectors import (
        ConnectorConfig,
        save_connector,
    )

    rows_dir = tmp_path / "bronze" / "loop" / "_rows" / "2026-10-08"
    rows_dir.mkdir(parents=True)
    with (rows_dir / "a.jsonl").open("w") as fh:
        for i in range(n):
            ts = f"2026-10-08T10:{(i // 60) % 60:02d}:{i % 60:02d}+00:00"
            fh.write(json.dumps({"item_id": str(i), "row_hash": f"rh{i}", "schema_ref": "loop/v1",
                                 "row": {"ts": ts, "t": float(i), "i": i}}) + "\n")
    save_connector(ConnectorConfig(name="loop", kind="push", site="s",
                                   bronze_root=str(tmp_path / "bronze"),
                                   params={"schema_ref": "loop/v1"}), state_dir=tmp_path)


def _registry(dsn, at, seen):
    """A normalizer that, at reading ``at``, reads silver from another connection."""
    reg = NormalizerRegistry()

    def norm(rec):
        if rec["row"]["i"] == at:
            with psycopg.connect(dsn, autocommit=True) as other:
                other.execute("SET lock_timeout = '2s'")
                seen.append(other.execute("SELECT count(*) FROM silver.signals").fetchone()[0])
        yield {"feed": "loop", "channel": "temp", "ts": rec["row"]["ts"],
               "value": rec["row"]["t"], "unit": "degC", "source_class": "measured"}

    reg.register("loop/v1", norm)
    return reg


def _run(tmp_path, dsn, reg, **kw):
    return run_conform(bronze_root=tmp_path / "bronze", dsn=dsn, state_dir=tmp_path,
                       registry=reg, **kw)


def test_a_reader_is_not_blocked_by_a_first_pass(fresh, tmp_path):
    """The first pass creates the schema: the DDL commits before the rows."""
    _bronze(tmp_path, 300)
    seen: list[int] = []
    stats = _run(tmp_path, fresh, _registry(fresh, 150, seen), batch_size=50, checkpoint_rows=100)
    assert stats["rows_out"] == 300
    assert len(seen) == 1  # the read answered instead of timing out


def test_a_reader_is_not_blocked_by_a_later_pass(fresh, tmp_path):
    """The case that bit: the schema already exists and the DDL is a no-op ALTER."""
    _bronze(tmp_path, 300)
    _run(tmp_path, fresh, _registry(fresh, -1, []))
    seen: list[int] = []
    _run(tmp_path, fresh, _registry(fresh, 150, seen), rederive=True)
    assert seen == [300]


def test_a_pass_commits_every_checkpoint(fresh, tmp_path):
    _bronze(tmp_path, 300)
    seen: list[int] = []
    _run(tmp_path, fresh, _registry(fresh, 250, seen), batch_size=50, checkpoint_rows=100)
    # rows 0..199 were committed at the second checkpoint; 200..249 not yet
    assert seen == [200]


def test_without_checkpoints_nothing_is_visible_until_the_end(fresh, tmp_path):
    _bronze(tmp_path, 300)
    seen: list[int] = []
    stats = _run(tmp_path, fresh, _registry(fresh, 250, seen), batch_size=50, checkpoint_rows=0)
    assert seen == [0] and stats["rows_out"] == 300


def test_a_dry_run_still_writes_nothing(fresh, tmp_path):
    _bronze(tmp_path, 300)
    seen: list[int] = []
    _run(tmp_path, fresh, _registry(fresh, -1, seen), batch_size=50, checkpoint_rows=100, apply=False)
    with psycopg.connect(fresh) as c:
        exists = c.execute("SELECT to_regclass('silver.signals')").fetchone()[0]
        n = c.execute("SELECT count(*) FROM silver.signals").fetchone()[0] if exists else 0
    assert n == 0


def test_the_cli_options_reach_the_pass(monkeypatch, tmp_path):
    from axiom.extensions.builtins.data_platform.skills import conform_run

    got = {}

    def fake(**kw):
        got.update(kw)
        return {"rows_in": 0, "rows_out": 0, "errored": 0, "unknown_schema": {},
                "unmapped_connectors": [], "registered_without_site": [],
                "distributions_loaded": []}

    monkeypatch.setattr(conform_run, "run_conform", fake)

    class Ctx:
        state_dir = tmp_path

    conform_run.run({"dsn": "postgresql://x/y", "bronze_root": str(tmp_path),
                     "batch_size": 7, "checkpoint_rows": 9}, Ctx())
    assert (got["batch_size"], got["checkpoint_rows"]) == (7, 9)
