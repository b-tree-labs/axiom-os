# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Logbook predicates and their discrepancy facts (spec-attestation, "Logbook predicates", #1816).

A log declares ``[[predicate]] id, label, source, expects = "interval.<kind>.open"``: a fact an
independent source states about the site, for example "an open interval is expected now". An
extension binds the source (``Inputs.predicate_source``); it answers ``True``, ``False`` or
``None`` (it cannot say) with the time its answer was current, reading gold through the gold door
(:class:`~.gold_reader.GoldReader`).

On the one-minute tick, :func:`tick` evaluates each predicate at each site and records a change
of state (or of why it cannot be known) once, as an append-only fact carrying the log's labels:

- ``discrepancy``: the source says the interval should be open and none is (the log is missing an
  entry). Published as ``attest.<logbook>.predicate_discrepancy`` and told to the ``notify`` roles.
- ``consistent``: an interval is open, or the source does not expect one. An interval open while
  the source says no is consistent: an interval may be open while idle.
- ``unknown``: the source is stale (older than ``stale_after``), faulted, silent, not bound or
  failing. Never ``consistent``: a predicate that cannot run says so.

:func:`current` words each predicate's latest state for a surface: ``unlogged`` and
``feed_stale`` are the Log page's honest states (appkit ``logbookStates.ts``).
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import text

from . import registry, roles, store
from .gold_reader import GoldReader
from .logbooks import LogbookError, PredicateDecl

log = logging.getLogger("axiom.attest.predicates")

DISCREPANCY, CONSISTENT, UNKNOWN = "discrepancy", "consistent", "unknown"

#: The Log page's honest state for each predicate state (W4): consistent is no warning.
HONEST_STATE = {DISCREPANCY: "unlogged", UNKNOWN: "feed_stale", CONSISTENT: None}

#: Why a predicate cannot run, in the words that end "the source is ...".
_UNKNOWN_WORDS = {"stale": "stale", "faulted": "faulted", "unbound": "not bound on this node",
                  "source_failed": "failing", "no_value": "silent"}  # fmt: skip


@dataclass(frozen=True)
class PredicateReading:
    """A source's answer: ``value`` True, False or None (cannot say), ``as_of`` when it was
    current, ``reason`` in a word (``faulted``) or a short phrase, ``detail`` for the record."""

    value: bool | None
    as_of: datetime | None
    reason: str = ""
    detail: dict[str, Any] | None = None


PredicateSource = Callable[[str, datetime, GoldReader], PredicateReading]
Sender = Callable[[str, dict[str, Any]], None]

_sources: dict[str, PredicateSource] = {}


def register_source(name: str, source: PredicateSource) -> None:
    """Bind the source a ``[[predicate]] source = name`` names (``Inputs.predicate_source``)."""
    _sources[name] = source


def unregister_source(name: str) -> None:
    _sources.pop(name, None)


def bound(name: str) -> bool:
    if name not in _sources:
        registry.ensure()
    return name in _sources


def _default_sender(recipient: str, fact: dict[str, Any]) -> None:
    from axiom.extensions.builtins.notifications.send import (
        NotificationPayload,
        Priority,
        SendContext,
        send,
    )
    from axiom.governance.classification import Classification

    send(
        SendContext.default(),
        actor=f"@attest:{fact['site_id']}",
        recipient=recipient,
        payload=NotificationPayload(
            summary=fact["words"],
            body=f"{fact['logbook']}: {fact['reason'] or fact['words']}",
            metadata={k: str(v) for k, v in fact.items() if k not in ("labels", "expects", "source_value")},
        ),
        classification=Classification.INTERNAL,
        priority=Priority.URGENT if fact["severity"] == "alarm" else Priority.HIGH,
        dedup_key=f"{fact['logbook']}:{fact['predicate']}:{fact['at']}:{fact['state']}",
    )


_sender: Sender = _default_sender


def set_sender(sender: Sender) -> None:
    """Seam: where discrepancy notices go (tests capture them)."""
    global _sender
    _sender = sender


def reset_sender() -> None:
    global _sender
    _sender = _default_sender


def _aware(at: datetime | None) -> datetime | None:
    if at is None:
        return None
    return at if at.tzinfo else at.replace(tzinfo=UTC)


def _read(p: PredicateDecl, site_id: str, now: datetime) -> tuple[PredicateReading | None, str]:
    fn = _sources.get(p.source)
    if fn is None:
        registry.ensure()
        fn = _sources.get(p.source)
    if fn is None:
        return None, "unbound"
    try:
        got = fn(site_id, now, GoldReader())
    except Exception:  # noqa: BLE001 - a failing source is a fact (unknown), never a crash
        log.warning("predicate source %s failed at %s", p.source, site_id, exc_info=True)
        return None, "source_failed"
    if not isinstance(got, PredicateReading):
        return None, "source_failed"
    if got.value is None:
        return got, got.reason or "no_value"
    as_of = _aware(got.as_of)
    if as_of is None or as_of < now - timedelta(seconds=p.stale_after_seconds):
        return got, "stale"
    return got, ""


def _open(s: Any, site_id: str, logbook: str, kind: str) -> int | None:
    return s.execute(
        text("SELECT number FROM attest_intervals WHERE site_id = :site AND logbook = :log AND "
             "kind = :kind AND closed_at IS NULL"),
        {"site": site_id, "log": logbook, "kind": kind},
    ).scalar()  # fmt: skip


def _words(label: str, state: str, reason: str, kind: str, as_of: Any, at: Any) -> str:
    if state == DISCREPANCY:
        return f"{label} with no open {kind}"
    if state == UNKNOWN:
        why = _UNKNOWN_WORDS.get(reason, reason or "silent")
        if reason == "stale" and as_of is not None and at is not None:
            minutes = int((_aware(at) - _aware(as_of)).total_seconds() // 60)  # type: ignore[operator]
            why = f"{minutes} min old"
        return f"{label} check cannot run: the source is {why}"
    return f"{label}: the log agrees with the source"


def evaluate(site_id: str, logbook_id: str, *, now: datetime) -> list[dict[str, Any]]:
    """Each declared predicate's state at the site now. Reads the source and the interval
    projection; records nothing."""
    book = registry.get(logbook_id)
    out: list[dict[str, Any]] = []
    with store.session_scope() as s:
        for p in book.predicates:
            got, why = _read(p, site_id, now)
            number = _open(s, site_id, logbook_id, p.expects_interval)
            if why:
                state = UNKNOWN
            elif got is not None and got.value is True and number is None:
                state, why = DISCREPANCY, got.reason or "expected_open"
            else:
                state = CONSISTENT
            as_of = _aware(got.as_of) if got is not None else None
            out.append({
                "site_id": site_id, "logbook": logbook_id, "predicate": p.id, "label": p.label,
                "state": state, "reason": why if state != CONSISTENT else "",
                "severity": "alarm" if p.required else "notice",
                "expects": {"kind": p.expects_interval, "open": number is not None,
                            "number": int(number) if number is not None else None},
                "source_value": {"value": got.value if got is not None else None,
                                 "as_of": as_of.isoformat() if as_of else None,
                                 "detail": got.detail if got is not None else None},
                "as_of": as_of, "at": now, "labels": book.labels, "notify": list(p.notify),
            })  # fmt: skip
    return out


def _last_state(s: Any, site_id: str, logbook: str, predicate: str) -> tuple[str, str] | None:
    row = s.execute(
        text("SELECT state, reason FROM attest_predicate_events WHERE site_id = :site AND logbook = :log "
             "AND predicate = :p ORDER BY at DESC, event_id DESC LIMIT 1"),
        {"site": site_id, "log": logbook, "p": predicate},
    ).one_or_none()  # fmt: skip
    return (row.state, row.reason) if row is not None else None


def tick(site_id: str, logbook_id: str, *, now: datetime, state_dir: Path) -> list[dict[str, Any]]:
    """Record, publish and notify each change of a predicate's state since the last tick."""
    from axiom.infra.bus import get_default_eventbus

    changes: list[dict[str, Any]] = []
    states = evaluate(site_id, logbook_id, now=now)
    with store.session_scope() as s:
        for st in states:
            # A change of state, or of why a source cannot say, is recorded once.
            if _last_state(s, site_id, logbook_id, st["predicate"]) == (st["state"], st["reason"]):
                continue
            event_id = str(uuid.uuid4())
            s.execute(
                text(
                    "INSERT INTO attest_predicate_events (event_id, site_id, logbook, predicate, state, "
                    "reason, severity, source_value, labels, as_of, at) VALUES (:id, :site, :log, :p, "
                    ":state, :reason, :sev, CAST(:src AS jsonb), CAST(:labels AS jsonb), :as_of, :at)"
                ),
                {"id": event_id, "site": site_id, "log": logbook_id, "p": st["predicate"],
                 "state": st["state"], "reason": st["reason"], "sev": st["severity"],
                 "src": json.dumps({**st["source_value"], "expects": st["expects"]}, default=str),
                 "labels": json.dumps(st["labels"]), "as_of": st["as_of"], "at": now},
            )  # fmt: skip
            words = _words(st["label"], st["state"], st["reason"], st["expects"]["kind"], st["as_of"], now)
            changes.append({**st, "event_id": event_id, "words": words,
                            "honest_state": HONEST_STATE[st["state"]]})  # fmt: skip
        s.commit()
    bus = get_default_eventbus()
    holders = roles.assignments(site_id, state_dir=state_dir) if changes else []
    for ch in changes:
        payload = {"schema_version": 1, **{k: (v.isoformat() if isinstance(v, datetime) else v)
                                           for k, v in ch.items() if k != "notify"}}  # fmt: skip
        try:
            bus.publish(f"attest.{logbook_id}.predicate_{ch['state']}", payload, source="attest")
        except Exception:  # noqa: BLE001 - the fact is recorded; delivery is best effort
            pass
        if ch["state"] != DISCREPANCY:
            continue
        for h in holders:
            if set(h["roles"]) & set(ch["notify"]):
                _sender(h["principal"], payload)
    return changes


def events(site_id: str, logbook_id: str) -> list[dict[str, Any]]:
    """Every recorded predicate event of the log at the site, oldest first."""
    with store.session_scope() as s:
        rows = s.execute(
            text("SELECT event_id, predicate, state, reason, severity, source_value, labels, as_of, at "
                 "FROM attest_predicate_events WHERE site_id = :site AND logbook = :log "
                 "ORDER BY at, event_id"),
            {"site": site_id, "log": logbook_id},
        ).mappings().all()  # fmt: skip
    return [dict(r) for r in rows]


def current(site_id: str, logbook_id: str) -> list[dict[str, Any]]:
    """Each declared predicate's latest recorded state, worded; none recorded is not listed (a
    surface then claims nothing, never an all-clear)."""
    try:
        book = registry.get(logbook_id)
    except LogbookError:
        return []
    by_id = {p.id: p for p in book.predicates}
    latest: dict[str, dict[str, Any]] = {}
    for e in events(site_id, logbook_id):
        latest[e["predicate"]] = e
    out = []
    for pid, p in by_id.items():
        e = latest.get(pid)
        if e is None:
            continue
        expects = (e.get("source_value") or {}).get("expects") or {"kind": p.expects_interval}
        out.append({
            "predicate": pid, "label": p.label, "state": e["state"], "reason": e["reason"],
            "severity": e["severity"], "honest_state": HONEST_STATE.get(e["state"]),
            "words": _words(p.label, e["state"], e["reason"], expects.get("kind") or p.expects_interval,
                            e["as_of"], e["at"]),
            "since": e["at"].isoformat(), "as_of": e["as_of"].isoformat() if e["as_of"] else None,
            "expects": expects, "labels": e["labels"],
        })  # fmt: skip
    return out


# -- the tick over every log -------------------------------------------------------------------------


def _sites(logbook_id: str) -> list[str]:
    from axiom.infra.site_scope import deployment_sites

    served = deployment_sites()
    if served is not None:
        return sorted(served)
    with store.session_scope() as s:
        return sorted(s.execute(
            text("SELECT DISTINCT site_id FROM attest_chain_heads WHERE logbook = :log"),
            {"log": logbook_id},
        ).scalars().all())  # fmt: skip


def tick_all(*, now: datetime, state_dir: Path) -> list[dict[str, Any]]:
    """The scheduled pass: every predicate at every site the node serves, and the cross-check
    catch-up for any signed record the post-sign hook missed."""
    from . import crosscheck

    changes: list[dict[str, Any]] = []
    for book in registry.all_logbooks():
        if not (book.predicates or book.crosschecks):
            continue
        for site in _sites(book.id):
            try:
                if book.predicates:
                    changes += tick(site, book.id, now=now, state_dir=state_dir)
                if book.crosschecks:
                    crosscheck.catch_up(site, book.id)
            except Exception:  # noqa: BLE001 - one log's failure never stops the others
                log.warning("predicate tick for %s at %s failed", book.id, site, exc_info=True)
    return changes


__all__ = [
    "CONSISTENT", "DISCREPANCY", "HONEST_STATE", "PredicateReading", "UNKNOWN", "bound", "current",
    "evaluate", "events", "register_source", "reset_sender", "set_sender", "tick", "tick_all",
    "unregister_source",
]  # fmt: skip
