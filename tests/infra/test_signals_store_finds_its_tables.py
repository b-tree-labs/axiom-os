# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The signals store keeps its tables in the ``signals`` schema only (issue 869, step 2c).

Moving the four tables into ``signals`` was expand-then-contract (ADR-182): 0.70.47 found them in
either schema, 0.70.48's revision 002 moved them. This is the contraction: the store no longer
looks in ``public``. A node whose tables are still there has skipped revision 002, and creating an
empty set in ``signals`` beside them would hide its data, so the store refuses and names the fix.

* SS-1: tables only in ``public`` (revision 002 not run): the store refuses, says to run the
  migration, and creates nothing.
* SS-2: tables in ``signals`` (after the move): reads and writes go there, and the store
  does not create a second, empty set in ``public``.
* SS-3: no tables yet: the store creates them in ``signals``.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text

pytestmark = pytest.mark.integration

TABLES = ("signals", "media", "participants", "people")


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


def _store(url: str):
    from axiom.extensions.builtins.signals.pgvector_store import VectorDB
    from axiom.infra.db import libpq_url

    return VectorDB(libpq_url(url))


def _make_the_tables_in(url: str, schema: str) -> None:
    """Have the store create its tables (in signals), then put them where an older install has them."""
    store = _store(url)
    store.connect()
    store.close()
    if schema != "signals":
        _run(url, *[f"ALTER TABLE signals.{t} SET SCHEMA {schema}" for t in TABLES])


def _record(identifier: str):
    from axiom.extensions.builtins.signals.pgvector_store import SignalRecord

    return SignalRecord(id=identifier, text=f"text of {identifier}", embedding=[0.0] * 1536)


def test_ss1_tables_left_in_public_are_refused_not_hidden(fresh_postgres):
    """An install that skipped revision 002: refuse, name the fix, create nothing."""
    _make_the_tables_in(fresh_postgres, "public")
    seed = create_engine(fresh_postgres)
    try:
        with seed.begin() as connection:
            connection.execute(text("INSERT INTO public.signals (id, text) VALUES ('old', 'x')"))
    finally:
        seed.dispose()

    from axiom.extensions.builtins.signals.pgvector_store import SignalsTablesNotMoved

    store = _store(fresh_postgres)
    with pytest.raises(SignalsTablesNotMoved, match="migrat"):
        store.upsert_signal(_record("new"))
    store.close()

    assert _scalar(fresh_postgres, "SELECT count(*) FROM public.signals") == 1
    assert _scalar(fresh_postgres, "SELECT to_regclass('signals.signals')") is None


def test_ss2_tables_in_signals_are_used_and_no_second_set_is_made(fresh_postgres):
    _make_the_tables_in(fresh_postgres, "public")
    _run(fresh_postgres, "INSERT INTO public.signals (id, text) VALUES ('kept', 'x')")
    _run(
        fresh_postgres,
        "CREATE SCHEMA IF NOT EXISTS signals",
        *[f"ALTER TABLE public.{t} SET SCHEMA signals" for t in TABLES],
    )

    moved = _store(fresh_postgres)
    assert moved.get_signal("kept") is not None, "the moved row is not visible to the store"
    moved.upsert_signal(_record("after"))
    moved.close()

    assert _scalar(fresh_postgres, "SELECT count(*) FROM signals.signals") == 2
    for table in TABLES:
        assert _scalar(fresh_postgres, f"SELECT to_regclass('public.{table}')") is None, (
            f"the store recreated an empty public.{table} beside the moved one"
        )


def test_ss3_with_no_tables_the_store_creates_them_in_signals(fresh_postgres):
    store = _store(fresh_postgres)
    store.upsert_signal(_record("first"))
    store.close()
    assert _scalar(fresh_postgres, "SELECT count(*) FROM signals.signals") == 1
    assert _scalar(fresh_postgres, "SELECT to_regclass('public.signals')") is None
