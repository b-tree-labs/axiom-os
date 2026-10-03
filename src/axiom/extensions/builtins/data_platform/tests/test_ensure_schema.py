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
        # Cleared per statement. Holding the previous answer made this fake
        # reply to "what views depend on gold.signals" with a list of COLUMN
        # NAMES, and the repair path duly refused to touch a view because
        # something called `site` read it.
        self._rows = []
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
    patched(_Cursor({"site", "feed", "channel", "ts", "value"}))
    result = ensure_schema.run({"dsn": "postgresql://u@h/db"}, _ctx(tmp_path))

    assert result.ok
    added = result.value["columns_added"]
    for expected in ("uncertainty", "role", "derivation", "quality_reason"):
        assert expected in added, f"{expected} should have been reported as added"
    assert any("added silver.signals.role" in a for a in result.actions_taken)


def test_a_current_schema_says_nothing_changed_rather_than_claiming_success(patched, tmp_path):
    patched(
        _Cursor(
            {
                "site",
                "feed",
                "channel",
                "ts",
                "value",
                "unit",
                "quality",
                "source_class",
                "schema_ref",
                "row_hash",
                "model_ref",
                "basis",
                "uncertainty",
                "role",
                "derivation",
                "quality_reason",
            }
        )
    )
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
    from axiom.infra.db import libpq_url, platform_db_url

    for var in ("DP1_RAG_DSN", "DATABASE_URL", "AXIOM_DB_URL"):
        monkeypatch.delenv(var, raising=False)

    # The platform's own database, in LIBPQ spelling. The equality used to be
    # against `platform_db_url()` directly, and that broke the moment the
    # platform URL started naming its driver so SQLAlchemy would not pick
    # psycopg3: `psycopg2.connect("postgresql+psycopg2://…")` raises
    # `invalid dsn`, because `+psycopg2` is dialect notation and means nothing
    # to a connection string.
    #
    # Two audiences want opposite spellings of one URL. Every caller of
    # `resolve_dsn` connects directly rather than building an Engine, so this
    # side strips.
    assert ensure_schema._resolve_dsn({}) == libpq_url(platform_db_url())


def test_the_resolved_dsn_is_one_psycopg2_will_actually_accept(monkeypatch):
    """The assertion the equality above cannot make.

    Comparing two strings proves they match; it does not prove either one
    works. The agent-facing gold tools failed on a default-configured node
    with `invalid dsn` while `axi db migrate` on the same node succeeded, and
    a string-equality test could not see the difference.

    So this parses the resolved DSN the way libpq does. No connection is
    opened — `parse_dsn` is the part that was raising.
    """
    pytest.importorskip("psycopg2")
    from psycopg2.extensions import parse_dsn

    for var in ("DP1_RAG_DSN", "DATABASE_URL", "AXIOM_DB_URL"):
        monkeypatch.delenv(var, raising=False)
    parsed = parse_dsn(ensure_schema._resolve_dsn({}))
    assert parsed.get("dbname")

    # And from the environment, which is where a Helm chart would put a
    # SQLAlchemy-shaped URL.
    monkeypatch.setenv("AXIOM_DB_URL", "postgresql+psycopg2://u:p@h:5432/from_env")
    assert parse_dsn(ensure_schema._resolve_dsn({}))["dbname"] == "from_env"


def test_an_explicit_dsn_is_returned_verbatim(monkeypatch):
    """A caller who passed a string knows what they meant, so it is not
    rewritten — including a deliberate driver suffix for a caller that does
    build an Engine."""
    monkeypatch.delenv("AXIOM_DB_URL", raising=False)
    named = "postgresql+psycopg://u@h/explicit"
    assert ensure_schema._resolve_dsn({"dsn": named}) == named


def test_the_two_doors_to_the_database_agree(monkeypatch):
    """Whatever the platform would connect to, this skill resolves the same."""
    from axiom.infra.db import platform_db_url

    for var in ("DP1_RAG_DSN", "DATABASE_URL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AXIOM_DB_URL", "postgresql://u@h/agreed")
    # A bare URL needs no stripping, so the two doors still agree exactly —
    # which is the case this test was written for.
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
        "site",
        "feed",
        "channel",
        "ts",
        "value",
        "unit",
        "quality",
        "source_class",
        "model_ref",
        "basis",
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
    # Matched by the exact view name, not by prefix: `gold.signals_uncertainty`
    # is also a `gold.signals*` view and carries a DIFFERENT contract (it is
    # the structured-uncertainty companion, keyed rather than projected), so a
    # prefix match would drag it in and fail on a shape it was never meant to
    # have.
    views = [
        s
        for s in GOLD_SIGNALS_DDL
        if "CREATE OR REPLACE VIEW gold.signals AS" in s
        or "CREATE OR REPLACE VIEW gold.signals_latest AS" in s
    ]
    assert len(views) == 2
    plain = next(v for v in views if "gold.signals AS" in v)
    latest = next(v for v in views if "signals_latest" in v)

    # The pass-through view selects the contract contiguously.
    assert ", ".join(GOLD_SIGNALS_COLUMNS) in " ".join(plain.split()), plain

    # `signals_latest` selects the same columns in the same ORDER, but not
    # contiguously: `ts_ambiguous` sits inside the tail because the flag was
    # added before `quality_reason`, and CREATE OR REPLACE VIEW cannot move an
    # existing column. So the check is order, not adjacency — which is the
    # property that actually matters, since a reordering is what a deploy
    # refuses.
    flat = " ".join(latest.split())
    seen = [flat.index(c) for c in GOLD_SIGNALS_COLUMNS]
    assert seen == sorted(seen), (
        f"signals_latest selects the contract out of order: {GOLD_SIGNALS_COLUMNS}"
    )


def test_a_drifted_view_is_recreated_rather_than_blocking_the_deploy(patched, tmp_path):
    """A database predating the append-only rule must be recoverable."""
    conflict = 'cannot change name of view column "ts" to "role"'

    class _ViewCursor(_Cursor):
        def __init__(self):
            super().__init__({"site"})
            self._refused = set()

        def execute(self, sql, params=None):
            if (
                "CREATE OR REPLACE VIEW gold.signals " in sql
                and "gold.signals" not in self._refused
            ):
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
        """Answers the two catalogue questions the repair asks before it drops:
        what reads this view, and what is granted on it."""

        def __init__(self):
            self._rows: list[tuple] = []

        def execute(self, sql, params=None):
            executed.append(sql)
            self._rows = []
            if sql.startswith("DROP VIEW"):
                dropped.append(sql)
                return
            if "CREATE OR REPLACE VIEW gold.signals " in sql and not dropped:
                raise RuntimeError('cannot change name of view column "ts" to "role"')

        def fetchall(self):
            return self._rows

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
