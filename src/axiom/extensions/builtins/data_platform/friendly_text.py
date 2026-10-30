# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Terminal text for the everyday verbs, ending in one ``next:`` block.

Used only for ``--format text``. JSON, CSV and NDJSON are written from the
result itself and never see a ``next:`` line, so a pipe or a script reads
exactly what the verb returned.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from . import friendly as fr

TEXT_VERBS = frozenset({"roles", "channels", "latest", "show"})

#: Most channels shown side by side in a ``show`` table before the rest are only summarised.
_MAX_COLUMNS = 6


def _table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    cells = [[str(c) for c in headers]] + [["" if c is None else str(c) for c in r] for r in rows]
    widths = [max(len(r[i]) for r in cells) for i in range(len(headers))]
    lines = ["  ".join(c.ljust(widths[i]) for i, c in enumerate(r)).rstrip() for r in cells]
    return "\n".join(lines)


def _stamp(value: Any) -> str:
    dt = fr.parse_instant(value)
    return dt.strftime("%Y-%m-%d %H:%MZ") if dt else "-"


def _num(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int, float)):
        return f"{value:.6g}"
    return str(value)


def _roles(value: dict[str, Any], prog: str) -> str:
    roles = (value.get("data") or {}).get("roles") or []
    if not roles:
        return "no roles are declared on this node yet"
    body = _table(
        ["role", "sites", "units", "comparable"],
        [(r.get("role"), ", ".join(r.get("sites") or []), ", ".join(r.get("units") or []) or "-",
          "yes" if r.get("comparable") else "no") for r in roles],
    )
    return body + fr.next_block(fr.next_for_roles(roles, prog))


def _channels(value: dict[str, Any], prog: str) -> str:
    data = value.get("data") or {}
    rows = data.get("channels") or []
    site = data.get("site") or ""
    head = f"{len(rows)} channel{'' if len(rows) == 1 else 's'} at {site}"
    if data.get("role"):
        head += f" for role {data['role']}"
    if not rows:
        return head
    body = _table(
        ["feed", "channel", "unit", "role", "first reading", "last reading"],
        [(r.get("feed"), r.get("channel"), r.get("unit") or "-", r.get("role") or "-",
          _stamp(r.get("first_ts")), _stamp(r.get("last_ts"))) for r in rows],
    )
    return f"{head}\n\n{body}" + fr.next_block(fr.next_for_channels(rows, site, prog))


def _latest(value: dict[str, Any], prog: str) -> str:
    data = value.get("data") or {}
    rows = data.get("readings") or []
    site = data.get("site") or ""
    head = f"latest value of {len(rows)} channel{'' if len(rows) == 1 else 's'} at {site}"
    if not rows:
        return head
    body = _table(
        ["feed", "channel", "role", "value", "unit", "at"],
        [(r.get("feed"), r.get("channel"), r.get("role") or "-", _num(r.get("value")),
          r.get("unit") or "", _stamp(r.get("ts")) if r.get("ts") else f"not read (last {_stamp(r.get('last_ts'))})")
         for r in rows],
    )
    note = (value.get("provenance") or {}).get("note")
    return f"{head}\n\n{body}" + (f"\n\n{note}" if note else "") + fr.next_block(fr.next_for_latest(rows, site, prog))


def _show(value: dict[str, Any], prog: str) -> str:
    data = value.get("data") or {}
    points = data.get("series") or []
    units = data.get("units") or []
    win = data.get("window") or {}
    when = (f"{data['day']} (UTC)" if data.get("day")
            else f"{_stamp(win.get('start'))} to {_stamp(win.get('end'))}")
    head = (f"{data.get('target')} at {data.get('site')}, {when}, "
            f"{data.get('fn')} every {data.get('bucket')}" + (f", {', '.join(units)}" if units else ""))
    lines = [head]
    if data.get("defaulted_window"):
        lines.append("(no window given: showing the latest day with readings)")

    by_channel: dict[str, list[dict[str, Any]]] = {}
    for p in points:
        by_channel.setdefault(str(p.get("channel")), []).append(p)
    stats = []
    for name, pts in sorted(by_channel.items()):
        vals = [p["value"] for p in pts if isinstance(p.get("value"), (int, float))]
        stats.append((name, len(pts), _num(min(vals)) if vals else "-",
                      _num(sum(vals) / len(vals)) if vals else "-", _num(max(vals)) if vals else "-"))
    lines += ["", _table(["channel", "points", "min", "mean", "max"], stats)]

    shown = sorted(by_channel)[:_MAX_COLUMNS]
    stamps = sorted({p["t"] for name in shown for p in by_channel[name]})
    single_day = bool(data.get("day"))
    grid = {(str(p["channel"]), p["t"]): p["value"] for name in shown for p in by_channel[name]}
    table = _table(
        ["time (UTC)", *shown],
        [((fr.parse_instant(t).strftime("%H:%M") if single_day else fr.parse_instant(t).strftime("%Y-%m-%d %H:%M")),
          *[_num(grid.get((c, t))) for c in shown]) for t in stamps],
    )
    lines += ["", table]
    if len(by_channel) > _MAX_COLUMNS:
        lines.append(f"\n({len(by_channel) - _MAX_COLUMNS} more channel(s) summarised above; "
                     "use --format csv for every point)")
    return "\n".join(lines) + fr.next_block(fr.next_for_show(data, prog))


_RENDERERS = {"roles": _roles, "channels": _channels, "latest": _latest, "show": _show}


def render(verb: str, value: Any, prog: str | None = None) -> str:
    """The text answer for ``verb``, ending in a ``next:`` block of ready-to-run lines."""
    value = value if isinstance(value, dict) else {}
    return _RENDERERS[verb](value, prog or fr.cli_name())
