# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Apply the conformance DDL only when it has changed, and wait politely when it must.

The conformance tier's DDL is written to be re-runnable, and re-running it is
harmless to the data. It is not harmless to the *lock queue*: Postgres takes
ACCESS EXCLUSIVE on the table before it decides an ``ADD COLUMN IF NOT EXISTS``
or a ``CREATE OR REPLACE VIEW`` has nothing to do. A node whose conform loops
keep a transaction open on ``silver.signals`` therefore failed every deploy that
had nothing to change (v1.6.80 twice, site 1.6.119 twice).

Two rules, both read off the catalogue rather than assumed:

1. **A stamp says what was applied.** After a successful apply the DDL's
   fingerprint is written to ``silver.schema_state``. If the stamp matches the
   code's DDL, *and* the catalogue still shows every column and view that DDL
   adds, nothing runs and no lock is asked for. The catalogue check is what
   stops a stale stamp from being trusted over a dropped column.
2. **A real change waits for a gap.** A lock timeout on an autocommit
   connection is retried with a pause, up to a budget, because the writer in the
   way commits between batches. If the budget runs out the error names the
   sessions holding the table (pid, state, how long) so the next step is a
   decision, not an investigation.
"""

from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Callable, Sequence

STATE_TABLE = "silver.schema_state"
#: One stamp per kind of DDL list. A deploy applies the schema without building
#: indexes (that blocks writes); a conform pass applies the full list. They must
#: not overwrite each other's stamp, and a full stamp satisfies a deploy's check
#: because it is a superset.
KEY_SCHEMA = "conformance:schema"
KEY_INDEXED = "conformance:indexed"

#: Seconds to keep retrying a lock that is held, when the caller does not say.
DEFAULT_WAIT_S = 120.0
#: Pause between attempts. Short: the writer's gap between batches is short.
RETRY_PAUSE_S = 2.0

_ADD_COLUMN = re.compile(
    r"ALTER\s+TABLE\s+silver\.signals\s+ADD\s+COLUMN\s+IF\s+NOT\s+EXISTS\s+([a-z_][a-z0-9_]*)",
    re.I,
)
_VIEW = re.compile(r"CREATE\s+OR\s+REPLACE\s+VIEW\s+([A-Za-z0-9_.]+)", re.I)


class SchemaLockBlocked(RuntimeError):
    """The DDL could not get its lock inside the budget; says who held it."""


def fingerprint(statements: Sequence[str]) -> str:
    """A stable identity for a list of DDL statements (whitespace-insensitive)."""
    h = hashlib.sha256()
    for stmt in statements:
        h.update(" ".join(stmt.split()).encode())
        h.update(b"\x00")
    return h.hexdigest()


def is_lock_timeout(exc: BaseException) -> bool:
    code = getattr(exc, "sqlstate", None) or getattr(exc, "pgcode", None)
    return code == "55P03"


def _expected(statements: Sequence[str]) -> tuple[set[str], set[str]]:
    columns: set[str] = set()
    views: set[str] = set()
    for stmt in statements:
        columns.update(m.lower() for m in _ADD_COLUMN.findall(stmt))
        views.update(m.lower() for m in _VIEW.findall(stmt))
    return columns, views


def key_for(statements: Sequence[str]) -> str:
    return KEY_INDEXED if any("CREATE INDEX" in s.upper() for s in statements) else KEY_SCHEMA


def _stamped(cur, statements: Sequence[str]) -> bool:
    cur.execute(f"SELECT fingerprint FROM {STATE_TABLE} WHERE key = %s", (key_for(statements),))
    row = cur.fetchone()
    return row is not None and row[0] == fingerprint(statements)


def is_current(
    cur, statements: Sequence[str], superset: Sequence[str] | None = None
) -> bool:
    """True when a stamp matches and the catalogue still shows what the DDL adds.

    *superset* is a longer list that, if it is the one stamped, also satisfies
    this one (the full DDL includes everything a deploy's list does).

    Reads only. A missing stamp table, a stale stamp, a dropped column or a
    dropped view all answer False, so the caller applies and repairs.
    """
    cur.execute("SELECT to_regclass(%s)", (STATE_TABLE,))
    if cur.fetchone()[0] is None:
        return False
    if not (_stamped(cur, statements) or (superset is not None and _stamped(cur, superset))):
        return False
    columns, views = _expected(statements)
    if columns:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'silver' AND table_name = 'signals'"
        )
        have = {r[0].lower() for r in cur.fetchall()}
        if not columns <= have:
            return False
    for view in views:
        cur.execute("SELECT to_regclass(%s)", (view,))
        if cur.fetchone()[0] is None:
            return False
    return True


def holders(cur, relation: str = "silver.signals") -> list[dict]:
    """The other sessions holding or awaiting a lock on *relation*.

    Pid, state, how long the transaction has been open, how long the session
    has been silent, and the verb and table of its last statement. Never the
    full statement text or the connection string.
    """
    cur.execute(
        """
        SELECT a.pid, a.state, a.application_name,
               extract(epoch FROM now() - a.xact_start)::int,
               extract(epoch FROM now() - a.state_change)::int,
               left(a.query, 60)
          FROM pg_stat_activity a
          JOIN pg_locks l ON l.pid = a.pid AND l.relation = to_regclass(%s)
         WHERE a.pid <> pg_backend_pid() AND a.datname = current_database()
         GROUP BY a.pid, a.state, a.application_name, a.xact_start, a.state_change, a.query
         ORDER BY a.xact_start NULLS LAST
        """,
        (relation,),
    )
    out = []
    for pid, state, app, xact_age, silent, query in cur.fetchall():
        verb = " ".join((query or "").split()[:3])
        out.append(
            {
                "pid": pid,
                "state": state,
                "application": app or "",
                "transaction_age_s": xact_age,
                "silent_s": silent,
                "last_statement": verb,
            }
        )
    return out


def describe_holders(found: list[dict], relation: str = "silver.signals") -> str:
    if not found:
        return f"no other session holds a lock on {relation} right now (it may have just released)"
    parts = []
    for h in found:
        parts.append(
            f"pid {h['pid']} ({h['state']}, transaction open {h['transaction_age_s']}s, "
            f"silent {h['silent_s']}s, last: {h['last_statement'] or '?'}"
            f"{', app ' + h['application'] if h['application'] else ''})"
        )
    return f"{relation} is held by " + "; ".join(parts)


def _stamp(cur, statements: Sequence[str]) -> None:
    cur.execute("CREATE SCHEMA IF NOT EXISTS silver")
    cur.execute(
        f"CREATE TABLE IF NOT EXISTS {STATE_TABLE} ("
        "key text PRIMARY KEY, fingerprint text NOT NULL, applied_at timestamptz NOT NULL DEFAULT now())"
    )
    cur.execute(
        f"INSERT INTO {STATE_TABLE} (key, fingerprint) VALUES (%s, %s) "
        "ON CONFLICT (key) DO UPDATE SET fingerprint = EXCLUDED.fingerprint, applied_at = now()",
        (key_for(statements), fingerprint(statements)),
    )


def apply_if_changed(
    cur,
    statements: Sequence[str],
    apply: Callable[[object, Sequence[str]], list[str]],
    *,
    wait_s: float = DEFAULT_WAIT_S,
    sleep: Callable[[float], None] = time.sleep,
    superset: Sequence[str] | None = None,
) -> tuple[list[str], bool]:
    """Run ``apply(cur, statements)`` only when the schema is not current.

    Returns ``(recreated_views, ran)``. ``ran`` is False when nothing was run
    and no lock was requested. Retrying a lock timeout is only valid on an
    autocommit connection (each statement is its own transaction); inside a
    caller's transaction the first timeout propagates, because the transaction
    is already aborted.
    """
    try:
        current = is_current(cur, statements, superset)
    except AttributeError:
        # A cursor that cannot be read from (a test double) cannot prove currency.
        current = False
    if current:
        return [], False
    deadline = time.monotonic() + max(0.0, wait_s)
    while True:
        try:
            recreated = apply(cur, statements)
            _stamp(cur, statements)
            return recreated, True
        except Exception as exc:  # noqa: BLE001 — only a lock timeout is retried
            if not is_lock_timeout(exc):
                raise
            if time.monotonic() >= deadline or not _autocommit(cur):
                try:
                    found = holders(cur)
                except Exception:  # noqa: BLE001 — naming the holder is best effort
                    found = []
                raise SchemaLockBlocked(
                    f"could not lock silver.signals within {wait_s:g}s: {describe_holders(found)}. "
                    "Nothing was changed. A session idle in transaction is a writer that "
                    "opened a batch and is not committing; let it commit or stop it, "
                    "then re-run."
                ) from exc
            sleep(RETRY_PAUSE_S)


def _autocommit(cur) -> bool:
    conn = getattr(cur, "connection", None)
    value = getattr(conn, "autocommit", False)
    return bool(value)
