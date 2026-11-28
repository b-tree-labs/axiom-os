# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The record filters a surface may ask for, parsed once for the HTTP route, the skill and the
exports (unit W3): entry types, meanings, the signer, a time window, one interval and a text;
and the log's own declared field filters (ADR-268 D3), spelled ``field.<id>`` (equals, repeat for
any of), ``field.<id>.contains``, ``field.<id>.gte`` / ``.lte`` with ``field.<id>.unit`` (a number or a
quantity) or as ISO 8601 instants (a ``datetime``, phase 2), or as a
``where`` list of ``{field, op, value, unit?}``. Both spellings parse to the same predicates.

A filter narrows what the caller may already read; it never widens the site. A malformed value
is a :class:`ValueError` whose message names the field, so a route can answer 400 and a skill
can fail with the same words.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from axiom.attest import query

__all__ = ["FieldPredicate", "RecordFilters", "has_field_filters", "parse_field_filters", "parse_filters"]


@dataclass(frozen=True)
class FieldPredicate:
    """One declared filter as asked: ``eq`` (any of ``values``), ``contains`` (every word),
    ``gte`` / ``lte`` (one exact decimal, already in the field's declared ``unit``; or, for a
    ``datetime`` field, one instant in ISO 8601 with its offset)."""

    field: str
    op: str
    values: tuple[str, ...]
    unit: str = ""
    #: The entry types the filter covers.
    types: tuple[str, ...] = ()
    #: The declared field's type: a ``datetime`` bound compares instants, not decimals.
    field_type: str = ""


def _list(value: Any) -> tuple[str, ...] | None:
    if value is None or value == "":
        return None
    if isinstance(value, str):
        items = [v.strip() for v in value.split(",")]
    else:
        items = [str(v).strip() for v in value]
    items = [v for v in items if v]
    return tuple(items) if items else None


@dataclass(frozen=True)
class RecordFilters:
    entry_types: tuple[str, ...] | None = None
    meanings: tuple[str, ...] | None = None
    signer: str | None = None
    since: datetime | None = None
    until: datetime | None = None
    interval: tuple[str, str] | None = None
    text: str | None = None
    #: The log's declared field filters (ADR-268), ANDed with each other and everything above.
    fields: tuple[FieldPredicate, ...] | None = None

    def apply(
        self,
        logbook: str,
        site: str,
        *,
        from_seq: int = 1,
        before_seq: int | None = None,
        after_seq: int | None = None,
        limit: int | None = None,
        order: str = "seq",
        before_key: Any = None,
        after_key: Any = None,
    ) -> list[dict[str, Any]]:
        """The records of ``logbook`` at ``site`` these filters keep, in chain order, read by the
        database from its indexes; with ``limit`` one keyset page (newest, or before or after a
        sequence)."""
        if self.fields:
            # A person who has just signed finds the entry with the filter they are using: the
            # chain's index is brought to its head first (ADR-268 D4).
            from . import field_index

            field_index.catch_up(site, logbook)
        return query.records(
            logbook,
            site=site,
            from_seq=from_seq,
            before_seq=before_seq,
            after_seq=after_seq,
            limit=limit,
            entry_types=self.entry_types,
            meanings=self.meanings,
            signer=self.signer,
            since=self.since,
            until=self.until,
            interval=self.interval,
            text=self.text,
            order=order,
            before_key=before_key,
            after_key=after_key,
            fields=self.fields,
        )

    def any(self) -> bool:
        return any(v is not None for v in vars(self).values())


# Each surface's spelling of a filter, so one parser serves them all.
_TYPES = ("entry_type", "entry_types", "type", "types")
_MEANINGS = ("meaning", "meanings")
_SINCE = ("from", "since")
_UNTIL = ("to", "until")
_TEXT = ("q", "text")


def _first(raw: Mapping[str, Any], names: tuple[str, ...]) -> tuple[str | None, Any]:
    for name in names:
        value = raw.get(name)
        if value is not None and value != "" and value != []:
            return name, value
    return None, None


def has_field_filters(raw: Mapping[str, Any]) -> bool:
    return any(str(k).startswith("field.") for k in raw) or raw.get("where") not in (None, "", [])


_OPS = {"": "eq", "contains": "contains", "gte": "gte", "lte": "lte", "unit": "unit"}
_WHERE_OPS = ("eq", "contains", "gte", "lte")
_TRUE = {"true", "yes", "1", "on"}
_FALSE = {"false", "no", "0", "off"}


def _values_of(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, (list, tuple)):
        return [_scalar(v) for v in value if v not in (None, "")]
    return [_scalar(value)]


def _scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value).strip()


def _spelled(raw: Mapping[str, Any]) -> dict[str, dict[str, list[str]]]:
    """``{field: {op: [values]}}`` from ``field.<id>[.<op>]`` keys and a ``where`` list."""
    asked: dict[str, dict[str, list[str]]] = {}
    for key, value in raw.items():
        key = str(key)
        if not key.startswith("field."):
            continue
        name, _, op = key[len("field."):].partition(".")
        if "@" in name:
            raise ValueError(f"{key}: a filter on one instrument's readings is not available yet")
        if not name or op not in _OPS:
            raise ValueError(f"{key}: a field filter is field.<id>, field.<id>.contains, "
                             "field.<id>.gte, field.<id>.lte or field.<id>.unit")  # fmt: skip
        asked.setdefault(name, {}).setdefault(_OPS[op], []).extend(_values_of(value))
    where = raw.get("where")
    if isinstance(where, str) and where.strip():
        try:
            where = json.loads(where)
        except ValueError:
            raise ValueError("where is a list of {field, op, value, unit?}") from None
    if where not in (None, "", []):
        if isinstance(where, Mapping):
            where = [where]
        if not isinstance(where, (list, tuple)):
            raise ValueError("where is a list of {field, op, value, unit?}")
        for w in where:
            if not isinstance(w, Mapping) or not w.get("field") or w.get("op") not in _WHERE_OPS:
                raise ValueError("each where is {field, op, value, unit?} with op one of eq, contains, "
                                 "gte, lte")  # fmt: skip
            got = asked.setdefault(str(w["field"]), {})
            got.setdefault(str(w["op"]), []).extend(_values_of(w.get("value")))
            if w.get("unit"):
                got.setdefault("unit", []).append(_scalar(w["unit"]))
    return asked


def _decimal(value: str, key: str) -> Decimal:
    try:
        d = Decimal(value)
    except InvalidOperation:
        d = None
    if d is None or not d.is_finite():
        raise ValueError(f"{key} must be a number, e.g. 4.5, not {value!r}")
    return d


def parse_field_filters(raw: Mapping[str, Any], logbook: Any) -> tuple[FieldPredicate, ...] | None:
    """The declared field filters a caller asked for, or None. Every key names a filter the log
    declares and is in force; anything else is refused with the words that list them (ADR-268
    D3): a filter that silently does nothing returns too many records, which reads as an answer."""
    asked = _spelled(raw)
    if not asked:
        return None
    if logbook is None:
        raise ValueError("field filters need a declared log")
    if isinstance(logbook, str):
        from . import registry

        logbook = registry.get(registry.resolve_id(logbook) or logbook)
    decls = {f.field: f for f in logbook.field_filters}
    out: list[FieldPredicate] = []
    for name, ops in asked.items():
        decl = decls.get(name)
        if decl is None:
            listed = ", ".join(decls)
            raise ValueError(f"`{name}` is not a filter of this log; "
                             + (f"its filters are {listed}" if listed else "it declares no filters"))
        key = f"field.{name}"
        units = ops.pop("unit", [])
        ops = {op: v for op, v in ops.items() if v}
        if not ops:
            continue
        allowed = {"choice": ("eq",), "values": ("eq",), "checkbox": ("eq",), "words": ("contains",),
                   "range": ("gte", "lte")}[decl.control]  # fmt: skip
        wrong = sorted(set(ops) - set(allowed))
        if wrong:
            n = "<time>" if decl.field_type == "datetime" else "<n>"
            how = {"eq": f"{key}=<value>", "contains": f"{key}.contains=<words>",
                   "gte": f"{key}.gte={n} or {key}.lte={n}", "lte": f"{key}.gte={n} or {key}.lte={n}"}
            raise ValueError(f"{key}: {decl.label} is not filtered by {', '.join(wrong)}; use "
                             f"{how[allowed[0]]}")  # fmt: skip
        if decl.control == "checkbox":
            vals = []
            for v in ops["eq"]:
                word = v.lower()
                if word not in _TRUE | _FALSE:
                    raise ValueError(f"{key} is true or false (yes or no), not {v!r}")
                vals.append("true" if word in _TRUE else "false")
            out.append(FieldPredicate(name, "eq", tuple(dict.fromkeys(vals)), types=decl.types))
        elif decl.control in ("choice", "values"):
            out.append(FieldPredicate(name, "eq", tuple(dict.fromkeys(ops["eq"])), types=decl.types))
        elif decl.control == "words":
            out.append(FieldPredicate(name, "contains", (" ".join(ops["contains"]),), types=decl.types))
        elif decl.field_type == "datetime":
            # An inclusive range of instants (ADR-268 D2, phase 2); a naive bound is UTC, as the
            # records door's ``from`` and ``to`` are.
            if units:
                raise ValueError(f"{key}.unit: {decl.label} is a date and time and takes no unit")
            for op in ("gte", "lte"):
                for v in ops.get(op, []):
                    at = query.parse_time(v, f"{key}.{op}")
                    out.append(FieldPredicate(name, op, (at.isoformat(),), types=decl.types,
                                              field_type="datetime"))  # fmt: skip
        else:
            unit = units[-1] if units else decl.unit
            factor = Decimal(1)
            if unit != decl.unit:
                conv = [f for frm, to, f in decl.convert if frm == unit and to == decl.unit]
                if not conv:
                    accepted = " or ".join(decl.units) if decl.units else "no unit"
                    raise ValueError(f"{key}.unit: {unit!r} is not a unit {decl.label} accepts; "
                                     f"give it in {accepted}")  # fmt: skip
                factor = Decimal(conv[0])
            for op in ("gte", "lte"):
                for v in ops.get(op, []):
                    bound = _decimal(v, f"{key}.{op}") * factor
                    out.append(FieldPredicate(name, op, (str(bound),), unit=decl.unit, types=decl.types,
                                              field_type=decl.field_type))  # fmt: skip
    return tuple(out) or None


def parse_filters(raw: Mapping[str, Any], logbook: Any = None) -> RecordFilters:
    """Read the filters a caller spelled (``entry_type`` or ``type``, ``from`` or ``since``,
    ``q`` or ``text``, ``field.<id>`` or ``where``, ...). Unknown keys are ignored: they are the
    caller's other parameters. ``logbook`` (a :class:`Logbook` or its id) is needed for field
    filters, which are checked against its declaration."""
    _, types = _first(raw, _TYPES)
    _, meanings = _first(raw, _MEANINGS)
    since_name, since = _first(raw, _SINCE)
    until_name, until = _first(raw, _UNTIL)
    _, interval = _first(raw, ("interval",))
    _, text = _first(raw, _TEXT)
    signer = raw.get("signer")
    return RecordFilters(
        entry_types=_list(types),
        meanings=_list(meanings),
        signer=str(signer).strip() if signer else None,
        since=query.parse_time(since, since_name or "from") if since else None,
        until=query.parse_time(until, until_name or "to") if until else None,
        interval=query.parse_interval(interval) if interval else None,
        text=str(text).strip() if text else None,
        fields=parse_field_filters(raw, logbook),
    )
