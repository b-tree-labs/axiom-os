# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""From the data an answer read to the chart it carries: at most one per answer.

The chat loop hands every tool result of a turn to :func:`collect`; at the end
of the turn :func:`block_for_turn` merges the data reads into one result and
asks :func:`.block.build` for its chart block. Nothing here asks a model which
chart: the block is the deterministic ranking, and the person switches kinds
in the browser without another question.

Kept quiet on purpose (ADR-216): no block when the turn read nothing that
draws honestly (a single bare number, a refusal, an empty window), and never
more than one block per answer.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

#: The served data-door reads whose results can be charted. Tool names are the
#: verbs with ``.`` spelled ``__`` (the tool-name convention); both are read.
DATA_READS = frozenset({"data.series", "data.show", "data.aggregate", "data.compare", "data.latest"})


def is_data_read(tool_name: str) -> bool:
    return tool_name.replace("__", ".") in DATA_READS


def _envelope(result: Any) -> Mapping[str, Any] | None:
    """The data-door envelope inside a tool result, unwrapped."""
    cur = result
    for _ in range(3):
        if not isinstance(cur, Mapping):
            return None
        if cur.get("error") or cur.get("ok") is False:
            return None
        if isinstance(cur.get("data"), (Mapping, list)):
            return cur
        cur = cur.get("value")
    return None


def _unit(env: Mapping[str, Any], row: Mapping[str, Any] | None = None) -> str:
    if row and row.get("unit"):
        return str(row["unit"])
    u = env.get("unit")
    if isinstance(u, Mapping):
        return str(u.get("value") or "")
    data = env.get("data") or {}
    units = data.get("units") if isinstance(data, Mapping) else None
    if isinstance(units, Sequence) and units and not isinstance(units, str):
        return str(units[0] or "")
    return str(u or "")


def _name(args: Mapping[str, Any], data: Mapping[str, Any]) -> str:
    for k in ("target", "channel", "column"):
        v = args.get(k)
        if isinstance(v, str) and v:
            return v
    chans = args.get("channels")
    if isinstance(chans, Sequence) and len(chans) == 1 and not isinstance(chans, str):
        return str(chans[0])
    return str(data.get("column") or "value")


def read_to_result(tool_name: str, args: Mapping[str, Any], result: Any) -> dict[str, Any] | None:
    """One data read as something ``suggest`` reads, or None when it holds no drawable data."""
    env = _envelope(result)
    if env is None:
        return None
    data = env["data"]
    if isinstance(data, list):
        data = {"series": data}
    rows = data.get("series")
    if isinstance(rows, list) and rows:
        out: list[dict[str, Any]] = []
        if all(isinstance(r, Mapping) and isinstance(r.get("points"), list) for r in rows):
            for r in rows:
                key = r.get("key") or {}
                label = " ".join(str(v) for v in key.values()) if isinstance(key, Mapping) else str(key)
                pts = [p for p in r["points"] if isinstance(p, Mapping)]
                out.append({"name": label or _name(args, data), "unit": _unit(env, pts[0] if pts else None),
                            "x": [p.get("t") for p in pts], "y": [p.get("value") for p in pts]})
        else:
            by: dict[str, dict[str, Any]] = {}
            for r in rows:
                if not isinstance(r, Mapping) or "value" not in r:
                    continue
                label = str(r.get("channel") or r.get("target") or _name(args, data))
                s = by.setdefault(label, {"name": label, "unit": _unit(env, r), "x": [], "y": []})
                s["x"].append(r.get("t") or r.get("ts") or r.get("time"))
                s["y"].append(r.get("value"))
            out = list(by.values())
        out = [s for s in out if any(isinstance(v, (int, float)) for v in s["y"])]
        return {"series": out} if out else None
    groups = data.get("groups")
    if isinstance(groups, list) and groups:
        by_cols = [str(g) for g in data.get("group_by") or []]
        table = []
        for g in groups:
            if not isinstance(g, Mapping):
                continue
            row = {c: g.get(c) for c in by_cols}
            row[str(data.get("fn") or "value")] = g.get("value")
            table.append(row)
        unit = _unit(env)
        return {"rows": table, "units": {str(data.get("fn") or "value"): unit} if unit else {}}
    # A hierarchy: a nested {name, children}, bare or under ``tree``.
    tree = data.get("tree") if isinstance(data.get("tree"), Mapping) else data
    if isinstance(tree.get("children"), list) and tree["children"]:
        return dict(tree)
    if isinstance(data.get("value"), (int, float)):
        return {"value": data["value"], "unit": _unit(env), "label": _name(args, data)}
    return None


def merge(results: Sequence[Mapping[str, Any]]) -> Any:
    """The turn's reads as one result: series sets join; otherwise the last read stands."""
    series = [s for r in results for s in (r.get("series") or [])]
    if series:
        seen: dict[str, int] = {}
        for s in series:
            seen[s["name"]] = seen.get(s["name"], 0) + 1
        return {"series": series}
    return results[-1] if results else None


def collect(reads: list[tuple[str, dict[str, Any], Any]], tool_name: str, args: Any, result: Any) -> None:
    """Keep one tool result for the turn's chart, if it is a data read."""
    if is_data_read(tool_name) and isinstance(result, Mapping):
        reads.append((tool_name, dict(args or {}), result))


def block_for_turn(question: str, reads: Sequence[tuple[str, Mapping[str, Any], Any]], *,
                   width: int = 720) -> dict[str, Any] | None:
    """The one chart block this answer carries, or None."""
    from axiom.extensions.builtins.scidisplay.suggest import SuggestError

    from . import block

    found = [r for r in (read_to_result(n, a, res) for n, a, res in reads) if r]
    if not found:
        return None
    merged = merge(found)
    verbs = sorted({n.replace("__", ".") for n, _a, _r in reads})
    sites = sorted({str(a.get("site")) for _n, a, _r in reads if a.get("site")})
    source = {"verbs": verbs, **({"sites": sites} if sites else {})}
    try:
        return block.build(merged, question, source=source, width=width)
    except SuggestError:
        return None


__all__ = ["DATA_READS", "block_for_turn", "collect", "is_data_read", "merge", "read_to_result"]
