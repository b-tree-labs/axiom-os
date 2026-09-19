# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``data.ensure_schema`` — bring the conformance tier's schema up to the code.

The Alembic-versioned extensions are handled by ``axi db migrate upgrade head``.
The conformance tier is not versioned that way: ``silver.signals`` and the gold
views are raw DDL, and the only thing that applied them was a conform pass.

That lazy application is why a release could install cleanly and leave the
database behind it. Axiom 0.50.0 added ``uncertainty``, ``role`` and
``derivation``, the deploy went green across all seven steps, and the columns
were absent on the node — because a deploy is not a provision, and no conform
timer was armed to apply them later. The installed ``pg_upsert`` named three
columns the table did not have, so the next conform would have failed. A
release whose schema change silently does not happen is the failure this exists
to close.

**It reports the difference, not the outcome.** "Schema ensured" is the kind of
statement that has been wrong all week. This returns the columns it actually
added, read back from ``information_schema`` afterwards, so a deploy log says
``added silver.signals.role`` or says nothing changed — both checkable.

Indexes are deliberately NOT created here. A partial index over a column that
is NULL in every one of tens of millions of rows still scans the table to
discover that, and ``CREATE INDEX`` blocks writes for the duration. Adding a
nullable column is metadata-only and safe in a deploy; building an index is a
maintenance operation that wants a window and someone watching.
"""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

#: How long any single DDL statement may wait for its lock. Short on purpose:
#: a deploy must never queue in front of readers on a live table. If the lock
#: is held, this fails and says so, which is recoverable — blocking the node
#: behind a deploy step is not.
DEFAULT_LOCK_TIMEOUT = "5s"


def _columns(cur, schema: str, table: str) -> set[str]:
    cur.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = %s AND table_name = %s",
        (schema, table),
    )
    return {r[0] for r in cur.fetchall()}



#: Postgres refuses a CREATE OR REPLACE VIEW that changes the name, type or
#: order of an existing column — only appending is allowed. The message is
#: distinctive, so it can be recognised without parsing a SQLSTATE that also
#: covers unrelated failures.
_VIEW_SHAPE_CONFLICT = "cannot change name of view column"


def _view_name(stmt: str) -> str | None:
    """``gold.signals`` out of a CREATE OR REPLACE VIEW statement."""
    import re

    m = re.search(r"CREATE\s+OR\s+REPLACE\s+VIEW\s+([A-Za-z0-9_.\"]+)", stmt, re.I)
    return m.group(1) if m else None

def _resolve_dsn(params: dict[str, Any]) -> str | None:
    """Explicit DSN, then the env names in use, then the platform's own answer.

    The last step is the point. Without it, a node whose environment carries
    none of these names got "no DSN" from this skill in the same deploy step
    where `axi db migrate upgrade head` had just reached the database two lines
    earlier — because the migration path resolves through
    :func:`axiom.infra.db.platform_db_url`, which falls back to a default, and
    this one only read the environment and gave up.

    Two doors to the same database must not disagree about whether it exists.
    """
    from .._dsn import resolve_dsn

    return resolve_dsn(params)


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """Apply the conformance tier's DDL and report what actually changed.

    Params: ``dsn`` (else DP1_RAG_DSN / DATABASE_URL / AXIOM_DB_URL),
    ``lock_timeout`` (default ``5s``).
    """
    dsn = _resolve_dsn(params)
    if not dsn:
        return SkillResult(
            ok=False,
            errors=[
                "no DSN: pass dsn= or set DP1_RAG_DSN / DATABASE_URL / AXIOM_DB_URL. "
                "Note these three names all address the same database and are not "
                "interchangeable across skills — that inconsistency is its own bug."
            ],
        )

    from ..conformance import GOLD_SIGNALS_DDL, SILVER_SIGNALS_DDL

    lock_timeout = str(params.get("lock_timeout") or DEFAULT_LOCK_TIMEOUT)

    # Index creation is excluded here; see the module docstring.
    statements = [
        stmt
        for stmt in (*SILVER_SIGNALS_DDL, *GOLD_SIGNALS_DDL)
        if "CREATE INDEX" not in stmt.upper()
    ]
    skipped_indexes = [
        stmt for stmt in (*SILVER_SIGNALS_DDL, *GOLD_SIGNALS_DDL) if "CREATE INDEX" in stmt.upper()
    ]

    try:
        import psycopg2
    except ImportError:  # pragma: no cover - deployment always has it
        return SkillResult(ok=False, errors=["psycopg2 is not installed"])

    try:
        conn = psycopg2.connect(dsn, connect_timeout=15)
    except Exception as exc:  # noqa: BLE001
        return SkillResult(ok=False, errors=[f"could not connect: {exc}"])

    conn.autocommit = True
    added: list[str] = []
    recreated: list[str] = []
    try:
        with conn.cursor() as cur:
            cur.execute(f"SET lock_timeout = '{lock_timeout}'")
            before = _columns(cur, "silver", "signals")
            for stmt in statements:
                try:
                    cur.execute(stmt)
                except Exception as exc:  # noqa: BLE001 — one recoverable shape below
                    # A view whose column order drifted from the code cannot be
                    # replaced in place. The DDL is append-only by construction
                    # (GOLD_SIGNALS_BASE_COLUMNS is frozen), so reaching here
                    # means the database predates that rule — recover rather
                    # than block every future deploy on it.
                    #
                    # DROP without CASCADE on purpose: if something depends on
                    # this view, that is a decision for a person, not a deploy
                    # step, and the drop fails loudly instead of taking the
                    # dependent with it.
                    name = _view_name(stmt)
                    if _VIEW_SHAPE_CONFLICT not in str(exc) or not name:
                        raise
                    cur.execute(f"DROP VIEW IF EXISTS {name}")
                    cur.execute(stmt)
                    recreated.append(name)
            after = _columns(cur, "silver", "signals")
            added = sorted(after - before)
    except Exception as exc:  # noqa: BLE001
        return SkillResult(
            ok=False,
            errors=[
                f"schema not ensured: {exc}. A deploy that cannot reach the schema "
                "must fail rather than restart services onto a database that is "
                "behind the code they run."
            ],
        )
    finally:
        conn.close()

    actions = (
        [f"added silver.signals.{c}" for c in added]
        if added
        else ["schema already current — no columns added"]
    )
    if recreated:
        actions.append(
            f"recreated {len(recreated)} view(s) whose column order predated the "
            f"append-only rule: {', '.join(recreated)} — replacing in place is "
            "impossible once a column has been inserted rather than appended"
        )
    if skipped_indexes:
        actions.append(
            f"{len(skipped_indexes)} index statement(s) skipped — build them in a "
            "maintenance window, not a deploy (CREATE INDEX blocks writes)"
        )

    return SkillResult(
        ok=True,
        value={
            "columns_added": added,
            "views_recreated": recreated,
            "indexes_skipped": len(skipped_indexes),
        },
        actions_taken=actions,
    )


__all__ = ["DEFAULT_LOCK_TIMEOUT", "run"]
