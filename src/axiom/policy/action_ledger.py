# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Action-provenance ledger — the searchable store behind ``guarded_act``
decision provenance (issue #665).

Every autonomous agent action that flows through
``axiom.policy.agent_action_guard.guarded_act`` journals one structured
record per candidate: agent, op_class, action name, candidate identifier,
guards evaluated, the refusing rule when refused, dry-run flag, outcome
(``proceeded`` / ``refused`` / ``failed``), an undo handle when the
consumer provides one, and a timestamp. Refusals are first-class records,
not just successes.

Storage
-------
The default backend is PostgreSQL — the platform's ``axi db`` — via the
shared ADR-052 engine (``axiom.infra.db``). Records live in the
``policy`` schema, table ``agent_actions``, with composite indexes on
``(agent, ts)`` and ``(op_class, ts)`` so the query surfaces (MCP, CLI,
chat) stay fast. The table is created with idempotent inline DDL on
first use, following the ``axiom.infra.state_pg`` precedent for
infra-level tables (platform primitives are not extensions, so there is
no per-extension Alembic tree; see the migration note below).

When SQL is unavailable the ledger degrades to a locked JSONL flat file
(``<state_dir>/audit/actions.jsonl``) — reusing the
``axiom.infra.audit_log`` JSONL backend — so provenance is never
silently dropped. ``AXIOM_ACTION_LEDGER_BACKEND`` mirrors
``AXIOM_AUDIT_BACKEND``: ``auto`` (default), ``sql``, ``jsonl``.

Always-on posture — deliberate divergence from AuditLog
-------------------------------------------------------
``axiom.infra.audit_log.AuditLog`` is EC-scoped: all writes are no-ops
in standard mode. Action provenance deliberately diverges: records are
**always written**, in every mode. In standard mode they are plain rows;
when an HMAC chain key is configured (``AXIOM_AUDIT_HMAC_KEY``, the same
key AuditLog uses for EC mode) records are HMAC-chained with the exact
chain scheme AuditLog uses (``GENESIS`` sentinel, SHA-256, canonical
JSON) and ``verify_chain()`` detects tampering or deletion. An agent
that deleted a branch yesterday must be accountable today regardless of
compliance mode — that is why this log cannot be EC-only.

Migration note
--------------
ADR-052 gives *extensions* schema-per-extension Alembic trees. This is a
platform-level (policy) table, so it follows the infra-level convention
established by ``state_pg.ensure_schema()``: idempotent
``CREATE TABLE IF NOT EXISTS`` DDL executed on first connection, scoped
to the ``policy`` schema via ``axiom.infra.db.ensure_schema``. If the
schema later grows, promote to an Alembic tree under a future
``policy`` migrations home.

Usage::

    from axiom.policy.action_ledger import ActionLedger, search_actions

    ledger = ActionLedger(state_dir=Path.home() / ".axi")
    ledger.record_action(agent="tidy", op_class="git.branch.delete", ...)
    rows = ledger.query(agent="tidy", since="1d", outcome="refused")
"""

from __future__ import annotations

import logging
import os
import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from axiom.infra.audit_log import _GENESIS, _compute_hmac, _JsonlBackend

if TYPE_CHECKING:
    from sqlalchemy import Engine

    from axiom.policy.agent_action_guard import AgentAction, GuardDecision

_log = logging.getLogger(__name__)

#: Schema the SQL backend writes into (via ``axiom.infra.db.ensure_schema``).
LEDGER_SCHEMA = "policy"
#: Table name inside :data:`LEDGER_SCHEMA` (JSONL file stem for fallback).
LEDGER_TABLE = "agent_actions"

OUTCOME_PROCEEDED = "proceeded"
OUTCOME_REFUSED = "refused"
OUTCOME_FAILED = "failed"

_DEFAULT_LIMIT = 200
_MAX_LIMIT = 1000

#: Candidate identifier for a batch-level refusal with no enumerated
#: candidates (e.g. hard-disable short-circuits before enumeration).
BATCH_CANDIDATE = "(batch)"

# Fields (in canonical order) that participate in the HMAC chain.
_RECORD_FIELDS = (
    "id", "ts", "agent", "op_class", "name", "candidate", "guards",
    "refusing_rule", "dry_run", "outcome", "undo_ref", "metadata",
)


# ---------------------------------------------------------------------------
# --since / --until parsing (Nm/Nh/Nd/Nw shorthand or ISO-8601)
# ---------------------------------------------------------------------------

_SHORTHAND = re.compile(r"^\s*(\d+)\s*(m|h|d|w)\s*$", re.IGNORECASE)
_UNIT_SECONDS = {"m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_when(value: str | datetime | None, *, now: datetime | None = None) -> datetime | None:
    """Parse ``'7d'`` / ``'24h'`` / ISO-8601 / ``datetime`` → aware datetime.

    ``None`` passes through. Raises ``ValueError`` on garbage so callers
    surface a clean error instead of silently widening the window.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    now = now or datetime.now(UTC)
    m = _SHORTHAND.match(value)
    if m:
        return now - timedelta(seconds=int(m.group(1)) * _UNIT_SECONDS[m.group(2).lower()])
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(
            f"time filter must be shorthand (Nm/Nh/Nd/Nw) or ISO-8601, got {value!r}"
        ) from exc
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------


class _JsonlActionBackend:
    """Degraded fallback: locked JSONL flat file under the state dir.

    Reuses :class:`axiom.infra.audit_log._JsonlBackend` for the locked
    append + ordered read machinery.
    """

    name = "jsonl"

    def __init__(self, state_dir: Path) -> None:
        self._inner = _JsonlBackend(Path(state_dir) / "audit")

    def write(self, record: dict) -> None:
        self._inner.write("actions", record)

    def read_ordered(self) -> list[dict]:
        return self._inner.read_ordered("actions")

    def last_hmac(self) -> str:
        records = self.read_ordered()
        for rec in reversed(records):
            if rec.get("hmac"):
                return rec["hmac"]
        return _GENESIS

    def query(self, **filters: Any) -> list[dict]:
        rows = [_public_row(r) for r in self.read_ordered()]
        rows = [r for r in rows if _matches(r, filters)]
        rows.sort(key=lambda r: r["ts"], reverse=True)
        return rows[: filters.get("limit") or _DEFAULT_LIMIT]


def _matches(row: dict, f: dict) -> bool:
    if f.get("agent") and row["agent"] != f["agent"]:
        return False
    if f.get("op_class") and row["op_class"] != f["op_class"]:
        return False
    if f.get("outcome") and row["outcome"] != f["outcome"]:
        return False
    since, until = f.get("since"), f.get("until")
    if since or until:
        ts = datetime.fromisoformat(row["ts"])
        if since and ts < since:
            return False
        if until and ts > until:
            return False
    if f.get("text"):
        blob = " ".join(
            str(row.get(k) or "")
            for k in ("candidate", "name", "refusing_rule", "undo_ref", "metadata")
        ).lower()
        if f["text"].lower() not in blob:
            return False
    return True


class _SqlActionBackend:
    """Primary backend: ``agent_actions`` table on the shared ADR-052
    engine (PostgreSQL in production; any SQLAlchemy engine in tests).

    DDL is inline + idempotent per the ``state_pg`` infra convention;
    composite indexes on (agent, ts) and (op_class, ts) keep the MCP /
    CLI query paths fast.
    """

    name = "sql"

    def __init__(self, engine: "Engine", schema: str | None = None) -> None:
        from sqlalchemy import (
            JSON,
            Boolean,
            Column,
            DateTime,
            Index,
            Integer,
            MetaData,
            String,
            Table,
            Text,
        )

        self._engine = engine
        self._schema = schema
        metadata = MetaData(schema=schema)
        self._table = Table(
            LEDGER_TABLE, metadata,
            Column("seq", Integer, primary_key=True, autoincrement=True),
            Column("id", String(36), nullable=False, unique=True),
            Column("ts", DateTime(timezone=True), nullable=False),
            Column("agent", String(64), nullable=False),
            Column("op_class", String(128), nullable=False),
            Column("name", String(128), nullable=False),
            Column("candidate", Text, nullable=False),
            Column("guards", JSON, nullable=True),
            Column("refusing_rule", Text, nullable=True),
            Column("dry_run", Boolean, nullable=False, default=False),
            Column("outcome", String(16), nullable=False),
            Column("undo_ref", Text, nullable=True),
            Column("metadata", JSON, nullable=True),
            Column("hmac", String(64), nullable=True),
            Index(f"ix_{LEDGER_TABLE}_agent_ts", "agent", "ts"),
            Index(f"ix_{LEDGER_TABLE}_op_class_ts", "op_class", "ts"),
        )
        metadata.create_all(engine, checkfirst=True)

    def write(self, record: dict) -> None:
        from sqlalchemy import insert

        values = dict(record)
        values["ts"] = datetime.fromisoformat(values["ts"])
        with self._engine.begin() as conn:
            conn.execute(insert(self._table).values(**values))

    def read_ordered(self) -> list[dict]:
        from sqlalchemy import select

        t = self._table
        with self._engine.connect() as conn:
            rows = conn.execute(select(t).order_by(t.c.seq.asc())).mappings().all()
        return [self._row_to_record(r) for r in rows]

    def last_hmac(self) -> str:
        from sqlalchemy import select

        t = self._table
        with self._engine.connect() as conn:
            row = conn.execute(
                select(t.c.hmac)
                .where(t.c.hmac.is_not(None))
                .order_by(t.c.seq.desc())
                .limit(1)
            ).first()
        return row[0] if row and row[0] else _GENESIS

    def query(self, **filters: Any) -> list[dict]:
        from sqlalchemy import or_, select

        t = self._table
        q = select(t)
        if filters.get("agent"):
            q = q.where(t.c.agent == filters["agent"])
        if filters.get("op_class"):
            q = q.where(t.c.op_class == filters["op_class"])
        if filters.get("outcome"):
            q = q.where(t.c.outcome == filters["outcome"])
        if filters.get("since"):
            q = q.where(t.c.ts >= filters["since"])
        if filters.get("until"):
            q = q.where(t.c.ts <= filters["until"])
        if filters.get("text"):
            needle = f"%{filters['text']}%"
            q = q.where(or_(
                t.c.candidate.ilike(needle),
                t.c.name.ilike(needle),
                t.c.refusing_rule.ilike(needle),
                t.c.undo_ref.ilike(needle),
            ))
        q = q.order_by(t.c.ts.desc(), t.c.seq.desc())
        q = q.limit(filters.get("limit") or _DEFAULT_LIMIT)
        with self._engine.connect() as conn:
            rows = conn.execute(q).mappings().all()
        return [_public_row(self._row_to_record(r)) for r in rows]

    @staticmethod
    def _row_to_record(row: Any) -> dict:
        rec = {k: row[k] for k in _RECORD_FIELDS}
        ts = rec["ts"]
        if isinstance(ts, datetime):
            rec["ts"] = (ts if ts.tzinfo else ts.replace(tzinfo=UTC)).isoformat()
        rec["dry_run"] = bool(rec["dry_run"])
        if row.get("hmac"):
            rec["hmac"] = row["hmac"]
        return rec


def _public_row(record: dict) -> dict:
    """Record as served to query surfaces (chain field preserved when set)."""
    out = {k: record.get(k) for k in _RECORD_FIELDS}
    if record.get("hmac"):
        out["hmac"] = record["hmac"]
    return out


# ---------------------------------------------------------------------------
# Backend selection (SQL default, JSONL degraded fallback) + probe cache
# ---------------------------------------------------------------------------

# A dead DB probe on every guarded tick would tax the fleet; remember the
# fallback decision per (mode, url) until _reset_backend_cache().
_FALLBACK_CACHE: set[tuple[str, str]] = set()


def _reset_backend_cache() -> None:
    """Test hook: forget cached SQL-unavailable decisions."""
    _FALLBACK_CACHE.clear()


def _select_backend(state_dir: Path, engine: "Engine | None" = None):
    if engine is not None:
        schema = None
        if engine.dialect.name == "postgresql":
            from axiom.infra.db import ensure_schema
            schema = ensure_schema(engine, LEDGER_SCHEMA)
        return _SqlActionBackend(engine, schema=schema)

    mode = os.environ.get("AXIOM_ACTION_LEDGER_BACKEND", "auto")
    if mode == "jsonl":
        return _JsonlActionBackend(state_dir)

    from axiom.infra import db as _db

    cache_key = (mode, os.environ.get("AXIOM_DB_URL", _db.DEFAULT_DB_URL))
    if cache_key in _FALLBACK_CACHE:
        return _JsonlActionBackend(state_dir)
    try:
        eng = _db.get_engine()
        if eng.dialect.name == "postgresql":
            schema = _db.ensure_schema(eng, LEDGER_SCHEMA)  # connectivity probe
        else:
            schema = None
            with eng.connect():
                pass
        return _SqlActionBackend(eng, schema=schema)
    except Exception as exc:
        if mode == "sql":
            raise
        _FALLBACK_CACHE.add(cache_key)
        _log.info(
            "action_ledger: SQL backend unavailable (%s: %s) — degraded to "
            "JSONL fallback at %s",
            type(exc).__name__, exc, Path(state_dir) / "audit" / "actions.jsonl",
        )
        return _JsonlActionBackend(state_dir)


# ---------------------------------------------------------------------------
# ActionLedger
# ---------------------------------------------------------------------------


class ActionLedger:
    """Write + query surface over the selected backend.

    ``hmac_key`` defaults from ``AXIOM_AUDIT_HMAC_KEY`` (the AuditLog EC
    chain key): present → records chain; absent → plain rows. Either way
    records are written — see the module docstring for the divergence
    from AuditLog's EC-only posture.
    """

    def __init__(
        self,
        state_dir: Path,
        *,
        hmac_key: str | None = None,
        engine: "Engine | None" = None,
    ) -> None:
        self._state_dir = Path(state_dir)
        self._hmac_key = (
            hmac_key if hmac_key is not None
            else os.environ.get("AXIOM_AUDIT_HMAC_KEY")
        )
        self._backend = _select_backend(self._state_dir, engine)
        self._last_hmac: str | None = None  # lazy — read from backend tail

    @property
    def backend_name(self) -> str:
        return self._backend.name

    # ---- write -----------------------------------------------------------

    def record_action(
        self,
        *,
        agent: str,
        op_class: str,
        name: str,
        candidate: str,
        guards: list[str],
        outcome: str,
        refusing_rule: str | None = None,
        dry_run: bool = False,
        undo_ref: str | None = None,
        metadata: dict | None = None,
        ts: datetime | None = None,
    ) -> dict:
        """Append one provenance record; returns the stored record."""
        when = ts or datetime.now(UTC)
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        record: dict[str, Any] = {
            "id": str(uuid.uuid4()),
            "ts": when.isoformat(),
            "agent": agent,
            "op_class": op_class,
            "name": name,
            "candidate": str(candidate),
            "guards": list(guards),
            "refusing_rule": refusing_rule,
            "dry_run": bool(dry_run),
            "outcome": outcome,
            "undo_ref": undo_ref,
            "metadata": metadata,
        }
        if self._hmac_key:
            if self._last_hmac is None:
                self._last_hmac = self._backend.last_hmac()
            record["hmac"] = _compute_hmac(self._hmac_key, record, self._last_hmac)
            self._last_hmac = record["hmac"]
        self._backend.write(record)
        return record

    # ---- read ------------------------------------------------------------

    def query(
        self,
        *,
        agent: str | None = None,
        op_class: str | None = None,
        outcome: str | None = None,
        since: str | datetime | None = None,
        until: str | datetime | None = None,
        text: str | None = None,
        limit: int = _DEFAULT_LIMIT,
    ) -> list[dict]:
        """Filtered records, most recent first. ``since``/``until`` accept
        shorthand (``7d``), ISO-8601, or ``datetime``. Raises
        ``ValueError`` on an unparseable time filter."""
        limit = max(1, min(int(limit), _MAX_LIMIT))
        return self._backend.query(
            agent=agent,
            op_class=op_class,
            outcome=outcome,
            since=parse_when(since),
            until=parse_when(until),
            text=text,
            limit=limit,
        )

    def recent(self, *, n: int = 10, agent: str | None = None) -> list[dict]:
        """The N most-recent records, optionally scoped to one agent."""
        return self.query(agent=agent, limit=n)

    # ---- chain -----------------------------------------------------------

    def verify_chain(self) -> tuple[bool, int | None]:
        """Verify the HMAC chain over chained records, in write order.

        Mirrors ``AuditLog.verify_chain``: ``(True, None)`` when intact
        (or no key configured), else ``(False, index)`` of the first
        broken link. Unchained (standard-mode) records written before a
        key existed are skipped — the chain covers the chained suffix.
        """
        if not self._hmac_key:
            return True, None
        records = [r for r in self._backend.read_ordered() if r.get("hmac")]
        prev = _GENESIS
        for i, record in enumerate(records):
            body = {k: v for k, v in record.items() if k != "hmac"}
            if record["hmac"] != _compute_hmac(self._hmac_key, body, prev):
                _log.error(
                    "action ledger HMAC chain broken at record %d — possible "
                    "tampering detected.", i,
                )
                return False, i
            prev = record["hmac"]
        return True, None


# ---------------------------------------------------------------------------
# Query service — the single seam MCP / CLI / chat surfaces call into
# ---------------------------------------------------------------------------


def _default_state_dir(state_dir: Path | None) -> Path:
    if state_dir is not None:
        return Path(state_dir)
    from axiom.infra.paths import get_user_state_dir
    return get_user_state_dir()


def search_actions(
    *,
    agent: str | None = None,
    op_class: str | None = None,
    outcome: str | None = None,
    since: str | None = None,
    until: str | None = None,
    text: str | None = None,
    limit: int = 50,
    state_dir: Path | None = None,
) -> dict:
    """Search the ledger; JSON-able payload for any transport.

    Returns ``{"backend", "count", "actions"}`` or ``{"error": ...}`` on
    a bad filter — never raises across the tool boundary.
    """
    ledger = ActionLedger(state_dir=_default_state_dir(state_dir))
    try:
        rows = ledger.query(
            agent=agent, op_class=op_class, outcome=outcome,
            since=since, until=until, text=text, limit=limit,
        )
    except ValueError as exc:
        return {"error": str(exc)}
    return {"backend": ledger.backend_name, "count": len(rows), "actions": rows}


def recent_actions(
    *,
    n: int = 10,
    agent: str | None = None,
    state_dir: Path | None = None,
) -> dict:
    """The N most-recent action records, optionally scoped to one agent."""
    ledger = ActionLedger(state_dir=_default_state_dir(state_dir))
    rows = ledger.recent(n=n, agent=agent)
    return {"backend": ledger.backend_name, "count": len(rows), "actions": rows}


def verify_actions(*, state_dir: Path | None = None) -> dict:
    """Verify the HMAC chain over the ledger (EC-mode integrity check)."""
    ledger = ActionLedger(state_dir=_default_state_dir(state_dir))
    ok, broken_at = ledger.verify_chain()
    return {
        "backend": ledger.backend_name,
        "chained": bool(ledger._hmac_key),
        "ok": ok,
        "broken_at": broken_at,
    }


# ---------------------------------------------------------------------------
# Guard emission — called by guarded_act; one record per candidate
# ---------------------------------------------------------------------------


def record_guard_decision(
    action: "AgentAction",
    decision: "GuardDecision",
    *,
    state_dir: Path,
    guards: list[str],
    dry_run: bool = False,
    undo_ref_for: Callable[[Any], str | None] | None = None,
) -> None:
    """Map a ``GuardDecision`` to provenance records and journal them.

    - batch refusal (``proceed=False``): every candidate gets a
      ``refused`` record with ``decision.reason`` as the refusing rule;
      an empty candidate set still journals one ``(batch)`` record so
      the refusal is never invisible.
    - dry run: every would-proceed candidate gets a ``proceeded`` record
      with ``dry_run=True``.
    - acted batch: completed → ``proceeded`` (with the consumer's undo
      handle when ``undo_ref_for`` is given); do_one failures → ``failed``.
    """
    ledger = ActionLedger(state_dir=state_dir)
    common = dict(
        agent=action.agent,
        op_class=action.op_class,
        name=action.name,
        guards=guards,
        metadata=dict(action.metadata) if action.metadata else None,
    )

    if not decision.proceed:
        refused = list(decision.refused) or list(action.candidates) or [BATCH_CANDIDATE]
        for candidate in refused:
            ledger.record_action(
                candidate=str(candidate),
                outcome=OUTCOME_REFUSED,
                refusing_rule=decision.reason or "refused",
                dry_run=dry_run,
                **common,
            )
        return

    if decision.reason == "dry_run":
        for candidate in decision.would_proceed:
            ledger.record_action(
                candidate=str(candidate),
                outcome=OUTCOME_PROCEEDED,
                dry_run=True,
                **common,
            )
        return

    for candidate in decision.completed:
        undo_ref = None
        if undo_ref_for is not None:
            try:
                undo_ref = undo_ref_for(candidate)
            except Exception:  # consumer callback must never break emission
                undo_ref = None
        ledger.record_action(
            candidate=str(candidate),
            outcome=OUTCOME_PROCEEDED,
            undo_ref=undo_ref,
            dry_run=dry_run,
            **common,
        )
    for candidate in decision.refused:
        ledger.record_action(
            candidate=str(candidate),
            outcome=OUTCOME_FAILED,
            dry_run=dry_run,
            **common,
        )


__all__ = [
    "ActionLedger",
    "BATCH_CANDIDATE",
    "LEDGER_SCHEMA",
    "LEDGER_TABLE",
    "OUTCOME_FAILED",
    "OUTCOME_PROCEEDED",
    "OUTCOME_REFUSED",
    "parse_when",
    "recent_actions",
    "record_guard_decision",
    "search_actions",
    "verify_actions",
]
