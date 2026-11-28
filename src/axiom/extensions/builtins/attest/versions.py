# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The version history of a state derived from signed records (#1803).

A **state** is a declared key set (field ids) of one log at one site, optionally of one entry
type. Reading the log's signed entries oldest first, each entry that changes the value of at least
one declared key **opens a version**; an entry that states every key it holds unchanged opens
none. Each version names the entry that opened it, so a surface links to it through the record
door, and lists every key known so far with its change since the version before, worded here:
``no change``, or ``from X``. A surface formats nothing (L7's rule for the web).

This is L7's comparison, not a second one: numbers are compared exactly with
:func:`changes.compare` (so ``500`` and ``500.0`` are the same value, and a unit change is a
change), and every value is read after corrections with :func:`changes.corrected`, so a
correction changes the version its entry opened and never opens one of its own.

It reads signed statements only (never a draft, telemetry or a site setting), bounded: at most
:data:`MAX_RECORDS` entries, read in pages by ``seq``; ``complete`` says whether it read them all.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from sqlalchemy import or_, select

from . import changes, registry, service, store
from .db_models import AttestRecord

#: Entries read per page, and at most in all.
PAGE = 500
MAX_RECORDS = 5000
#: The words of a change. A key's value in the first version has none; a key first given a value
#: in a later version is ``set``, so every key of every later version carries words.
NO_CHANGE = "no change"
FROM = "from {}"
SET = "set"
_NUMBERS = ("quantity", "number")


def same(a: Any, b: Any) -> bool:
    """Whether two values of a key are the same: L7's exact comparison for numbers
    (``changes.compare``), equality of their canonical JSON for anything else."""
    if isinstance(a, dict) and isinstance(b, dict) and "value" in a and "value" in b:
        try:
            return changes.compare(a, b)["kind"] == "no_change"
        except (TypeError, ValueError):
            pass
    return json.dumps(a, sort_keys=True, default=str) == json.dumps(b, sort_keys=True, default=str)


def shown(v: Any) -> str:
    """A value as words: ``500 kW`` for a quantity, a text as it is, a list joined."""
    if v is None:
        return ""
    if isinstance(v, dict) and "value" in v:
        return f"{v.get('value')} {v.get('unit') or ''}".strip()
    if isinstance(v, dict):
        return "; ".join(f"{k} {shown(x)}" for k, x in v.items())
    if isinstance(v, (list, tuple)):
        return ", ".join(shown(x) for x in v)
    return str(v)


def _picker(f: Any, key: str):
    def pick(fields: dict[str, Any]) -> Any:
        if key not in fields or fields[key] is None:
            return None
        raw = fields[key]
        if f.type in _NUMBERS:
            v = changes._value(f, raw)  # L7's reader: {value, unit} as decimal strings
            if v is not None:
                return v
        return raw

    return pick


def _labels(book: Any, keys: list[str], entry_type: str | None) -> list[dict[str, str]]:
    types = [book.type(entry_type)] if entry_type else list(book.types)
    out = []
    for k in keys:
        label = next((f.label for t in types for f in t.fields if f.id == k and f.label), None)
        out.append({"key": k, "label": label or k})
    return out


def _rows(site_id: str, logbook: str, keys: list[str], entry_type: str | None) -> tuple[list[dict], bool]:
    """Signed statements holding any key, oldest first, bounded; and whether all were read."""
    fields = AttestRecord.record["content"]["fields"]
    out: list[dict] = []
    after: int | None = None
    while len(out) < MAX_RECORDS:
        q = (
            select(AttestRecord.record, AttestRecord.seq)
            .where(
                AttestRecord.site_id == site_id,
                AttestRecord.logbook == logbook,
                AttestRecord.kind == "statement",
                or_(*[fields.has_key(k) for k in keys]),
            )
            .order_by(AttestRecord.seq.asc())
            .limit(min(PAGE, MAX_RECORDS - len(out)))
        )
        if entry_type:
            q = q.where(AttestRecord.entry_type == entry_type)
        if after is not None:
            q = q.where(AttestRecord.seq > after)
        with store.session_scope() as s:
            rows = s.execute(q).all()
        out.extend(rec for rec, _ in rows)
        if len(rows) < PAGE:
            return out, True
        after = int(rows[-1][1])
    return out, False


def _opened_by(rec: dict[str, Any]) -> dict[str, Any]:
    signer = rec.get("signer") or {}
    return {
        "attestation_id": rec["attestation_id"],
        "seq": rec["seq"],
        "occurred_at": rec.get("occurred_at"),
        "signer": signer.get("display") or signer.get("principal"),
        "entry_type": rec.get("entry_type"),
        "logbook": rec.get("logbook"),
    }


def derive(site_id: str, logbook: str, keys: Iterable[str], *,
           entry_type: str | None = None) -> dict[str, Any]:  # fmt: skip
    """The versions of ``keys`` in ``logbook`` at ``site_id``, newest first.

    ``{site_id, logbook, entry_type, keys: [{key, label}], versions: [...], complete}``; each
    version is ``{number, opened_by: {attestation_id, seq, occurred_at, signer, entry_type,
    logbook}, keys: [{key, label, value, previous, kind, change, corrected_by}]}`` with ``kind``
    one of ``first``, ``set`` (first given a value after the first version), ``no_change``,
    ``changed``, and ``change`` the words (empty for ``first``).
    Raises :class:`LogbookError` for an undeclared log or entry type."""
    keys = list(dict.fromkeys(k for k in keys if k))
    book = registry.get(logbook)
    labels = _labels(book, keys, entry_type)
    body: dict[str, Any] = {"site_id": site_id, "logbook": logbook, "entry_type": entry_type,
                            "keys": labels, "versions": [], "complete": True}  # fmt: skip
    if not keys:
        return body
    label_of = {x["key"]: x["label"] for x in labels}
    rows, body["complete"] = _rows(site_id, logbook, keys, entry_type)
    state: dict[str, tuple[Any, str | None]] = {}
    out: list[dict[str, Any]] = []
    for rec in rows:
        try:
            et = book.type(rec.get("entry_type") or "")
        except Exception:  # noqa: BLE001 - a type no longer declared has no fields to read
            continue
        declared = {f.id: f for f in et.fields}
        thread = service.thread(rec["attestation_id"])
        stated: dict[str, tuple[Any, str | None]] = {}
        for k in keys:
            if k in declared:
                v, by = changes.corrected(rec, _picker(declared[k], k), thread=thread)
                if v is not None:
                    stated[k] = (v, by)
        if not any(k not in state or not same(state[k][0], v) for k, (v, _) in stated.items()):
            continue
        now = {**state, **stated}
        items = []
        for k in keys:
            if k not in now:
                continue
            value, by = now[k]
            before = state.get(k)
            if before is None:
                kind, words = ("first", "") if not out else ("set", SET)
            elif same(before[0], value):
                kind, words = "no_change", NO_CHANGE
            else:
                kind, words = "changed", FROM.format(shown(before[0]))
            items.append({"key": k, "label": label_of[k], "value": shown(value),
                          "previous": shown(before[0]) if before else None, "kind": kind,
                          "change": words, "corrected_by": by})  # fmt: skip
        out.append({"number": len(out) + 1, "opened_by": _opened_by(rec), "keys": items})
        state = now
    body["versions"] = out[::-1]
    return body


__all__ = ["FROM", "MAX_RECORDS", "NO_CHANGE", "SET", "derive", "same", "shown"]
