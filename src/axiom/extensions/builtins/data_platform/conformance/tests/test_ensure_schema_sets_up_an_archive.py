# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``axi data ensure-schema`` sets up a site's local medallion (ADR-180).

The archive role's one-time setup was an inline Python one-liner in the chart,
and the read-only SQL role ADR-180 §3a promises was created by nothing.
``ensure-schema`` already owns "bring silver up to the code" at deploy time, so
it grows the two archive options instead of a second verb:

- ``time_partitioned``: silver as a compressed TimescaleDB hypertable, with
  ``compress_after_days`` and ``retain_days`` (unset means never delete);
- ``reader``: a LOGIN role that can SELECT silver and gold and nothing else,
  including gold views a later conform creates. Its password comes from the
  environment (``AXIOM_READER_PASSWORD``, written from the vault), never a
  parameter, and never appears in what the skill returns.
"""

from __future__ import annotations

import json

import psycopg
import pytest

from axiom.extensions.builtins.data_platform.skills import ensure_schema

PASSWORD = "reader-test-only-not-a-secret"


class _Ctx:
    state_dir = None


@pytest.fixture
def fresh(timescale_dsn, monkeypatch):
    with psycopg.connect(timescale_dsn, autocommit=True) as c:
        c.execute("DROP SCHEMA IF EXISTS silver CASCADE")
        c.execute("DROP SCHEMA IF EXISTS gold CASCADE")
        c.execute("DROP ROLE IF EXISTS archive_reader")
    monkeypatch.setenv("AXIOM_READER_PASSWORD", PASSWORD)
    return timescale_dsn


def _as_reader(dsn):
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    info = conninfo_to_dict(dsn)
    info.update(user="archive_reader", password=PASSWORD)
    return psycopg.connect(make_conninfo(**info), autocommit=True)


def test_it_makes_silver_a_compressed_hypertable(fresh):
    r = ensure_schema.run({"dsn": fresh, "time_partitioned": True, "compress_after_days": 3}, _Ctx())
    assert r.ok, r.errors
    with psycopg.connect(fresh) as c:
        tables = {row[0] for row in c.execute(
            "SELECT hypertable_name FROM timescaledb_information.hypertables "
            "WHERE hypertable_schema = 'silver'")}
        jobs = {row[0] for row in c.execute(
            "SELECT proc_name FROM timescaledb_information.jobs WHERE hypertable_name = 'signals'")}
    assert {"signals", "signal_uncertainty"} <= tables
    assert "policy_compression" in jobs and "policy_retention" not in jobs
    assert any("hypertable" in a for a in r.actions_taken)


def test_retention_is_installed_only_when_asked(fresh):
    r = ensure_schema.run({"dsn": fresh, "time_partitioned": True, "retain_days": 400}, _Ctx())
    assert r.ok, r.errors
    with psycopg.connect(fresh) as c:
        jobs = {row[0] for row in c.execute(
            "SELECT proc_name FROM timescaledb_information.jobs WHERE hypertable_name = 'signals'")}
    assert "policy_retention" in jobs


def test_running_it_twice_changes_nothing(fresh):
    params = {"dsn": fresh, "time_partitioned": True, "reader": "archive_reader"}
    assert ensure_schema.run(params, _Ctx()).ok
    again = ensure_schema.run(params, _Ctx())
    assert again.ok, again.errors


def test_the_reader_can_read_silver_and_gold_and_write_nothing(fresh, tmp_path):
    r = ensure_schema.run({"dsn": fresh, "time_partitioned": True, "reader": "archive_reader"}, _Ctx())
    assert r.ok, r.errors
    with psycopg.connect(fresh, autocommit=True) as owner:
        owner.execute(
            "INSERT INTO silver.signals (site, feed, channel, ts, value, schema_ref, row_hash) "
            "VALUES ('s', 's.live', 'TC1', '2026-10-08T10:00:00Z', 1.5, 's/v1', 'rh1')")
        # a gold view the NEXT conform creates must be readable too
        owner.execute("CREATE VIEW gold.later_view AS SELECT channel, value FROM silver.signals")
    with _as_reader(fresh) as reader:
        assert reader.execute("SELECT count(*) FROM silver.signals").fetchone()[0] == 1
        assert reader.execute("SELECT value FROM gold.later_view").fetchone()[0] == 1.5
        for statement in (
            "INSERT INTO silver.signals (site, feed, channel, ts, value, schema_ref, row_hash) "
            "VALUES ('s', 's.live', 'TC2', '2026-10-08T10:00:01Z', 2.0, 's/v1', 'rh2')",
            "DELETE FROM silver.signals",
            "CREATE TABLE silver.mine (x int)",
        ):
            with pytest.raises(psycopg.Error):
                reader.execute(statement)


def test_the_reader_password_comes_only_from_the_environment(fresh, monkeypatch):
    monkeypatch.delenv("AXIOM_READER_PASSWORD")
    r = ensure_schema.run({"dsn": fresh, "reader": "archive_reader"}, _Ctx())
    assert not r.ok
    assert "AXIOM_READER_PASSWORD" in " ".join(r.errors)


def test_the_password_never_appears_in_what_it_returns(fresh):
    r = ensure_schema.run({"dsn": fresh, "reader": "archive_reader"}, _Ctx())
    assert r.ok, r.errors
    shown = json.dumps({"value": r.value, "actions": r.actions_taken, "errors": r.errors}, default=str)
    assert PASSWORD not in shown
    assert "archive_reader" in shown


def test_a_reader_name_that_is_not_an_identifier_is_refused(fresh):
    r = ensure_schema.run({"dsn": fresh, "reader": "x; DROP TABLE silver.signals"}, _Ctx())
    assert not r.ok


def test_without_the_new_options_nothing_changes(fresh):
    r = ensure_schema.run({"dsn": fresh}, _Ctx())
    assert r.ok, r.errors
    with psycopg.connect(fresh) as c:
        n = c.execute("SELECT count(*) FROM timescaledb_information.hypertables "
                      "WHERE hypertable_schema = 'silver'").fetchone()[0]
        role = c.execute("SELECT 1 FROM pg_roles WHERE rolname = 'archive_reader'").fetchone()
    assert n == 0 and role is None


def test_the_cli_flags_reach_the_skill():
    from axiom.extensions.builtins.data_platform import cli

    args = cli._parser().parse_args(
        ["ensure-schema", "--time-partitioned", "--retain-days", "400", "--reader", "archive_reader"]
    )
    assert cli.resolve_skill(args)[0] == "ensure_schema"
    params = cli._args_to_params(args)
    assert params == {"time_partitioned": True, "retain_days": 400, "reader": "archive_reader"}


def test_without_the_flags_the_cli_sends_none_of_them():
    from axiom.extensions.builtins.data_platform import cli

    params = cli._args_to_params(cli._parser().parse_args(["ensure-schema"]))
    assert not {"time_partitioned", "retain_days", "compress_after_days", "reader"} & set(params)
