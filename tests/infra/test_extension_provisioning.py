# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Provisioning runs an extension's MIGRATIONS, not `create_all`.

`create_all` makes tables and stamps no version, so the first column change
afterwards has no upgrade path and `db migrate` reports "up to date" over
tables Alembic never made — the false green in issue #826. These tests pin
that the provisioning path leaves a real revision behind.
"""

from __future__ import annotations

from axiom.infra.db import (
    ProvisionResult,
    extensions_with_migrations,
    provision_extension,
)


class TestResultReporting:
    """A provisioning pass has to say what it actually did."""

    def test_a_move_reports_both_ends(self):
        result = ProvisionResult("chat", "chat", before="", after="0001", ok=True)
        assert result.migrated is True
        assert result.summary == "chat: base → 0001"

    def test_a_no_op_is_not_reported_as_a_move(self):
        result = ProvisionResult("chat", "chat", before="0001", after="0001", ok=True)
        assert result.migrated is False
        assert result.summary == "chat: up to date (0001)"

    def test_a_failure_never_reads_as_success(self):
        result = ProvisionResult(
            "chat", "chat", before="", after="", ok=False, error="boom"
        )
        assert result.migrated is False
        assert "failed" in result.summary and "boom" in result.summary


class TestFailureIsReportedNotRaised:
    """One extension's broken migrations must not stop a node provisioning
    the rest. Reporting is the contract; raising breaks the whole pass."""

    def test_an_extension_without_migrations_is_reported(self):
        result = provision_extension("nonexistent-extension")
        assert result.ok is False
        assert result.error == "ships no migrations"

    def test_an_unusable_migrations_directory_is_reported(self, tmp_path):
        empty = tmp_path / "not-a-migration-env"
        empty.mkdir()
        result = provision_extension("chat", migrations_dir=empty)
        assert result.ok is False
        assert result.summary.startswith("chat: failed")


class TestDiscoveryFeedsProvisioning:
    def test_every_discovered_extension_can_be_looked_up(self):
        from axiom.infra.db import _migrations_dir_for

        for name, directory in extensions_with_migrations():
            assert _migrations_dir_for(name) == directory

    def test_lookup_of_an_unknown_extension_returns_none(self):
        from axiom.infra.db import _migrations_dir_for

        assert _migrations_dir_for("nonexistent-extension") is None


class TestChatRevision:
    """The session store's own revision, read from the script directory —
    this is what a node upgrades to."""

    def test_chat_head_creates_the_sessions_table(self):
        from alembic.script import ScriptDirectory

        directory = None
        for name, path in extensions_with_migrations():
            if name == "chat":
                directory = path
        assert directory is not None
        script = ScriptDirectory(str(directory))
        # Not pinned to a revision id — adding a migration is ordinary, and a
        # pin turns that into a failure here instead of where it would matter.
        # What a node actually needs is ONE resolvable head: a branched chain
        # cannot be upgraded, and that is the failure worth catching.
        heads = script.get_heads()
        assert len(heads) == 1, f"chat migrations branched into {heads}; a node cannot upgrade that"
        assert script.get_current_head() == heads[0]
        source = (directory / "versions" / "0001_initial.py").read_text()
        assert "chat_sessions" in source
        # Resuming asks for the newest session for a principal; the index has
        # to carry the sort column or that answer scans the whole history.
        assert "ix_chat_sessions_principal_updated" in source
        assert '"principal_id", "updated_at"' in source

    def test_chat_revision_is_reversible(self):
        directory = dict(extensions_with_migrations())["chat"]
        source = (directory / "versions" / "0001_initial.py").read_text()
        assert "def downgrade()" in source
        assert "drop_table" in source


class TestStrayVersionLookup:
    """This helper runs INSIDE the failure path, to explain a failure. If it
    can raise, it converts a reported failure into an unhandled one."""

    def test_it_never_raises_when_the_query_cannot_run(self):
        from sqlalchemy import create_engine

        from axiom.infra.db import _stray_version_schema

        # SQLite has no information_schema, so the query fails outright.
        engine = create_engine("sqlite://")
        assert _stray_version_schema(engine, "001") == ""

    def test_it_never_raises_on_an_unreachable_database(self):
        from sqlalchemy import create_engine

        from axiom.infra.db import _stray_version_schema

        # The driver is named. A bare "postgresql://" leaves it to
        # SQLAlchemy's default, which is psycopg2 on 2.0 and psycopg (v3)
        # on 2.1 — so under a fresh install this line raised
        # ModuleNotFoundError while BUILDING the fixture, and the test
        # about a helper that never raises died before reaching it.
        engine = create_engine("postgresql+psycopg2://nobody@127.0.0.1:1/none")
        assert _stray_version_schema(engine, "001") == ""
