# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The field-value index behind a log's declared filters (ADR-268 D4, D5, D7).

A labelled projection of the chain (ADR-251 D1): one row per (entry, field, and for ``readings``
the instrument) and per record the value came from, typed by the declaration, carrying the
entry's labels (site, log, entry type) and the source record's digest. Every scalar field of
every entry is indexed, filter or not, so declaring a filter later needs no backfill.

- **The projector** reads the records past the chain's watermark in ``seq`` order, writes their
  rows, applies a correction by moving ``current`` to the restated value, and advances the
  watermark, in one transaction under a per-chain advisory lock. Writing the same records again
  changes nothing. It runs after an append (:func:`after_append`, never inside the signing
  transaction) and, so that a person finds what they just signed, before every field-filtered
  read (:func:`catch_up`).
- **Rebuild** (:func:`reindex`) deletes the chain's rows and resets its watermark; a new
  :data:`PROJECTOR_VERSION` does the same on the next catch-up. It never touches a signed row.
- **Suggestions** (:func:`describe` with values) come only from the index under the same site,
  log and type predicates as a records read, after the door's read check; nothing is cached. A
  number or quantity range also offers the lowest and highest current values in its declared
  unit (phase 2), read the same way.
- **Health** (:func:`health`) is what ``ext doctor`` reports: a watermark far behind the head,
  values only indexable as text (a number, or a time, that would not parse), values in a unit no
  filter compares, and rows whose source digest no longer matches its record.

No entry form, proposal or presenter reads this module (ADR-268 D6).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, tzinfo
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import and_, delete, func, select, text, update

from . import store
from .db_models import AttestFieldValue, AttestFieldWatermark, AttestRecord
from .logbooks import FilterDecl, Logbook, LogbookError

__all__ = [
    "CATCH_UP_BOUND",
    "PROJECTOR_VERSION",
    "SUGGESTIONS",
    "after_append",
    "analyze_statement",
    "catch_up",
    "describe",
    "health",
    "reindex",
    "seq_clause",
]

#: Bumped when the projection changes; a chain indexed by another version is rebuilt. Version 2:
#: a ``datetime`` value signed without an offset is the site's wall time (phase 2's datetime
#: filters). A site that changes its ``site.timezone`` reindexes its logs.
PROJECTOR_VERSION = "2"
#: Records one statement projects at a time.
BATCH = 500
#: Rows one insert writes (each row binds 18 values).
INSERT_ROWS = 2000
#: How far behind the head a chain may be before doctor reports it: a read brings up at most
#: this many records without anybody noticing the wait.
CATCH_UP_BOUND = 1000
#: The most values an exact-value text filter offers (ADR-268 D5).
SUGGESTIONS = 50

_ENTRY_KINDS = ("statement", "import")
_NUMERIC = ("number", "quantity")


# -- typing a value --------------------------------------------------------------------------


def _dec(value: Any) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    try:
        d = Decimal(value) if isinstance(value, str) else Decimal(int(value))
    except InvalidOperation:
        return None
    return d if d.is_finite() else None


def _text(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (str, int)):
        return str(value)
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


_TRUE = {"true", "yes", "1", "on"}
_FALSE = {"false", "no", "0", "off"}


def _checkbox(value: Any) -> str | None:
    if isinstance(value, bool):
        return "true" if value else "false"
    word = str(value).strip().lower()
    return "true" if word in _TRUE else "false" if word in _FALSE else None


def _in_unit(f: Any, value: Any, unit: str, converted: Any) -> tuple[Decimal | None, str]:
    """A quantity's comparable decimal and its unit: in the declared unit when the record carries
    it, or the ``converted`` value it carries, or by the field's declared exact conversion;
    otherwise the value in the unit it was entered in, which no range compares."""
    want = getattr(f, "unit", "") or ""
    d = _dec(value)
    if d is None:
        return None, unit or want
    if not unit or unit == want:
        return d, want
    if isinstance(converted, dict) and converted.get("unit") == want and _dec(converted.get("value")) is not None:
        return _dec(converted["value"]), want
    for frm, to, factor in getattr(f, "convert", ()) or ():
        if frm == unit and to == want:
            return d * Decimal(factor), want
    return d, unit


def _values(f: Any, raw: Any, zone: tzinfo = UTC) -> list[dict[str, Any]]:
    """The rows one field value gives: one, or one per instrument for ``readings``."""
    ftype = getattr(f, "type", "text")
    if ftype == "readings":
        out = []
        for inst, e in (raw or {}).items() if isinstance(raw, dict) else ():
            if not isinstance(e, dict) or e.get("not_taken"):
                continue
            num, unit = _dec(e.get("value")), str(e.get("unit") or "")
            conv = e.get("converted")
            if isinstance(conv, dict) and _dec(conv.get("value")) is not None:
                num, unit = _dec(conv["value"]), str(conv.get("unit") or "")
            out.append({"instrument": str(inst), "value_text": _text(e.get("value")),
                        "value_num": num, "value_unit": unit or None})  # fmt: skip
        return out
    row: dict[str, Any] = {"instrument": "", "value_text": _text(raw), "value_num": None,
                           "value_unit": None, "value_at": None}  # fmt: skip
    if ftype == "attest_checkbox":
        row["value_text"] = _checkbox(raw) or _text(raw)
    elif ftype in _NUMERIC:
        if isinstance(raw, dict):
            num, unit = _in_unit(f, raw.get("value"), str(raw.get("unit") or ""), raw.get("converted"))
            row["value_text"] = _text(raw.get("value"))
        else:
            num, unit = _in_unit(f, raw, "", None)
        row["value_num"], row["value_unit"] = num, (unit or None)
        if num is None:
            row["value_unit"] = None
    elif ftype == "datetime":
        row["value_at"] = _instant(raw, zone)
    return [row]


def _instant(raw: Any, zone: tzinfo = UTC) -> datetime | None:
    """A ``datetime`` field's instant: ISO 8601. A value without an offset is the wall time the
    person signed at the site (the entry form's own time box), so the site's clock places it:
    ``zone`` is the site's time zone, or UTC when the site names none. Anything else is not a time."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        at = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return at if at.tzinfo else at.replace(tzinfo=zone)


def _zone(site: str) -> tzinfo:
    """The site's clock (``site.timezone``), or UTC when it names none."""
    from . import periods

    try:
        return periods.site_zone(site)
    except periods.PeriodError:
        return UTC


def _field(book: Logbook | None, entry_type: str | None, field_id: str) -> Any:
    if book is None or not entry_type:
        return _Text
    try:
        return book.type(entry_type).field(field_id)
    except LogbookError:
        return _Text


class _Text:
    """A field the declaration no longer names: indexed as text only."""

    type = "text"
    unit = ""
    convert: tuple = ()


# -- the projector ---------------------------------------------------------------------------


def _lock(s: Any, site: str, logbook: str, *, wait: bool) -> bool:
    key = f"attest_field_values:{site}/{logbook}"
    if wait:
        s.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": key})
        return True
    return bool(s.execute(text("SELECT pg_try_advisory_xact_lock(hashtext(:k))"), {"k": key}).scalar())


def _root(s: Any, target: str | None) -> AttestRecord | None:
    """The entry at the root of a correction's thread (a correction of a correction)."""
    seen = 0
    row = s.get(AttestRecord, target) if target else None
    while row is not None and row.kind not in _ENTRY_KINDS and seen < 64:
        row = s.get(AttestRecord, row.target) if row.target else None
        seen += 1
    return row if row is not None and row.kind in _ENTRY_KINDS else None


def _insert(s: Any, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    from sqlalchemy.dialects.postgresql import insert

    for r in rows:
        r.setdefault("value_at", None)
    # A statement carries at most 65,535 bound values; a batch of records can give more rows.
    for i in range(0, len(rows), INSERT_ROWS):
        stmt = insert(AttestFieldValue).values([
            {**r, "value_words": func.to_tsvector("simple", func.coalesce(r["value_text"], ""))}
            for r in rows[i:i + INSERT_ROWS]
        ]).on_conflict_do_nothing(constraint="uq_attest_field_values_source")  # fmt: skip
        s.execute(stmt)


def _project(s: Any, book: Logbook | None, rec: dict[str, Any], row: AttestRecord,
             pending: list[dict[str, Any]], zone: tzinfo = UTC) -> None:  # fmt: skip
    """Add the rows ``row`` gives to ``pending``. A correction first writes what is pending, so
    the value it restates is in the table when ``current`` moves off it."""
    fields = (rec.get("content") or {}).get("fields") or {}
    if not isinstance(fields, dict) or not fields:
        return
    source = {"source_attestation_id": row.attestation_id, "source_seq": row.seq,
              "source_digest": row.digest}  # fmt: skip
    if row.kind in _ENTRY_KINDS:
        entry, restating = row, False
    elif row.kind == "supplement" and rec.get("reason") == "correction":
        _insert(s, pending)
        pending.clear()
        entry, restating = _root(s, row.target), True
        if entry is None:
            return
    else:
        return
    base = {"site_id": entry.site_id, "logbook": entry.logbook, "entry_type": entry.entry_type,
            "entry_seq": entry.seq, "attestation_id": entry.attestation_id, "current": True}  # fmt: skip
    out = pending if not restating else []
    for fid, raw in fields.items():
        f = _field(book, entry.entry_type, fid)
        for v in _values(f, raw, zone):
            out.append({**base, **source, "field": fid, **v})
            if restating:
                # The latest signed correction of the field is its current value (ADR-268 D2).
                s.execute(update(AttestFieldValue).where(
                    AttestFieldValue.site_id == entry.site_id, AttestFieldValue.logbook == entry.logbook,
                    AttestFieldValue.entry_seq == entry.seq, AttestFieldValue.field == fid,
                    AttestFieldValue.instrument == v["instrument"], AttestFieldValue.current.is_(True),
                    AttestFieldValue.source_seq < row.seq,
                ).values(current=False))  # fmt: skip
    if restating:
        _insert(s, out)


def _book(logbook: str) -> Logbook | None:
    from . import registry

    try:
        return registry.get(logbook)
    except LogbookError:
        return None


def catch_up(site: str, logbook: str, *, wait: bool = True) -> int:
    """Bring the chain's index to its head; the number of records projected. ``wait=False``
    gives up at once when another process holds the chain (the read that follows brings it up)."""
    book = _book(logbook)
    if book is None:
        return 0
    done = 0
    with store.session_scope() as s:
        head = s.execute(
            select(func.max(AttestRecord.seq)).where(AttestRecord.site_id == site, AttestRecord.logbook == logbook)
        ).scalar() or 0  # fmt: skip
        wm = s.get(AttestFieldWatermark, (site, logbook))
        if wm is not None and wm.version == PROJECTOR_VERSION and wm.indexed_seq >= head:
            return 0
        if not _lock(s, site, logbook, wait=wait):
            return 0
        s.expire_all()
        wm = s.get(AttestFieldWatermark, (site, logbook))
        if wm is None or wm.version != PROJECTOR_VERSION:
            _clear(s, site, logbook)
            wm = AttestFieldWatermark(site_id=site, logbook=logbook, indexed_seq=0, version=PROJECTOR_VERSION)
            s.add(wm)
        at = wm.indexed_seq
        zone = _zone(site)
        while True:
            rows = s.execute(
                select(AttestRecord).where(
                    AttestRecord.site_id == site, AttestRecord.logbook == logbook, AttestRecord.seq > at,
                ).order_by(AttestRecord.seq).limit(BATCH)
            ).scalars().all()  # fmt: skip
            if not rows:
                break
            pending: list[dict[str, Any]] = []
            for row in rows:
                _project(s, book, row.record, row, pending, zone)
            _insert(s, pending)
            at = rows[-1].seq
            done += len(rows)
        wm.indexed_seq = at
        s.commit()
    return done


def after_append(site: str, logbook: str) -> None:
    """After a record is committed (the signing path's bus publish): best effort, never waits."""
    try:
        catch_up(site, logbook, wait=False)
    except Exception:  # noqa: BLE001 - the next field-filtered read brings the index up
        pass


def _clear(s: Any, site: str, logbook: str) -> None:
    s.execute(delete(AttestFieldValue).where(AttestFieldValue.site_id == site,
                                             AttestFieldValue.logbook == logbook))  # fmt: skip
    s.execute(delete(AttestFieldWatermark).where(AttestFieldWatermark.site_id == site,
                                                 AttestFieldWatermark.logbook == logbook))  # fmt: skip
    s.flush()


def reindex(site: str, logbook: str) -> dict[str, Any]:
    """Delete the chain's rows, reset its watermark and project it again from the records."""
    with store.session_scope() as s:
        _lock(s, site, logbook, wait=True)
        _clear(s, site, logbook)
        s.commit()
    records = catch_up(site, logbook)
    with store.session_scope() as s:
        rows = s.execute(select(func.count()).select_from(AttestFieldValue).where(
            AttestFieldValue.site_id == site, AttestFieldValue.logbook == logbook)).scalar() or 0  # fmt: skip
    return {"site_id": site, "logbook": logbook, "records": records, "values": int(rows)}


# -- the read side ---------------------------------------------------------------------------


def seq_clause(site: str, logbook: str, p: Any) -> Any:
    """``seq IN (...)``: the entries whose current value of ``p.field`` matches ``p``."""
    from .service import text_query

    fv = AttestFieldValue
    sub = select(fv.entry_seq).where(fv.site_id == site, fv.logbook == logbook, fv.field == p.field,
                                     fv.instrument == "", fv.current.is_(True))  # fmt: skip
    if p.types:
        sub = sub.where(fv.entry_type.in_(list(p.types)))
    if p.op == "eq":
        sub = sub.where(fv.value_text.in_(list(p.values)))
    elif p.op == "contains":
        pattern = text_query(" ".join(p.values))
        if pattern is None:
            return AttestRecord.seq.in_(select(fv.entry_seq).where(text("false")))
        sub = sub.where(fv.value_words.op("@@")(func.to_tsquery("simple", pattern)))
    elif p.op in ("gte", "lte") and getattr(p, "field_type", "") == "datetime":
        at = datetime.fromisoformat(p.values[0])
        sub = sub.where(fv.value_at >= at if p.op == "gte" else fv.value_at <= at)
    elif p.op in ("gte", "lte"):
        bound = Decimal(p.values[0])
        cmp = fv.value_num >= bound if p.op == "gte" else fv.value_num <= bound
        unit = fv.value_unit == p.unit if p.unit else fv.value_unit.is_(None)
        sub = sub.where(unit, cmp)
    else:
        raise ValueError(f"unknown filter operation {p.op!r}")
    return AttestRecord.seq.in_(sub)


def _decl_json(f: FilterDecl) -> dict[str, Any]:
    return {"field": f.field, "label": f.label, "field_type": f.field_type, "control": f.control,
            "unit": f.unit or None, "units": list(f.units), "match": f.match or None,
            "types": list(f.types), "observe": f.observe}  # fmt: skip


def _where(site: str, logbook: str, f: FilterDecl) -> Any:
    fv = AttestFieldValue
    return and_(fv.site_id == site, fv.logbook == logbook, fv.field == f.field, fv.instrument == "",
                fv.current.is_(True), fv.entry_type.in_(list(f.types)))  # fmt: skip


def _suggest(s: Any, site: str, logbook: str, f: FilterDecl, prefix: str | None) -> list[dict[str, Any]] | None:
    fv = AttestFieldValue
    if f.control == "choice":
        seen = s.execute(select(fv.value_text).where(_where(site, logbook, f)).distinct()).scalars().all()
        old = sorted({v for v in seen if v is not None} - set(f.choices))
        return ([{"value": c, "offered": True} for c in f.choices]
                + [{"value": v, "offered": False} for v in old])  # fmt: skip
    if f.control == "values":
        stmt = select(fv.value_text, func.count().label("n")).where(_where(site, logbook, f),
                                                                    fv.value_text.is_not(None))  # fmt: skip
        if prefix:
            escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            stmt = stmt.where(fv.value_text.like(f"{escaped}%", escape="\\"))
        stmt = stmt.group_by(fv.value_text).order_by(func.count().desc(), fv.value_text).limit(SUGGESTIONS)
        return [{"value": v, "offered": True} for v, _n in s.execute(stmt).all()]
    return None


def _plain(d: Decimal) -> str:
    """A decimal as a person writes it: no exponent, no trailing zeros."""
    out = format(d.normalize(), "f")
    return "0" if out in ("-0", "") else out


def _range(s: Any, site: str, logbook: str, f: FilterDecl) -> dict[str, Any] | None:
    """The lowest and highest current values of a number or quantity filter in its declared unit
    (ADR-268 D5, phase 2), or None when no readable entry has one. Read under the same predicates
    as suggestions; a value in a unit the filter cannot compare is never part of it."""
    if f.field_type not in _NUMERIC:
        return None
    fv = AttestFieldValue
    unit = fv.value_unit == f.unit if f.unit else fv.value_unit.is_(None)
    low, high = s.execute(select(func.min(fv.value_num), func.max(fv.value_num)).where(
        _where(site, logbook, f), fv.value_num.is_not(None), unit)).one()  # fmt: skip
    if low is None:
        return None
    return {"lowest": _plain(Decimal(low)), "highest": _plain(Decimal(high)), "unit": f.unit or None}


def describe(book: Logbook, site: str | None = None, *, field: str | None = None,
             prefix: str | None = None) -> list[dict[str, Any]]:  # fmt: skip
    """The log's filters in force, in declaration order; with ``site``, the values a reader may
    pick, read from the index at that site only. The caller has made the read check."""
    wanted = [f for f in book.field_filters if field is None or f.field == field]
    out = [_decl_json(f) for f in wanted]
    if site is None:
        return out
    catch_up(site, book.id)
    with store.session_scope() as s:
        for f, row in zip(wanted, out, strict=True):
            row["values"] = _suggest(s, site, book.id, f, prefix)
            row["range"] = _range(s, site, book.id, f)
    return out


def analyze_statement() -> Any:
    """Refresh the planner's statistics of the index after a bulk build (the scale fixture)."""
    return text("ANALYZE attest_field_values")  # nosemgrep: avoid-sqlalchemy-text -- fixed maintenance statement, no input


# -- doctor ----------------------------------------------------------------------------------


def health(book: Logbook) -> list[dict[str, Any]]:
    """What doctor reports for one log, per site with a chain; empty when the index is healthy."""
    from .db_models import AttestChainHead

    fv = AttestFieldValue
    out: list[dict[str, Any]] = []
    with store.session_scope() as s:
        heads = s.execute(select(AttestChainHead.site_id, AttestChainHead.seq)
                          .where(AttestChainHead.logbook == book.id)).all()  # fmt: skip
        for site, head in heads:
            wm = s.get(AttestFieldWatermark, (site, book.id))
            at = wm.indexed_seq if wm is not None and wm.version == PROJECTOR_VERSION else 0
            if head - at > CATCH_UP_BOUND:
                out.append({"site": site, "problem": "behind", "count": int(head - at)})
            for f in book.field_filters:
                if f.field_type not in (*_NUMERIC, "datetime"):
                    continue
                typed = fv.value_at if f.field_type == "datetime" else fv.value_num
                n = s.execute(select(func.count()).where(_where(site, book.id, f), typed.is_(None),
                                                         fv.value_text.is_not(None))).scalar() or 0  # fmt: skip
                if f.field_type == "datetime":
                    if n:
                        out.append({"site": site, "problem": "text_only", "field": f.field, "count": int(n)})
                    continue
                if n:
                    out.append({"site": site, "problem": "text_only", "field": f.field, "count": int(n)})
                if f.unit:
                    units = s.execute(select(fv.value_unit, func.count()).where(
                        _where(site, book.id, f), fv.value_num.is_not(None), fv.value_unit != f.unit,
                    ).group_by(fv.value_unit)).all()  # fmt: skip
                    if units:
                        out.append({"site": site, "problem": "unit", "field": f.field,
                                    "count": int(sum(c for _u, c in units)),
                                    "units": sorted(str(u) for u, _c in units)})  # fmt: skip
            drift = s.execute(
                select(func.count()).select_from(fv)
                .outerjoin(AttestRecord, AttestRecord.attestation_id == fv.source_attestation_id)
                .where(fv.site_id == site, fv.logbook == book.id,
                       (AttestRecord.digest.is_(None)) | (AttestRecord.digest != fv.source_digest))
            ).scalar() or 0  # fmt: skip
            if drift:
                out.append({"site": site, "problem": "digest", "count": int(drift)})
    return out
