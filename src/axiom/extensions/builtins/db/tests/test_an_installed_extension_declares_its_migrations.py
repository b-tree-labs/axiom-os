# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""An installed extension's ``[database]`` section is read, and its migrations run (#2006).

Migrations were found only under the ``axiom`` package and portfolio-member packages, so an
extension installed as an ordinary package had to run its own (declaring the portfolio entry
point to get found would present it under another product's brand, ADR-048). The manifest's
``[database]`` block (AEOS 1.0 section 4.x, ADR-052) is now read for every discovered extension,
and ``axi db migrate upgrade`` applies its migrations into the extension's own schema, once the
operator trusts them (the safeguards are in test_third_party_migrations_are_safeguarded.py).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

import axiom.infra.db as db_module
from axiom.extensions import discovery

EXT = "dbprobe_ext"

_MANIFEST = """
[extension]
name = "{name}"
version = "0.1.0"
description = "An installed extension that owns a schema"
{database}
"""

_ENV = """
from alembic import context
from axiom.infra.db import engine_for

engine, schema = engine_for("{name}")
with engine.connect() as connection:
    context.configure(connection=connection, target_metadata=None,
                      version_table_schema=schema, include_schemas=True)
    with context.begin_transaction():
        context.run_migrations()
    connection.commit()
"""

_REV = """
import sqlalchemy as sa
from alembic import context, op

revision = "0001"
down_revision = None


def upgrade():
    op.create_table("probe_rows", sa.Column("id", sa.Integer, primary_key=True),
                    schema=context.get_context().version_table_schema)


def downgrade():
    op.drop_table("probe_rows", schema=context.get_context().version_table_schema)
"""


def _installed_package(tmp_path: Path, *, name: str, database: str, migrations: str | None) -> Path:
    """``<site>/<package>/extensions/builtins/<ext>/``: the layout an installed package ships."""
    ext_dir = tmp_path / "site" / "probe_pkg" / "extensions" / "builtins" / name
    ext_dir.mkdir(parents=True)
    (ext_dir / "axiom-extension.toml").write_text(_MANIFEST.format(name=name, database=database))
    if migrations:
        mig = ext_dir / migrations
        (mig / "versions").mkdir(parents=True)
        (mig / "env.py").write_text(_ENV.format(name=name))
        (mig / "versions" / "0001_probe.py").write_text(_REV)
    return ext_dir


@pytest.fixture
def installed(monkeypatch, tmp_path):
    """Make discovery see packages under ``tmp_path`` as installed, and nothing as a portfolio member."""
    builtins_dirs: list[Path] = []
    monkeypatch.setattr(discovery, "_installed_package_extension_dirs", lambda: list(builtins_dirs))
    monkeypatch.setattr("axiom.infra.branding.discover_portfolio_members", list)

    def install(name: str, database: str, migrations: str | None) -> Path:
        ext_dir = _installed_package(tmp_path, name=name, database=database, migrations=migrations)
        builtins_dirs.append(ext_dir.parent)
        return ext_dir

    return install


@pytest.mark.acceptance("2006-A1")
def test_an_installed_extensions_declared_migrations_are_found(installed):
    ext_dir = installed(EXT, '[database]\nneeds_schema = true\nmigrations_path = "schema_revs"', "schema_revs")

    found = dict(db_module.declared_migration_dirs())

    assert found.get(EXT) == ext_dir / "schema_revs"


@pytest.mark.acceptance("2006-A1")
def test_db_migrate_applies_them_in_the_extensions_own_schema(installed, fresh_postgres, monkeypatch, tmp_path):
    from axiom.infra import extension_migrations

    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path / "state"))
    installed(EXT, "[database]\nneeds_schema = true", "migrations")  # migrations_path defaults

    assert EXT in {t.extension for t in extension_migrations.third_party()}
    extension_migrations.trust(EXT)  # third-party: only once the operator trusts it (S1)
    [result] = [r for r in extension_migrations.apply_trusted() if r.extension == EXT]

    assert result.ok, result.summary
    assert result.after == "0001"
    schema = db_module.normalize_extension_name(EXT)
    engine = create_engine(fresh_postgres)
    try:
        with engine.connect() as connection:
            where = connection.execute(
                text("SELECT table_schema FROM information_schema.tables WHERE table_name = 'probe_rows'")
            ).scalars().all()
    finally:
        engine.dispose()
    assert where == [schema], "the table lands in the extension's own schema (ADR-052)"
    assert schema != "public"


@pytest.mark.acceptance("2006-A2")
def test_an_extension_without_a_database_section_is_unaffected(installed):
    before = db_module.declared_migration_dirs()
    installed("plain_ext", "", None)

    after = db_module.declared_migration_dirs()

    assert "plain_ext" not in dict(after)
    assert after == before


def test_built_in_discovery_is_unchanged(installed):
    """Built-ins are still found by the tree scan, at the same directories."""
    from axiom.infra.db import scan_migration_roots

    import axiom

    scanned = scan_migration_roots([Path(p) for p in axiom.__path__])
    found = dict(db_module.extensions_with_migrations())
    for name, directory in scanned:
        assert found[name] == directory


def test_a_migrations_path_outside_the_extension_is_refused(installed, tmp_path):
    outside = tmp_path / "elsewhere"
    (outside / "versions").mkdir(parents=True)
    (outside / "env.py").write_text("")
    installed("escape_ext", '[database]\nneeds_schema = true\nmigrations_path = "../../../../../elsewhere"', None)

    assert "escape_ext" not in dict(db_module.declared_migration_dirs())
