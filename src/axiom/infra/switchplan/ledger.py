# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""One ledger shape for every switch, in two stores (ADR-197 D8).

``SwitchRecord`` is the adoption ledger generalized: the phase a switch is in,
what each probe last said, the lock holds that were measured, the approver, and
the retire task. A file store needs no database (supervisor, update swap, pack
lifecycle); a Postgres store lives in the owning extension's schema (ADR-052) and
lets a transactional kind write its record in the commit's own transaction, so
the ledger cannot disagree with the world.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

PHASES = ("planned", "prepared", "proved", "committed", "soaked", "rolled_back", "retired", "failed")
#: Phases from which a rollback is still possible.
ROLLBACKABLE = ("planned", "prepared", "proved", "committed", "soaked", "failed")
OPEN_PHASES = ("planned", "prepared", "proved", "committed", "soaked")

__all__ = ["FileLedger", "Ledger", "OPEN_PHASES", "PHASES", "PgLedger", "ROLLBACKABLE", "SwitchRecord", "now"]


def now() -> datetime:
    return datetime.now(UTC)


@dataclass
class SwitchRecord:
    id: str
    kind: str
    subject: str
    phase: str = "planned"
    change_id: str | None = None
    created_at: str = ""
    updated_at: str = ""
    #: fingerprint of the probes the last passing prove covered
    proved_fingerprint: str | None = None
    proved_at: str | None = None
    probe_results: list[dict[str, Any]] = field(default_factory=list)
    budget: dict[str, float] = field(default_factory=dict)
    holds: list[dict[str, Any]] = field(default_factory=list)
    committed_at: str | None = None
    soak_until: str | None = None
    retire_class: str = "reclaim"
    retire_task: dict[str, Any] | None = None
    approver: str | None = None
    rollback_command: str = ""
    events: list[dict[str, Any]] = field(default_factory=list)
    #: free state a kind keeps between calls (JSON only)
    state: dict[str, Any] = field(default_factory=dict)

    def event(self, step: str, detail: str = "") -> None:
        self.events.append({"step": step, "at": now().isoformat(), "detail": detail[:500]})
        self.updated_at = now().isoformat()


class Ledger(Protocol):
    def load(self, record_id: str) -> SwitchRecord | None: ...
    def save(self, record: SwitchRecord, *, cursor: Any = None) -> None: ...
    def records(self) -> list[SwitchRecord]: ...


def new_record(kind: str, subject: str) -> SwitchRecord:
    t = now().isoformat()
    return SwitchRecord(id=uuid.uuid4().hex, kind=kind, subject=subject, created_at=t, updated_at=t)


def _from_dict(d: dict[str, Any]) -> SwitchRecord:
    return SwitchRecord(**{k: v for k, v in d.items() if k in SwitchRecord.__dataclass_fields__})


class FileLedger:
    """One JSON file per switch under a directory, written atomically."""

    def __init__(self, directory: str | Path) -> None:
        self.dir = Path(directory)

    def _path(self, record_id: str) -> Path:
        return self.dir / f"{record_id}.json"

    def load(self, record_id: str) -> SwitchRecord | None:
        try:
            return _from_dict(json.loads(self._path(record_id).read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError):
            return None

    def save(self, record: SwitchRecord, *, cursor: Any = None) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.dir / f".{record.id}.{uuid.uuid4().hex}.tmp"
        with tmp.open("w", encoding="utf-8") as fh:
            fh.write(json.dumps(asdict(record), indent=2, default=str))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self._path(record.id))

    def records(self) -> list[SwitchRecord]:
        out = []
        for p in sorted(self.dir.glob("*.json")) if self.dir.exists() else []:
            r = self.load(p.stem)
            if r is not None:
                out.append(r)
        return sorted(out, key=lambda r: r.created_at)


_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")


class PgLedger:
    """The same record in a table of the owning extension's schema.

    ``save(record, cursor=cur)`` writes through the caller's cursor, so a commit
    that runs in a transaction writes its record in that transaction.
    """

    def __init__(self, conn, schema: str, table: str = "switch_records") -> None:
        if not (_IDENT.match(schema) and _IDENT.match(table)):
            raise ValueError("schema and table must be plain identifiers")
        self.conn, self.schema, self.table = conn, schema, table
        self._ensured = False

    @property
    def qualified(self) -> str:
        return f'"{self.schema}"."{self.table}"'

    def ensure(self) -> None:
        if self._ensured:
            return
        self.conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{self.schema}"')
        self.conn.execute(
            f"CREATE TABLE IF NOT EXISTS {self.qualified} ("
            "id text PRIMARY KEY, kind text NOT NULL, subject text NOT NULL, phase text NOT NULL, "
            "body jsonb NOT NULL, updated_at timestamptz NOT NULL DEFAULT now())")
        self._ensured = True

    def load(self, record_id: str) -> SwitchRecord | None:
        self.ensure()
        row = self.conn.execute(f"SELECT body FROM {self.qualified} WHERE id = %s", (record_id,)).fetchone()
        if row is None:
            return None
        body = row[0] if isinstance(row[0], dict) else json.loads(row[0])
        return _from_dict(body)

    def save(self, record: SwitchRecord, *, cursor: Any = None) -> None:
        self.ensure()
        runner = cursor if cursor is not None else self.conn
        runner.execute(
            f"INSERT INTO {self.qualified} (id, kind, subject, phase, body, updated_at) VALUES (%s, %s, %s, %s, %s::jsonb, now()) "
            "ON CONFLICT (id) DO UPDATE SET phase = EXCLUDED.phase, body = EXCLUDED.body, updated_at = now()",
            (record.id, record.kind, record.subject, record.phase, json.dumps(asdict(record), default=str)))

    def records(self) -> list[SwitchRecord]:
        self.ensure()
        rows = self.conn.execute(f"SELECT body FROM {self.qualified} ORDER BY updated_at").fetchall()
        return [_from_dict(r[0] if isinstance(r[0], dict) else json.loads(r[0])) for r in rows]
