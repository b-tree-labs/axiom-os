# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Migrations Axiom did not ship run only when the operator trusts them, and only inside
their own schema (#2006).

Reading an installed extension's ``[database]`` block is the first time Axiom runs database
migrations it did not ship. So they are held to more than a built-in's are:

- S1 opt-in trust: an untrusted extension's revisions are listed, never applied.
- S2 trust is pinned to content: changed migrations need re-approval.
- S3 plan before apply: ``--dry-run`` lists pending revisions and their offline SQL, read-only.
- S4 confinement: they run with ``search_path`` on their own schema, in one transaction that
  is rolled back when anything outside that schema was created, altered, dropped or written.
- S5 audit: every applied revision is one record in the audit trail.
- S6 never automatic: nothing but an explicit ``db migrate upgrade`` applies them.
- S7 built-in migrations are untouched by all of the above.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

import axiom.infra.db as db_module
from axiom.extensions import discovery

EXT = "trustprobe_ext"

_MANIFEST = """
[extension]
name = "{name}"
version = "{version}"
description = "An installed extension that owns a schema"

[database]
needs_schema = true
"""

# The extension's own env.py. Axiom does not run it for a third-party extension (it would
# own the connection and the transaction, so nothing could be confined); it is still the
# marker that the folder is an Alembic history.
_ENV = "raise RuntimeError('a third-party env.py must not be executed by Axiom')\n"

_REV = '''
import sqlalchemy as sa
from alembic import op

revision = "{rev}"
down_revision = {down!r}


def upgrade():
{body}


def downgrade():
    pass
'''

_CREATE_ROWS = '    op.create_table("probe_rows", sa.Column("id", sa.Integer, primary_key=True))'
_ADD_NOTE = '    op.add_column("probe_rows", sa.Column("note", sa.Text))'


def _write_rev(mig: Path, rev: str, down: str | None, body: str) -> None:
    (mig / "versions" / f"{rev}_probe.py").write_text(_REV.format(rev=rev, down=down, body=body))


@pytest.fixture
def state_dir(monkeypatch, tmp_path):
    """Node settings and the audit trail in a directory of this test's own."""
    d = tmp_path / "state"
    d.mkdir()
    monkeypatch.setenv("AXI_STATE_DIR", str(d))
    return d


@pytest.fixture
def installed(monkeypatch, tmp_path, state_dir):
    """Install a third-party extension as an ordinary package; returns its migrations dir."""
    builtins_dirs: list[Path] = []
    # pytest-xdist workers append a suffix to every normalized schema name
    # (AXIOM_TEST_SCHEMA_SUFFIX, see normalize_extension_name), which would turn
    # "Public" into "public_gw1" and slip past the reserved-name check. Production
    # never sets it; these tests check the production mapping.
    monkeypatch.delenv("AXIOM_TEST_SCHEMA_SUFFIX", raising=False)
    monkeypatch.setattr(discovery, "_installed_package_extension_dirs", lambda: list(builtins_dirs))
    monkeypatch.setattr("axiom.infra.branding.discover_portfolio_members", list)

    def install(name: str = EXT, *, version: str = "0.1.0", revs=(("0001", None, _CREATE_ROWS),)) -> Path:
        ext_dir = tmp_path / "site" / "probe_pkg" / "extensions" / "builtins" / name
        mig = ext_dir / "migrations"
        (mig / "versions").mkdir(parents=True, exist_ok=True)
        (ext_dir / "axiom-extension.toml").write_text(_MANIFEST.format(name=name, version=version))
        (mig / "env.py").write_text(_ENV)
        for rev, down, body in revs:
            _write_rev(mig, rev, down, body)
        if ext_dir.parent not in builtins_dirs:
            builtins_dirs.append(ext_dir.parent)
        return mig

    return install


def _tables(url: str, name: str) -> list[tuple[str, str]]:
    engine = create_engine(url)
    try:
        with engine.connect() as c:
            return [tuple(r) for r in c.execute(text(
                "SELECT table_schema, table_name FROM information_schema.tables WHERE table_name = :n"
            ), {"n": name})]
    finally:
        engine.dispose()


def _schema_exists(url: str, schema: str) -> bool:
    engine = create_engine(url)
    try:
        with engine.connect() as c:
            return bool(c.execute(text("SELECT 1 FROM pg_namespace WHERE nspname = :s"), {"s": schema}).scalar())
    finally:
        engine.dispose()


def _audit(state_dir: Path) -> list[dict]:
    path = state_dir / "audit" / "actions.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# ------------------------------------------------------------------ S1 opt-in trust


@pytest.mark.acceptance("2006-S1")
def test_an_untrusted_extension_is_listed_and_nothing_is_applied(installed, fresh_postgres):
    from axiom.infra import extension_migrations as em

    installed()

    results = em.apply_trusted()

    [result] = [r for r in results if r.extension == EXT]
    assert result.state == "untrusted"
    assert result.applied == []
    assert result.pending == ["0001"]
    assert _tables(fresh_postgres, "probe_rows") == []
    assert not _schema_exists(fresh_postgres, db_module.normalize_extension_name(EXT)), (
        "an untrusted extension does not even get its schema created"
    )


@pytest.mark.acceptance("2006-S1")
def test_trusting_records_it_in_node_settings_and_applies_in_its_own_schema(installed, fresh_postgres):
    from axiom.extensions.builtins.settings.store import SettingsStore
    from axiom.infra import extension_migrations as em

    installed()

    record = em.trust(EXT)
    results = em.apply_trusted()

    schema = db_module.normalize_extension_name(EXT)
    [result] = [r for r in results if r.extension == EXT]
    assert result.state == "applied", result.error
    assert result.applied == ["0001"]
    assert _tables(fresh_postgres, "probe_rows") == [(schema, "probe_rows")], (
        "an unqualified create_table lands in the extension's own schema (search_path)"
    )
    assert SettingsStore().get(f"db.trusted_migrations.{schema}.content_hash") == record.content_hash
    assert db_module.current_revision(EXT) == "0001"


# ------------------------------------------------------------------ S2 pinned to content


@pytest.mark.acceptance("2006-S2")
def test_changed_migrations_are_blocked_until_re_trusted(installed, fresh_postgres):
    from axiom.infra import extension_migrations as em

    mig = installed()
    em.trust(EXT)
    assert em.apply_trusted()[0].applied == ["0001"]

    # An upgrade of the extension adds a revision.
    installed(version="0.2.0", revs=(("0001", None, _CREATE_ROWS), ("0002", "0001", _ADD_NOTE)))
    assert (mig / "versions" / "0002_probe.py").exists()

    [blocked] = [r for r in em.apply_trusted() if r.extension == EXT]
    assert blocked.state == "stale"
    assert blocked.applied == []
    assert blocked.new_since_trust == ["0002"], "the new revisions are named for review"
    assert db_module.current_revision(EXT) == "0001", "what was applied stays applied"

    em.trust(EXT)
    [applied] = [r for r in em.apply_trusted() if r.extension == EXT]
    assert applied.applied == ["0002"]
    assert db_module.current_revision(EXT) == "0002"


# ------------------------------------------------------------------ S3 plan before apply


@pytest.mark.acceptance("2006-S3")
def test_the_plan_lists_pending_revisions_and_their_sql_without_touching_the_database(
    installed, fresh_postgres
):
    from axiom.infra import extension_migrations as em

    installed()

    [plan] = [p for p in em.plan() if p.extension == EXT]

    assert plan.state == "untrusted"
    assert plan.pending == ["0001"]
    assert plan.sql and "CREATE TABLE" in plan.sql and "probe_rows" in plan.sql
    assert not _schema_exists(fresh_postgres, db_module.normalize_extension_name(EXT))


@pytest.mark.acceptance("2006-S3")
def test_db_migrate_dry_run_prints_the_plan_and_the_trust_command(installed, fresh_postgres, capsys, monkeypatch):
    from axiom.extensions.builtins.db import cli

    monkeypatch.setattr(db_module, "extensions_with_migrations", list)  # built-ins are not this test's subject
    installed()

    code = cli.cmd_migrate(argparse.Namespace(
        migrate_command="upgrade", revision="head", message="", autogenerate=False,
        dry_run=True, trust=[], untrust=[],
    ))

    out = capsys.readouterr().out
    assert code == 0
    assert EXT in out and "untrusted" in out and "0001" in out
    assert "CREATE TABLE" in out
    assert f"--trust {EXT}" in out
    assert _tables(fresh_postgres, "probe_rows") == []


# ------------------------------------------------------------------ S4 confinement


@pytest.mark.acceptance("2006-S4")
def test_a_migration_that_creates_outside_its_schema_is_rolled_back_naming_the_object(
    installed, fresh_postgres
):
    from axiom.infra import extension_migrations as em

    installed(revs=(
        ("0001", None, _CREATE_ROWS + '\n    op.create_table("leak", sa.Column("id", sa.Integer), schema="public")'),
    ))
    em.trust(EXT)

    [result] = [r for r in em.apply_trusted() if r.extension == EXT]

    assert result.state == "rolled_back"
    assert "public.leak" in result.error
    assert _tables(fresh_postgres, "leak") == []
    assert _tables(fresh_postgres, "probe_rows") == [], "the whole transaction is rolled back"
    assert db_module.current_revision(EXT) == ""


@pytest.mark.acceptance("2006-S4")
def test_a_migration_that_alters_or_writes_another_schemas_table_is_rolled_back(installed, fresh_postgres):
    from axiom.infra import extension_migrations as em

    engine = create_engine(fresh_postgres)
    with engine.begin() as c:
        c.execute(text("CREATE TABLE public.victim (id int)"))
    engine.dispose()
    installed(revs=(("0001", None, '    op.execute("ALTER TABLE public.victim ADD COLUMN pwned int")'),))
    em.trust(EXT)
    [altered] = [r for r in em.apply_trusted() if r.extension == EXT]
    assert altered.state == "rolled_back"
    assert "public.victim" in altered.error

    installed(revs=(("0001", None, '    op.execute("INSERT INTO public.victim VALUES (1)")'),))
    em.trust(EXT)
    [written] = [r for r in em.apply_trusted() if r.extension == EXT]
    assert written.state == "rolled_back"
    assert "public.victim" in written.error


# ------------------------------------------------------------------ S5 audit


@pytest.mark.acceptance("2006-S5")
def test_each_applied_revision_is_one_audit_record(installed, fresh_postgres, state_dir):
    from axiom.infra import extension_migrations as em

    installed(version="1.2.3", revs=(("0001", None, _CREATE_ROWS), ("0002", "0001", _ADD_NOTE)))
    record = em.trust(EXT)
    em.apply_trusted()

    rows = [r for r in _audit(state_dir) if r.get("skill") == "db.migrate.extension"]
    assert [r["change"]["revision"] for r in rows] == ["0001", "0002"]
    for row in rows:
        change = row["change"]
        assert change["extension"] == EXT
        assert change["version"] == "1.2.3"
        assert change["content_hash"] == record.content_hash
        assert change["trusted_by"] == record.trusted_by and record.trusted_by
        assert change["trusted_at"] == record.trusted_at and record.trusted_at
        assert row["outcome"] == "ok"
        assert row["hash"] and row["prev_hash"], "it is a link of the tamper-evident chain"


# ------------------------------------------------------------------ S6 never automatic


@pytest.mark.acceptance("2006-S6")
def test_nothing_but_an_explicit_migrate_applies_them(installed, fresh_postgres):
    """Start-up, serve and install reach the database through ``extensions_with_migrations``,
    ``provision_extension`` and a store's ``SchemaSeam``; none of them may run these."""
    from axiom.infra import extension_migrations as em
    from axiom.infra.schema_seam import SchemaSeam, StoreUnavailable

    installed()
    em.trust(EXT)  # even trusted: trust permits the explicit command, it does not schedule anything

    assert EXT not in dict(db_module.extensions_with_migrations())
    result = db_module.provision_extension(EXT)
    assert not result.ok
    assert "db migrate upgrade" in result.error
    with pytest.raises(StoreUnavailable, match="db migrate upgrade"):
        SchemaSeam(EXT, what="the probe").ensure_provisioned()
    assert _tables(fresh_postgres, "probe_rows") == []

    em.apply_trusted()
    assert db_module.provision_extension(EXT).ok, "once migrated, a store finds its schema at head"


# ------------------------------------------------------------------ S7 built-ins unchanged


@pytest.mark.acceptance("2006-S7")
def test_built_in_migration_discovery_and_running_are_unchanged(installed):
    from axiom.infra import extension_migrations as em

    import axiom

    installed()
    scanned = db_module.scan_migration_roots([Path(p) for p in axiom.__path__])

    assert db_module.extensions_with_migrations() == scanned
    third = {t.extension for t in em.third_party()}
    assert third == {EXT}
    assert not third & {name for name, _ in scanned}


def test_a_third_party_extension_cannot_claim_a_reserved_or_built_in_schema(installed):
    from axiom.infra import extension_migrations as em

    installed("Public")
    installed("MEMORY")  # normalizes onto a built-in's schema

    assert {t.extension for t in em.third_party()} == set()
