# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Change since the previous signed value (logbook unit L7).

Design note ``docs/working/design-note-l7-change.md``, approved 2026-11-12. For a ``quantity``,
``number`` or ``readings`` value (per instrument) this module finds the previous **signed** value
of the same field, in the same log, at the same site, for the same entry type, across intervals,
after corrections (a later supplement with reason ``correction`` that restates the field
replaces it); compares the two exactly, with :class:`decimal.Decimal`; and words the change: a
short default phrase only when the value moved ("up 450 kW"), and a detail one step away ("from
500 kW, #412, 06:58, in run 41"; "same as last, ..."). A unit mismatch is told to the signer at
confirmation and to nobody else.

It reads signed records only (never a draft, telemetry or a site setting), offers nothing before
a person has entered a value, and never touches a statement or its digest: the presentation
appends the words to its stored card and page text.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select

from . import registry, store
from .db_models import AttestRecord

#: The field types that have a change.
NUMERIC_TYPES = ("quantity", "number", "readings")
#: Candidates read per page while looking for the previous value, and pages at most: newest
#: first by ``seq``, so the first candidate almost always answers.
PAGE = 20
MAX_PAGES = 5
BLOCK_TITLE = "Change since previous"


# -- exact comparison ---------------------------------------------------------------------


def _dec(value: Any) -> Decimal:
    """A finite decimal from a decimal string or an int; a float or anything else is refused."""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise TypeError(f"a value is compared as a decimal string, not {type(value).__name__}")
    try:
        d = Decimal(value) if isinstance(value, str) else Decimal(int(value))
    except InvalidOperation:
        raise ValueError(f"{value!r} is not a decimal value") from None
    if not d.is_finite():
        raise ValueError(f"{value!r} is not a finite value")
    return d


def _fmt(d: Decimal) -> str:
    return format(d, "f")


def compare(previous: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    """``{kind, delta}``: ``up``, ``down`` or ``no_change`` with the signed difference as a decimal
    string kept to the larger number of decimal places of the two inputs, or ``unit_changed``
    with no number when the units differ."""
    if str(previous.get("unit") or "") != str(current.get("unit") or ""):
        _dec(previous.get("value"))
        _dec(current.get("value"))
        return {"kind": "unit_changed", "delta": None}
    p, c = _dec(previous.get("value")), _dec(current.get("value"))
    exponent = min(int(p.as_tuple().exponent), int(c.as_tuple().exponent), 0)
    diff = (c - p).quantize(Decimal(1).scaleb(exponent))
    kind = "up" if diff > 0 else ("down" if diff < 0 else "no_change")
    return {"kind": kind, "delta": _fmt(diff)}


# -- the words ----------------------------------------------------------------------------


def _at(occurred_at: Any) -> str:
    try:
        return datetime.fromisoformat(str(occurred_at).replace("Z", "+00:00")).strftime("%H:%M")
    except ValueError:
        return str(occurred_at or "")


def _shown(v: dict[str, Any]) -> str:
    return f"{v.get('value')} {v.get('unit') or ''}".strip()


def _same_interval(a: dict[str, Any] | None, b: dict[str, Any] | None) -> bool:
    if not a:
        return True
    return bool(b) and (a.get("kind"), a.get("number")) == (b.get("kind"), b.get("number"))


def describe(field: str, instrument: str | None, label: str, previous: dict[str, Any],
             current: dict[str, Any], *, signer: bool = True,
             current_interval: dict[str, Any] | None = None) -> dict[str, Any]:  # fmt: skip
    """One change: what the comparison found, and its words (design note, section 3).

    ``text`` is the default phrase, empty when nothing shows by default; ``detail`` is the line
    one step away. ``signer`` is true at confirmation: only the signer is told the unit differs,
    because only the signer can fix it."""
    found = compare(previous, current)
    unit = str(current.get("unit") or "")
    where = f"#{previous.get('seq')}, {_at(previous.get('occurred_at'))}"
    interval = previous.get("interval")
    if interval and not _same_interval(interval, current_interval):
        where += f", in {interval.get('kind')} {interval.get('number')}"
    if found["kind"] in ("up", "down"):
        size = _fmt(abs(Decimal(found["delta"])))
        text = f"{found['kind']} {size} {unit}".strip()
        detail = f"from {_shown(previous)}, {where}"
    elif found["kind"] == "no_change":
        text = ""
        detail = f"same as last, {_shown(previous)}, {where}"
    else:
        text = f"unit differs from the last entry ({previous.get('unit')})" if signer else ""
        detail = f"from {_shown(previous)}, {where}; now {_shown(current)}"
    return {
        "field": field,
        "instrument": instrument,
        "label": label,
        "kind": found["kind"],
        "delta": found["delta"],
        "previous": {"value": str(previous.get("value")), "unit": str(previous.get("unit") or "")},
        "previous_ref": {
            "attestation_id": previous.get("attestation_id"),
            "seq": previous.get("seq"),
            "occurred_at": previous.get("occurred_at"),
            "interval": interval,
            "corrected_by": previous.get("corrected_by"),
        },
        "current": {"value": str(current.get("value")), "unit": unit},
        "text": text,
        "detail": detail,
    }


def block(items: list[dict[str, Any]]) -> str:
    """The "Change since previous" block a card and a page append; empty when there is none."""
    if not items:
        return ""
    lines = [BLOCK_TITLE]
    for c in items:
        said = f"{c['text']}; {c['detail']}" if c["text"] else c["detail"]
        lines.append(f"  {c['label']}: {said}")
    return "\n".join(lines)


# -- values of a field --------------------------------------------------------------------


def _value(f: Any, raw: Any) -> dict[str, Any] | None:
    """``{value, unit}`` of a quantity or number as stored, or None when it is not a number."""
    if isinstance(raw, dict):
        value, unit = raw.get("value"), raw.get("unit") or getattr(f, "unit", "")
    else:
        value, unit = raw, getattr(f, "unit", "")
    try:
        _dec(value)
    except (TypeError, ValueError):
        return None
    return {"value": str(value), "unit": str(unit or "")}


def _values(f: Any, raw: Any) -> list[tuple[str | None, dict[str, Any]]]:
    """``(instrument, {value, unit})`` for each value a field holds: one, or one per instrument."""
    if f.type == "readings":
        if not isinstance(raw, dict):
            return []
        out: list[tuple[str | None, dict[str, Any]]] = []
        for inst, entry in raw.items():
            v = _value(f, entry) if isinstance(entry, dict) else None
            if v is not None:
                out.append((str(inst), v))
        return out
    v = _value(f, raw)
    return [] if v is None else [(None, v)]


def _pick(f: Any, fields: dict[str, Any], instrument: str | None) -> dict[str, Any] | None:
    for inst, v in _values(f, fields.get(f.id)) if f.id in fields else ():
        if inst == instrument:
            return v
    return None


def effective(rec: dict[str, Any], f: Any, instrument: str | None) -> dict[str, Any] | None:
    """A signed entry's value for a field after corrections (design note L2, section 6): the
    last record of its thread that restates it, the entry itself or a ``correction``."""
    from . import service

    best = _pick(f, (rec.get("content") or {}).get("fields") or {}, instrument)
    corrected_by = None
    for item in service.thread(rec["attestation_id"])[1:]:
        if item.get("kind") != "supplement" or item.get("reason") != "correction":
            continue
        v = _pick(f, (item.get("content") or {}).get("fields") or {}, instrument)
        if v is not None:
            best, corrected_by = v, item["attestation_id"]
    if best is None:
        return None
    return {**best, "corrected_by": corrected_by}


def previous_value(site_id: str, logbook: str, entry_type: str, field_id: str, *,
                   instrument: str | None = None,
                   before_seq: int | None = None) -> dict[str, Any] | None:  # fmt: skip
    """The latest earlier signed value of a field, after corrections, as ``{value, unit,
    attestation_id, seq, occurred_at, interval, corrected_by}``, or None. Keyset by ``seq``,
    newest first and bounded; never a whole chain."""
    f = registry.get(logbook).type(entry_type).field(field_id)
    position = before_seq
    for _ in range(MAX_PAGES):
        q = (
            select(AttestRecord.record, AttestRecord.seq)
            .where(
                AttestRecord.site_id == site_id,
                AttestRecord.logbook == logbook,
                AttestRecord.kind == "statement",
                AttestRecord.entry_type == entry_type,
                AttestRecord.record["content"]["fields"].has_key(field_id),
            )
            .order_by(AttestRecord.seq.desc())
            .limit(PAGE)
        )
        if instrument is not None:
            q = q.where(AttestRecord.record["content"]["fields"][field_id].has_key(instrument))
        if position is not None:
            q = q.where(AttestRecord.seq < position)
        with store.session_scope() as s:
            rows = s.execute(q).all()
        for rec, seq in rows:
            v = effective(rec, f, instrument)
            if v is not None:
                return {**v, "attestation_id": rec["attestation_id"], "seq": int(seq),
                        "occurred_at": rec.get("occurred_at"),
                        "interval": rec.get("interval")}  # fmt: skip
            position = int(seq)
        if len(rows) < PAGE:
            return None
        position = int(rows[-1][1])
    return None


# -- for an entry, and for a signed record ------------------------------------------------


def _labels(f: Any, site_id: str) -> dict[str, str]:
    from . import field_sources

    if f.type != "readings" or not f.from_site:
        return {}
    try:
        return {i.id: i.label for i in field_sources.resolve(f.from_site, site_id)}
    except LookupError:
        return {}


def for_entry(site_id: str, logbook: str, entry_type: str, fields: dict[str, Any], *,
              before_seq: int | None = None, signer: bool = True,
              current_interval: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None,
              ) -> list[dict[str, Any]]:  # fmt: skip
    """The changes of the values ``fields`` holds, against the previous signed values. A field
    with no value has none, so nothing is ever offered before a person has entered one."""
    et = registry.get(logbook).type(entry_type)
    out: list[dict[str, Any]] = []
    for f in et.fields:
        if f.type not in NUMERIC_TYPES or f.id not in fields:
            continue
        labels = _labels(f, site_id)
        for inst, value in _values(f, fields[f.id]):
            prev = previous_value(site_id, logbook, entry_type, f.id, instrument=inst,
                                  before_seq=before_seq)  # fmt: skip
            if prev is None:
                continue
            label = labels.get(inst, inst) if inst else (f.label or f.id)
            here = current_interval(prev) if current_interval is not None else None
            out.append(describe(f.id, inst, label, prev, value, signer=signer,
                                current_interval=here))  # fmt: skip
    return out


def at_confirmation(site_id: str, logbook: str, entry_type: str,
                    fields: dict[str, Any]) -> list[dict[str, Any]]:  # fmt: skip
    """What the confirm card shows: the person's entered values against the previous signed ones,
    the interval compared with the one open now."""
    from . import intervals

    def open_now(prev: dict[str, Any]) -> dict[str, Any] | None:
        kind = (prev.get("interval") or {}).get("kind")
        if not kind:
            return None
        row = intervals.open_interval(site_id, logbook, kind)
        return None if row is None else {"kind": kind, "number": row["number"]}

    return for_entry(site_id, logbook, entry_type, fields, signer=True, current_interval=open_now)


def for_record(rec: dict[str, Any]) -> list[dict[str, Any]]:
    """A signed entry's changes, read now: its values (after corrections) against the signed
    values before it. A reader is never warned about a unit; the two values sit in the detail."""
    if rec.get("kind", "statement") != "statement" or not rec.get("entry_type"):
        return []
    try:
        et = registry.get(rec["logbook"]).type(rec["entry_type"])
    except Exception:  # noqa: BLE001 - an undeclared type has no fields to compare
        return []
    fields: dict[str, Any] = {}
    for f in et.fields:
        if f.type not in NUMERIC_TYPES:
            continue
        stated = ((rec.get("content") or {}).get("fields") or {}).get(f.id)
        if stated is None:
            continue
        if f.type == "readings":
            now = {inst: effective(rec, f, inst) for inst, _ in _values(f, stated)}
            fields[f.id] = {k: v for k, v in now.items() if v is not None}
        else:
            v = effective(rec, f, None)
            if v is not None:
                fields[f.id] = {"value": v["value"], "unit": v["unit"]}
    own = rec.get("interval")
    return for_entry(rec["site_id"], rec["logbook"], rec["entry_type"], fields,
                     before_seq=int(rec["seq"]), signer=False,
                     current_interval=lambda prev: own)  # fmt: skip


__all__ = [
    "BLOCK_TITLE",
    "NUMERIC_TYPES",
    "at_confirmation",
    "block",
    "compare",
    "describe",
    "effective",
    "for_entry",
    "for_record",
    "previous_value",
]
