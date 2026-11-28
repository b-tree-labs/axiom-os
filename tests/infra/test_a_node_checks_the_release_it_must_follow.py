# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A contracting migration runs only on a node shown to run the release it names (ADR-182 D4a).

The signals tables moved across two releases, 0.70.47 and 0.70.48. What kept
0.70.48's revision off a node still on 0.70.45 was an agreement between
sessions. A process older than 0.70.47 run after that revision would have found
no tables at the old name and created them again, empty.

The rule is tested here without a database: a real Alembic script directory is
written to a temporary folder, and the one path that brings a schema to head is
driven with its database calls replaced, so the test can say whether the
upgrade was attempted.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from axiom.infra import db
from axiom.infra.deploy import proven_release as pr

EXPAND = '''"""add a column"""
revision = "r1"
down_revision = None

def upgrade():
    pass
'''

CONTRACT = '''"""drop the old table"""
revision = "r2"
down_revision = "r1"

def upgrade():
    # contract: the public signals tables unused since 0.70.47
    pass
'''

EXPAND_AGAIN = '''"""add another column"""
revision = "r2"
down_revision = "r1"

def upgrade():
    pass
'''


def _scripts(tmp_path: Path, second: str = CONTRACT) -> Path:
    directory = tmp_path / "migrations"
    (directory / "versions").mkdir(parents=True)
    (directory / "env.py").write_text("")
    (directory / "script.py.mako").write_text("")
    (directory / "versions" / "r1_expand.py").write_text(EXPAND)
    (directory / "versions" / "r2_second.py").write_text(second)
    return directory


@pytest.fixture
def record(tmp_path, monkeypatch):
    path = tmp_path / "state" / "releases" / "proven.json"
    monkeypatch.setenv("AXI_PROVEN_RELEASE", str(path))
    monkeypatch.setenv("AXI_CHANGE_LEDGER", str(tmp_path / "state" / "changes.jsonl"))
    return path


@pytest.fixture
def node(tmp_path, monkeypatch, record):
    """The migration path with its database replaced: ``node.at`` is the schema's
    revision, and it moves to head only if the upgrade is actually attempted."""

    class Node:
        at = "r1"
        upgrades = 0
        directory = _scripts(tmp_path)

        def migrate(self):
            return db.provision_extension("demo", self.directory)

    state = Node()

    def upgrade(_config, _target):
        state.upgrades += 1
        state.at = "r2"

    import alembic.command

    monkeypatch.setattr(alembic.command, "upgrade", upgrade)
    monkeypatch.setattr(db, "current_revision", lambda _name: state.at)
    monkeypatch.setattr(db, "engine_for", lambda _name: (object(), None))
    return state


def test_a_node_that_proved_an_older_release_refuses_and_the_schema_does_not_move(node):
    pr.write("0.70.45", ["axiom-serve"])
    result = node.migrate()
    assert not result.ok
    assert node.upgrades == 0 and node.at == "r1"
    assert "0.70.47" in result.error and "0.70.45" in result.error
    assert "Deploy 0.70.47" in result.error


def test_the_same_node_applies_it_after_proving_the_named_release(node):
    pr.write("0.70.45", ["axiom-serve"])
    assert not node.migrate().ok
    pr.write("0.70.47", ["axiom-serve"])
    result = node.migrate()
    assert result.ok, result.error
    assert node.at == "r2"


def test_a_later_release_than_the_one_named_is_enough(node):
    pr.write("0.71.2", ["axiom-serve"])
    assert node.migrate().ok


def test_a_fresh_database_applies_it_with_no_record(node, record):
    node.at = ""
    assert not record.exists()
    result = node.migrate()
    assert result.ok, result.error
    assert node.upgrades == 1


def test_a_node_with_no_record_refuses_and_names_the_way_through(node, record):
    result = node.migrate()
    assert not result.ok
    assert node.upgrades == 0
    assert "--prerequisite-confirmed 0.70.47" in result.error


def test_removing_the_record_makes_the_check_fail(node, record):
    pr.write("0.70.47", ["axiom-serve"])
    record.unlink()
    assert not node.migrate().ok
    assert node.upgrades == 0


def test_a_record_nobody_can_read_is_no_record(node, record):
    record.parent.mkdir(parents=True)
    record.write_text("{not json")
    assert not node.migrate().ok


def test_the_operators_statement_lets_it_through_and_is_in_the_change_ledger(
    node, record, tmp_path
):
    assert not node.migrate().ok
    proof = pr.confirm("0.70.47", who="operator")
    assert proof.confirmed_by == "operator"
    assert node.migrate().ok
    changes = [
        json.loads(line) for line in (tmp_path / "state" / "changes.jsonl").read_text().splitlines()
    ]
    stated = [
        c
        for c in changes
        if c.get("subject") == "release-prerequisite" and c.get("phase") == "intent"
    ]
    assert len(stated) == 1
    assert "operator" in stated[0]["detail"] and "0.70.47" in stated[0]["detail"]


def test_a_statement_cannot_replace_a_record(node):
    pr.write("0.70.45", ["axiom-serve"])
    with pytest.raises(pr.AlreadyProven):
        pr.confirm("0.70.47", who="operator")
    assert pr.read().version == "0.70.45"
    assert not node.migrate().ok


def test_an_expand_only_revision_is_never_refused(node, tmp_path, record):
    node.directory = _scripts(tmp_path / "other", EXPAND_AGAIN)
    assert not record.exists()
    result = node.migrate()
    assert result.ok, result.error
    assert node.upgrades == 1


def test_a_schema_already_at_head_is_not_refused(node):
    node.at = "r2"
    assert node.migrate().ok


# -- proving a release -----------------------------------------------------------------


def test_a_release_is_proven_only_when_every_unit_started_on_the_installed_code(record):
    started = {"axiom-serve": 200.0, "axiom-rag": 50.0}
    proof, stale = pr.prove("0.70.47", started, installed_at=100.0, started_at=started.get)
    assert proof is None and stale == ["axiom-rag"]
    assert not record.exists(), "installed is not proven"

    started["axiom-rag"] = 150.0
    proof, stale = pr.prove("0.70.47", started, installed_at=100.0, started_at=started.get)
    assert stale == [] and proof.version == "0.70.47"
    assert pr.read().units == ("axiom-serve", "axiom-rag")


def test_a_unit_whose_start_cannot_be_read_is_not_proven(record):
    proof, stale = pr.prove(
        "0.70.47", ["axiom-serve"], installed_at=100.0, started_at=lambda _u: None
    )
    assert proof is None and stale == ["axiom-serve"]


def test_no_units_proves_nothing(record):
    proof, _stale = pr.prove("0.70.47", [], installed_at=100.0, started_at=lambda _u: 200.0)
    assert proof is None and not record.exists()


# -- the signals path runs Alembic itself, so it asks on its own ---------------------------


def test_the_signals_upgrade_asks_too(record, tmp_path, monkeypatch, capsys):
    import alembic.command

    from axiom.extensions.builtins.signals import migrations as signals

    directory = _scripts(tmp_path / "signals")
    attempted = []
    monkeypatch.setattr(signals, "MIGRATIONS_DIR", directory)
    monkeypatch.setattr(signals, "get_alembic_config", lambda: object())
    monkeypatch.setattr(signals, "get_current_revision", lambda: "r1")
    monkeypatch.setattr(alembic.command, "upgrade", lambda _c, r: attempted.append(r))

    pr.write("0.70.45", ["axiom-serve"])
    assert signals.run_migrations("upgrade", "head") is False
    assert attempted == []
    assert "0.70.47" in capsys.readouterr().out

    pr.write("0.70.47", ["axiom-serve"])
    assert signals.run_migrations("upgrade", "head") is True
    assert attempted == ["head"]


def test_the_migrate_command_records_the_statement_and_refuses_it_on_a_node_with_a_record(
    record, capsys
):
    import argparse

    from axiom.extensions.builtins.db import cli

    pr.write("0.70.45", ["axiom-serve"])
    args = argparse.Namespace(
        migrate_command="upgrade", revision="head", prerequisite_confirmed="0.70.47"
    )
    assert cli.cmd_migrate(args) == 1
    assert "refused" in capsys.readouterr().out
    assert pr.read().version == "0.70.45"
