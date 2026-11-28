# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The gold projection of the log's chain: ``gold.log_records``, one row per signed record.

The chain is the record of truth (ADR-241 D6). Everything else about a record that serves (the
search index, the corpus, chat) is derived from this view, never from the chain's own table, a
draft or a tray (ADR-251 D1). The view derives only: it selects, renames and leaves out what no
projection needs (signatures, keys, the canonical bytes). It holds no copy: a view is a query
over the chain, so dropping it loses nothing.

Building gold is the one place that reads the owner's table (ADR-128: "you are building gold").
The gold schema is ``axiom.infra.db.normalize_extension_name("gold")``: ``gold`` on a node, a
suffixed schema per test so parallel tests never share one.
"""

from __future__ import annotations

import os
import re
import threading
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, text

from axiom.governance import doors as g

from . import store

#: The gold object every log projection reads.
VIEW = "log_records"

#: Served only through the log's doors: the generic gold verbs answer it as missing.
COMMENT = g.door_only_comment(
    ("attest.search", "journal.search", "search.logbooks", "attest.reference"),
    "the gold projection of the log's chain; record content, read through the log's doors",
)

_ensured: set[tuple[str, str]] = set()
_lock = threading.Lock()


#: A plain SQL identifier: the only shape a schema, table or column name may take here. Names
#: cannot be bound parameters, so anything else is refused rather than quoted into a statement.
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,62}")


def ident(name: str) -> str:
    """``name`` double-quoted for SQL, or :class:`ValueError` when it is not a plain identifier
    (letters, digits and underscores, not starting with a digit, at most 63 characters)."""
    if not isinstance(name, str) or not _IDENT.fullmatch(name):
        raise ValueError(f"refusing unsafe SQL identifier {name!r}")
    return '"' + name + '"'


def schema() -> str:
    from axiom.infra.db import normalize_extension_name

    name = normalize_extension_name("gold")
    ident(name)  # refused here, before any statement names it
    return name


def _owner() -> str:
    from axiom.infra.db import normalize_extension_name

    name = normalize_extension_name("attest")
    ident(name)
    return name


def qualified() -> str:
    """``"gold"."log_records"``, each part validated and quoted by :func:`ident`."""
    return f"{ident(schema())}.{ident(VIEW)}"


#: The view's columns, for SQLAlchemy Core reads (no SQL text is built for a read).
COLUMNS = ("attestation_id", "site_id", "logbook", "seq", "kind", "meaning", "entry_type", "signer",
           "occurred_at", "recorded_at", "target", "content", "interval", "digest")  # fmt: skip


def view() -> Any:
    """The view as a Core table clause, in the (validated) gold schema."""
    from sqlalchemy import DateTime, column, table
    from sqlalchemy.dialects.postgresql import JSONB

    types = {"content": JSONB, "interval": JSONB, "occurred_at": DateTime(timezone=True),
             "recorded_at": DateTime(timezone=True)}  # fmt: skip
    return table(VIEW, *(column(c, types.get(c)) for c in COLUMNS), schema=schema())


#: The view's select list over a chain row: columns a projection reads, and nothing a projection
#: must not hold (no signatures, keys or canonical bytes). ``content`` is the signed content a
#: person wrote, read through this view. A tier move projects a cold table with the same list, so
#: its digest is the view's (unit LD-7).
PROJECTION = (
    "attestation_id, site_id, logbook, seq, kind, meaning, entry_type, signer, "
    "occurred_at, recorded_at, target, record->'content' AS content, "
    "record->'interval' AS interval, digest"
)


def view_sql() -> str:
    # Over the chain table and, through inheritance, every cold year table a move attached to it
    # (unit LD-7): the view never names a tier.
    return f"CREATE OR REPLACE VIEW {qualified()} AS SELECT {PROJECTION} FROM {ident(_owner())}.attest_records"


def ensure(session: Any) -> None:
    """Create the gold schema and the view once per process (and per test schema)."""
    key = (schema(), _owner())
    if key in _ensured:
        return
    with _lock:
        if key in _ensured:
            return
        # nosemgrep: avoid-sqlalchemy-text -- DDL: the schema name passed ident(); nothing else is interpolated
        session.execute(text(f"CREATE SCHEMA IF NOT EXISTS {ident(schema())}"))
        # nosemgrep: avoid-sqlalchemy-text -- DDL: both schema names and the view name passed ident(); no value
        session.execute(text(view_sql()))
        # Driver SQL, not ``text()``: the mark's colon is not a bound parameter.
        session.connection().exec_driver_sql(f"COMMENT ON VIEW {qualified()} IS {comment_literal()}")
        session.commit()
        _ensured.add(key)


def comment_literal() -> str:
    """:data:`COMMENT` as a SQL literal (a utility statement takes no bound parameter)."""
    return "'" + COMMENT.replace("'", "''") + "'"


def forget() -> None:
    """Forget what was ensured (a schema was dropped)."""
    _ensured.clear()


def iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC).isoformat()
    return str(value)


def watermark(site: str, logbook: str) -> str | None:
    """How far gold reaches for one log at one site: the newest record's ``recorded_at``."""
    with store.session_scope() as s:
        ensure(s)
        v = view()
        got = s.execute(
            select(func.max(v.c.recorded_at)).where(v.c.site_id == site, v.c.logbook == logbook)
        ).scalar()
    return iso(got)


def _norm(col: str, value: Any) -> Any:
    if col in ("occurred_at", "recorded_at") and value is not None:
        if isinstance(value, str):
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return iso(value)
    if col == "seq" and value is not None:
        return int(value)
    return value


def partition_digest(rows: Any) -> str:
    """One digest over gold rows in ``seq`` order, each column normalised (times as UTC ISO):
    equal digests, equal partitions, however the rows were built (unit LD-5-A7). It covers
    :data:`COLUMNS`, in the view's order."""
    import hashlib
    import json

    h = hashlib.sha256()
    for row in sorted(rows, key=lambda r: int(r["seq"])):
        body = [_norm(c, row.get(c)) for c in COLUMNS]
        h.update(json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()


def partition(site: str, logbook: str, from_seq: int, to_seq: int) -> list[dict[str, Any]]:
    """The gold rows of ``#from_seq`` to ``#to_seq`` of one log, as the view builds them."""
    from sqlalchemy import select

    v = view()
    stmt = (select(v).where(v.c.site_id == site, v.c.logbook == logbook,
                            v.c.seq.between(from_seq, to_seq)).order_by(v.c.seq))  # fmt: skip
    with store.session_scope() as s:
        ensure(s)
        return [dict(r._mapping) for r in s.execute(stmt)]


def drop_for_tests() -> None:
    """Drop a test's own gold schema. Refused for the node's real one."""
    if not os.environ.get("AXIOM_TEST_SCHEMA_SUFFIX") or schema() == "gold":
        raise RuntimeError("drop_for_tests drops only a test's suffixed gold schema")
    with store.session_scope() as s:
        # nosemgrep: avoid-sqlalchemy-text -- DDL: the test schema name passed ident(); nothing else is interpolated
        s.execute(text(f"DROP SCHEMA IF EXISTS {ident(schema())} CASCADE"))
        s.commit()
    forget()
    from . import index

    index.forget()


__all__ = ["COLUMNS", "PROJECTION", "VIEW", "drop_for_tests", "ensure", "forget", "ident", "iso", "partition",
           "partition_digest", "qualified", "schema", "view", "view_sql", "watermark"]  # fmt: skip
