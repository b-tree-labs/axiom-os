# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Bringing the conformance schema up to the code, and saying what changed.

The failure this closes: Axiom 0.50.0 added three columns to silver.signals,
the deploy went green across all seven steps, and the columns were absent on
the node — a deploy is not a provision, and the conformance DDL was only ever
applied by a conform pass. The installed upsert then named columns the table
did not have.
"""

from __future__ import annotations

import logging

import pytest

from axiom.extensions.builtins.data_platform.skills import ensure_schema
from axiom.infra.skills import SkillContext


def _ctx(tmp_path):
    from axiom.extensions.builtins.data_platform import skills as data_skills

    return SkillContext(
        registry=data_skills.bind_default(),
        state_dir=tmp_path,
        logger=logging.getLogger("test"),
        user_prompt=None,
    )


class _Cursor:
    """Fake cursor whose column set grows only when an ALTER runs."""

    def __init__(self, columns, *, fail_on=None):
        self.columns = set(columns)
        self.executed: list[str] = []
        self._rows: list[tuple] = []
        self._fail_on = fail_on

    def execute(self, sql, params=None):
        self.executed.append(sql)
        if self._fail_on and self._fail_on in sql:
            raise RuntimeError("canceling statement due to lock timeout")
        if sql.startswith("SELECT column_name"):
            self._rows = [(c,) for c in sorted(self.columns)]
            return
        upper = sql.upper()
        if "ADD COLUMN IF NOT EXISTS" in upper:
            name = sql.split("ADD COLUMN IF NOT EXISTS")[1].split()[0]
            self.columns.add(name)

    def fetchall(self):
        return self._rows

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Conn:
    def __init__(self, cursor):
        self._cursor = cursor
        self.autocommit = False
        self.closed = False

    def cursor(self):
        return self._cursor

    def close(self):
        self.closed = True


@pytest.fixture
def patched(monkeypatch):
    holder = {}

    def _install(cursor):
        holder["cursor"] = cursor
        import psycopg2

        monkeypatch.setattr(psycopg2, "connect", lambda *a, **k: _Conn(cursor))
        return cursor

    return _install


def test_it_reports_the_columns_it_actually_added(patched, tmp_path):
    """Read back from information_schema, not asserted from intent."""
    patched(_Cursor({"site", "stream", "channel", "ts", "value"}))
    result = ensure_schema.run({"dsn": "postgresql://u@h/db"}, _ctx(tmp_path))

    assert result.ok
    added = result.value["columns_added"]
    for expected in ("uncertainty", "role", "derivation"):
        assert expected in added, f"{expected} should have been reported as added"
    assert any("added silver.signals.role" in a for a in result.actions_taken)


def test_a_current_schema_says_nothing_changed_rather_than_claiming_success(patched, tmp_path):
    patched(_Cursor({
        "site", "stream", "channel", "ts", "value", "unit", "quality", "source_class",
        "schema_ref", "row_hash", "model_ref", "basis", "uncertainty", "role", "derivation",
    }))
    result = ensure_schema.run({"dsn": "postgresql://u@h/db"}, _ctx(tmp_path))
    assert result.ok
    assert result.value["columns_added"] == []
    assert any("already current" in a for a in result.actions_taken)


def test_index_creation_is_skipped_and_said_out_loud(patched, tmp_path):
    """CREATE INDEX blocks writes; a deploy is the wrong place for it."""
    cur = patched(_Cursor({"site"}))
    result = ensure_schema.run({"dsn": "postgresql://u@h/db"}, _ctx(tmp_path))

    assert result.value["indexes_skipped"] >= 1
    assert not any("CREATE INDEX" in s.upper() for s in cur.executed)
    assert any("maintenance window" in a for a in result.actions_taken)


def test_the_lock_timeout_is_short_and_set_first(patched, tmp_path):
    """A deploy must never queue in front of readers on a live table."""
    cur = patched(_Cursor({"site"}))
    ensure_schema.run({"dsn": "postgresql://u@h/db"}, _ctx(tmp_path))
    assert cur.executed[0].startswith("SET lock_timeout")
    assert "'5s'" in cur.executed[0]

    cur2 = patched(_Cursor({"site"}))
    ensure_schema.run({"dsn": "postgresql://u@h/db", "lock_timeout": "30s"}, _ctx(tmp_path))
    assert "'30s'" in cur2.executed[0]


def test_a_blocked_lock_fails_loudly_rather_than_reporting_success(patched, tmp_path):
    """The whole point: a deploy that cannot reach the schema must not go green
    and then restart services onto a database behind the code they run."""
    patched(_Cursor({"site"}, fail_on="ADD COLUMN IF NOT EXISTS uncertainty"))
    result = ensure_schema.run({"dsn": "postgresql://u@h/db"}, _ctx(tmp_path))

    assert not result.ok
    assert any("schema not ensured" in e for e in result.errors)
    assert any("behind the code" in e for e in result.errors)


def test_an_empty_environment_falls_back_to_the_platforms_own_database(monkeypatch):
    """This test used to assert the opposite, and the opposite was the bug.

    With no env names set, this skill reported "no DSN" in the same deploy step
    where `axi db migrate upgrade head` had reached the database two lines
    earlier — because the migration path resolves through
    `axiom.infra.db.platform_db_url`, which falls back to a default, and this
    one only read the environment.

    The old test locked that in by asserting the error message listed all three
    variable names. A nicely-worded refusal to find a database the process is
    already connected to is still a refusal.
    """
    from axiom.infra.db import platform_db_url

    for var in ("DP1_RAG_DSN", "DATABASE_URL", "AXIOM_DB_URL"):
        monkeypatch.delenv(var, raising=False)
    assert ensure_schema._resolve_dsn({}) == platform_db_url()


def test_the_two_doors_to_the_database_agree(monkeypatch):
    """Whatever the platform would connect to, this skill resolves the same."""
    from axiom.infra.db import platform_db_url

    for var in ("DP1_RAG_DSN", "DATABASE_URL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AXIOM_DB_URL", "postgresql://u@h/agreed")
    assert ensure_schema._resolve_dsn({}) == platform_db_url()
    assert platform_db_url() == "postgresql://u@h/agreed"


def test_an_explicit_dsn_still_wins_over_everything(monkeypatch):
    monkeypatch.setenv("AXIOM_DB_URL", "postgresql://u@h/env")
    assert ensure_schema._resolve_dsn({"dsn": "postgresql://u@h/explicit"}) == (
        "postgresql://u@h/explicit"
    )


def test_it_accepts_the_same_dsn_names_session_for_uses(monkeypatch, patched, tmp_path):
    """data.backup could not find the database session_for finds every day,
    because they read different variables. Accept all three here."""
    for var in ("DP1_RAG_DSN", "DATABASE_URL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AXIOM_DB_URL", "postgresql://u@h/db")
    patched(_Cursor({"site"}))
    assert ensure_schema.run({}, _ctx(tmp_path)).ok


# ---------------------------------------------------------------------------
# A view you can only replace by appending.
#
# `role`, `uncertainty` and `derivation` were written into the gold views
# interleaved — `channel, role, ts, ...` — which reads better and is unusable:
#
#     ERROR: schema not ensured: cannot change name of view column "ts" to "role"
#
# CREATE OR REPLACE VIEW may only append. The deploy stopped there, correctly,
# before restarting services onto a database behind the code.
# ---------------------------------------------------------------------------


def test_the_gold_view_columns_are_only_ever_appended():
    """The frozen base order must be a literal prefix of the full order.

    Not a hand-copied list: this reads both constants and checks the
    relationship between them, so inserting a column anywhere but the end
    fails here rather than on a node mid-deploy.
    """
    from axiom.extensions.builtins.data_platform.conformance import (
        GOLD_SIGNALS_BASE_COLUMNS,
        GOLD_SIGNALS_COLUMNS,
    )

    n = len(GOLD_SIGNALS_BASE_COLUMNS)
    assert GOLD_SIGNALS_COLUMNS[:n] == GOLD_SIGNALS_BASE_COLUMNS, (
        "a gold view column was inserted or reordered. CREATE OR REPLACE VIEW "
        "cannot do that — Postgres refuses with 'cannot change name of view "
        "column', and the deploy stops. New columns go at the END."
    )


def test_the_base_order_matches_what_live_installs_actually_have():
    """The ten columns read off the node, in order. Changing this is a migration."""
    from axiom.extensions.builtins.data_platform.conformance import GOLD_SIGNALS_BASE_COLUMNS

    assert GOLD_SIGNALS_BASE_COLUMNS == (
        "site", "stream", "channel", "ts", "value",
        "unit", "quality", "source_class", "model_ref", "basis",
    )


def test_the_view_ddl_selects_the_columns_in_that_order():
    """The constant and the SQL cannot drift: the SQL is built from it."""
    from axiom.extensions.builtins.data_platform.conformance import (
        GOLD_SIGNALS_COLUMNS,
        GOLD_SIGNALS_DDL,
    )

    # Only the two signal-projection views carry the column contract; the
    # freshness views (gold.ingest_freshness / gold.ingest_stale) aggregate
    # over silver and have their own shape.
    views = [
        s
        for s in GOLD_SIGNALS_DDL
        if "CREATE OR REPLACE VIEW gold.signals" in s
    ]
    assert len(views) == 2
    expected = ", ".join(GOLD_SIGNALS_COLUMNS)
    for stmt in views:
        assert expected in " ".join(stmt.split()), stmt


def test_a_drifted_view_is_recreated_rather_than_blocking_the_deploy(patched, tmp_path):
    """A database predating the append-only rule must be recoverable."""
    conflict = 'cannot change name of view column "ts" to "role"'

    class _ViewCursor(_Cursor):
        def __init__(self):
            super().__init__({"site"})
            self._refused = set()

        def execute(self, sql, params=None):
            if "CREATE OR REPLACE VIEW gold.signals " in sql and "gold.signals" not in self._refused:
                self._refused.add("gold.signals")
                self.executed.append(sql)
                raise RuntimeError(conflict)
            return super().execute(sql, params)

    cur = patched(_ViewCursor())
    result = ensure_schema.run({"dsn": "postgresql://u@h/db"}, _ctx(tmp_path))

    assert result.ok, result.errors
    assert "gold.signals" in result.value["views_recreated"]
    assert any("DROP VIEW IF EXISTS gold.signals" in s for s in cur.executed)
    assert any("append-only" in a for a in result.actions_taken)


def test_an_unrelated_view_error_still_fails_the_deploy(patched, tmp_path):
    """The recovery is for one specific, recognisable conflict — not a catch-all.

    Dropping a view because *any* statement failed would turn a syntax error or
    a permissions problem into silent data-shape churn.
    """

    class _BadCursor(_Cursor):
        def __init__(self):
            super().__init__({"site"})

        def execute(self, sql, params=None):
            if "CREATE OR REPLACE VIEW" in sql:
                raise RuntimeError("permission denied for schema gold")
            return super().execute(sql, params)

    cur = patched(_BadCursor())
    result = ensure_schema.run({"dsn": "postgresql://u@h/db"}, _ctx(tmp_path))

    assert not result.ok
    assert "permission denied" in " ".join(result.errors)
    assert not any("DROP VIEW" in s for s in cur.executed), "must not drop on an unrelated error"


# ---------------------------------------------------------------------------
# Postgres has THREE ways to refuse a reshaping CREATE OR REPLACE VIEW, and the
# recovery only recognised one of them.
#
# Observed on a live node, three mornings running, in the nightly telemetry
# conformance:
#
#     psycopg.errors.InvalidTableDefinition: cannot drop columns from view
#
# That is the same class of problem as "cannot change name of view column" and
# wants the same remedy — drop the view and recreate it — but the guard matched
# a single literal, so the recovery that existed for one case never fired for
# the other two. silver.signals went stale nightly while the recovery code sat
# right there.
# ---------------------------------------------------------------------------

_PG_RESHAPE_REFUSALS = (
    'cannot change name of view column "ts" to "role"',
    "cannot drop columns from view",
    'cannot change data type of view column "value" from integer to numeric',
)


@pytest.mark.parametrize("message", _PG_RESHAPE_REFUSALS)
def test_every_postgres_reshape_refusal_is_recognised(message):
    """All three are 'this view cannot be replaced in place'. Matching one
    literal made the other two fatal."""
    from axiom.extensions.builtins.data_platform.conformance import (
        is_view_reshape_conflict,
    )

    assert is_view_reshape_conflict(message) is True


def test_an_unrelated_error_is_not_mistaken_for_a_reshape():
    """The negative control. Dropping and recreating a view in response to, say,
    a permission error would turn a visible failure into data loss."""
    from axiom.extensions.builtins.data_platform.conformance import (
        is_view_reshape_conflict,
    )

    for other in (
        "permission denied for schema silver",
        "relation gold.signals does not exist",
        "deadlock detected",
        "could not serialize access due to concurrent update",
    ):
        assert is_view_reshape_conflict(other) is False, other


def test_a_reordered_view_is_repaired():
    """The site's nightly conformance executes the DDL directly and never
    reaches the skill, so the recovery has to be callable on its own — a
    mechanism only one caller can reach is one the other callers do without.
    """
    from axiom.extensions.builtins.data_platform.conformance import apply_conformance_ddl

    executed, dropped = [], []

    class _Cur:
        def execute(self, sql, params=None):
            executed.append(sql)
            if sql.startswith("DROP VIEW"):
                dropped.append(sql)
                return
            if "CREATE OR REPLACE VIEW gold.signals " in sql and not dropped:
                raise RuntimeError('cannot change name of view column "ts" to "role"')

    recreated = apply_conformance_ddl(_Cur(), ["CREATE OR REPLACE VIEW gold.signals AS SELECT 1"])
    assert recreated == ["gold.signals"]
    assert any(s.startswith("DROP VIEW") for s in executed)


def test_a_narrowing_view_is_REFUSED_not_repaired():
    """The case that actually occurred, and the one where "repair" is wrong.

    A node's nightly conformance ran on axiom 0.47.0 while gold.signals had
    been created by 0.58.x: ten columns being written over thirteen. Dropping
    and recreating would have silently removed role, uncertainty and derivation
    from a view the dashboards read, and the nightly job would then have fought
    the serving version for the view's shape every night.

    Making the error go away would have been worse than the error.
    """
    from axiom.extensions.builtins.data_platform.conformance import (
        ViewDowngradeRefused,
        apply_conformance_ddl,
    )

    executed = []

    class _Cur:
        def execute(self, sql, params=None):
            executed.append(sql)
            if sql.startswith("CREATE OR REPLACE VIEW"):
                raise RuntimeError("cannot drop columns from view")

    with pytest.raises(ViewDowngradeRefused, match="version skew"):
        apply_conformance_ddl(_Cur(), ["CREATE OR REPLACE VIEW gold.signals AS SELECT 1"])

    assert not any(s.startswith("DROP VIEW") for s in executed), (
        "a narrowing definition must never reach a DROP"
    )


def test_a_non_reshape_error_still_propagates_from_the_applier():
    """A permission error must not be silently converted into a view drop."""
    from axiom.extensions.builtins.data_platform.conformance import apply_conformance_ddl

    class _Cur:
        def execute(self, sql, params=None):
            raise RuntimeError("permission denied for schema silver")

    with pytest.raises(RuntimeError, match="permission denied"):
        apply_conformance_ddl(_Cur(), ["CREATE OR REPLACE VIEW gold.signals AS SELECT 1"])
