# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""signals keeps its Alembic version in its own schema, and adopts an old stamp (issue 869).

``signals/migrations/env.py`` never passed ``version_table_schema``, so every install stamped
``alembic_version`` in ``public``. ``provision_extension`` reads the version from the extension's
own schema, found none, and reported "stopped at base" on every start although the migration
had run.

The tables move in revision 002 (``test_signals_tables_live_in_their_schema``); this file is about the stamp.

* SV-1: a new database is stamped in ``signals`` and leaves no ``alembic_version`` in ``public``.
* SV-2: an install stamped in ``public`` is adopted: stamp moves, tables and rows are untouched,
  and ``provision_extension`` reports ok.
* SV-3: a ``public.alembic_version`` that holds a revision this extension does not own is left alone.
* SV-4: adopting is repeatable: a second provision changes nothing.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text

from axiom.infra.db import current_revision, extensions_with_migrations, provision_extension

pytestmark = pytest.mark.integration


def _head() -> str:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    directory = dict(extensions_with_migrations())["signals"]
    config = Config()
    config.set_main_option("script_location", str(directory))
    return ScriptDirectory.from_config(config).get_current_head()


def _scalar(url: str, sql: str):
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            return connection.execute(text(sql)).scalar()
    finally:
        engine.dispose()


def _run(url: str, *statements: str) -> None:
    engine = create_engine(url)
    try:
        with engine.begin() as connection:
            for statement in statements:
                connection.execute(text(statement))
    finally:
        engine.dispose()


def _stamp_in_public_like_an_old_install(url: str, revision: str) -> None:
    """Put the install back as every pre-fix node has it: the stamp in public, none in signals."""
    _run(
        url,
        "DROP TABLE IF EXISTS signals.alembic_version",
        "DROP TABLE IF EXISTS public.alembic_version",
        "CREATE TABLE public.alembic_version (version_num varchar(32) NOT NULL PRIMARY KEY)",
        f"INSERT INTO public.alembic_version VALUES ('{revision}')",
    )


def test_sv1_a_new_database_is_stamped_in_signals_and_not_in_public(fresh_postgres, monkeypatch):
    monkeypatch.delenv("AXIOM_TEST_SCHEMA_SUFFIX", raising=False)
    result = provision_extension("signals")
    if result.skipped:
        pytest.skip(result.skipped)
    assert result.ok, result.error
    assert current_revision("signals") == _head()
    assert _scalar(fresh_postgres, "SELECT to_regclass('public.alembic_version')") is None


def test_sv2_an_install_stamped_in_public_is_adopted_and_its_data_kept(fresh_postgres, monkeypatch):
    monkeypatch.delenv("AXIOM_TEST_SCHEMA_SUFFIX", raising=False)
    first = provision_extension("signals")
    if first.skipped:
        pytest.skip(first.skipped)
    assert first.ok, first.error
    _run(fresh_postgres, "INSERT INTO signals.signals (id, text) VALUES ('keep-me', 'a node already holds this')")
    _stamp_in_public_like_an_old_install(fresh_postgres, _head())

    again = provision_extension("signals")

    assert again.ok, again.error
    assert current_revision("signals") == _head()
    assert _scalar(fresh_postgres, "SELECT to_regclass('public.alembic_version')") is None
    assert _scalar(fresh_postgres, "SELECT text FROM signals.signals WHERE id = 'keep-me'") == "a node already holds this"


def test_sv3_a_public_stamp_this_extension_does_not_own_is_left_alone(fresh_postgres, monkeypatch):
    monkeypatch.delenv("AXIOM_TEST_SCHEMA_SUFFIX", raising=False)
    first = provision_extension("signals")
    if first.skipped:
        pytest.skip(first.skipped)
    assert first.ok, first.error
    _stamp_in_public_like_an_old_install(fresh_postgres, "not_a_signals_revision")
    _run(fresh_postgres, "CREATE TABLE IF NOT EXISTS signals.alembic_version (version_num varchar(32) NOT NULL PRIMARY KEY)")

    provision_extension("signals")

    assert _scalar(fresh_postgres, "SELECT version_num FROM public.alembic_version") == "not_a_signals_revision"


def test_sv4_adopting_twice_changes_nothing(fresh_postgres, monkeypatch):
    monkeypatch.delenv("AXIOM_TEST_SCHEMA_SUFFIX", raising=False)
    first = provision_extension("signals")
    if first.skipped:
        pytest.skip(first.skipped)
    _stamp_in_public_like_an_old_install(fresh_postgres, _head())
    provision_extension("signals")
    again = provision_extension("signals")
    assert again.ok, again.error
    assert current_revision("signals") == _head()


def test_sv5_the_status_readers_find_the_stamp_in_either_place(fresh_postgres, monkeypatch):
    """`check_migrations` and `verify_schema` read the version too; neither may call a stamped node unmigrated."""
    from axiom.extensions.builtins.signals import migrations as signals_migrations

    monkeypatch.delenv("AXIOM_TEST_SCHEMA_SUFFIX", raising=False)
    monkeypatch.setattr(signals_migrations, "get_db_url", lambda: fresh_postgres)
    first = provision_extension("signals")
    if first.skipped:
        pytest.skip(first.skipped)
    assert first.ok, first.error
    assert signals_migrations.get_current_revision() == _head()
    assert signals_migrations.verify_schema()["missing_tables"] == []

    _stamp_in_public_like_an_old_install(fresh_postgres, _head())
    assert signals_migrations.get_current_revision() == _head()
    assert signals_migrations.verify_schema()["missing_tables"] == []
