# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""What a caller may see (design note J1, section 5). One check, built on every door.

A row is returned when, in order: its site label is inside the caller's site scope (attest's
read rule as built, for the attestations lane; a row with no site is node-wide); the lane is
permitted to the caller by the authz extension (every lane but attestations); its classification
is at or below the caller's ceiling; and the lane's per-row resolver, if one is registered,
accepts it. A row that fails the first two is never counted; one that fails the last two is
hidden and counted, because the caller already has the right to know that place has activity.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from axiom.infra.site_scope import SiteScope

from . import lanes

log = logging.getLogger("axiom.journal.rights")

DECISION_TTL_SECONDS = 60.0
_ORDER = {c: i for i, c in enumerate(lanes.CLASSIFICATIONS)}


@dataclass(frozen=True)
class Caller:
    handle: str
    #: The sites the caller may read; None is unbounded (an operator on a node with no bound).
    sites: frozenset[str] | None
    ceiling: str = "internal"

    def may_see_site(self, site: str | None) -> bool:
        return site is None or self.sites is None or site in self.sites


# -- building a caller on each door ---------------------------------------------------------


def _scope_for(handle: str) -> SiteScope:
    """The current credential's site scope (``site_scope.resolve``); a seam for tests."""
    from axiom.infra.site_authority import SiteNotAuthorized
    from axiom.infra.site_scope import resolve

    try:
        return resolve()
    except SiteNotAuthorized:
        return SiteScope(sites=frozenset())


def _ceiling_for(handle: str) -> str:
    """Every caller's classification ceiling is ``internal`` in this unit; a seam may raise it."""
    return "internal"


def caller_from_ctx(ctx: Any) -> Caller:
    """From a skill context (CLI, MCP, chat): the principal and the credential's site scope."""
    handle = str(getattr(getattr(ctx, "principal", None), "handle", "") or "@cli:local")
    scope = _scope_for(handle)
    sites = None if scope.unbounded else frozenset(scope.sites)
    return Caller(handle=handle, sites=sites, ceiling=_ceiling_for(handle))


def caller_from_session(person: dict[str, Any]) -> Caller:
    """From a browser session: the session's site, narrowed by what this node serves."""
    from axiom.infra.site_scope import deployment_sites

    served = deployment_sites()
    site = person.get("site")
    if site:
        sites: frozenset[str] | None = (
            frozenset({site}) & served if served is not None else frozenset({site})
        )
    elif served is not None:
        sites = frozenset(served)
    else:
        sites = None
    return Caller(handle=str(person["handle"]), sites=sites, ceiling=_ceiling_for(person["handle"]))


# -- the logbook read rule and per-row resolvers --------------------------------------------

LogbookRule = Callable[[Caller, str | None, str | None], bool]
RowResolver = Callable[[dict[str, Any], Caller], bool]


def _site_rule(caller: Caller, site: str | None, logbook: str | None) -> bool:
    return caller.may_see_site(site)


_logbook_rule: LogbookRule = _site_rule
_row_resolvers: dict[str, RowResolver] = {}


def set_logbook_read_rule(rule: LogbookRule | None) -> None:
    """A finer attestations rule (W1, W4: a read-only or absent logbook) plugs in here."""
    global _logbook_rule
    _logbook_rule = rule or _site_rule


def register_row_resolver(lane: str, resolver: RowResolver | None) -> None:
    """Only rows linked to the caller's own objects, per lane (the design's "linked" state)."""
    lanes.check_lane(lane)
    if resolver is None:
        _row_resolvers.pop(lane, None)
    else:
        _row_resolvers[lane] = resolver


def _own_rows(row: dict[str, Any], caller: Caller) -> bool:
    return row.get("actor") == caller.handle


register_row_resolver("memory", _own_rows)


def has_row_resolver(lane: str) -> bool:
    return lane in _row_resolvers


# -- lane permission through the authz extension --------------------------------------------

_authn: Any = None
_decisions: dict[tuple[str, str], tuple[bool, float]] = {}
_decision_lock = threading.Lock()


def _guard_decides(caller: Caller, lane: str) -> bool:
    """Ask GUARD for ``journal.read`` on ``journal://lane/<lane>``; fail closed."""
    global _authn
    try:
        if _authn is None:
            from axiom.governance import setup_extension

            _authn = setup_extension("journal", verbs=["read"])
        from axiom.governance import AuthorizationDenied

        try:
            with _authn.action(verb="read", actor=caller.handle, resource=f"journal://lane/{lane}"):
                return True
        except AuthorizationDenied:
            return False
    except Exception:  # noqa: BLE001 - no GUARD is no permission
        log.debug("journal: lane permission for %s on %s could not be decided", caller.handle, lane,
                  exc_info=True)  # fmt: skip
        return False


_lane_decider: Callable[[Caller, str], bool] = _guard_decides


def set_lane_decider(fn: Callable[[Caller, str], bool] | None) -> None:
    global _lane_decider
    _lane_decider = fn or _guard_decides
    forget_decisions()


def forget_decisions() -> None:
    with _decision_lock:
        _decisions.clear()


def lane_allowed(caller: Caller, lane: str) -> bool:
    """Attestations follow attest's own read rule; every other lane asks the authz extension,
    cached per caller and lane for a minute so a polling Timeline does not write a receipt a tick."""
    if lane == "attestations":
        return True
    key = (caller.handle, lane)
    now = time.monotonic()
    with _decision_lock:
        cached = _decisions.get(key)
        if cached and cached[1] > now:
            return cached[0]
    allowed = bool(_lane_decider(caller, lane))
    with _decision_lock:
        _decisions[key] = (allowed, now + DECISION_TTL_SECONDS)
    return allowed


def allowed_lanes(caller: Caller, requested: list[str] | None = None) -> list[str]:
    wanted = [lanes.check_lane(x) for x in requested] if requested else lanes.names()
    return [lane for lane in wanted if lane_allowed(caller, lane)]


# -- the per-row check -----------------------------------------------------------------------


def visible(row: dict[str, Any], caller: Caller) -> tuple[bool, str | None]:
    """``(visible, reason)``. ``scope`` and ``lane`` are never counted; the rest are hidden counts."""
    lane = row["lane"]
    if lane == "attestations":
        if not _logbook_rule(caller, row.get("site"), row.get("logbook")):
            return False, "scope"
    elif not caller.may_see_site(row.get("site")):
        return False, "scope"
    if not lane_allowed(caller, lane):
        return False, "lane"
    if _ORDER.get(str(row.get("classification")), 1) > _ORDER.get(caller.ceiling, 1):
        return False, "classification"
    resolver = _row_resolvers.get(lane)
    if resolver is not None and not resolver(row, caller):
        return False, "resolver"
    return True, None


__all__ = [
    "Caller",
    "allowed_lanes",
    "caller_from_ctx",
    "caller_from_session",
    "forget_decisions",
    "has_row_resolver",
    "lane_allowed",
    "register_row_resolver",
    "set_lane_decider",
    "set_logbook_read_rule",
    "visible",
]
