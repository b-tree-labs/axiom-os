# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The values a data answer served, each with its unit, source, as-of and quality.

A number in an answer is only as good as what travels with it: the unit it is
in, the gold object it came from, when it was read, and the verdict on it. The
data door's envelope already carries those facts; the answer's prose drops
them. This module lifts them out of the turn's own reads (never the model's
text) so a surface can show each value as a card.

Wire contract, one object per served value::

    {"label": str, "value": number|str|null, "unit": str, "source": str,
     "as_of": ISO-8601 str|null, "quality": one of company.QUALITY|null,
     "mark": one of marks.MARKS|null, "mark_reason": str|null,
     "ref": {"kind": "gold", "id": str, "title": str}|null}

Absence is said, never filled in: a value whose quality nobody reported has
``quality: null`` (never "good"), and one with no timestamp has ``as_of: null``.
A read whose source is not a gold object serves nothing here (DC-12).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

#: More cards than this is a table, not a set of values; the rest are in the read.
MAX_SERVED_VALUES = 24


def envelope_of(result: Any) -> Mapping[str, Any] | None:
    from axiom.extensions.builtins.charts.answer import _envelope as unwrap

    return unwrap(result)


def gold_source(env: Mapping[str, Any]) -> str | None:
    """The gold object an envelope's provenance names, or None when it names none.

    ``source`` may list several objects ("gold.catalogue, gold.signals") and may
    say where it ran ("gold.x on site-a"). The values are read from the last
    object listed (an index is named before the table it indexes). A source that
    is not gold is not served: it returns None.
    """
    prov = env.get("provenance")
    raw = prov.get("source") if isinstance(prov, Mapping) else None
    if not isinstance(raw, str) or not raw.strip():
        return None
    parts = [p.strip().split(" on ", 1)[0].strip() for p in raw.split(",")]
    gold = [p for p in parts if p.startswith("gold.")]
    return gold[-1] if gold else None


def _unit(env: Mapping[str, Any], row: Mapping[str, Any] | None = None) -> str:
    from axiom.extensions.builtins.charts.answer import _unit as unit_of

    return unit_of(env, row) or ""


def _value(v: Any) -> Any:
    if v is None or isinstance(v, (int, float, str)) and not isinstance(v, bool):
        return v
    return str(v)


def _card(
    *,
    label: str,
    value: Any,
    unit: str,
    source: str,
    as_of: Any,
    point: Mapping[str, Any],
    span: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    from axiom.extensions.builtins.data_platform import marks

    mark = span.get("mark") if span else marks.mark_for(point)
    reason = (span.get("reason") if span else None) or (
        (marks.reason_for(point) or marks.DEFAULT_REASON.get(mark)) if mark else None
    )
    return {
        "label": label,
        "value": _value(value),
        "unit": unit,
        "source": source,
        "as_of": str(as_of) if as_of not in (None, "") else None,
        "quality": marks.quality_of(point),
        "mark": mark,
        "mark_reason": reason if mark else None,
        "ref": {"kind": "gold", "id": source, "title": label},
    }


def _series_label(args: Mapping[str, Any], data: Mapping[str, Any], row: Mapping[str, Any]) -> str:
    for k in ("channel", "target"):
        if row.get(k):
            return str(row[k])
    from axiom.extensions.builtins.charts.answer import _name

    return _name(args, data)


def _from_points(
    label: str, points: Sequence[Mapping[str, Any]], env: Mapping[str, Any], source: str
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """A series serves its newest present value, and its marked spans."""
    from axiom.extensions.builtins.data_platform import marks

    pts = [p for p in points if isinstance(p, Mapping)]
    spans = marks.mark_spans(pts)
    present = [p for p in pts if p.get("value") is not None]
    last = present[-1] if present else (pts[-1] if pts else None)
    if last is None:
        return None, spans
    card = _card(
        label=label,
        value=last.get("value"),
        unit=_unit(env, last),
        source=source,
        as_of=last.get("t", last.get("ts")),
        point=last,
        span=spans[-1] if spans else None,
    )
    return card, spans


def extract(
    reads: Sequence[tuple[str, Any, Any]],
) -> list[tuple[dict[str, Any], list[dict[str, Any]]]]:
    """``(card, spans)`` for every value the turn's data reads served. Never raises per read."""
    out: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    for read in reads or ():
        try:
            tool, args, result = read
            out.extend(_one(str(tool), args if isinstance(args, Mapping) else {}, result))
        except Exception:  # noqa: BLE001 - one odd read never costs the others their cards
            continue
    return out[:MAX_SERVED_VALUES]


def _one(
    tool: str, args: Mapping[str, Any], result: Any
) -> list[tuple[dict[str, Any], list[dict[str, Any]]]]:
    env = envelope_of(result)
    if env is None:
        return []
    source = gold_source(env)
    if source is None:
        return []
    data = env.get("data")
    if isinstance(data, list):
        data = {"series": data}
    if not isinstance(data, Mapping):
        return []
    found: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    readings = data.get("readings")
    if isinstance(readings, list):
        for r in readings:
            if not isinstance(r, Mapping) or (r.get("ts") is None and r.get("value") is None):
                continue  # not read this time (unfetched), so nothing was served
            found.append(
                (
                    _card(
                        label=str(r.get("channel") or ""),
                        value=r.get("value"),
                        unit=_unit(env, r),
                        source=source,
                        as_of=r.get("ts"),
                        point=r,
                    ),
                    [],
                )
            )
        return found
    rows = data.get("series")
    if isinstance(rows, list) and rows:
        if all(isinstance(r, Mapping) and isinstance(r.get("points"), list) for r in rows):
            for r in rows:
                key = r.get("key") or {}
                label = (
                    " ".join(str(v) for v in key.values()) if isinstance(key, Mapping) else str(key)
                )
                card, spans = _from_points(
                    label or _series_label(args, data, {}), r["points"], env, source
                )
                if card is not None:
                    found.append((card, spans))
            return found
        by: dict[str, list[Mapping[str, Any]]] = {}
        for r in rows:
            if isinstance(r, Mapping):
                by.setdefault(_series_label(args, data, r), []).append(r)
        for label, pts in by.items():
            card, spans = _from_points(label, pts, env, source)
            if card is not None:
                found.append((card, spans))
        return found
    groups = data.get("groups")
    if isinstance(groups, list):
        cols = [str(c) for c in data.get("group_by") or []]
        for g in groups:
            if isinstance(g, Mapping):
                label = " ".join(str(g.get(c)) for c in cols) or _series_label(args, data, {})
                found.append(
                    (
                        _card(
                            label=label,
                            value=g.get("value"),
                            unit=_unit(env, g),
                            source=source,
                            as_of=g.get("as_of"),
                            point=g,
                        ),
                        [],
                    )
                )
        return found
    if "value" in data:
        window = data.get("window") if isinstance(data.get("window"), Mapping) else {}
        as_of = data.get("as_of") or data.get("ts") or (window or {}).get("end")
        found.append(
            (
                _card(
                    label=_series_label(args, data, {}),
                    value=data.get("value"),
                    unit=_unit(env),
                    source=source,
                    as_of=as_of,
                    point=data,
                ),
                [],
            )
        )
    return found


def served_values(reads: Sequence[tuple[str, Any, Any]]) -> list[dict[str, Any]]:
    """The wire list: one card per value the turn served."""
    return [card for card, _ in extract(reads)]


def marked_spans_sentence(reads: Sequence[tuple[str, Any, Any]]) -> str:
    """The marked spans the turn's values carry, named in words, or ``""``.

    An answer that quotes a number from a series with a held span must say the
    span exists, or the reader takes the line as continuous.
    """
    from axiom.extensions.builtins.data_platform import marks

    said: list[str] = []
    for card, spans in extract(reads):
        if spans:
            said.extend(marks.describe_span(s, label=card["label"]) for s in spans)
        elif card.get("mark"):
            when = card.get("as_of")
            span = {
                "mark": card["mark"],
                "start": when,
                "end": when,
                "reason": card.get("mark_reason"),
            }
            said.append(
                marks.describe_span(span, label=card["label"])
                if when
                else f"{card['label']} {card['mark']}"
                + (f" ({card['mark_reason']})" if card.get("mark_reason") else "")
            )
    seen: list[str] = []
    for s in said:
        if s not in seen:
            seen.append(s)
    if not seen:
        return ""
    return "Marked: " + "; ".join(seen[:6]) + ("; and more" if len(seen) > 6 else "") + "."


__all__ = [
    "MAX_SERVED_VALUES",
    "envelope_of",
    "extract",
    "gold_source",
    "marked_spans_sentence",
    "served_values",
]
