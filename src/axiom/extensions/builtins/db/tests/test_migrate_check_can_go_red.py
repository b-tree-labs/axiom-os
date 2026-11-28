# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""`db migrate check` exits non-zero when any extension is behind (#826).

The screen learned to name every extension's revision, but the exit code still
spoke for one: `check` returned 0 with an extension's schema missing, and with
signals itself behind. A script or a deploy step reading the exit code got the
same green the issue was filed about. The block of other extensions was also
printed twice.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import axiom.infra.db as db_module
from axiom.extensions.builtins.db import cli


def _signals(monkeypatch, *, pending=0, schema_valid=True):
    monkeypatch.setattr(
        "axiom.extensions.builtins.signals.migrations.check_migrations",
        lambda: {"connected": True, "current": "001", "head": "002" if pending else "001",
                 "pending": pending, "pending_revisions": ["002"] if pending else [],
                 "up_to_date": not pending},
    )
    monkeypatch.setattr(
        "axiom.extensions.builtins.signals.migrations.verify_schema",
        lambda: {"valid": schema_valid, "has_pgvector": True,
                 "missing_tables": [] if schema_valid else ["readings"]},
    )


def _widgets(monkeypatch, tmp_path, *, current):
    """A real one-revision alembic tree, so its head is read, not faked."""
    root = tmp_path / "widgets" / "migrations"
    (root / "versions").mkdir(parents=True)
    (root / "env.py").write_text("")
    (root / "script.py.mako").write_text("")
    (root / "versions" / "w001_first.py").write_text(
        'revision = "w001"\ndown_revision = None\n'
    )
    monkeypatch.setattr(db_module, "extensions_with_migrations", lambda: [("widgets", root)])
    monkeypatch.setattr(db_module, "current_revision", lambda name: current)
    return root


def _check():
    return cli.cmd_migrate(argparse.Namespace(migrate_command="check"))


def test_an_extension_behind_head_is_not_green(capsys, monkeypatch, tmp_path):
    _signals(monkeypatch)
    _widgets(monkeypatch, tmp_path, current="")
    rc = _check()
    out = capsys.readouterr().out
    assert rc != 0, "check exited 0 with an extension's schema missing"
    assert out.count("widgets") == 1, "the other-extensions block printed twice"


def test_every_extension_at_head_is_green(capsys, monkeypatch, tmp_path):
    _signals(monkeypatch)
    _widgets(monkeypatch, tmp_path, current="w001")
    assert _check() == 0


def test_signals_pending_is_not_green(capsys, monkeypatch):
    _signals(monkeypatch, pending=1)
    monkeypatch.setattr(db_module, "extensions_with_migrations", list)
    assert _check() != 0


def test_missing_tables_are_not_green(capsys, monkeypatch):
    _signals(monkeypatch, schema_valid=False)
    monkeypatch.setattr(db_module, "extensions_with_migrations", list)
    assert _check() != 0


def test_an_unreadable_history_is_not_green(capsys, monkeypatch):
    _signals(monkeypatch)
    monkeypatch.setattr(db_module, "extensions_with_migrations",
                        lambda: [("widgets", Path("/nonexistent/widgets/migrations"))])
    monkeypatch.setattr(db_module, "current_revision", lambda name: "")
    assert _check() != 0
