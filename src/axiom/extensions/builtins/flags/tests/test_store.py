# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The flag_overrides table: upsert, delete, provenance, and the service over it.

SQLite through the store's provider seam (conftest), never the dev database.
The migration is checked for the ADR-052 rules: every op names the schema.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from axiom.extensions.builtins.flags.service import FlagService


def test_put_inserts_then_updates_one_row(sqlite_store):
    sqlite_store.put("node", "node-a", "history_timeline", "on", by="@cli:local")
    sqlite_store.put("node", "node-a", "history_timeline", "soon", by="@op:site")
    rows = sqlite_store.rows()
    assert len(rows) == 1
    row = rows[0]
    assert (row["scope"], row["scope_id"], row["key"], row["value"]) == (
        "node", "node-a", "history_timeline", "soon")
    assert row["updated_by"] == "@op:site" and row["updated_at"]


def test_overrides_are_kept_per_scope_and_id(sqlite_store):
    sqlite_store.put("node", "node-a", "history_timeline", "on", by="x")
    sqlite_store.put("account", "acct", "history_timeline", "off", by="x")
    sqlite_store.put("user", "u1", "history_timeline", "soon", by="x")
    sqlite_store.put("user", "u2", "library", "on", by="x")
    assert sqlite_store.overrides_for("user", "u1") == {"history_timeline": "soon"}
    assert sqlite_store.overrides_for("user", "u2") == {"library": "on"}
    assert sqlite_store.overrides_for("account", "acct") == {"history_timeline": "off"}
    assert sqlite_store.overrides_for("user", None) == {}
    assert [r["scope"] for r in sqlite_store.rows("history_timeline")] == ["node", "account", "user"]


def test_delete_removes_only_the_named_row(sqlite_store):
    sqlite_store.put("user", "u1", "history_timeline", "on", by="x")
    sqlite_store.put("user", "u1", "library", "on", by="x")
    assert sqlite_store.delete("user", "u1", "history_timeline") is True
    assert sqlite_store.delete("user", "u1", "history_timeline") is False
    assert sqlite_store.overrides_for("user", "u1") == {"library": "on"}


def test_an_unknown_scope_is_refused(sqlite_store):
    with pytest.raises(ValueError):
        sqlite_store.put("team", "t", "history_timeline", "on", by="x")


def test_the_service_resolves_through_the_real_store(sqlite_store, monkeypatch):
    service = FlagService(sqlite_store)
    service.set_override("node", "node-a", "history_timeline", "on", by="@op")
    service.set_override("user", "u1", "history_timeline", "soon", by="@u1")
    assert service.resolve(node="node-a").flags["history_timeline"] == "on"
    res = service.resolve(node="node-a", user="u1")
    assert (res.flags["history_timeline"], res.sources["history_timeline"]) == ("soon", "user")


def test_the_migration_stays_in_its_schema():
    src = (Path(__file__).parent.parent / "migrations" / "versions" / "0001_flag_overrides.py").read_text()
    src = src.split('"""', 2)[2]  # the code, not the docstring
    ops = src.count("op.create_table(") + src.count("op.create_index(") + src.count("op.drop_")
    assert ops == src.count("schema=SCHEMA"), "every op names schema=SCHEMA (ADR-052)"
    assert 'SCHEMA = "flags"' in src and "public" not in src
    for col in ("scope", "scope_id", "key", "value", "updated_by", "updated_at"):
        assert f'"{col}"' in src


def test_the_models_name_no_schema():
    from axiom.extensions.builtins.flags.db_models import FlagOverride

    assert FlagOverride.__table__.schema is None
