# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""`axi db migrate` covers every extension, and says so honestly.

It used to drive exactly one extension's revisions — signals' — while
reporting on "the database". So it printed "Database is up to date" on a
machine where a consumer extension's tables did not exist, and that
extension's own CLI answered with a forty-line SQL error (issue #826).

The first fix only NAMED the others, and its discovery globbed
``extensions/builtins/*/alembic.ini``. That found extensions keeping the file
at the extension root — the one case it was written against — but missed
every extension keeping it under ``migrations/``: five of them, all
invisible. Discovery now keys on
``migrations/env.py``, which all of them have, and `upgrade` runs them.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import axiom.infra.db as db_module
from axiom.extensions.builtins.db import cli


def _signals_reporting_clean(monkeypatch):
    monkeypatch.setattr(
        "axiom.extensions.builtins.signals.migrations.check_migrations",
        lambda: {"connected": True, "current": "001", "head": "001",
                 "pending": 0, "up_to_date": True},
    )
    monkeypatch.setattr(
        "axiom.extensions.builtins.signals.migrations.verify_schema",
        lambda: {"valid": True, "has_pgvector": True},
    )


class TestItDoesNotSpeakForTheWholeDatabase:
    def test_the_up_to_date_line_names_its_scope(self, capsys, monkeypatch):
        _signals_reporting_clean(monkeypatch)
        monkeypatch.setattr(db_module, "extensions_with_migrations", list)
        cli.cmd_migrate(argparse.Namespace(migrate_command="check"))
        out = capsys.readouterr().out
        assert "Up to date (signals migrations)" in out
        assert "Database is up to date" not in out, (
            "claiming the whole database is what hid a missing schema"
        )

    def test_every_other_extension_gets_a_line(self, capsys, monkeypatch):
        _signals_reporting_clean(monkeypatch)
        monkeypatch.setattr(
            db_module,
            "extensions_with_migrations",
            lambda: [("widgets", Path("/x/widgets/migrations"))],
        )
        monkeypatch.setattr(db_module, "current_revision", lambda name: "")
        cli.cmd_migrate(argparse.Namespace(migrate_command="check"))
        out = capsys.readouterr().out
        assert "widgets" in out


class TestDiscovery:
    """Against the real tree, so a regression in the glob shows up here."""

    def test_it_finds_extensions_keeping_alembic_under_migrations(self):
        names = {name for name, _ in db_module.extensions_with_migrations()}
        # The five the old glob could not see, plus the session store.
        assert {"vault", "authz", "notifications", "schedule", "chat"} <= names

    def test_it_finds_a_consumer_extension_without_naming_one(self, tmp_path):
        """A consumer package ships extensions too, and they must be found.

        This asserts the CAPABILITY against a synthetic tree. The version that
        named a real consumer extension passed locally — where that package is
        installed — and failed in CI, where it is not. A platform test must
        not depend on a consumer being present, and Axiom's tests must not
        name domain consumers.
        """
        env = tmp_path / "extensions" / "builtins" / "widgets" / "migrations"
        env.mkdir(parents=True)
        (env / "env.py").write_text("")
        found = db_module.scan_migration_roots([tmp_path])
        assert found == [("widgets", env)]

    def test_a_directory_without_an_env_is_not_discovered(self, tmp_path):
        """Pinned so the scan cannot report every directory it walks past."""
        stray = tmp_path / "extensions" / "builtins" / "notanext" / "migrations"
        stray.mkdir(parents=True)
        assert db_module.scan_migration_roots([tmp_path]) == []

    def test_signals_is_discovered_but_not_double_reported(self):
        names = {name for name, _ in db_module.extensions_with_migrations()}
        assert "signals" in names
        # …and the status block excludes it, since check_migrations covers it.
        assert not any("signals" in line for line in cli._other_extension_status())
