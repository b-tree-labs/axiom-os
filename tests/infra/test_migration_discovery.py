# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Extensions that ship migrations must be discoverable.

Written after finding that the discovery used to report them globbed
``extensions/builtins/*/alembic.ini`` — one directory short of where the file
lives, AND keyed on a file only one extension ships. It therefore returned an
empty list under every condition: a check that could not fail, reporting "no
other extensions" while four shipped migrations nothing ran.

The marker is ``migrations/env.py``, which is what every one of them has.
"""

from __future__ import annotations

from axiom.infra.db import extensions_with_migrations


class TestDiscovery:
    def test_finds_every_extension_that_ships_migrations(self):
        found = {name for name, _ in extensions_with_migrations()}
        # Discovered from the tree, so the assertion is on the floor, not the
        # exact set — a new extension shipping migrations must not fail this.
        assert {"vault", "authz", "notifications", "schedule", "signals"} <= found

    def test_each_discovered_path_is_a_real_migration_environment(self):
        for name, directory in extensions_with_migrations():
            assert (directory / "env.py").is_file(), name
            assert (directory / "versions").is_dir(), name

    def test_chat_ships_migrations_so_the_session_store_is_versioned(self):
        assert "chat" in {name for name, _ in extensions_with_migrations()}


class TestMigrateReportsEveryExtension:
    """`axi db migrate check` used to say "Up to date (signals migrations)"
    and nothing at all about the others, because its discovery could not
    return anything. Issue #826."""

    def test_the_status_block_is_not_structurally_empty(self, monkeypatch):
        from pathlib import Path

        import axiom.infra.db as db_module
        from axiom.extensions.builtins.db.cli import _other_extension_status

        fake = [
            ("signals", Path("/nowhere/signals/migrations")),
            ("chat", Path("/nowhere/chat/migrations")),
            ("vault", Path("/nowhere/vault/migrations")),
        ]
        monkeypatch.setattr(db_module, "extensions_with_migrations", lambda: fake)
        lines = _other_extension_status()
        # signals is reported by check_migrations() above this block; the
        # others must each get a line rather than being silently dropped.
        assert len(lines) == 2
        assert not any("signals" in line for line in lines)
        assert any("chat" in line for line in lines)
        assert any("vault" in line for line in lines)

    def test_an_unreadable_extension_is_named_not_skipped(self, monkeypatch):
        from pathlib import Path

        import axiom.infra.db as db_module
        from axiom.extensions.builtins.db.cli import _other_extension_status

        monkeypatch.setattr(
            db_module,
            "extensions_with_migrations",
            lambda: [("broken", Path("/definitely/not/here"))],
        )
        lines = _other_extension_status()
        assert len(lines) == 1
        assert "broken" in lines[0] and "unreadable" in lines[0]
