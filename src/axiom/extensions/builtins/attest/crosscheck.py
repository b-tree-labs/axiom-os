# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The post-sign cross-check hook (spec-attestation, "Cross-check hook (post-sign)", #1816).

A log declares ``[[crosscheck]] field, source, tolerance_from_site``. After a statement whose
type carries the field is signed, attest asks the source the extension bound for the field's
value at the record's ``occurred_at`` and stores one linked system fact per (record, field):
``{attestation_id, field, source_value, tolerance, result, reason}``, with one detail per
instrument for a readings field.

- **Never in the record, never before it.** The fact names the record and lives beside the chain;
  the record, its digest and the chain are untouched. No source is asked while a person drafts or
  confirms, so nothing is offered for an observed field (ADR-144).
- **Honest.** A source that is stale, faulted, unitless or silent, that is not bound or fails, a
  unit with no declared conversion, a missing tolerance and a reading not taken are each
  ``not_checked`` with that reason, never ``agrees``. A field with any instrument not checked is
  never ``agrees``.
- **Exact.** Agreement is ``|entered - source| <= tolerance`` in decimal arithmetic, after a
  declared conversion only. Each side's uncertainty is carried on the fact as stated and never
  combined here (core invariant: combining is :mod:`axiom.uncertainty`'s, with correlation).
- **Gold.** A source reads gold through :class:`~.gold_reader.GoldReader`, the gold verbs only.
- **Best effort after the commit.** A failing hook never fails or undoes the signature; the tick
  (:func:`catch_up`) checks a signed record the hook missed, once.

A ``disagrees`` fact publishes ``attest.<logbook>.crosscheck_disagrees`` with the log's labels.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import text

from . import registry, site_settings, store
from .gold_reader import GoldReader
from .logbooks import CrosscheckDecl, Field, LogbookError, convert_value

log = logging.getLogger("axiom.attest.crosscheck")

AGREES, DISAGREES, NOT_CHECKED = "agrees", "disagrees", "not_checked"
RESULTS = (AGREES, DISAGREES, NOT_CHECKED)


@dataclass(frozen=True)
class SourceRequest:
    """What a source is asked: one field (one instrument of a readings field) at one time."""

    site_id: str
    logbook: str
    field: str
    #: The instrument of a readings field, else None.
    instrument: str | None
    #: The unit the person's value was entered in ("" for a number).
    unit: str
    #: The record's ``occurred_at``: the time the value is asked for.
    at: datetime
    attestation_id: str


@dataclass(frozen=True)
class SourceValue:
    """A source's answer. ``value`` is a decimal string or None; ``state`` is ``ok``, ``stale``
    or ``faulted``; ``as_of`` is when the source's value was current; ``uncertainty`` is what the
    source states for it, carried and never combined."""

    value: str | None
    unit: str | None
    as_of: datetime | None = None
    state: str = "ok"
    uncertainty: dict[str, Any] | None = None


Source = Callable[[SourceRequest, GoldReader], SourceValue]

_sources: dict[str, Source] = {}


def register_source(name: str, source: Source) -> None:
    """Bind the source a ``[[crosscheck]] source = name`` names (``Inputs.crosscheck_source``)."""
    _sources[name] = source


def unregister_source(name: str) -> None:
    _sources.pop(name, None)


def bound(name: str) -> bool:
    """Whether an extension on this node binds ``name``."""
    if name not in _sources:
        registry.ensure()
    return name in _sources


# -- the comparison ------------------------------------------------------------------------------


def _dec(value: Any) -> Decimal | None:
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


def _tolerance(decl: CrosscheckDecl, site_id: str, instrument: str | None) -> str | None:
    got = site_settings.value(site_id, decl.tolerance_key, None) if decl.tolerance_key else None
    if isinstance(got, dict):
        got = got.get(instrument) if instrument else None
    if got is None:
        got = decl.tolerance_default
    if got is None:
        return None
    d = _dec(got)
    return str(got) if d is not None and d >= 0 else None


def _source_doc(v: SourceValue | None) -> dict[str, Any] | None:
    if v is None:
        return None
    return {"value": v.value, "unit": v.unit, "state": v.state,
            "as_of": v.as_of.isoformat() if v.as_of else None, "uncertainty": v.uncertainty}  # fmt: skip


def _entered_pair(entered: Any, want_unit: str, f: Field) -> tuple[str | None, str]:
    """The person's value in ``want_unit``: as entered, or its stored conversion; else None."""
    if isinstance(entered, dict):
        value, unit = entered.get("value"), str(entered.get("unit") or "")
        if unit == want_unit:
            return value, ""
        conv = entered.get("converted")
        if isinstance(conv, dict) and conv.get("unit") == want_unit:
            return conv.get("value"), ""
        # The field's declared exact conversion (#1801), the same one signing uses; never inferred.
        if value is not None and _dec(value) is not None and want_unit:
            made = convert_value(f, str(value), unit, want_unit)
            if made is not None:
                return made["value"], ""
        return None, unit
    return entered, ""


def compare(entered: Any, got: SourceValue | None, *, tolerance: str | None, at: datetime,
            stale_after: int, f: Field, needs_unit: bool, why: str = "") -> dict[str, Any]:  # fmt: skip
    """One comparison: ``{result, reason, entered, source_value, tolerance}``. ``why`` is a reason
    already known (unbound, the source failed)."""
    out = {"entered": entered, "source_value": _source_doc(got), "tolerance": tolerance}

    def no(reason: str) -> dict[str, Any]:
        return {**out, "result": NOT_CHECKED, "reason": reason}

    if why:
        return no(why)
    if isinstance(entered, dict) and entered.get("not_taken"):
        return no("not_taken")
    if got is None:
        return no("no_value")
    if got.state == "faulted":
        return no("faulted")
    if got.state != "ok":
        return no("stale")
    if got.value is None:
        return no("no_value")
    as_of = got.as_of if got.as_of is None or got.as_of.tzinfo else got.as_of.replace(tzinfo=UTC)
    if as_of is None or as_of < at - timedelta(seconds=stale_after):
        return no("stale")
    if needs_unit and not got.unit:
        return no("unitless")
    source = _dec(got.value)
    if source is None:
        return no("not_a_number")
    src_unit = str(got.unit or "")
    mine, entered_unit = _entered_pair(entered, src_unit, f)
    if mine is None:
        # The source's value converted into the entered unit, if the field declares it.
        conv = convert_value(f, str(source), src_unit, entered_unit) if src_unit else None
        if conv is None:
            return no("unit")
        source = Decimal(conv["value"])
        mine = entered.get("value") if isinstance(entered, dict) else entered
    value = _dec(mine)
    if value is None:
        return no("not_a_number")
    if tolerance is None:
        return no("no_tolerance")
    ok = abs(value - source) <= Decimal(tolerance)
    return {**out, "result": AGREES if ok else DISAGREES, "reason": ""}


def _ask(decl: CrosscheckDecl, req: SourceRequest) -> tuple[SourceValue | None, str]:
    fn = _sources.get(decl.source)
    if fn is None:
        registry.ensure()
        fn = _sources.get(decl.source)
    if fn is None:
        return None, "unbound"
    try:
        got = fn(req, GoldReader())
    except Exception:  # noqa: BLE001 - a source's failure is a fact, never the signer's problem
        log.warning("cross-check source %s failed for %s", decl.source, req.attestation_id, exc_info=True)
        return None, "source_failed"
    if not isinstance(got, SourceValue):
        return None, "source_failed"
    return got, ""


def _instrument_unit(f: Field, site_id: str, entered: Any, instrument: str) -> str:
    if isinstance(entered, dict) and entered.get("unit"):
        return str(entered["unit"])
    from . import field_sources

    try:
        for inst in field_sources.resolve(f.from_site, site_id):
            if inst.id == instrument:
                return inst.unit
    except LookupError:
        pass
    return ""


def _overall(details: list[dict[str, Any]]) -> tuple[str, str]:
    results = [d["result"] for d in details]
    if DISAGREES in results:
        return DISAGREES, ""
    for d in details:
        if d["result"] == NOT_CHECKED:
            return NOT_CHECKED, d["reason"]
    return (AGREES, "") if details else (NOT_CHECKED, "no_value")


def _parse_at(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    at = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return at if at.tzinfo else at.replace(tzinfo=UTC)


def _fact(record: dict[str, Any], decl: CrosscheckDecl, f: Field, entered: Any) -> dict[str, Any]:
    site, book = record["site_id"], record["logbook"]
    at = _parse_at(record.get("occurred_at") or record.get("signed_at"))

    def req(instrument: str | None, unit: str) -> SourceRequest:
        return SourceRequest(site_id=site, logbook=book, field=f.id, instrument=instrument, unit=unit,
                             at=at, attestation_id=record["attestation_id"])  # fmt: skip

    if f.type == "readings":
        details = []
        for inst_id, reading in (entered or {}).items():
            unit = _instrument_unit(f, site, reading, inst_id)
            got, why = (None, "") if isinstance(reading, dict) and reading.get("not_taken") else \
                _ask(decl, req(inst_id, unit))  # fmt: skip
            details.append({"instrument": inst_id, **compare(
                reading, got, tolerance=_tolerance(decl, site, inst_id), at=at,
                stale_after=decl.stale_after_seconds, f=f, needs_unit=True, why=why)})  # fmt: skip
        result, reason = _overall(details)
        return {"result": result, "reason": reason, "tolerance": None, "source_value": None,
                "detail": details}  # fmt: skip
    unit = str(entered.get("unit") or "") if isinstance(entered, dict) else ""
    got, why = _ask(decl, req(None, unit))
    one = compare(entered, got, tolerance=_tolerance(decl, site, None), at=at,
                  stale_after=decl.stale_after_seconds, f=f, needs_unit=f.type != "number", why=why)  # fmt: skip
    one.pop("entered", None)
    return {**one, "detail": None}


# -- the hook --------------------------------------------------------------------------------------


def _decls_for(record: dict[str, Any]) -> list[tuple[CrosscheckDecl, Field, Any]]:
    if record.get("kind") != "statement":
        return []
    try:
        book = registry.get(record["logbook"])
    except LogbookError:
        return []
    fields = ((record.get("content") or {}).get("fields")) or {}
    out = []
    for decl in book.crosschecks:
        if record.get("entry_type") in decl.types and decl.field in fields:
            out.append((decl, book.type(record["entry_type"]).field(decl.field), fields[decl.field]))
    return out


def check_record(record: dict[str, Any]) -> list[dict[str, Any]]:
    """Compare each cross-checked field of a signed statement; store and return the new facts.
    A field already checked is not checked again."""
    from axiom.infra.bus import get_default_eventbus

    todo = _decls_for(record)
    if not todo:
        return []
    labels = registry.get(record["logbook"]).labels
    now = datetime.now(UTC)
    written: list[dict[str, Any]] = []
    with store.session_scope() as s:
        for decl, f, entered in todo:
            fact = _fact(record, decl, f, entered)
            fact_id = s.execute(
                text(
                    "INSERT INTO attest_crosschecks (fact_id, site_id, logbook, attestation_id, seq, "
                    "field, result, reason, tolerance, source_value, detail, labels, occurred_at, "
                    "checked_at) VALUES (:id, :site, :log, :aid, :seq, :field, :result, :reason, :tol, "
                    "CAST(:src AS jsonb), CAST(:detail AS jsonb), CAST(:labels AS jsonb), :occ, :at) "
                    "ON CONFLICT ON CONSTRAINT attest_crosschecks_once DO NOTHING RETURNING fact_id"
                ),
                {"id": str(uuid.uuid4()), "site": record["site_id"], "log": record["logbook"],
                 "aid": record["attestation_id"], "seq": int(record["seq"]), "field": f.id,
                 "result": fact["result"], "reason": fact["reason"], "tol": fact["tolerance"],
                 "src": json.dumps(fact["source_value"]) if fact["source_value"] is not None else None,
                 "detail": json.dumps(fact["detail"]) if fact["detail"] is not None else None,
                 "labels": json.dumps(labels),
                 "occ": _parse_at(record.get("occurred_at") or record.get("signed_at")), "at": now},
            ).scalar()  # fmt: skip
            if fact_id is not None:
                written.append(_row_doc({
                    "fact_id": fact_id, "site_id": record["site_id"], "logbook": record["logbook"],
                    "attestation_id": record["attestation_id"], "seq": int(record["seq"]),
                    "field": f.id, "result": fact["result"], "reason": fact["reason"],
                    "tolerance": fact["tolerance"],
                    "source_value": fact["source_value"],
                    "detail": fact["detail"], "labels": labels,
                    "occurred_at": _parse_at(record.get("occurred_at") or record.get("signed_at")),
                    "checked_at": now,
                }))  # fmt: skip
        s.commit()
    bus = get_default_eventbus()
    for fact in written:
        if fact["result"] != DISAGREES:
            continue
        try:
            bus.publish(f"attest.{fact['logbook']}.crosscheck_disagrees",
                        {"schema_version": 1, **fact}, source="attest")  # fmt: skip
        except Exception:  # noqa: BLE001 - the fact is stored; delivery is best effort
            pass
    return written


def after_sign(record: dict[str, Any]) -> None:
    """Run by signing after the commit. Never raises: the signature is the truth, and the tick
    checks anything missed here (:func:`catch_up`)."""
    try:
        check_record(record)
    except Exception:  # noqa: BLE001
        log.warning("cross-check of %s #%s failed; the tick checks it", record.get("logbook"),
                    record.get("seq"), exc_info=True)  # fmt: skip


def catch_up(site_id: str, logbook: str, *, limit: int = 200) -> int:
    """Check signed statements of ``logbook`` at ``site_id`` with a declared field and no fact yet;
    the number of facts written."""
    from . import service

    try:
        book = registry.get(logbook)
    except LogbookError:
        return 0
    types = sorted({t for d in book.crosschecks for t in d.types})
    if not types:
        return 0
    fields = sorted({d.field for d in book.crosschecks})
    with store.session_scope() as s:
        ids = s.execute(
            text(
                "SELECT r.attestation_id FROM attest_records r WHERE r.site_id = :site AND "
                "r.logbook = :log AND r.kind = 'statement' AND r.entry_type = ANY(:types) AND "
                "EXISTS (SELECT 1 FROM unnest(CAST(:fields AS text[])) AS f(name) WHERE "
                "r.record->'content'->'fields' ? f.name AND NOT EXISTS (SELECT 1 FROM "
                "attest_crosschecks c WHERE c.attestation_id = r.attestation_id AND c.field = f.name)) "
                "ORDER BY r.seq LIMIT :limit"
            ),
            {"site": site_id, "log": logbook, "types": types, "fields": fields, "limit": limit},
        ).scalars().all()  # fmt: skip
    done = 0
    for aid in ids:
        rec = service.record(aid)
        if rec is not None:
            done += len(check_record(rec))
    return done


# -- reading the facts -----------------------------------------------------------------------------


def _row_doc(row: Any) -> dict[str, Any]:
    d = dict(row)
    for k in ("occurred_at", "checked_at"):
        if hasattr(d.get(k), "isoformat"):
            d[k] = d[k].isoformat()
    return d


def facts(site_id: str, logbook: str, *, result: str | None = None,
          attestation_id: str | None = None, limit: int = 500) -> list[dict[str, Any]]:  # fmt: skip
    """The cross-check facts of a log at a site, newest record first."""
    where = ["site_id = :site", "logbook = :log"]
    params: dict[str, Any] = {"site": site_id, "log": logbook, "limit": limit}
    if result:
        where.append("result = :result")
        params["result"] = result
    if attestation_id:
        where.append("attestation_id = :aid")
        params["aid"] = attestation_id
    with store.session_scope() as s:
        rows = s.execute(
            # nosemgrep: avoid-sqlalchemy-text -- the clauses are fixed strings above; values are bound
            text(
                "SELECT fact_id, site_id, logbook, attestation_id, seq, field, result, reason, "
                "tolerance, source_value, detail, labels, occurred_at, checked_at FROM "
                f"attest_crosschecks WHERE {' AND '.join(where)} ORDER BY seq DESC, field LIMIT :limit"
            ),
            params,
        ).mappings().all()  # fmt: skip
    return [_row_doc(r) for r in rows]


class FilterError(ValueError):
    """A ``result`` that is not one of :data:`RESULTS`."""


def view(site_id: str, logbook: str, *, result: str | None = None,
         attestation_id: str | None = None) -> dict[str, Any]:  # fmt: skip
    """What the crosschecks door serves: the log's labels, its cross-check facts and each
    predicate's latest state, worded (#1816). Raises :class:`LogbookError` for an unknown log."""
    from . import predicates

    book = registry.get(logbook)
    if result is not None and result not in RESULTS:
        raise FilterError(f"result is one of {', '.join(RESULTS)}, not {result!r}")
    states = predicates.current(site_id, logbook)
    if result == DISAGREES:
        states = [p for p in states if p["state"] == predicates.DISCREPANCY]
    return {"site_id": site_id, "logbook": logbook, "labels": book.labels,
            "crosschecks": facts(site_id, logbook, result=result, attestation_id=attestation_id),
            "predicates": states}  # fmt: skip


__all__ = [
    "FilterError", "view",
    "AGREES", "DISAGREES", "NOT_CHECKED", "RESULTS", "SourceRequest", "SourceValue", "after_sign",
    "bound", "catch_up", "check_record", "compare", "facts", "register_source", "unregister_source",
]  # fmt: skip
