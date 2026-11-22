# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A site's structure, read where its heartbeats are: nodes, health, uptime, topology, checklist.

All five are built from the heartbeats a site's sending nodes post to the node that serves the
site. That store exists on one machine. An install bound to the site (ADR-207) has a heartbeat
store too, and it describes that install, so answering from it draws the wrong machines under the
site's name.

So there is one implementation and two ways in:

* the node's own ``/api/v1/sites/{site}/…`` routes call the functions here directly;
* a bound install's same routes forward to the node's data door (``POST /data/query``), which
  calls :func:`answer` with the verb from ``read_target.SITE_READS``.

Both run on the node that holds the heartbeats, so the two answers are the same answer.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

#: The longest uptime period answered, in days.
MAX_UPTIME_DAYS = 90.0


def health_settings() -> dict[str, Any]:
    """The window and oldest accepted version, from the platform's environment."""
    out: dict[str, Any] = {"min_version": os.environ.get("AXIOM_INTAKE_MIN_VERSION", "")}
    raw = os.environ.get("AXIOM_PARTNER_STOPPED_AFTER_H", "")
    if raw:
        out["stopped_after_s"] = float(raw) * 3600
    return out


def nodes(site: str) -> dict[str, Any]:
    """The nodes that send this site's data, and whether each is alive (ADR-164)."""
    from .ingest_sink.heartbeat import HeartbeatStore, liveness

    return {"site": site,
            "nodes": [{**node, "state": liveness(node)} for node in HeartbeatStore.from_env().nodes(site)]}


def health(site: str) -> dict[str, Any]:
    """The site's health from its nodes' heartbeats: heard, fresh, lanes, version, disk."""
    from . import partner_health

    return partner_health.site_health(site, **health_settings())


def uptime(site: str, days: float = 7) -> dict[str, Any]:
    """Downtime per installation, each interval attributed to a cause (ADR-182 D5a)."""
    from . import uptime as _uptime

    return _uptime.site_report(site, period_s=max(1.0, min(float(days), MAX_UPTIME_DAYS)) * 86400)


def topology(site: str) -> dict[str, Any]:
    """Every hop from this site's sending nodes to the node that serves it."""
    from axiom.infra import topology as _topology

    from . import partner_health
    from .ingest_sink.heartbeat import HeartbeatStore

    store = HeartbeatStore.from_env()
    topo = _topology.build(site, store.nodes(site), here=_topology.this_node(),
                           planned=_topology.planned_for(site))
    # Each reporting hop carries its health (W8): the same checks the site card and the
    # alerts use, so the three never disagree.
    return partner_health.annotate_topology(
        topo, partner_health.site_health(site, store=store, **health_settings()))


def checklist(site: str) -> dict[str, Any]:
    """ADR-178 D4: every readiness check, each ok, waiting, failing or not applicable."""
    # The checklist is the sites extension's; it is read here, lazily, so this door has one
    # implementation of every structural read rather than a second route for one of them.
    from axiom.extensions.builtins.sites import checklist as _checklist

    return _checklist.report(site, **health_settings())


_READS: dict[str, Callable[..., dict[str, Any]]] = {
    "nodes": nodes,
    "health": health,
    "uptime": uptime,
    "topology": topology,
    "checklist": checklist,
}


def not_found(site: str) -> str:
    """"No such site", and, for a key held to its site, why it sees only that one.

    The same words whichever site was asked for, so it confirms nothing about what else is
    hosted here.
    """
    from axiom.infra.site_authority import (
        UNSITED_KEY_REFUSAL,
        SiteNotAuthorized,
        held_site_note,
        tenant_scope,
    )

    try:
        held = tenant_scope()
    except SiteNotAuthorized:
        return f"no such site: {site}. {UNSITED_KEY_REFUSAL}"
    if held is None:
        return f"no such site: {site}"
    return f"no such site: {site}. {held_site_note(held)}"


def answer(read: str, params: dict[str, Any], ctx: SkillContext | None = None) -> SkillResult:
    """One structural read through the data door, held to the caller's site.

    The site is checked against the verified credential (``site_scope``) before anything is
    read, exactly as the node's own route does. A site out of scope is "no such site" and never
    an empty answer, which would read as "that site has no machines".
    """
    del ctx
    fn = _READS.get(read)
    if fn is None:
        return SkillResult(ok=False, errors=[f"not a site read: {read}"])
    site = str(params.get("site") or "").strip()
    if not site:
        return SkillResult(ok=False, errors=["missing required param: site"])
    from axiom.infra import site_scope

    try:
        site_scope.resolve().require(site)
    except site_scope.SiteOutOfScope:
        return SkillResult(ok=False, errors=[not_found(site)])
    try:
        if read == "uptime":
            return SkillResult(ok=True, value=uptime(site, float(params.get("days") or 7)))
        return SkillResult(ok=True, value=fn(site))
    except (TypeError, ValueError) as exc:
        return SkillResult(ok=False, errors=[f"site.{read}: {exc}"])
    except LookupError:
        # The checklist asks the scope again itself; the answer is the same refusal.
        return SkillResult(ok=False, errors=[not_found(site)])


__all__ = [
    "MAX_UPTIME_DAYS",
    "answer",
    "checklist",
    "health",
    "health_settings",
    "nodes",
    "not_found",
    "topology",
    "uptime",
]
