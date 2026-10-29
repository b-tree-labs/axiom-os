# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The everyday questions: ``channels``, ``latest`` and ``show`` (ADR-056 skills).

Each is a composition of verbs the serving door already answers
(``aggregate`` and ``series`` on the gold tables, ``roles``). That is the
design constraint, not an accident: bound, these ride ``read_target.ask`` one
served verb at a time, so they work against a node that has never heard of
them, and on a node they run against the local medallion through the same
code. Nothing here opens a database.

What they take off the asker:

- the table, the value column and the time column are known (``signals``,
  ``value``, ``ts``);
- the site defaults to the binding's site, or the only site there is;
- a channel is resolved to its feed through ``signal_catalogue``, so a channel
  filter always carries the feed that makes it an index lookup (a channel
  filter without its feed scans every feed and times out);
- a window that is not given is the latest day that has readings for what was
  asked about, never "now";
- the bucket follows the window's length unless told.

The failures name what the catalogue really holds: an unknown site, role or
channel answers with the nearest real one, and an empty window answers with the
last reading and the flag that reaches it.
"""

from __future__ import annotations

from datetime import UTC, timedelta
from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from .. import friendly as fr
from . import gold

SIGNALS = "signals"
CATALOGUE = "signal_catalogue"

#: How far back (days, before the site's latest reading) a role is looked for,
#: narrowest first. A role's rows are found through its (role, ts) index, which
#: is only cheap when the window is bounded.
_LADDER_DAYS = (1, 7, 30, 120, 400)

#: At most this many served reads to fetch the latest value of many channels.
_MAX_LATEST_QUERIES = 6


#: Rows within this many seconds of a feed's newest reading are read together.
_LATEST_SPAN_S = 3


class _Refused(Exception):
    """An answer to give the caller as the error, in words."""


def _lit(text: str) -> str:
    """A quoted literal for the filter grammar. A quote inside it is never a real name."""
    if "'" in str(text):
        raise _Refused(f"{text!r} cannot be a name here (it holds a quote)")
    return f"'{text}'"


def _call(fn, params: dict[str, Any], ctx: SkillContext) -> dict[str, Any]:
    res = fn(params, ctx)
    if not res.ok:
        raise _Refused("; ".join(res.errors) or "the read failed")
    return res.value if isinstance(res.value, dict) else {}


def _groups(value: dict[str, Any]) -> list[dict[str, Any]]:
    data = value.get("data")
    if isinstance(data, dict):
        return list(data.get("groups") or [])
    return []


def _agg(ctx, *, table, column, fn, filter, group_by, window=None) -> list[dict[str, Any]]:
    params: dict[str, Any] = {
        "table": table, "column": column, "fn": fn,
        "filter": filter, "group_by": group_by,
    }
    if window:
        params["window"] = window
    return _groups(_call(gold.aggregate, params, ctx))


# ---------------------------------------------------------------------------
# the catalogue
# ---------------------------------------------------------------------------


def _catalogue(ctx, site: str | None = None) -> list[dict[str, Any]]:
    """Every catalogued channel (optionally one site's) with its first and last reading."""
    keys = "site,feed,channel,unit"
    flt = f"site = {_lit(site)}" if site else None
    try:
        last = _agg(ctx, table=CATALOGUE, column="last_ts", fn="max", filter=flt, group_by=keys)
        first = _agg(ctx, table=CATALOGUE, column="first_ts", fn="min", filter=flt, group_by=keys)
    except _Refused as exc:
        msg = str(exc)
        if "signal_catalogue" in msg or "no column" in msg.lower():
            raise _Refused(
                "this node has no signal catalogue yet, so channels cannot be listed; "
                "it is written by the conform pass"
            ) from None
        raise
    firsts = {(g.get("site"), g.get("feed"), g.get("channel"), g.get("unit")): g.get("value") for g in first}
    rows = []
    for g in last:
        k = (g.get("site"), g.get("feed"), g.get("channel"), g.get("unit"))
        lt, ft = fr.parse_instant(g.get("value")), fr.parse_instant(firsts.get(k))
        rows.append({
            "site": g.get("site"), "feed": g.get("feed"), "channel": g.get("channel"),
            "unit": g.get("unit"), "role": None,
            "first_dt": ft, "last_dt": lt,
            "first_ts": fr.iso(ft) if ft else None, "last_ts": fr.iso(lt) if lt else None,
        })
    rows.sort(key=lambda r: (str(r["feed"]), str(r["channel"])))
    return rows


def _resolve_site(params: dict[str, Any], ctx) -> tuple[str, list[dict[str, Any]]]:
    """``(site, its catalogue rows)``: the one named, the binding's, or the only one."""
    named = str(params.get("site") or "").strip()
    if named:
        rows = _catalogue(ctx, named)
        if rows:
            return named, rows
        sites = sorted({r["site"] for r in _catalogue(ctx) if r["site"]})
        raise _Refused(fr.unknown_message(
            "site", named, "", sites,
            listing=f"sites here: {', '.join(sites)}" if sites else "no site has any catalogued channel yet",
        ))
    everything = _catalogue(ctx)
    sites = sorted({r["site"] for r in everything if r["site"]})
    from ..read_target import current_binding

    bound = current_binding()
    if bound and bound[0] in sites:
        return bound[0], [r for r in everything if r["site"] == bound[0]]
    if len(sites) == 1:
        return sites[0], everything
    if not sites:
        raise _Refused("no site has any catalogued channel yet; nothing has been conformed")
    raise _Refused(f"which site? pass --site; sites here: {', '.join(sites)}")


def _site_roles(ctx, site: str) -> list[str]:
    """Every role declared at ``site``. A full read of the role index: slow, so it is only
    asked for to answer "did you mean", never to decide whether a name is a role."""
    value = _call(gold.roles, {}, ctx)
    entries = (value.get("data") or {}).get("roles") or []
    return sorted(e["role"] for e in entries if site in (e.get("sites") or []) and e.get("role"))


def _unknown_role(ctx, site: str, role: str) -> _Refused:
    roles = _site_roles(ctx, site)
    return _Refused(fr.unknown_message(
        "role", role, site, roles,
        listing=f"roles here: {', '.join(roles)}" if roles else "this site declares no roles"))


def _anchor(rows: list[dict[str, Any]]):
    stamps = [r["last_dt"] for r in rows if r["last_dt"]]
    return max(stamps) if stamps else None


def _window(start, end) -> dict[str, Any]:
    return {"column": "ts", "start": fr.iso(start), "end": fr.iso(end)}


def _role_channels(ctx, site: str, role: str, anchor) -> list[dict[str, Any]]:
    """The channels answering ``role`` at ``site`` and when each last did.

    Looked for in widening windows that end at the site's latest reading, so
    the read is always bounded, and stops at the first window that finds any.
    """
    if anchor is None:
        return []
    end = anchor + timedelta(seconds=1)
    flt = f"site = {_lit(site)} and role = {_lit(role)}"
    for days in _LADDER_DAYS:
        found = _agg(
            ctx, table=SIGNALS, column="ts", fn="max", filter=flt,
            group_by="feed,channel,unit", window=_window(end - timedelta(days=days), end),
        )
        if found:
            return [{
                "feed": g.get("feed"), "channel": g.get("channel"), "unit": g.get("unit"),
                "last_dt": fr.parse_instant(g.get("value")),
            } for g in found]
    return []


def _sweep(ctx, site: str, rows: list[dict[str, Any]], *, span_s: int, column: str, fn: str,
           group_by: str, max_queries: int):
    """Yield ``(cluster, groups)`` reading each channel at its own last timestamp.

    Channels of one feed usually stop together, so one served read covers every
    channel whose last reading is within ``span_s`` of the newest one still
    pending in that feed; a channel that stopped long before its feed gets a read
    of its own. Each read names the feed and a short window, which is what keeps
    it an index lookup. At most ``max_queries`` reads are made.
    """
    pending: dict[Any, list[dict[str, Any]]] = {}
    for r in rows:
        if r["last_dt"]:
            pending.setdefault(r["feed"], []).append(r)
    queries = 0
    while pending and queries < max_queries:
        feed = next(iter(pending))
        group = sorted(pending[feed], key=lambda t: t["last_dt"], reverse=True)
        newest = group[0]["last_dt"]
        cluster = [t for t in group if (newest - t["last_dt"]).total_seconds() <= span_s]
        rest = [t for t in group if t not in cluster]
        if rest:
            pending[feed] = rest
        else:
            del pending[feed]
        queries += 1
        names = ", ".join(_lit(t["channel"]) for t in cluster)
        yield cluster, _agg(
            ctx, table=SIGNALS, column=column, fn=fn,
            filter=f"site = {_lit(site)} and feed = {_lit(feed)} and channel in ({names})",
            group_by=group_by,
            window=_window(newest - timedelta(seconds=span_s), newest + timedelta(seconds=1)),
        )


#: How far back (days, before the site's latest reading) a channel's role is read from.
_ROLE_LOOKBACK_DAYS = (7, 120)


def _attach_roles(ctx, site: str, rows: list[dict[str, Any]]) -> str | None:
    """Fill ``role`` on each row, best effort. Returns a note when roles could not be read.

    A role is carried by some of a channel's rows and not others, so it is read
    from a bounded window of the site's rows that have one (``role != ''`` is
    "has a role"; the grammar has no ``is not null``), not from a channel's last
    reading. The look-back widens once if the first window finds none. A read
    that fails leaves the roles blank and says so; it never fails the listing.
    """
    anchor = _anchor(rows)
    if anchor is None:
        return None
    end = anchor + timedelta(seconds=1)
    found: dict[tuple[Any, Any], str] = {}
    try:
        for days in _ROLE_LOOKBACK_DAYS:
            for g in _agg(ctx, table=SIGNALS, column="ts", fn="max",
                          filter=f"site = {_lit(site)} and role != ''",
                          group_by="feed,channel,role,unit",  # unit: the population guard refuses a blend

                          window=_window(end - timedelta(days=days), end)):
                found.setdefault((g.get("feed"), g.get("channel")), g.get("role"))
            if found:
                break
    except _Refused as exc:
        return f"roles not read ({exc})"
    for r in rows:
        r["role"] = found.get((r["feed"], r["channel"]))
    return None


def _public(row: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in row.items() if k not in ("first_dt", "last_dt")}


def _provenance(site: str, method: str, **extra: Any) -> dict[str, Any]:
    return {"source": f"gold.{CATALOGUE}, gold.{SIGNALS}", "method": method, "site": site, **extra}


# ---------------------------------------------------------------------------
# channels
# ---------------------------------------------------------------------------


def _only_role(ctx, site: str, role: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The rows answering ``role``. An empty answer is "no such role" until the role list says otherwise."""
    members = {(c["feed"], c["channel"]) for c in _role_channels(ctx, site, role, _anchor(rows))}
    if not members:
        raise _unknown_role(ctx, site, role)
    kept = [r for r in rows if (r["feed"], r["channel"]) in members]
    for r in kept:
        r["role"] = role
    return kept


def channels(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """Each channel at a site with its feed, unit, role, and first and last reading."""
    try:
        site, rows = _resolve_site(params, ctx)
        role = str(params.get("role") or "").strip()
        note = None
        if role:
            rows = _only_role(ctx, site, role, rows)
        else:
            note = _attach_roles(ctx, site, rows)
    except _Refused as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    return SkillResult(ok=True, value={
        "data": {"site": site, "role": role or None, "channels": [_public(r) for r in rows]},
        "provenance": _provenance(site, "channels from the signal catalogue; roles from the role index",
                                  rows=len(rows), **({"note": note} if note else {})),
    })


# ---------------------------------------------------------------------------
# latest
# ---------------------------------------------------------------------------


def _fetch_latest(ctx, site: str, rows: list[dict[str, Any]]) -> None:
    """Fill ``ts`` and ``value`` (and ``role``) on each row: the reading at its channel's last timestamp."""
    for cluster, groups in _sweep(ctx, site, rows, span_s=_LATEST_SPAN_S, column="value", fn="max",
                                  group_by="channel,unit,ts,role", max_queries=_MAX_LATEST_QUERIES):
        best: dict[Any, tuple[Any, Any, Any]] = {}
        for g in groups:
            ts = fr.parse_instant(g.get("ts"))
            name = g.get("channel")
            if ts is not None and (name not in best or ts > best[name][0]):
                best[name] = (ts, g.get("value"), g.get("role"))
        for r in cluster:
            if r["channel"] in best:
                r["ts_dt"], r["value"], found_role = best[r["channel"]]
                r["role"] = r["role"] or found_role


def latest(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """The most recent value of each channel at a site, optionally one role or one channel."""
    try:
        site, rows = _resolve_site(params, ctx)
        role = str(params.get("role") or "").strip()
        channel = str(params.get("channel") or "").strip()
        if role and channel:
            raise _Refused("pass --role or --channel, not both")
        if role:
            rows = _only_role(ctx, site, role, rows)
        elif channel:
            names = sorted({r["channel"] for r in rows if r["channel"]})
            rows = [r for r in rows if r["channel"] == channel]
            if not rows:
                raise _Refused(fr.unknown_message(
                    "channel", channel, site, names,
                    listing=f"see: {fr.cli_name()} data channels --site {site}"))
        note = None
        if not role:
            note = _attach_roles(ctx, site, rows)
        _fetch_latest(ctx, site, rows)
    except _Refused as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    out = []
    for r in rows:
        ts = r.get("ts_dt")
        out.append({
            "feed": r["feed"], "channel": r["channel"], "unit": r["unit"], "role": r["role"],
            "ts": fr.iso(ts) if ts else None, "value": r.get("value"), "last_ts": r["last_ts"],
        })
    unfetched = sum(1 for o in out if o["ts"] is None)
    return SkillResult(ok=True, value={
        "data": {"site": site, "role": role or None, "channel": channel or None, "readings": out,
                 "unfetched": unfetched},
        "provenance": _provenance(
            site, "reading at each channel's last timestamp in the signal catalogue", rows=len(out),
            **({"note": "; ".join(n for n in (
                f"{unfetched} channel(s) not read; narrow with --role or --channel" if unfetched else "",
                note or "") if n)} if (unfetched or note) else {}),
        ),
    })


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------


def _flatten(series_value: dict[str, Any], fallback: dict[str, Any]) -> list[dict[str, Any]]:
    data = series_value.get("data")
    if not isinstance(data, dict):
        return []
    rows: list[dict[str, Any]] = []
    for item in data.get("series") or []:
        if isinstance(item, dict) and "points" in item:
            key = item.get("key") or {}
            for p in item["points"]:
                rows.append({"t": p.get("t"), "value": p.get("value"), **key})
        elif isinstance(item, dict):
            rows.append({"t": item.get("t"), "value": item.get("value"), **fallback})
    for r in rows:
        if r.get("t") is not None:
            dt = fr.parse_instant(r["t"])
            r["t"] = fr.iso(dt) if dt else str(r["t"])
    return rows


def _which(ctx, site: str, rows: list[dict[str, Any]], target: str, anchor):
    """``(kind, target, role members)``: a channel if it is one here, else a role if it has readings."""
    names = {r["channel"] for r in rows if r["channel"]}
    if target in names:
        return "channel", target, []
    lowered = {n.lower(): n for n in names}
    members = _role_channels(ctx, site, target, anchor)
    if members:
        return "role", target, members
    if target.lower() in lowered:
        return "channel", lowered[target.lower()], []
    roles = _site_roles(ctx, site)
    for r in roles:
        if r.lower() == target.lower():
            found = _role_channels(ctx, site, r, anchor)
            if found:
                return "role", r, found
    raise _Refused(fr.unknown_message(
        "role or channel", target, site, roles + sorted(names),
        listing=f"see: {fr.cli_name()} data roles; {fr.cli_name()} data channels --site {site}"))


def show(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """A series for a role or a channel, with the site, window and bucket worked out."""
    target = str(params.get("target") or "").strip()
    if not target:
        return SkillResult(ok=False, errors=[
            f"show what? name a role or a channel; see them with: {fr.cli_name()} data roles"])
    try:
        site, rows = _resolve_site(params, ctx)
        feed_arg = str(params.get("feed") or "").strip()
        anchor = _anchor(rows)
        kind, target, members = _which(ctx, site, rows, target, anchor)

        if kind == "role":
            last_dt = max((m["last_dt"] for m in members if m["last_dt"]), default=None)
            feeds = sorted({m["feed"] for m in members if m["feed"]})
            names = sorted({m["channel"] for m in members})
            units = sorted({m["unit"] for m in members if m["unit"]})
            filt = f"site = {_lit(site)} and role = {_lit(target)}"
            if feeds:
                filt += (f" and feed = {_lit(feeds[0])}" if len(feeds) == 1
                         else f" and feed in ({', '.join(_lit(f) for f in feeds)})")
            group_by = "feed,channel,unit"
            fallback: dict[str, Any] = {}
        else:
            hits = [r for r in rows if r["channel"] == target and (not feed_arg or r["feed"] == feed_arg)]
            if not hits:
                feeds = sorted({r["feed"] for r in rows if r["channel"] == target})
                raise _Refused(f"channel {target!r} is not in feed {feed_arg!r} at {site}; "
                               f"its feed is {', '.join(feeds)}")
            if len({h["feed"] for h in hits}) > 1:
                raise _Refused(
                    f"channel {target!r} exists in more than one feed at {site} "
                    f"({', '.join(sorted({h['feed'] for h in hits}))}); pass --feed")
            hit = hits[0]
            last_dt = hit["last_dt"]
            feeds, names, units = [hit["feed"]], [target], [hit["unit"]] if hit["unit"] else []
            filt = f"site = {_lit(site)} and feed = {_lit(hit['feed'])} and channel = {_lit(target)}"
            group_by = None
            fallback = {"feed": hit["feed"], "channel": target, "unit": hit["unit"]}

        win = fr.resolve_window(
            start=params.get("start"), end=params.get("end"), day=params.get("day"),
            last=params.get("last"), yesterday=bool(params.get("yesterday")),
        )
        defaulted = win is None
        if win is None:
            if last_dt is None:
                raise _Refused(f"{target!r} at {site} has no readings to show")
            win = fr.resolve_window(day=last_dt.astimezone(UTC).date().isoformat())
        every = str(params.get("every") or "").strip()
        bucket = fr.interval_words(every) if every else fr.choose_bucket(
            fr.window_seconds(win.start, win.end) or 3600)

        series_params: dict[str, Any] = {
            "table": SIGNALS, "column": "value", "time_column": "ts", "bucket": bucket,
            "fn": str(params.get("fn") or "mean"), "filter": filt,
            "window": {"column": "ts", "start": win.start, "end": win.end},
        }
        if group_by:
            series_params["group_by"] = group_by
        for passthrough in ("allow_mixed", "allow_mixed_units", "include_synthetic"):
            if params.get(passthrough):
                series_params[passthrough] = True
        value = _call(gold.series, series_params, ctx)
        points = _flatten(value, fallback)
        if not points:
            last_day = last_dt.astimezone(UTC).date().isoformat() if last_dt else None
            between = f"between {win.start or '...'} and {win.end or '...'}"
            tail = (f"; its last reading was {last_day}; try --day {last_day}" if last_day
                    else "; it has no readings on record")
            raise _Refused(f"no readings at {site} for {kind} {target} {between}{tail}")
    except (_Refused, fr.WindowError) as exc:
        return SkillResult(ok=False, errors=[str(exc)])

    day = ""
    s, e = fr.parse_instant(win.start), fr.parse_instant(win.end)
    if s and e and e - s == timedelta(days=1) and s.hour == s.minute == 0:
        day = s.date().isoformat()
    out: dict[str, Any] = {
        "data": {
            "site": site, "kind": kind, "target": target, "channels": names, "feeds": feeds,
            "window": {"start": win.start, "end": win.end}, "day": day,
            "defaulted_window": defaulted, "bucket": bucket, "fn": series_params["fn"],
            "units": units, "series": points,
        },
        "provenance": {**(value.get("provenance") or {}), "site": site},
    }
    if value.get("served"):
        out["served"] = value["served"]
    if len(units) == 1:
        out["unit"] = {"value": units[0]}
    return SkillResult(ok=True, value=out)


__all__ = ["channels", "latest", "show"]
