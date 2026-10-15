# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""`stream` became `feed`, proven against a real Postgres rather than a fake.

A migration that has only ever run against an empty database has not been
tested. The interesting case is a table that already carries the old column,
with rows in it, an index over it, views on top that Postgres will refuse to
replace, another view on top of those, and a read-only role holding grants.

Every one of those found something:

  - the view rename is refused by `CREATE OR REPLACE`, and is repaired;
  - `gold.ingest_stale` reads `gold.ingest_freshness`, so the repair had to
    learn about dependents rather than reaching for CASCADE;
  - a dropped view takes its grants with it, and nothing brings them back;
  - and the rename script rewrote the migration's own SQL into
    `RENAME COLUMN feed TO feed`, which every fake in this suite was happy to
    accept because none of them run SQL.

Skipped without a server, which is honest: a fake cannot answer any of this.
"""

from __future__ import annotations

import os

import pytest

psycopg = pytest.importorskip("psycopg")

from axiom.extensions.builtins.data_platform.conformance import (  # noqa: E402
    GOLD_SIGNALS_DDL,
    SILVER_SIGNALS_DDL,
    apply_conformance_ddl,
)

#: The old shape, written out rather than imported, because the point is to
#: start from what is actually deployed and no future edit should move it.
OLD_DDL = [
    "CREATE SCHEMA IF NOT EXISTS silver",
    """CREATE TABLE IF NOT EXISTS silver.signals (
         site text NOT NULL, stream text NOT NULL, channel text NOT NULL,
         ts timestamptz NOT NULL, value double precision, unit text,
         quality text NOT NULL DEFAULT 'ok',
         source_class text NOT NULL DEFAULT 'measured',
         schema_ref text NOT NULL, row_hash text NOT NULL,
         model_ref text, basis text NOT NULL DEFAULT 'live',
         uncertainty double precision, role text, derivation text,
         PRIMARY KEY (row_hash, channel))""",
    """CREATE INDEX IF NOT EXISTS signals_site_stream_channel_ts
         ON silver.signals (site, stream, channel, ts)""",
    "CREATE SCHEMA IF NOT EXISTS gold",
    """CREATE TABLE IF NOT EXISTS gold.signal_catalogue (
         site text NOT NULL, stream text NOT NULL, channel text NOT NULL,
         unit text, rows bigint NOT NULL DEFAULT 0,
         first_ts timestamptz, last_ts timestamptz,
         computed_at timestamptz NOT NULL DEFAULT now(),
         PRIMARY KEY (site, stream, channel))""",
    """CREATE OR REPLACE VIEW gold.signals AS
         SELECT site, stream, channel, ts, value, unit, quality, source_class,
                model_ref, basis, role, uncertainty, derivation
           FROM silver.signals""",
    """CREATE OR REPLACE VIEW gold.ingest_freshness AS
         SELECT site, stream, count(*) AS points,
                count(DISTINCT channel) AS channels,
                min(ts) AS first_ts, max(ts) AS last_ts,
                now() - max(ts) AS lag,
                CASE WHEN count(*) > 1
                     THEN (max(ts) - min(ts)) / (count(*) - 1) END AS typical_gap,
                max(model_ref) AS model_ref
           FROM silver.signals GROUP BY site, stream""",
    """CREATE OR REPLACE VIEW gold.ingest_stale AS
         SELECT site, stream, last_ts, lag FROM gold.ingest_freshness
          WHERE typical_gap IS NOT NULL AND lag > typical_gap * 4""",
]


def _admin_dsn() -> str | None:
    return os.environ.get("AXIOM_TEST_PG_DSN")


@pytest.fixture
def scratch_db():
    admin = _admin_dsn()
    if not admin:
        pytest.skip("set AXIOM_TEST_PG_DSN to a superuser DSN to run this")
    name = "axiom_feed_rename_test"
    with psycopg.connect(admin, autocommit=True) as c, c.cursor() as cur:
        cur.execute(f"DROP DATABASE IF EXISTS {name}")
        cur.execute(f"CREATE DATABASE {name}")
    dsn = admin.rsplit("/", 1)[0] + "/" + name
    try:
        yield dsn
    finally:
        with psycopg.connect(dsn, autocommit=True) as c, c.cursor() as cur:
            cur.execute("DROP OWNED BY axiom_feed_probe_ro")
        with psycopg.connect(admin, autocommit=True) as c, c.cursor() as cur:
            cur.execute(f"DROP DATABASE IF EXISTS {name}")
            cur.execute("DROP ROLE IF EXISTS axiom_feed_probe_ro")


@pytest.fixture
def deployed(scratch_db):
    """A database in the shape the node is actually in."""
    with psycopg.connect(scratch_db, autocommit=True) as c, c.cursor() as cur:
        for stmt in OLD_DDL:
            cur.execute(stmt)
        cur.execute(
            """INSERT INTO silver.signals
                 (site, stream, channel, ts, value, unit, quality,
                  source_class, schema_ref, row_hash)
               VALUES ('s1','f.a','ch1', now(), 1.0,'degC','ok','measured','x/v1','h1'),
                      ('s1','f.a','ch1', now()-interval '1 min', 2.0,'degC','ok','measured','x/v1','h2'),
                      ('s1','f.b','ch2', now(), 3.0, NULL,'ok','predicted','x/v1','h3')"""
        )
        cur.execute("DROP ROLE IF EXISTS axiom_feed_probe_ro")
        cur.execute("CREATE ROLE axiom_feed_probe_ro")
        cur.execute(
            "GRANT SELECT ON gold.signals, gold.ingest_freshness, gold.ingest_stale "
            "TO axiom_feed_probe_ro"
        )
    return scratch_db


def _migrate(dsn):
    with psycopg.connect(dsn, autocommit=True) as c, c.cursor() as cur:
        return apply_conformance_ddl(cur, SILVER_SIGNALS_DDL + GOLD_SIGNALS_DDL), cur


def test_the_column_is_renamed_and_the_rows_are_still_there(deployed):
    with psycopg.connect(deployed, autocommit=True) as c, c.cursor() as cur:
        apply_conformance_ddl(cur, SILVER_SIGNALS_DDL + GOLD_SIGNALS_DDL)
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='silver' AND table_name='signals'"
        )
        columns = {r[0] for r in cur.fetchall()}
        assert "feed" in columns and "stream" not in columns
        cur.execute("SELECT count(*), count(DISTINCT feed) FROM gold.signals")
        assert cur.fetchone() == (3, 2)


def test_the_index_is_renamed_rather_than_rebuilt(deployed):
    """A rename is a catalogue edit. Building a fresh index over a table of
    seventy million rows is not, and would be a very long lock."""
    with psycopg.connect(deployed, autocommit=True) as c, c.cursor() as cur:
        apply_conformance_ddl(cur, SILVER_SIGNALS_DDL + GOLD_SIGNALS_DDL)
        cur.execute("SELECT indexname FROM pg_indexes WHERE schemaname='silver' ORDER BY 1")
        names = [r[0] for r in cur.fetchall()]
        assert "signals_site_feed_channel_ts" in names
        assert "signals_site_stream_channel_ts" not in names


def test_a_view_that_something_else_reads_survives(deployed):
    """`gold.ingest_stale` reads `gold.ingest_freshness`. Postgres will not drop
    the second without the first, and CASCADE would have deleted it."""
    with psycopg.connect(deployed, autocommit=True) as c, c.cursor() as cur:
        apply_conformance_ddl(cur, SILVER_SIGNALS_DDL + GOLD_SIGNALS_DDL)
        cur.execute("SELECT site, feed FROM gold.ingest_stale")
        cur.fetchall()  # it exists and reads `feed`; emptiness is not the point


def test_a_read_only_role_keeps_every_grant(deployed):
    """A view's ACL dies with the view, and this migration drops three of them.
    Losing the grants would surface as "permission denied" somewhere else
    entirely, hours later."""
    with psycopg.connect(deployed, autocommit=True) as c, c.cursor() as cur:
        apply_conformance_ddl(cur, SILVER_SIGNALS_DDL + GOLD_SIGNALS_DDL)
        cur.execute(
            """SELECT c.relname FROM pg_class c
                 JOIN pg_namespace n ON n.oid = c.relnamespace
                 CROSS JOIN LATERAL aclexplode(c.relacl) a
                WHERE n.nspname = 'gold'
                  AND pg_get_userbyid(a.grantee) = 'axiom_feed_probe_ro'"""
        )
        kept = {r[0] for r in cur.fetchall()}
        assert kept == {"signals", "ingest_freshness", "ingest_stale"}


def test_the_catalogue_column_is_renamed_too(deployed):
    """`CREATE TABLE IF NOT EXISTS` is a no-op over a deployed table, so a
    rename in the DDL is not a rename on the server unless it is said out
    loud. The catalogue would otherwise have been the last place the old word
    survived, and it is the one every page reads."""
    from axiom.extensions.builtins.data_platform.conformance import catalogue

    with psycopg.connect(deployed, autocommit=True) as c, c.cursor() as cur:
        cur.execute(
            "INSERT INTO gold.signal_catalogue (site, stream, channel, rows) "
            "VALUES ('s1','f.a','ch1',2)"
        )
        catalogue.ensure_schema(cur)
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='gold' AND table_name='signal_catalogue'"
        )
        columns = {r[0] for r in cur.fetchall()}
        assert "feed" in columns and "stream" not in columns
        cur.execute("SELECT site, feed, channel, rows FROM gold.signal_catalogue")
        assert cur.fetchall() == [("s1", "f.a", "ch1", 2)]


def test_running_it_twice_changes_nothing(deployed):
    with psycopg.connect(deployed, autocommit=True) as c, c.cursor() as cur:
        apply_conformance_ddl(cur, SILVER_SIGNALS_DDL + GOLD_SIGNALS_DDL)
        again = apply_conformance_ddl(cur, SILVER_SIGNALS_DDL + GOLD_SIGNALS_DDL)
        assert again == [], f"a second run reshaped {again}"
        cur.execute("SELECT count(*) FROM gold.signals")
        assert cur.fetchone()[0] == 3
