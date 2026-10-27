# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Promote a kit as data (ADR-203), and serve what was promoted.

``kit_promote`` runs on the contributor's machine. It packs the kit and, on a
bound install, sends it to the node through the same authenticated door a
bound read uses (``data.kit_submit`` over ``/data/query``). On the node itself
it submits directly.

``kit_submit``, ``kit_verbs``, ``kit_verb``, ``kit_charts`` and
``kit_automations`` are served: the site comes from
the verified credential (``site_authority``), never from the request, and the
database is the node's own (no caller-supplied DSN reaches ``resolve_dsn``).

``kit_review``, ``kit_approve`` and ``kit_retire`` are deliberately NOT served:
approving is an operator's act on the node (ADR-151's governing role).
"""

from __future__ import annotations

import base64
import os
from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from .._dsn import resolve_dsn


def _node_dsn() -> str:
    return resolve_dsn({})  # the node's own database; never a caller's


def _caller() -> tuple[str | None, bool]:
    """(the site this request's credential is held to, whether it may act for any site)."""
    from axiom.infra.site_authority import caller_site, tenant_scope

    scope = tenant_scope()
    return (scope or caller_site()), scope is None


def _operator() -> str:
    return os.environ.get("AXIOM_ACTOR") or f"@{os.environ.get('USER', 'operator')}:node"


def kit_promote(params: dict[str, Any], ctx: SkillContext) -> SkillResult:  # noqa: ARG001
    from ..kit import load
    from ..kit.bundle import pack
    from ..kit.project import KitError
    from ..read_target import ask, resolve_read_target

    try:
        b = pack(load(params.get("dir")))
    except KitError as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    target = resolve_read_target()
    if target.where == "binding":
        result = ask(
            target,
            verb="data.kit_submit",
            params={"bundle": base64.b64encode(b.data).decode("ascii")},
        )
        if result.ok and isinstance(result.value, dict):
            result.actions_taken.append(_next_step(result.value, target.site))
        return result
    from ..kit import host

    try:
        value = host.submit(_node_dsn(), b.data, caller_site=None, principal=_operator(), home=True)
    except KitError as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    return SkillResult(ok=value["state"] == "checked", value=value,
                       errors=list(value["problems"]), actions_taken=[_next_step(value, "this node")])


def _next_step(value: dict[str, Any], where: str) -> str:
    if value.get("state") == "checked":
        return (f"bundle {value['id']} for {value['site']} is checked on {where}; an operator approves "
                f"it there (kit-approve {value['id']}), then its verbs and charts are served")
    return f"bundle {value.get('id')} was rejected: fix the problems above and promote again"


def kit_submit(params: dict[str, Any], ctx: SkillContext) -> SkillResult:  # noqa: ARG001
    from ..kit import host
    from ..kit.project import KitError

    raw = params.get("bundle")
    if not raw:
        return SkillResult(ok=False, errors=["`bundle` (base64) is required"])
    try:
        data = base64.b64decode(str(raw), validate=True)
    except ValueError:
        return SkillResult(ok=False, errors=["`bundle` is not valid base64"])
    site, home = _caller()
    principal = str(params.get("_principal") or (f"@site:{site}" if site else _operator()))
    try:
        value = host.submit(_node_dsn(), data, caller_site=site, principal=principal, home=home)
    except KitError as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    return SkillResult(ok=value["state"] == "checked", value=value, errors=list(value["problems"]))


def kit_review(params: dict[str, Any], ctx: SkillContext) -> SkillResult:  # noqa: ARG001
    from ..kit import host

    rows = host.review(_node_dsn(), site=params.get("site") or None, state=params.get("state") or None)
    return SkillResult(ok=True, value={"bundles": rows})


def kit_approve(params: dict[str, Any], ctx: SkillContext) -> SkillResult:  # noqa: ARG001
    from ..kit import host
    from ..kit.project import KitError

    try:
        bundle_id = int(params.get("id"))
    except (TypeError, ValueError):
        return SkillResult(ok=False, errors=["`id` (the bundle number from kit-review) is required"])
    try:
        value = host.approve(_node_dsn(), bundle_id, approver=_operator())
    except KitError as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    return SkillResult(ok=True, value=value, actions_taken=[
        f"applied {len(value['views'])} gold view(s), {len(value['verbs'])} verb(s) and "
        f"{len(value['charts'])} chart(s) for {value['site']}"])


def kit_retire(params: dict[str, Any], ctx: SkillContext) -> SkillResult:  # noqa: ARG001
    from ..kit import host

    site = str(params.get("site") or "").strip()
    if not site:
        return SkillResult(ok=False, errors=["`site` is required"])
    return SkillResult(ok=True, value=host.retire(_node_dsn(), site, by=_operator()))


def kit_verbs(params: dict[str, Any], ctx: SkillContext) -> SkillResult:  # noqa: ARG001
    """The promoted verbs this caller may run: its own site's, or every site's for the home site."""
    from ..kit import host

    site, home = _caller()
    wanted = None if home else site
    if home and params.get("site"):
        wanted = str(params["site"])
    return SkillResult(ok=True, value={"verbs": host.list_verbs(_node_dsn(), site=wanted)})


def kit_charts(params: dict[str, Any], ctx: SkillContext) -> SkillResult:  # noqa: ARG001
    """The promoted chart specs this caller may see (ChartSpec 1.1 documents)."""
    from ..kit import host

    site, home = _caller()
    wanted = None if home else site
    if home and params.get("site"):
        wanted = str(params["site"])
    return SkillResult(ok=True, value={"charts": host.list_charts(_node_dsn(), site=wanted)})


def _render_automations(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "no promoted automations"
    lines = []
    for r in rows:
        last = (f"last run {r['last_run_at']}: {r['last_outcome']}"
                + (f" ({r['last_rows_matched']} matched)" if r.get("last_rows_matched") is not None else "")
                + (f" — {r['last_error']}" if r.get("last_error") else "")) if r.get("last_run_at") else "not run yet"
        lines.append(f"{r['site']}/{r['name']}  {r['schedule']}  {r['verb']} when {r['when']} → "
                     f"{', '.join(r['then'])}")
        lines.append(f"    {r['scheduled']}" + (f", next {r['next_run_at']}" if r.get("next_run_at") else "")
                     + f"; {last}")
    return "\n".join(lines)


def kit_automations(params: dict[str, Any], ctx: SkillContext) -> SkillResult:  # noqa: ARG001
    """The promoted automations this caller may see, with schedule, last run and outcome."""
    from ..kit import host

    site, home = _caller()
    wanted = None if home else site
    if home and params.get("site"):
        wanted = str(params["site"])
    rows = host.list_automations(_node_dsn(), site=wanted)
    return SkillResult(ok=True, value={"automations": rows, "text": _render_automations(rows)})


def kit_automation_fire(params: dict[str, Any], ctx: SkillContext) -> SkillResult:  # noqa: ARG001
    """One run of one promoted automation. Fired by the scheduler (PULSE), not served.

    The verb runs as the site's role; a failure comes back ``ok=False`` so the
    scheduler records it and dead-letters the fire to the operator.
    """
    from ..kit import automations

    site = str(params.get("site") or "").strip()
    name = str(params.get("automation") or "").strip()
    if not (site and name):
        return SkillResult(ok=False, errors=["`site` and `automation` are required"])
    try:
        value = automations.fire(_node_dsn(), site=site, name=name)
    except Exception as exc:  # noqa: BLE001 - the scheduler's record of a failed fire
        return SkillResult(ok=False, errors=[f"{site}/{name}: {str(exc).splitlines()[0] if str(exc) else exc!r}"])
    return SkillResult(ok=True, value=value, actions_taken=[f"{site}/{name}: {value['outcome']}"])


def kit_verb(params: dict[str, Any], ctx: SkillContext) -> SkillResult:  # noqa: ARG001
    """Run one promoted verb. A site's credential runs its own site's verbs only."""
    from ..kit import host
    from ..kit.project import KitError

    name = str(params.get("name") or "").strip()
    if not name:
        return SkillResult(ok=False, errors=["`name` is required (see kit-verbs)"])
    site, home = _caller()
    asked = str(params.get("site") or "").strip()
    if not home:
        if asked and asked != site:
            return SkillResult(ok=False, errors=[
                f"this credential reads site {site!r} only, not {asked!r}"])
        asked = site or ""
    if not asked:
        return SkillResult(ok=False, errors=["`site` is required (which site's verb to run)"])
    args = params.get("params") or {}
    if not isinstance(args, dict):
        return SkillResult(ok=False, errors=["`params` must be an object of the verb's parameters"])
    try:
        rows = host.run_registered_verb(_node_dsn(), site=asked, name=name, params=args)
    except KitError as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    return SkillResult(ok=True, value={"site": asked, "verb": name, "rows": rows,
                                       "served_by": "medallion"})


def kit_pull(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """Start from a frame that already exists: pull a site's gold series into a kit snapshot.

    On a bound install the read goes through the node's door (``data.series``), so it
    is held to what the caller's key may read; on the node it reads locally. What
    comes back is the series the serving budget allows (it may be coarser than
    asked, and says so); it is kept as a snapshot the kit can ``kit-materialize``.
    """
    from ..kit import load
    from ..kit.project import KitError
    from ..kit.snapshot import take_snapshot
    from ..read_target import ask, resolve_read_target

    try:
        project = load(params.get("dir"))
    except KitError as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    site = str(params.get("site") or project.tenant).strip()
    start, end = params.get("start"), params.get("end")
    if not (start and end):
        return SkillResult(ok=False, errors=["`start` and `end` are required (a pull is always a window)"])
    raw = params.get("channels") or ""
    channels = [c.strip() for c in (raw.split(",") if isinstance(raw, str) else raw) if str(c).strip()]
    series_params = {
        "table": "signals", "column": "value", "time_column": "ts",
        "bucket": str(params.get("bucket") or "1 minute"),
        "filter": "site = '" + site.replace("'", "''") + "'",
        "group_by": "channel,unit",
        "window": {"column": "ts", "start": str(start), "end": str(end)},
    }
    target = resolve_read_target()
    if target.where == "binding":
        result = ask(target, verb="data.series", params=series_params)
    else:
        from . import gold

        result = gold.series(series_params, ctx)
    if not result.ok:
        return result
    value = result.value or {}
    rows: list[dict[str, Any]] = []
    for line in (value.get("data") or {}).get("series") or []:
        key = line.get("key") or {}
        channel, unit = key.get("channel"), key.get("unit")
        if channels and channel not in channels:
            continue
        for point in line.get("points") or []:
            if point.get("value") is None:
                continue
            rows.append({"site": site, "channel": channel, "ts": str(point.get("t")),
                         "value": point.get("value"), "unit": unit})
    if not rows:
        return SkillResult(ok=False, errors=[f"no readings for {site} in that window"
                                             + (f" on {channels}" if channels else "")])
    try:
        kept = take_snapshot(project, site=site, channels=channels or sorted({r["channel"] for r in rows}),
                             rows=rows, window={"start": str(start), "end": str(end)},
                             snapshot_id=str(params.get("id") or ""))
    except KitError as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    served = value.get("served") or {}
    note = []
    if served.get("reduced"):
        note.append(f"the node served {served.get('returned_points')} points at "
                    f"{served.get('resolution_seconds')} s resolution (coarser than asked) to stay "
                    f"within its serving budget; narrow the window for finer data")
    summary = {k: v for k, v in kept.items() if k != "rows"}
    per_channel: dict[str, int] = {}
    for row in kept.get("rows") or []:
        per_channel[str(row.get("channel"))] = per_channel.get(str(row.get("channel")), 0) + 1
    summary["rows"] = sum(per_channel.values())
    summary["rows_per_channel"] = per_channel
    ident = summary.get("id")
    # The rows are in the snapshot file; printing them all buried the next step.
    return SkillResult(ok=True, value={**summary, "served": served, "from": target.where},
                       actions_taken=[*note, f"next: kit-materialize {ident} loads it into your sandbox",
                                      f"then: kit-scaffold starter my_view --from-snapshot {ident} "
                                      "writes a gold view, a verb and a chart over these channels"])


__all__ = ["kit_automation_fire", "kit_automations", "kit_charts", "kit_pull", "kit_approve", "kit_promote", "kit_retire", "kit_review", "kit_submit", "kit_verb", "kit_verbs"]
