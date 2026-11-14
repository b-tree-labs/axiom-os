# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Catch-up readers over the durable sources, each with a stable position to resume from.

A reader returns rows and a new position; it never writes to its source. The positions are
kept in ``journal_cursors`` by :mod:`ingest`. Sources on main (design note, section 2):

- ``attest_records``: signed records by ``(site, logbook, seq)`` from the chain heads, and
  obligation facts by time. Through the attest service's read functions.
- ``skill_audit``: the action audit chain, by ``seq``.
- ``action_ledger``: the agent action ledger, by ``ts``, deduplicated by ``id``.
- ``program_changelog`` and ``cli_events``: append-only JSON Lines files, by line.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from . import rows
from .rows import Row

log = logging.getLogger("axiom.journal.sources")

#: How many records one catch-up pass takes from one source, so a first run on an old node is
#: bounded; the next pass continues from the position.
BATCH = 2000


class Source:
    name: str

    def read(
        self, position: dict[str, Any] | None, *, state_dir: Path
    ) -> tuple[list[Row], dict[str, Any]]:
        raise NotImplementedError

    def head(self, *, state_dir: Path) -> dict[str, Any]:
        """Where the source is now, for the lag report. Empty when it cannot say."""
        return {}


class AttestRecords(Source):
    name = "attest_records"

    def read(self, position, *, state_dir):
        from sqlalchemy import text

        from axiom.extensions.builtins.attest import obligations, service, store

        pos = dict(position or {})
        records = dict(pos.get("records") or {})
        obligation_at = dict(pos.get("obligations") or {})
        out: list[Row] = []
        with store.session_scope() as s:
            heads = s.execute(
                text(
                    "SELECT site_id, logbook, seq FROM attest_chain_heads WHERE seq > 0 "
                    "ORDER BY site_id, logbook"
                )  # fmt: skip
            ).all()
        budget = BATCH
        for site, logbook, head in heads:
            key = f"{site}|{logbook}"
            last = int(records.get(key, 0))
            if head <= last or budget <= 0:
                continue
            for rec in service.records(site, logbook, from_seq=last + 1)[:budget]:
                out.append(rows.from_brief(service.brief(rec)))
                last = int(rec["seq"])
            budget -= max(0, last - int(records.get(key, 0)))
            records[key] = last
            since = obligation_at.get(key)
            newest = since
            for ev in obligations.events(site, logbook):
                stamp = rows.when(ev.get("at")).isoformat()
                if since and stamp <= since:
                    continue
                out.append(rows.from_obligation(
                    {**ev, "interval_number": ev.get("interval_number")},
                    logbook=logbook, state=str(ev.get("state")), site=site, at=ev.get("at"),
                ))  # fmt: skip
                newest = stamp if newest is None or stamp > newest else newest
            if newest:
                obligation_at[key] = newest
        return out, {"records": records, "obligations": obligation_at}

    def head(self, *, state_dir):
        try:
            from sqlalchemy import text

            from axiom.extensions.builtins.attest import store

            with store.session_scope() as s:
                heads = s.execute(
                    text("SELECT site_id, logbook, seq FROM attest_chain_heads WHERE seq > 0")
                ).all()
            return {f"{site}|{logbook}": int(seq) for site, logbook, seq in heads}
        except Exception:  # noqa: BLE001 - no attest store is "cannot say"
            return {}


class SkillAudit(Source):
    name = "skill_audit"

    def read(self, position, *, state_dir):
        from axiom.infra import audit_trail

        last = int((position or {}).get("seq") or 0)
        out: list[Row] = []
        for rec in audit_trail.read_chain(audit_trail.audit_path(state_dir)):
            seq = int(rec.get("seq") or 0)
            if seq <= last:
                continue
            out.append(rows.from_audit_record(rec))
            last = seq
            if len(out) >= BATCH:
                break
        return out, {"seq": last}

    def head(self, *, state_dir):
        from axiom.infra import audit_trail

        chain = audit_trail.read_chain(audit_trail.audit_path(state_dir))
        return {"seq": int(chain[-1].get("seq") or 0)} if chain else {"seq": 0}


class ActionLedgerSource(Source):
    name = "action_ledger"

    def read(self, position, *, state_dir):
        from axiom.policy.action_ledger import ActionLedger

        since = (position or {}).get("ts")
        ledger = ActionLedger(state_dir=state_dir)
        found = ledger.query(since=since, limit=1000)
        out = [rows.from_ledger_row(r) for r in reversed(found)]
        newest = max((str(r.get("ts")) for r in found), default=since)
        return out, {"ts": newest} if newest else {}


class JsonlSource(Source):
    """An append-only JSON Lines file read by line; a shrunken file starts over, and the
    content key keeps that idempotent."""

    def __init__(self, name: str, path_for: Callable[[Path], Path],
                 build: Callable[[dict[str, Any], str], Row | None]) -> None:  # fmt: skip
        self.name = name
        self._path_for = path_for
        self._build = build

    def read(self, position, *, state_dir):
        path = self._path_for(state_dir)
        done = int((position or {}).get("lines") or 0)
        if not path.exists():
            return [], {"lines": 0}
        lines = path.read_text(encoding="utf-8").splitlines()
        if len(lines) < done:
            done = 0
        out: list[Row] = []
        n = done
        for n, line in enumerate(lines[done : done + BATCH], start=done + 1):
            raw = line.strip()
            if not raw:
                continue
            try:
                entry = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict):
                continue
            key = hashlib.sha256(raw.encode()).hexdigest()[:40]
            try:
                row = self._build(entry, key)
            except Exception:  # noqa: BLE001 - one bad line never stops the file
                log.debug("%s: line %d unreadable", self.name, n, exc_info=True)
                continue
            if row is not None:
                out.append(row)
        return out, {"lines": n}

    def head(self, *, state_dir):
        path = self._path_for(state_dir)
        if not path.exists():
            return {"lines": 0}
        return {"lines": sum(1 for _ in path.open(encoding="utf-8"))}


def _cli_event(entry: dict[str, Any], key: str) -> Row | None:
    subject = entry.get("subject")
    if not isinstance(subject, str) or not subject:
        return None
    return rows.from_event(subject, entry.get("payload") or {}, source="cli_events",
                           at=entry.get("timestamp"), source_id=key)  # fmt: skip


def _program_change(entry: dict[str, Any], key: str) -> Row | None:
    return rows.from_program_change(entry, source_id=key)


def default_sources() -> list[Source]:
    return [
        AttestRecords(),
        SkillAudit(),
        ActionLedgerSource(),
        JsonlSource(
            "program_changelog", lambda d: d / "program" / "changelog.jsonl", _program_change
        ),
        JsonlSource("cli_events", lambda d: d / "logs" / "cli_events.jsonl", _cli_event),
    ]


def by_name(sources: Iterable[Source]) -> dict[str, Source]:
    return {s.name: s for s in sources}


__all__ = [
    "BATCH",
    "ActionLedgerSource",
    "AttestRecords",
    "JsonlSource",
    "SkillAudit",
    "Source",
    "by_name",
    "default_sources",
]
