# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Promote a kit as data (ADR-203), and serve what was promoted.

``kit_promote`` runs on the contributor's machine. It packs the kit and, on a
bound install, sends it to the node through the same authenticated door a
bound read uses (``data.kit_submit`` over ``/data/query``). On the node itself
it submits directly.

``kit_submit``, ``kit_withdraw``, ``kit_verbs``, ``kit_verb``, ``kit_charts`` and
``kit_automations`` are served: the site comes from the verified credential
(``_caller``), never from the request, and any credential bound to a site is held
to that site; only a caller with no site on its credential (an operator) may name
one. The database is the node's own (no caller-supplied DSN reaches
``resolve_dsn``). On a bound laptop the four reads (``read_target.KIT_READS``) go
to the node, as ``kit_promote`` and ``kit_pull`` do; unbound, they read this
install's own registry and say so.

``kit_review``, ``kit_approve``, ``kit_rollback``, ``kit_retire`` and
``kit_host_setup`` are deliberately NOT served: approving, rolling back and
dropping are an operator's acts on the node (ADR-151's governing role). A
contributor's ``kit_withdraw`` is served (``/data/submit``, an invoke) and only
records a request the operator applies.
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
    """(the site this request's credential is held to, whether it may act for any site).

    A kit object belongs to one site, so ANY credential bound to a site is held to it,
    the node's home site included: ``tenant_scope`` lets a home-site key read across
    sites, which for kit objects let a contributor's key list every site's verbs and
    charts and treat a ``site`` param as its own to choose. Only a caller with no site
    on its credential (an operator at the node, a stdio session) may name a site. The
    site is read from the verified credential, never from the request.
    """
    from axiom.infra.site_authority import caller_site
    from axiom.infra.site_identity import resolve as resolve_site

    own = (caller_site() or "").strip()
    if own:
        return resolve_site(own), False
    return None, True


def _held(params: dict[str, Any], what: str) -> tuple[str | None, SkillResult | None]:
    """The site a served kit read is for, or the refusal: a sited credential reads its own
    site only, and a ``site`` param naming another is refused, not answered as empty."""
    site, home = _caller()
    asked = str(params.get("site") or "").strip()
    if home:
        return (asked or None), None
    if asked:
        from axiom.infra.site_identity import resolve as resolve_site

        if resolve_site(asked) != site:
            return None, SkillResult(ok=False, errors=[
                f"this credential reads site {site!r} only, not {asked!r}; it cannot see {what} "
                f"of another site"])
    return site, None


def _forwarded(verb: str, params: dict[str, Any]) -> tuple[SkillResult | None, str]:
    """On a bound install, the node's answer; otherwise ``None`` and where the local read runs.

    These are ``read_target.KIT_READS``: the node's door serves each by the skill of the same
    name, so the laptop and the node declare one list. ``_principal`` marks a request the node's data door is serving, which is never forwarded.
    """
    if "_principal" in params:
        return None, ""
    from ..read_target import ask, resolve_read_target

    target = resolve_read_target()
    if target.where == "binding":
        sent = {k: v for k, v in params.items() if not k.startswith("_")}
        result = ask(target, verb=verb, params=sent)
        result.actions_taken.append(f"from {target.site} at {target.face_url} (the current binding)")
        return result, ""
    if target.reason.startswith("no binding"):
        return None, ("local: this install is not bound to a node, so this is its own registry; "
                      "bind with `site bind` to see a node's promoted kits")
    return None, "this node's own registry"


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
    kept = _left_behind_note(b.left_behind)
    target = resolve_read_target()
    if target.where == "binding":
        result = ask(
            target,
            verb="data.kit_submit",
            params={"bundle": base64.b64encode(b.data).decode("ascii")},
        )
        result.actions_taken[:0] = kept
        if result.ok and isinstance(result.value, dict):
            result.actions_taken.append(_next_step(result.value, target.site))
        return result
    from ..kit import host

    try:
        value = host.submit(_node_dsn(), b.data, caller_site=None, principal=_operator(), home=True)
    except (KitError, host.RegistryNotReady) as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    return SkillResult(ok=value["state"] == "checked", value=value,
                       errors=list(value["problems"]), actions_taken=[*kept, _next_step(value, "this node")])


def _left_behind_note(files: tuple[str, ...]) -> list[str]:
    if not files:
        return []
    return [f"sent declarations only: conform/ ({len(files)} file(s)) stayed on this machine. The node "
            f"never runs contributor code (ADR-203 decision 6); your normalizer stays in your site's "
            f"reviewed package"]


def _next_step(value: dict[str, Any], where: str) -> str:
    if value.get("state") == "checked":
        return (f"bundle {value['id']} for {value['site']} is checked on {where}; an operator approves "
                f"it there (kit-approve {value['id']}), then its verbs and charts are served")
    return f"bundle {value.get('id')} was rejected: fix the problems above and promote again"


def kit_submit(params: dict[str, Any], ctx: SkillContext) -> SkillResult:  # noqa: ARG001
    """Served (``/data/submit``). Runs no DDL; a node never set up raises
    ``host.RegistryNotReady``, which the door answers as 503."""
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


#: The state of a bundle that passed the checks and waits for an operator's kit-approve.
PENDING_STATE = "checked"


def kit_review(params: dict[str, Any], ctx: SkillContext) -> SkillResult:  # noqa: ARG001
    from ..kit import host

    site = params.get("site") or None
    everything = bool(params.get("all"))
    # The bundles waiting for a decision, unless asked for one state or for all of them: a node
    # that has taken a hundred bundles listed every one back to #1, and the one to approve was lost.
    state = params.get("state") or (None if everything else PENDING_STATE)
    try:
        rows = host.review(_node_dsn(), site=site, state=state)
        asked = host.withdrawals(_node_dsn(), site=site)
    except host.RegistryNotReady as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    notes = []
    if state == PENDING_STATE and not params.get("state"):
        notes.append(f"{len(rows)} pending bundle(s) (state {PENDING_STATE}, waiting for kit-approve); "
                     "kit-review --all lists every bundle")
    notes += [f"withdrawal #{w['id']}: {w['principal']} asks to withdraw their objects at {w['site']}"
             + (f" ({w['reason']})" if w.get("reason") else "")
             + f"; apply it with kit-retire {w['site']} --principal {w['principal']} --apply" for w in asked]
    return SkillResult(ok=True, value={"bundles": rows, "withdrawals": asked}, actions_taken=notes)


def kit_host_setup(params: dict[str, Any], ctx: SkillContext) -> SkillResult:  # noqa: ARG001
    """The operator's one-time step on a node that takes kit bundles. Not served.

    Run with the database's writer role (``AXIOM_DB_URL`` / the node's DSN as that
    role). Creates the registry, grants ``serve_role`` the serving rights, and turns
    row-level security on ``silver.signals`` once, so approves stay off its lock.
    """
    from ..kit import host
    from ..kit.project import KitError

    try:
        value = host.setup(_node_dsn(), serve_role=str(params.get("serve_role") or "").strip() or None)
    except KitError as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    notes = list(value["done"])
    if not value["serve_role"]:
        notes.append("no --serve-role given: the serving app's login gets no grants, so a submit "
                     "through the door is refused until you run this again with it")
    return SkillResult(ok=True, value=value, actions_taken=notes)


def kit_approve(params: dict[str, Any], ctx: SkillContext) -> SkillResult:  # noqa: ARG001
    from ..kit import host
    from ..kit.project import KitError

    try:
        bundle_id = int(params.get("id"))
    except (TypeError, ValueError):
        return SkillResult(ok=False, errors=["`id` (the bundle number from kit-review) is required"])
    try:
        value = host.approve(_node_dsn(), bundle_id, approver=_operator(), override=bool(params.get("override")))
    except (KitError, host.RegistryNotReady) as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    return SkillResult(ok=True, value=value, actions_taken=[
        f"applied {len(value['views'])} gold view(s), {len(value['verbs'])} verb(s) and "
        f"{len(value['charts'])} chart(s) for {value['site']}"])


def kit_rollback(params: dict[str, Any], ctx: SkillContext) -> SkillResult:  # noqa: ARG001
    """Operator: re-apply an earlier applied or superseded bundle, with the same isolation proof."""
    from ..kit import host
    from ..kit.project import KitError

    site = str(params.get("site") or "").strip()
    try:
        bundle_id = int(params.get("id"))
    except (TypeError, ValueError):
        return SkillResult(ok=False, errors=["`id` (an earlier bundle number from kit-review) is required"])
    if not site:
        return SkillResult(ok=False, errors=["`site` is required (the site the bundle is for)"])
    try:
        value = host.rollback(_node_dsn(), bundle_id, site=site, approver=_operator(),
                              override=bool(params.get("override")))
    except (KitError, host.RegistryNotReady) as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    return SkillResult(ok=True, value=value, actions_taken=[
        f"bundle {bundle_id} is applied again at {site}; the bundle it replaced is superseded"])


def kit_withdraw(params: dict[str, Any], ctx: SkillContext) -> SkillResult:  # noqa: ARG001
    """Ask the node to remove your objects at a site. From a bound laptop it goes through the
    data door (an invoke, like a submit); it records a request and drops nothing."""
    from ..kit import host
    from ..kit.project import KitError
    from ..read_target import ask, resolve_read_target

    site = str(params.get("site") or "").strip()
    reason = str(params.get("reason") or "")
    served = "_principal" in params
    if not served:
        target = resolve_read_target()
        if target.where == "binding":
            result = ask(target, verb="data.kit_withdraw", params={"site": site or target.site, "reason": reason})
            if result.ok:
                result.actions_taken.append(
                    f"recorded; an operator at {target.site} sees it in kit-review and applies it")
            return result
    caller, home = _caller() if served else (None, True)
    site = site or caller or ""
    if not site:
        return SkillResult(ok=False, errors=["`site` is required"])
    principal = str(params.get("_principal") or (f"@site:{caller}" if caller else _operator()))
    try:
        value = host.request_withdraw(_node_dsn(), site=site, principal=principal, caller_site=caller,
                                      home=home, reason=reason)
    except KitError as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    except host.RegistryNotReady as exc:
        if served:
            raise
        return SkillResult(ok=False, errors=[str(exc)])
    return SkillResult(ok=True, value=value)


def kit_retire(params: dict[str, Any], ctx: SkillContext) -> SkillResult:  # noqa: ARG001
    """Operator. Without ``apply`` or ``force`` it lists what a retire would drop, and every
    dependent outside the kit, and changes nothing. ``apply`` retires (RESTRICT: refused when
    something outside depends on it); ``force`` also drops those dependents (CASCADE)."""
    from ..kit import host

    site = str(params.get("site") or "").strip()
    if not site:
        return SkillResult(ok=False, errors=["`site` is required"])
    principal = str(params.get("principal") or "").strip() or None
    force = bool(params.get("force"))
    whose = f" --principal {principal}" if principal else ""
    try:
        if not (force or params.get("apply")):
            plan = host.retire_plan(_node_dsn(), site, principal=principal)
            notes = [f"would drop: {x}" for x in plan["would_drop"]]
            notes += [f"depends on it, outside the kit: {x}" for x in plan["dependents"]]
            if not plan["would_drop"]:
                notes.append(f"nothing to retire at {site}" + (f" for {principal}" if principal else ""))
            elif plan["dependents"]:
                notes.append(f"nothing was changed. Re-run with --force to also drop the dependents "
                             f"outside the kit (kit-retire {site}{whose} --force)")
            else:
                notes.append(f"nothing was changed. Re-run with --apply to retire "
                             f"(kit-retire {site}{whose} --apply)")
            return SkillResult(ok=True, value=plan, actions_taken=notes)
        value = host.retire(_node_dsn(), site, by=_operator(), principal=principal, force=force)
    except host.RetireRefused as exc:
        return SkillResult(ok=False, value=exc.plan, errors=[str(exc)],
                           actions_taken=[f"would drop: {x}" for x in exc.plan["would_drop"]])
    except host.RegistryNotReady as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    return SkillResult(ok=True, value=value,
                       actions_taken=[f"dropped: {x}" for x in value["would_drop"]]
                       + [f"dropped with --force: {x}" for x in value["dependents"]])


def kit_verbs(params: dict[str, Any], ctx: SkillContext) -> SkillResult:  # noqa: ARG001
    """The promoted verbs this caller may run: its credential's site's only; an operator
    (no site on the credential) sees every site's, or one with ``site``. A bound laptop
    asks its node."""
    from ..kit import host

    sent, where = _forwarded("data.kit_verbs", params)
    if sent is not None:
        return sent
    wanted, refused = _held(params, "the verbs")
    if refused:
        return refused
    try:
        rows = host.list_verbs(_node_dsn(), site=wanted)
    except host.RegistryNotReady as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    return SkillResult(ok=True, value={"verbs": rows}, actions_taken=[where] if where else [])


def kit_charts(params: dict[str, Any], ctx: SkillContext) -> SkillResult:  # noqa: ARG001
    """The promoted chart specs this caller may see (ChartSpec 1.1 documents), held like kit_verbs."""
    from ..kit import host

    sent, where = _forwarded("data.kit_charts", params)
    if sent is not None:
        return sent
    wanted, refused = _held(params, "the charts")
    if refused:
        return refused
    try:
        rows = host.list_charts(_node_dsn(), site=wanted)
    except host.RegistryNotReady as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    return SkillResult(ok=True, value={"charts": rows}, actions_taken=[where] if where else [])


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

    sent, where = _forwarded("data.kit_automations", params)
    if sent is not None:
        return sent
    wanted, refused = _held(params, "the automations")
    if refused:
        return refused
    try:
        rows = host.list_automations(_node_dsn(), site=wanted)
    except host.RegistryNotReady as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    return SkillResult(ok=True, value={"automations": rows, "text": _render_automations(rows)},
                       actions_taken=[where] if where else [])


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
    sent, where = _forwarded("data.kit_verb", params)
    if sent is not None:
        return sent
    held, refused = _held(params, "or run the verbs")
    if refused:
        return refused
    asked = held or ""
    if not asked:
        return SkillResult(ok=False, errors=["`site` is required (which site's verb to run)"])
    args = params.get("params") or {}
    if not isinstance(args, dict):
        return SkillResult(ok=False, errors=["`params` must be an object of the verb's parameters"])
    try:
        rows = host.run_registered_verb(_node_dsn(), site=asked, name=name, params=args)
    except (KitError, host.RegistryNotReady) as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    return SkillResult(ok=True, value={"site": asked, "verb": name, "rows": rows,
                                       "served_by": "medallion"},
                       actions_taken=[where] if where else [])


#: A pull is a working sample, not an export. Past these it is refused with a
#: narrower call to make, before the node is asked anything.
PULL_MAX_CHANNELS = 24
PULL_MAX_POINTS = 200_000


def _lit(text: str) -> str:
    if "'" in str(text):
        raise ValueError(f"{text!r} cannot be a name in a pull (it holds a quote)")
    return f"'{text}'"


def _in(column: str, values: list[str]) -> str:
    return f"{column} in ({', '.join(_lit(v) for v in values)})"


def _read(target, verb: str, params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """One served read: through the node's door when bound, on the local medallion otherwise."""
    from ..read_target import ask

    if target.where == "binding":
        return ask(target, verb=verb, params=params)
    from . import gold

    return getattr(gold, verb.split(".", 1)[1])(params, ctx)


def _catalogued(target, site: str, ctx: SkillContext) -> tuple[list[dict[str, Any]], str | None]:
    """``(feed, channel, unit)`` for every channel at the site, from the catalogue.

    The catalogue is one row per channel, so reading all of a site's is cheap where the
    signal table is not, and it is what a misspelt channel is matched against.
    """
    flt = f"site = {_lit(site)}"
    res = _read(target, "data.aggregate", {
        "table": "signal_catalogue", "column": "last_ts", "fn": "max",
        "filter": flt, "group_by": "feed,channel,unit"}, ctx)
    if not res.ok:
        return [], "; ".join(res.errors) or "the catalogue read failed"
    groups = ((res.value or {}).get("data") or {}).get("groups") or []
    return [{"feed": g.get("feed"), "channel": g.get("channel"), "unit": g.get("unit")}
            for g in groups if g.get("channel")], None


def _plan_pull(params: dict[str, Any], target, site: str, ctx: SkillContext) -> tuple[dict[str, Any] | None, list[str]]:
    """What a pull asks the node for, bounded, or why it is refused.

    Every series filter names the site, the feeds and the channels, because the
    signal index is ``(site, feed, channel, ts)``: a site-only filter walked every
    row the site holds and timed out at the door (measured 30 s and refused,
    against 0.95 s with the feed named).
    """
    from axiom.infra import serving_guard as sgm

    def names(key: str) -> list[str]:
        raw = params.get(key) or ""
        return [str(c).strip() for c in (raw.split(",") if isinstance(raw, str) else raw) if str(c).strip()]

    channels, feeds = names("channels"), names("feed")
    bucket = str(params.get("bucket") or "1 minute")
    start, end = str(params["start"]), str(params["end"])
    if not (channels and feeds):
        found, why = _catalogued(target, site, ctx)
        if why:
            return None, [f"could not look up {site}'s channels in the catalogue ({why}); "
                          f"name them with --channels and their feed with --feed"]
        if not found:
            return None, [f"the catalogue lists no channel at {site}; see `data channels --site {site}`, "
                          f"or name --channels and --feed"]
        known = {f["channel"] for f in found}
        missing = [c for c in channels if c not in known]
        if missing:
            from .. import friendly as fr

            hints = {c: fr.close_matches(c, known) for c in missing}
            return None, [f"{site} has no channel {c!r}" + (f"; did you mean {', '.join(h)}?" if h else "")
                          for c, h in hints.items()]
        if not channels:
            channels = sorted(known)
        if not feeds:
            feeds = sorted({str(f["feed"]) for f in found if f["channel"] in channels and f.get("feed")})
    if len(channels) > PULL_MAX_CHANNELS:
        return None, [f"{site} has {len(channels)} channels; a pull takes at most {PULL_MAX_CHANNELS}. "
                      f"Name the ones you want with --channels"]
    bucket_s = sgm.interval_seconds(bucket)
    window_s = sgm.window_seconds({"start": start, "end": end})
    if bucket_s is None:
        return None, [f"bucket {bucket!r} is not an interval like '1 minute' or '1 hour'"]
    if window_s is None or window_s <= 0:
        return None, [f"the window {start} .. {end} is not a bounded span (start before end)"]
    points = int(window_s / bucket_s) * len(channels)
    if points > PULL_MAX_POINTS:
        wider = sgm.format_interval(max(bucket_s, window_s * len(channels) / PULL_MAX_POINTS))
        return None, [f"that pull is about {points:,} points ({len(channels)} channel(s) at {bucket} over "
                      f"the window); a pull keeps at most {PULL_MAX_POINTS:,}. Narrow the window, name "
                      f"fewer --channels, or use --bucket '{wider}' or coarser"]
    flt = f"site = {_lit(site)}"
    if feeds:
        flt += f" AND {_in('feed', feeds)}"
    flt += f" AND {_in('channel', channels)}"
    return {
        "table": "signals", "column": "value", "time_column": "ts", "bucket": bucket,
        "filter": flt, "group_by": "feed,channel,unit",
        "window": {"column": "ts", "start": start, "end": end},
        "_channels": channels,
    }, []


def kit_pull(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """Start from a frame that already exists: pull a site's gold series into a kit snapshot.

    On a bound install the read goes through the node's door (``data.series``), so it
    is held to what the caller's key may read; on the node it reads locally. Each
    channel's feed comes from the catalogue unless ``feed`` names it, so the read is
    an index lookup, and the window and point count are bounded before anything is
    asked. What comes back is the series the serving budget allows (it may be
    coarser than asked, and says so); it is kept as a snapshot the kit can
    ``kit-materialize``.
    """
    from ..kit import load
    from ..kit.project import KitError
    from ..kit.snapshot import take_snapshot
    from ..read_target import resolve_read_target

    try:
        project = load(params.get("dir"))
    except KitError as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    site = str(params.get("site") or project.tenant).strip()
    start, end = params.get("start"), params.get("end")
    if not (start and end):
        return SkillResult(ok=False, errors=["`start` and `end` are required (a pull is always a window)"])
    target = resolve_read_target()
    try:
        series_params, refused = _plan_pull(params, target, site, ctx)
    except ValueError as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    if series_params is None:
        return SkillResult(ok=False, errors=refused)
    channels = list(series_params.pop("_channels"))
    result = _read(target, "data.series", series_params, ctx)
    if not result.ok:
        return result
    value = result.value or {}
    rows: list[dict[str, Any]] = []
    for line in (value.get("data") or {}).get("series") or []:
        key = line.get("key") or {}
        channel, unit, feed = key.get("channel"), key.get("unit"), key.get("feed")
        for point in line.get("points") or []:
            if point.get("value") is None:
                continue
            row = {"site": site, "channel": channel, "ts": str(point.get("t")),
                   "value": point.get("value"), "unit": unit}
            if feed:
                row["feed"] = feed
            rows.append(row)
    if not rows:
        return SkillResult(ok=False, errors=[f"no readings for {site} in that window on {', '.join(channels)}"])
    try:
        kept = take_snapshot(project, site=site, channels=channels,
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


__all__ = ["kit_automation_fire", "kit_automations", "kit_charts", "kit_host_setup", "kit_pull", "kit_approve", "kit_promote", "kit_retire", "kit_review", "kit_rollback", "kit_submit", "kit_withdraw", "kit_verb", "kit_verbs"]
