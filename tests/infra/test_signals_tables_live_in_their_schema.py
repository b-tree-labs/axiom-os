# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""signals' four tables move from ``public`` into the ``signals`` schema, with their data (issue 869).

Second release of the move. The first made the store find its tables in either place; this one
moves them, so a node still running the first release keeps working against a moved database.

The move is ``ALTER TABLE ... SET SCHEMA``: a catalogue change, not a copy. Rows, indexes,
constraints and the foreign key go with the table, and nothing is rewritten.

* TM-1: a new database ends with the four tables in ``signals`` and none in ``public``.
* TM-2: an install holding data in ``public`` keeps every row, index and the foreign key.
* TM-3: an empty set left in ``public`` beside a used one in ``signals`` is dropped.
* TM-4: two sets that both hold rows are never merged by guessing: the migration stops and says so.
* TM-5: running it again changes nothing, and a downgrade puts the tables back.
* TM-6: the store works on the migrated database and makes nothing in ``public``.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text

from axiom.infra.db import current_revision, provision_extension

pytestmark = pytest.mark.integration

TABLES = ("signals", "media", "participants", "people")


@pytest.fixture(autouse=True)
def _a_node_that_runs_the_release_before_the_move(tmp_path, monkeypatch):
    """The move is a contracting revision, applied only on a node shown to run 0.70.47 or
    later (ADR-182 D4a). These tests play such a node; the refusal has its own test."""
    from axiom.infra.deploy import proven_release

    monkeypatch.setenv("AXI_PROVEN_RELEASE", str(tmp_path / "proven.json"))
    proven_release.write("0.70.47", ["axiom-serve"])


def _run(url: str, *statements: str) -> None:
    engine = create_engine(url)
    try:
        with engine.begin() as connection:
            for statement in statements:
                connection.execute(text(statement))
    finally:
        engine.dispose()


def _scalar(url: str, sql: str):
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            return connection.execute(text(sql)).scalar()
    finally:
        engine.dispose()


def _where(url: str, table: str) -> set[str]:
    """The schemas (of public and signals) that hold *table*."""
    return {s for s in ("public", "signals") if _scalar(url, f"SELECT to_regclass('{s}.{table}')") is not None}


def _provision(monkeypatch):
    monkeypatch.delenv("AXIOM_TEST_SCHEMA_SUFFIX", raising=False)
    result = provision_extension("signals")
    if result.skipped:
        pytest.skip(result.skipped)
    return result


def _as_an_install_made_before_the_move(url: str, monkeypatch) -> None:
    """Migrate, then undo only the move: tables in public, stamp at the first revision, in public."""
    first = _provision(monkeypatch)
    assert first.ok, first.error
    _run(
        url,
        *[f"ALTER TABLE signals.{t} SET SCHEMA public" for t in TABLES],
        "DROP TABLE signals.alembic_version",
        "CREATE TABLE public.alembic_version (version_num varchar(32) NOT NULL PRIMARY KEY)",
        "INSERT INTO public.alembic_version VALUES ('001')",
    )


def test_tm1_a_new_database_ends_with_the_tables_in_signals_and_none_in_public(fresh_postgres, monkeypatch):
    result = _provision(monkeypatch)
    assert result.ok, result.error
    for table in TABLES:
        assert _where(fresh_postgres, table) == {"signals"}, f"{table} is in {_where(fresh_postgres, table)}"


def test_tm2_an_install_with_data_in_public_keeps_its_rows_indexes_and_foreign_key(fresh_postgres, monkeypatch):
    _as_an_install_made_before_the_move(fresh_postgres, monkeypatch)
    _run(
        fresh_postgres,
        "INSERT INTO public.signals (id, text) VALUES ('s1', 'a signal')",
        "INSERT INTO public.media (id, path) VALUES ('m1', '/a')",
        "INSERT INTO public.participants (id, media_id, person_id) VALUES ('p1', 'm1', 'x')",
        "INSERT INTO public.people (id, name) VALUES ('x', 'Someone')",
    )
    indexes_before = _scalar(fresh_postgres, "SELECT count(*) FROM pg_indexes WHERE schemaname = 'public' AND tablename IN ('signals','media','participants','people')")

    result = _provision(monkeypatch)

    assert result.ok, result.error
    for table in TABLES:
        assert _where(fresh_postgres, table) == {"signals"}, table
    assert _scalar(fresh_postgres, "SELECT text FROM signals.signals WHERE id = 's1'") == "a signal"
    assert _scalar(fresh_postgres, "SELECT count(*) FROM signals.participants") == 1
    assert _scalar(fresh_postgres, "SELECT count(*) FROM pg_indexes WHERE schemaname = 'signals' AND tablename IN ('signals','media','participants','people')") == indexes_before
    _run(fresh_postgres, "DELETE FROM signals.media WHERE id = 'm1'")
    assert _scalar(fresh_postgres, "SELECT count(*) FROM signals.participants") == 0, "the foreign key did not move with the table"
    assert current_revision("signals") != "001"


def test_tm3_an_empty_set_left_in_public_is_dropped_and_the_used_one_kept(fresh_postgres, monkeypatch):
    """A store that ran first made its tables in signals; the first revision then made a set in public."""
    _as_an_install_made_before_the_move(fresh_postgres, monkeypatch)  # an empty set in public
    _run(
        fresh_postgres,
        "CREATE SCHEMA IF NOT EXISTS signals",
        "CREATE TABLE signals.signals (LIKE public.signals INCLUDING ALL)",
        "CREATE TABLE signals.media (LIKE public.media INCLUDING ALL)",
        "CREATE TABLE signals.participants (LIKE public.participants INCLUDING ALL)",
        "CREATE TABLE signals.people (LIKE public.people INCLUDING ALL)",
        "INSERT INTO signals.signals (id, text) VALUES ('used', 'kept')",
    )

    result = _provision(monkeypatch)

    assert result.ok, result.error
    for table in TABLES:
        assert _where(fresh_postgres, table) == {"signals"}, table
    assert _scalar(fresh_postgres, "SELECT text FROM signals.signals WHERE id = 'used'") == "kept"


def test_tm4_two_sets_that_both_hold_rows_are_not_merged_by_guessing(fresh_postgres, monkeypatch):
    _as_an_install_made_before_the_move(fresh_postgres, monkeypatch)
    _run(
        fresh_postgres,
        "CREATE SCHEMA IF NOT EXISTS signals",
        "CREATE TABLE signals.signals (LIKE public.signals INCLUDING ALL)",
        "INSERT INTO signals.signals (id, text) VALUES ('a', 'in signals')",
        "INSERT INTO public.signals (id, text) VALUES ('b', 'in public')",
    )

    result = _provision(monkeypatch)

    assert not result.ok
    assert "signals" in (result.error or "") and "rows" in (result.error or ""), result.error
    assert _scalar(fresh_postgres, "SELECT count(*) FROM public.signals") == 1
    assert _scalar(fresh_postgres, "SELECT count(*) FROM signals.signals") == 1


def test_tm5_it_is_repeatable_and_a_downgrade_puts_the_tables_back(fresh_postgres, monkeypatch):
    from axiom.extensions.builtins.signals import migrations as signals_migrations

    monkeypatch.setattr(signals_migrations, "get_db_url", lambda: fresh_postgres)
    first = _provision(monkeypatch)
    assert first.ok, first.error
    again = _provision(monkeypatch)
    assert again.ok, again.error
    assert _where(fresh_postgres, "signals") == {"signals"}

    assert signals_migrations.run_migrations("downgrade", "001")
    for table in TABLES:
        assert _where(fresh_postgres, table) == {"public"}, table


def test_tm6_the_store_uses_the_moved_tables_and_makes_none_in_public(fresh_postgres, monkeypatch):
    from axiom.extensions.builtins.signals.pgvector_store import SignalRecord, VectorDB
    from axiom.infra.db import libpq_url

    result = _provision(monkeypatch)
    assert result.ok, result.error
    store = VectorDB(libpq_url(fresh_postgres))
    store.upsert_signal(SignalRecord(id="x", text="t", embedding=[0.0] * 1536))
    store.close()
    assert _scalar(fresh_postgres, "SELECT count(*) FROM signals.signals") == 1
    for table in TABLES:
        assert _where(fresh_postgres, table) == {"signals"}, table
