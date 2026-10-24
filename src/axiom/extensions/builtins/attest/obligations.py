# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Obligations and the missed-entry alarm (spec-attestation, Obligations).

A logbook declares ``[[obligation]]``: an entry type that must be signed every so
often while an interval is open. :func:`evaluate` says, for each open
interval, when the next entry is due and whether it is ``ok``, ``warn`` (inside
the lead window) or ``missed``. It reads only signed records and the interval
projection; it never guesses.

:func:`tick` runs on the schedule. It records each state change once, as an
append-only fact; publishes ``attest.<logbook>.obligation_warn|missed|met`` on the
bus (and so on the live stream); and notifies whoever holds the obligation's
``notify`` roles at the site. A ``met`` is recorded when the overdue entry is
finally signed. A miss is never erased: it stays a fact that a later entry
can refer to.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import text

from . import registry, roles, site_settings, store

Sender = Callable[[str, dict[str, Any]], None]


def _default_sender(recipient: str, state: dict[str, Any]) -> None:
    from axiom.extensions.builtins.notifications.send import (
        NotificationPayload,
        Priority,
        SendContext,
        send,
    )
    from axiom.governance.classification import Classification

    word = {"warn": "Due soon", "missed": "Missed", "met": "Now signed"}[state["state"]]
    interval = state["interval"]
    send(
        SendContext.default(),
        actor=f"@attest:{state['site_id']}",
        recipient=recipient,
        payload=NotificationPayload(
            summary=f"{word}: {state['type']} in {interval['kind']} {interval['number']}",
            body=f"Due {state['due_at'].isoformat()} ({state['logbook']}).",
            metadata={k: str(v) for k, v in state.items() if k != "interval"},
        ),
        classification=Classification.INTERNAL,
        priority=Priority.URGENT
        if state["severity"] == "alarm" and state["state"] == "missed"
        else Priority.HIGH,
        dedup_key=f"{state['logbook']}:{state['obligation']}:{interval['number']}:{state['due_at'].isoformat()}:{state['state']}",
    )


_sender: Sender = _default_sender


def set_sender(sender: Sender) -> None:
    """Seam: where notifications go (tests capture them)."""
    global _sender
    _sender = sender


def reset_sender() -> None:
    global _sender
    _sender = _default_sender


def evaluate(site_id: str, logbook_id: str, *, now: datetime) -> list[dict[str, Any]]:
    logbook = registry.get(logbook_id)
    out: list[dict[str, Any]] = []
    with store.session_scope() as s:
        for ob in logbook.obligations:
            row = s.execute(
                text(
                    "SELECT number, opened_at FROM attest_intervals WHERE site_id = :site AND "
                    "logbook = :logbook AND kind = :kind AND closed_at IS NULL"
                ),
                {"site": site_id, "logbook": logbook_id, "kind": ob.while_interval},
            ).one_or_none()
            if row is None:
                continue
            last = s.execute(
                text(
                    "SELECT max(occurred_at) FROM attest_records WHERE site_id = :site AND "
                    "logbook = :logbook AND entry_type = :type AND "
                    "record->'interval'->>'kind' = :kind AND "
                    "(record->'interval'->>'number')::bigint = :number"
                ),
                {"site": site_id, "logbook": logbook_id, "type": ob.type, "kind": ob.while_interval,
                 "number": int(row.number)},
            ).scalar()  # fmt: skip
            base = max(last, row.opened_at) if last else row.opened_at
            every = int(site_settings.value(site_id, ob.every_key, ob.every_default))
            warn = int(site_settings.value(site_id, ob.warn_key, ob.warn_default))
            due = base + timedelta(minutes=every)
            warn_at = due - timedelta(minutes=warn)
            state = "missed" if now >= due else "warn" if now >= warn_at else "ok"
            out.append(
                {
                    "site_id": site_id,
                    "logbook": logbook_id,
                    "obligation": ob.id,
                    "type": ob.type,
                    "interval": {"kind": ob.while_interval, "number": int(row.number)},
                    "last_met_at": last,
                    "due_at": due,
                    "warn_at": warn_at,
                    "state": state,
                    "severity": "alarm" if ob.required else "notice",
                    "overdue_seconds": max(0, int((now - due).total_seconds())),
                    "notify": list(ob.notify),
                }
            )
    return out


def _record(s: Any, st: dict[str, Any], state: str, at: datetime) -> bool:
    """Insert one event; False when it was already recorded."""
    inserted = s.execute(
        text(
            "INSERT INTO attest_obligation_events (event_id, site_id, logbook, obligation, "
            "interval_kind, interval_number, due_at, state, severity, at) VALUES (:id, :site, "
            ":logbook, :ob, :kind, :number, :due, :state, :sev, :at) "
            "ON CONFLICT ON CONSTRAINT attest_obligation_events_once DO NOTHING RETURNING event_id"
        ),
        {"id": str(uuid.uuid4()), "site": st["site_id"], "logbook": st["logbook"], "ob": st["obligation"],
         "kind": st["interval"]["kind"], "number": st["interval"]["number"], "due": st["due_at"],
         "state": state, "sev": st["severity"], "at": at},
    ).scalar()  # fmt: skip
    return inserted is not None


def tick(site_id: str, logbook_id: str, *, now: datetime, state_dir: Path) -> list[dict[str, Any]]:
    """Record, publish and notify each change since the last tick."""
    from axiom.infra.bus import get_default_eventbus

    changes: list[dict[str, Any]] = []
    states = evaluate(site_id, logbook_id, now=now)
    with store.session_scope() as s:
        for st in states:
            if st["state"] in ("warn", "missed") and _record(s, st, st["state"], now):
                changes.append({**st})
            unmet = s.execute(
                text(
                    "SELECT due_at FROM attest_obligation_events e WHERE site_id = :site AND "
                    "logbook = :logbook AND obligation = :ob AND interval_number = :number AND "
                    "state = 'missed' AND due_at < :due AND NOT EXISTS (SELECT 1 FROM "
                    "attest_obligation_events m WHERE m.site_id = e.site_id AND m.logbook = e.logbook "
                    "AND m.obligation = e.obligation AND m.interval_number = e.interval_number "
                    "AND m.due_at = e.due_at AND m.state = 'met')"
                ),
                {"site": site_id, "logbook": logbook_id, "ob": st["obligation"],
                 "number": st["interval"]["number"], "due": st["due_at"]},
            ).scalars().all()  # fmt: skip
            for due in unmet:
                met = {**st, "due_at": due, "state": "met"}
                if _record(s, met, "met", now):
                    changes.append(met)
        s.commit()
    bus = get_default_eventbus()
    holders = roles.assignments(site_id, state_dir=state_dir)
    for ch in changes:
        payload = {"schema_version": 1, **{k: (v.isoformat() if isinstance(v, datetime) else v)
                                           for k, v in ch.items()}}  # fmt: skip
        try:
            bus.publish(f"attest.{logbook_id}.obligation_{ch['state']}", payload, source="attest")
        except Exception:  # noqa: BLE001 - the fact is recorded; delivery is best effort
            pass
        for h in holders:
            if set(h["roles"]) & set(ch["notify"]):
                _sender(h["principal"], ch)
    return changes


def events(site_id: str, logbook_id: str) -> list[dict[str, Any]]:
    with store.session_scope() as s:
        rows = (
            s.execute(
                text(
                    "SELECT obligation, interval_number, due_at, state, severity, at FROM "
                    "attest_obligation_events WHERE site_id = :site AND logbook = :logbook ORDER BY at, state"
                ),
                {"site": site_id, "logbook": logbook_id},
            )
            .mappings()
            .all()
        )
        return [dict(r) for r in rows]


__all__ = ["evaluate", "events", "reset_sender", "set_sender", "tick"]
