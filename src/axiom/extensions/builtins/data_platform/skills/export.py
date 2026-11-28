# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``data.export`` and ``data.export_verify``: a window of gold as a file, with its provenance.

``spec-data-table.md`` ("Saved views, export and code") asks for the whole filtered and
sorted view as a file with a provenance sidecar. This is that export for the terminal, an
agent and chat. It adds no second way to read gold: it asks ``data.table`` for one window
after another by cursor and writes what comes back (:mod:`..export_file`). So every rule the
table door applies holds for an export with no code here to keep in step: gold only, the
caller's site and access tiers, the self-test hold-out, the cost and row budgets and the
per-caller rate limit. On an install bound to a node each window is read through the node's
door, which means a node needs no new release for a bound reader to export from it.

What that costs: a window is at most 1,000 rows and a caller is rate limited, so a large
export takes as many calls as it has thousands of rows. The server-side stream the spec
describes (and the job that lands a large export in the Library) is the faster path and is
not built here.

An export is all of what was asked for, or it says it is not. Reaching ``rows`` before the
view ends writes the file with ``complete: false`` and the reason. A window that fails part
way writes nothing: a file that stops at an arbitrary row and looks whole is the worst
outcome an export can have.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from axiom.infra.paths import get_user_state_dir
from axiom.infra.skills import SkillContext, SkillResult

from .. import export_file as ef
from ..table_door import READINGS

#: Rows asked for per window: the table door's own ceiling.
PAGE_ROWS = 1000
#: Rows written when the caller names no limit.
ROWS_DEFAULT = 100_000
#: The most rows one export will write, whatever is asked.
ROWS_MAX = 5_000_000
#: How many times one window is asked again after a rate-limit refusal, and the first wait.
RATE_RETRIES = 30
RATE_WAIT_S = 0.25
#: The time column of the readings (gold.signals), used when ``start`` or ``end`` is given without one.
TIME_COLUMN_DEFAULT = "ts"


class _Stopped(Exception):
    """A window could not be read."""

    def __init__(self, result: SkillResult):
        super().__init__("; ".join(result.errors or ["the window could not be read"]))
        self.result = result


def _list(value: Any) -> list[Any]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    return list(value)


def _request(params: dict[str, Any]) -> dict[str, Any]:
    """The table request an export pages through, from the short flags a person types."""
    time_column = str(params.get("time_column") or TIME_COLUMN_DEFAULT)
    where: list[Any] = list(_list(params.get("where")) if not isinstance(params.get("where"), list)
                            else params["where"])
    # Mappings, not text: a channel name may hold a comma or a space.
    channels = _list(params.get("channels"))
    if channels:
        where.append({"column": "channel", "op": "in", "value": channels})
    if params.get("feed"):
        where.append({"column": "feed", "op": "eq", "value": str(params["feed"])})
    if params.get("start"):
        where.append({"column": time_column, "op": "ge", "value": str(params["start"])})
    if params.get("end"):
        where.append({"column": time_column, "op": "lt", "value": str(params["end"])})
    request: dict[str, Any] = {"dataset": str(params.get("dataset") or READINGS)}
    if where:
        request["where"] = where
    if _list(params.get("sort")):
        request["sort"] = _list(params["sort"])
    elif request["dataset"] == READINGS:
        # Readings in the order they were taken, which is also the order gold.signals is indexed in.
        request["sort"] = [time_column]
    if params.get("search"):
        request["search"] = str(params["search"])
    if params.get("include_synthetic"):
        request["include_synthetic"] = True
    return request


def _default_out(params: dict[str, Any], ctx: SkillContext | None, fmt: str, dataset: str) -> Path:
    """Where the file goes when nobody says: here at a terminal, the exports folder elsewhere."""
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    name = f"{dataset}-{stamp}{ef.FORMATS[fmt]}"
    if getattr(ctx, "surface", None) in (None, "cli"):
        return Path.cwd() / name
    state = getattr(ctx, "state_dir", None) if ctx else None
    root = Path(state).expanduser() if state else get_user_state_dir()
    return root / "exports" / name


def _windows(request: dict[str, Any], ctx: SkillContext, table, first: dict[str, Any],
             sleep=time.sleep) -> Iterator[list[dict[str, Any]]]:
    """Each window's rows, in order. ``first`` is filled from the first window's answer."""
    cursor = None
    while True:
        page = {**request, "limit": PAGE_ROWS}
        if cursor:
            page["cursor"] = cursor
        wait = RATE_WAIT_S
        for _ in range(RATE_RETRIES + 1):
            result = table(dict(page), ctx)
            if result.ok or not any("rate limit" in str(e) for e in result.errors or ()):
                break
            sleep(wait)  # the door's own limit: wait, then ask for the same window again
            wait = min(wait * 2, 4.0)
        if not result.ok:
            raise _Stopped(result)
        value = result.value or {}
        if not first:
            first.update(
                spec=value.get("spec") or {},
                provenance=value.get("provenance") or {},
                applied=value.get("applied") or {},
                count=value.get("count") or {},
            )
        # A window cut short by a budget still pages on by its cursor; anything else the door
        # bounded (an estimated position, a count that gave up) is worth keeping.
        first.setdefault("notes", [])
        yield list(value.get("rows") or [])
        cursor = value.get("next")
        if not cursor:
            return


SOURCES_SUFFIX = ".uncertainty-sources"
#: Keys asked for per read of a companion, and the most rows whose sources are looked up.
KEY_BATCH = 200
KEYED_ROWS_MAX = 200_000


def _qualifier(site: str, state: dict[str, Any]):
    """A row of sources with its symbol qualified by the site it came from (ADR-136, the wire)."""

    def qualified(row: dict[str, Any]) -> dict[str, Any]:
        from axiom.uncertainty import wire

        origin = site or str(row.get("site") or row.get("site_id") or "")
        symbol = row.get("symbol")
        if not origin or not symbol:
            return row
        try:
            marked = wire.qualify(str(symbol), origin=origin if origin.startswith("@") else f"@{origin}")
        except ValueError:
            return row
        state["origin"] = True
        return {**row, "symbol": marked}

    return qualified


def _sources_by_keys(params, ctx, table, sleep, plan: dict[str, Any], seen: set[tuple], site: str,
                     out: Path, fmt: str) -> dict[str, Any]:
    """The companion's rows for the view's rows, found by the keys the two share."""
    keys, companion = tuple(plan["by_keys"]), plan["companion"]
    lead = "row_hash" if "row_hash" in keys else keys[0]
    at = keys.index(lead)
    leads = sorted({k[at] for k in seen if k[at] is not None}, key=str)
    state = {"origin": False}
    qualified = _qualifier(site, state)
    columns: list[str] = []

    def rows() -> Iterator[dict[str, Any]]:
        for i in range(0, len(leads), KEY_BATCH):
            ask = {"dataset": companion,
                   "where": [{"column": lead, "op": "in", "value": leads[i:i + KEY_BATCH]}]}
            first: dict[str, Any] = {}
            for window in _windows(ask, ctx, table, first, sleep):
                if not columns:
                    columns.extend(c.get("id") for c in (first.get("spec") or {}).get("columns", []))
                for row in window:
                    if tuple(row.get(k) for k in keys) in seen:
                        yield qualified(row)

    sources_out = out.with_name(out.stem + SOURCES_SUFFIX + ef.FORMATS[fmt])
    stream = rows()
    try:
        head = next(stream, None)
        if head is None:
            return {}  # no row of the view has declared sources
        done = ef.write(
            (r for part in ([head], stream) for r in part), sources_out,
            columns=columns or list(head), fmt=fmt, site=site, dataset=companion,
            request={"dataset": companion, "joined_on": list(keys), "for": out.name},
            exported_by=str(getattr(getattr(ctx, "principal", None), "handle", "") or ""),
            surface=str(getattr(ctx, "surface", "") or ""),
            note=f"the sources of the uncertainties in {out.name}",
        )
    except (_Stopped, ef.ExportError) as exc:
        sources_out.unlink(missing_ok=True)
        return {"skipped": str(exc)}
    return {"file": sources_out.name, "rows": done.rows, "sha256": done.sha256, "complete": True,
            "origin": "site" if state["origin"] else None,
            "paths": [str(done.data_path), str(done.sidecar_path)]}


def _predicate_columns(where: list[Any]) -> set[str]:
    return {str(w["column"]) if isinstance(w, dict) else str(w).split(None, 1)[0] for w in where}


def _export_sources(params, ctx, table, sleep, request, out: Path, fmt: str) -> dict[str, Any] | None:
    """Write the sources of the view's uncertainties beside it, when gold holds them.

    A value's sources live in the table's companion (``<table>_uncertainty``): one row per
    source, with its symbol and coefficient. The companion is asked through the same door
    with the same filter. Its symbols are qualified by the site on the way out, because a
    file crosses a boundary and two sites mint the same symbol for different instruments.
    """
    companion = f"{request['dataset']}_uncertainty"
    probe = table({"dataset": companion, "limit": 1}, ctx)
    if not probe.ok:
        why = "; ".join(probe.errors or [])
        # No companion is the common case and is no news. Any other refusal is said.
        return None if "no table " in why or "no such" in why else {"skipped": why}
    if not (probe.value or {}).get("rows"):
        return None  # a companion with nothing in it: the view's values have magnitudes at most
    carried = {c.get("id") for c in ((probe.value or {}).get("spec") or {}).get("columns", [])}
    if "symbol" not in carried:
        return None
    where = list(request.get("where") or [])
    if not _predicate_columns(where) <= carried or request.get("search"):
        # The companion is keyed by the row it describes and carries no time or feed of its
        # own, so the view's filter cannot be put to it. Its rows are found by those keys,
        # once the view's rows have been read.
        from axiom.uncertainty.serving import companion_join_keys

        keys = companion_join_keys(carried)
        if not keys:
            return {"skipped": f"{companion} has no key that joins it to {request['dataset']}"}
        return {"by_keys": keys, "companion": companion}
    site = str(params.get("site") or "")
    state = {"origin": False}
    qualified = _qualifier(site, state)
    sources_out = out.with_name(out.stem + SOURCES_SUFFIX + ef.FORMATS[fmt])
    done = _export(
        {"dataset": companion, "where": where, "format": fmt, "out": str(sources_out),
         "rows": ROWS_MAX, "overwrite": params.get("overwrite"),
         "include_synthetic": params.get("include_synthetic"), "site": site,
         "note": f"the sources of the uncertainties in {out.name}"},
        ctx, table, sleep, row_map=qualified,
    )
    if not done.ok:
        return {"skipped": "; ".join(done.errors)}
    return {"file": sources_out.name, "rows": done.value["rows"], "sha256": done.value["sha256"],
            "complete": done.value["complete"], "origin": "site" if state["origin"] else None,
            "paths": [done.value["path"], done.value["provenance_path"]]}


def export(params: dict[str, Any], ctx: SkillContext, *, table=None, sleep=time.sleep) -> SkillResult:
    """Write the rows of a gold view to a file, with a provenance sidecar beside it."""
    if table is None:
        from . import gold as _gold

        table = _gold.table
    return _export(params, ctx, table, sleep, with_sources=params.get("sources") is not False)


def _export(params: dict[str, Any], ctx: SkillContext, table, sleep, *, row_map=None,
            with_sources: bool = False) -> SkillResult:
    fmt = str(params.get("format") or "csv").lower()
    if fmt not in ef.FORMATS:
        return SkillResult(ok=False, errors=[
            f"unknown format {fmt!r}: one of {', '.join(sorted(ef.FORMATS))}"])
    try:
        limit = int(params.get("rows") or ROWS_DEFAULT)
    except (TypeError, ValueError):
        return SkillResult(ok=False, errors=["`rows` is a whole number of rows"])
    if not 1 <= limit <= ROWS_MAX:
        return SkillResult(ok=False, errors=[
            f"`rows` is between 1 and {ROWS_MAX:,}. A larger export wants a narrower window, "
            "or several exports by time"])

    request = _request(params)
    out = Path(str(params["out"])).expanduser() if params.get("out") else _default_out(
        params, ctx, fmt, request["dataset"])
    if out.suffix != ef.FORMATS[fmt]:
        out = out.with_name(out.name + ef.FORMATS[fmt])
    if (out.exists() or ef.sidecar_path(out).exists()) and not params.get("overwrite"):
        return SkillResult(ok=False, errors=[
            f"{out} is already there. Name another file, or pass overwrite: an export is "
            "evidence, and replacing one without saying so loses what a figure was drawn from"])

    wanted = [str(c) for c in _list(params.get("columns"))]
    first: dict[str, Any] = {}
    state = {"rows": 0, "more": False}

    def rows() -> Iterator[dict[str, Any]]:
        for window in _windows(request, ctx, table, first, sleep):
            for row in window:
                if state["rows"] >= limit:
                    state["more"] = True
                    return
                state["rows"] += 1
                yield row

    # The first window is read before the file is opened: it names the columns, and a
    # refusal (no such table, a sort the database cannot do) is the caller's answer.
    stream = rows()
    try:
        head = next(stream, None)
    except _Stopped as stop:
        return SkillResult(ok=False, errors=list(stop.result.errors or [str(stop)]),
                           unmet=getattr(stop.result, "unmet", None) or [])
    if head is None:
        return SkillResult(ok=False, errors=[
            "no rows matched, so nothing was written. Check the channel names with "
            "`data channels` and the window with `data latest`"])

    offered = [c.get("id") for c in (first.get("spec") or {}).get("columns", []) if c.get("id")]
    offered = offered or list(head)
    unknown = [c for c in wanted if c not in offered]
    if unknown:
        return SkillResult(ok=False, errors=[
            f"no column {', '.join(unknown)} in {request['dataset']}: it has {', '.join(offered)}"])
    columns = wanted or offered
    time_column = str(params.get("time_column") or TIME_COLUMN_DEFAULT)
    types = {c.get("id"): c.get("type") for c in (first.get("spec") or {}).get("columns", [])}
    if time_column not in columns:
        time_column = next((c for c in columns if types.get(c) == "time"), "")

    # A value does not leave without the column that says how well it is known.
    from axiom.uncertainty.serving import uncertainty_column_for

    carries: dict[str, str] = {}
    for column in list(columns):
        found = uncertainty_column_for(column, set(offered))
        if found:
            carries[column] = found
            if found not in columns:
                columns.append(found)

    sources = _export_sources(params, ctx, table, sleep, request, out, fmt) if with_sources and carries else None
    by_keys = sources if sources and sources.get("by_keys") else None
    if by_keys and not set(by_keys["by_keys"]) <= set(offered):
        sources, by_keys = {"skipped": "the view does not carry the keys its sources are filed under"}, None
    seen: set[tuple] = set()
    site_of = str(params.get("site") or head.get("site") or head.get("site_id") or "")
    late: dict[str, Any] = {}

    def keep(row: dict[str, Any]) -> dict[str, Any]:
        if by_keys and len(seen) <= KEYED_ROWS_MAX:
            seen.add(tuple(row.get(k) for k in by_keys["by_keys"]))
        return row_map(row) if row_map else row

    def all_rows() -> Iterator[dict[str, Any]]:
        yield keep(head)
        for row in stream:
            yield keep(row)

    def finish() -> dict[str, Any]:
        said: dict[str, Any] = {
            "complete": not state["more"],
            "bounded": ([f"stopped at the row limit of {limit:,}: more rows match. Ask for "
                         "more with `rows`, or narrow the window"] if state["more"] else []),
        }
        if by_keys:
            if len(seen) > KEYED_ROWS_MAX:
                late.update(skipped=f"sources are looked up for at most {KEYED_ROWS_MAX:,} rows; "
                                    "narrow the window to have them")
            else:
                late.update(_sources_by_keys(params, ctx, table, sleep, by_keys, seen, site_of, out, fmt))
            if late.get("file"):
                said["uncertainty_sources"] = late
        return said

    principal = getattr(ctx, "principal", None)
    gold = first.get("provenance") or {}
    try:
        done = ef.write(
            all_rows(), out, columns=columns, fmt=fmt,
            site=str(params.get("site") or head.get("site") or head.get("site_id") or ""),
            dataset=request["dataset"],
            request={**request, "rows": limit, **({"columns": wanted} if wanted else {})},
            time_column=time_column or None,
            exported_by=str(getattr(principal, "handle", "") or ""),
            surface=str(getattr(ctx, "surface", "") or ""),
            note=str(params.get("note") or ""),
            uncertainty_columns=carries,
            uncertainty_sources=None if (by_keys or (sources or {}).get("skipped")) else sources,
            # What the platform said about the rows it served. A storage tier is the
            # platform's own business: a file that leaves it names the table and nothing more.
            extra={"served": {k: (str(gold[k]).removeprefix("gold.") if k == "source" else gold[k])
                              for k in ("source", "reads", "as_of", "watermark")
                              if gold.get(k) is not None},
                   "order": (first.get("applied") or {}).get("order")},
            finish=finish,
        )
    except _Stopped as stop:
        out.unlink(missing_ok=True)
        for left in (sources or {}).get("paths", ()):
            Path(left).unlink(missing_ok=True)
        return SkillResult(ok=False, errors=[
            f"the export stopped after {state['rows']:,} rows and nothing was kept: {stop}. "
            "A narrower window needs fewer reads"],
            unmet=getattr(stop.result, "unmet", None) or [])
    except ef.ExportError as exc:
        return SkillResult(ok=False, errors=[str(exc)])

    if by_keys:
        sources = late or None
    skipped = (sources or {}).get("skipped")
    said = done.provenance
    lines = [
        f"wrote {done.data_path}   ({done.rows:,} rows, {fmt})",
        f"      {done.sidecar_path}",
        f"  from   {said['dataset']}" + (f" at {said['site']}" if said["site"] else ""),
    ]
    if said.get("first_ts"):
        lines.append(f"  window {said['first_ts']} to {said['last_ts']}")
    if said.get("source_class"):
        lines.append(f"  rows   {', '.join(said['source_class'])}")
    if not said["complete"]:
        lines.append("  NOT COMPLETE: " + "; ".join(said["bounded"]))
    known = said["uncertainty"]
    lines.append("  uncertainty " + {
        ef.SOURCES_KNOWN: f"sources in {known.get('sources_file')} ({known.get('sources_rows', 0):,} rows)",
        ef.MAGNITUDE_ONLY: "a magnitude per row; its sources are not in the file",
        ef.NONE_REPORTED: "none reported (that is not zero)",
    }[known["kind"]])
    if skipped:
        lines.append(f"  sources not written: {skipped}")
    lines.append(f"  sha256 {done.sha256}")
    return SkillResult(
        ok=True,
        value={
            "path": str(done.data_path), "provenance_path": str(done.sidecar_path),
            "rows": done.rows, "format": fmt, "sha256": done.sha256,
            "complete": said["complete"], "bounded": said["bounded"],
            "columns": columns, "units": said["units"],
            "first_ts": said.get("first_ts"), "last_ts": said.get("last_ts"),
            "source_class": said.get("source_class"),
            "uncertainty": known,
            **({"sources_path": sources["paths"][0]} if sources and sources.get("paths") else {}),
            "text": "\n".join(lines),
        },
        actions_taken=[f"wrote {done.rows} rows of {said['dataset']} to {done.data_path}"],
    )


def export_verify(params: dict[str, Any], ctx: SkillContext | None = None) -> SkillResult:
    """Whether an exported file still holds the rows its provenance describes."""
    path = params.get("path") or params.get("file")
    if not path:
        return SkillResult(ok=False, errors=["`path` is the exported file to check"])
    target = Path(str(path)).expanduser()
    if not target.is_file():
        return SkillResult(ok=False, errors=[f"{target} is not a file"])
    try:
        ok, said = ef.verify(target)
        provenance = ef.read_provenance(target)
    except ef.ExportError as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    value = {"same": ok, "text": said, "provenance": provenance}
    if not ok:
        return SkillResult(ok=False, value=value, errors=[said])
    return SkillResult(ok=True, value=value)
