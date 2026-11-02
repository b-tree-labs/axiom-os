# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The migration head carries the guard, stays in its own schema, and cannot
be downgraded away (ADR-143, ADR-052)."""

from __future__ import annotations

from sqlalchemy import text

from axiom.infra.db import engine_for

APPEND_ONLY = {
    "attest_records",
    "attest_presentations",
    "attest_anchors",
    "attest_responses",
    "attest_grant_uses",
}


def test_every_append_only_table_has_both_guards_at_head(attest_db):
    engine, _ = engine_for("attest")
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT c.relname, t.tgname FROM pg_trigger t "
                "JOIN pg_class c ON c.oid = t.tgrelid "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = :schema AND NOT t.tgisinternal"
            ),
            {"schema": attest_db},
        ).all()
    found = {(r.relname, r.tgname) for r in rows}
    for table in APPEND_ONLY:
        assert (table, f"{table}_append_only") in found
        assert (table, f"{table}_no_truncate") in found


def test_tables_live_only_in_the_extension_schema(attest_db):
    engine, _ = engine_for("attest")
    with engine.connect() as conn:
        schemas = conn.execute(
            text(
                "SELECT DISTINCT table_schema FROM information_schema.tables "
                "WHERE table_name LIKE 'attest\\_%' AND table_schema = ANY(:mine)"
            ),
            {"mine": [attest_db, "public"]},
        ).scalars()
        assert set(schemas) == {attest_db}


def test_downgrade_refuses_to_drop_evidence(attest_db):
    import pytest
    from alembic import command
    from alembic.config import Config

    from axiom.infra.db import extensions_with_migrations

    directory = dict(extensions_with_migrations())["attest"]
    config = Config()
    config.set_main_option("script_location", str(directory))
    # Every revision that holds evidence refuses; the newest refuses first.
    with pytest.raises(RuntimeError, match="cannot be downgraded"):
        command.downgrade(config, "base")
    with pytest.raises(RuntimeError, match="cannot be downgraded"):
        command.downgrade(config, "0001")


def test_no_column_or_index_still_says_book_at_head(attest_db):
    """ADR-150: "book" is the library's word. At head every attest table
    names its unit `logbook`, and no index name lags behind."""
    engine, _ = engine_for("attest")
    with engine.connect() as conn:
        cols = conn.execute(
            text(
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE table_schema = :s AND column_name IN ('book', 'logbook')"
            ),
            {"s": attest_db},
        ).all()
        stale = (
            conn.execute(
                text(
                    "SELECT indexname FROM pg_indexes WHERE schemaname = :s "
                    "AND indexname LIKE '%book%' AND indexname NOT LIKE '%logbook%'"
                ),
                {"s": attest_db},
            )
            .scalars()
            .all()
        )
    assert {c.column_name for c in cols} == {"logbook"}
    assert {c.table_name for c in cols} >= {
        "attest_records", "attest_chain_heads", "attest_drafts", "attest_intervals",
        "attest_obligation_events",
    }  # fmt: skip
    assert stale == []
