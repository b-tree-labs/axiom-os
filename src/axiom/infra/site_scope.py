# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Which sites a request may see — decided once, for every kind of resource.

One platform serves several sites at once. A partner's deployment should behave
as though it *is* their site and nothing else exists; a fleet operator, a fleet
researcher or a regulator may legitimately see many and compare across them. Both
are the same question asked with a different answer: which sites is this request
allowed to touch?

Asking it once is the point. Charts, models, logs and documents all belong to a
site, and if each endpoint grows its own filter they will diverge — one will
forget, and the one that forgets is the leak. So scope is resolved here and
handed to every surface, rather than re-derived per resource.

**Two layers, intersected.**

*Deployment bound* — what this node serves at all, declared in its own profile.
This is what makes a partner install present as that partner's site: the bound is
applied before anything else is consulted, so a misconfigured grant cannot widen
it. A node that serves one site cannot leak a second even to an administrator.

*Principal grant* — the subset that this requester may see, which is where a
fleet operator differs from a site operator. Until the authorization seam lands
this is supplied by the caller; the shape is deliberately already here so the
surfaces do not have to change when it does.

**A site is any operated location, and it is the only axis enforced here.**

A reactor, a flow loop, a factory, a data centre, a farm — one platform serving a
company's several factories is the same shape as one serving several reactors,
and ADR-050 already settled `site` as the platform's word for it.

Organisation, fleet and role are how a grant is *computed*, not a second thing to
filter on. A fleet researcher's access might be derived from their org, their
role, or an explicit list; all of it resolves to a set of sites before it reaches
here. Keeping enforcement on one axis is what stops a second filter appearing
later that disagrees with this one — and two filters that disagree is how a
resource ends up visible through one path and hidden through another.

**Out of scope answers 404, never 403.**

403 confirms the thing exists. On a shared platform that tells one partner that
another partner is hosted beside them, and tells a researcher which sites they
were not cleared for — both of which are disclosures in themselves. 404 is a
deliberate omission and it must be uniform: an endpoint that answers 403 while
its neighbours answer 404 re-reveals what the others conceal, so this module
raises one error and every surface maps it the same way.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

#: Sites this deployment serves, comma-separated. Unset means unbounded, which
#: is correct for a development box and wrong for anything hosting a partner —
#: see `deployment_sites` for why that default is not made stricter.
SERVED_SITES_ENV = "AXIOM_SERVED_SITES"


class SiteOutOfScope(LookupError):
    """A site this request may not see.

    A LookupError because "not found" is what the caller will be told. Surfaces
    map it to 404 — see the module docstring for why never 403.
    """


@dataclass(frozen=True)
class SiteScope:
    """The sites one request may touch."""

    sites: frozenset[str]
    #: True when no bound applies — a single-tenant development box, or an
    #: operator whose grant is genuinely fleet-wide on a node that serves all.
    unbounded: bool = False
    #: For an unbounded scope, the decision function that still answers per site
    #: (MVP-07-A1): no list to filter in advance, so each site is asked as it is
    #: met. ``None`` keeps the old "everything" answer.
    decider: Callable[[str], bool] | None = field(default=None, compare=False, repr=False)

    def permits(self, site: str) -> bool:
        if self.unbounded:
            # An object with no site is not a tenant's object; it stays visible
            # exactly as it was before any decision was asked.
            return self.decider is None or not site or self.decider(site)
        return site in self.sites

    def require(self, site: str) -> str:
        """Return *site*, or raise if this request may not see it."""
        if not self.permits(site):
            raise SiteOutOfScope(site)
        return site

    def filter(self, candidates: Iterable[str]) -> list[str]:
        """Only the candidates in scope, order preserved.

        A listing must never include a site the requester cannot open. Showing
        a name and then refusing it discloses exactly what the refusal is meant
        to withhold.
        """
        if self.unbounded:
            return [c for c in candidates if self.permits(c)]
        return [c for c in candidates if c in self.sites]

    def describe(self) -> str:
        if self.unbounded:
            return "all sites (no bound configured)"
        if not self.sites:
            return "no sites"
        return ", ".join(sorted(self.sites))


def deployment_sites(env: dict[str, str] | None = None) -> frozenset[str] | None:
    """Sites this node serves, or None when nothing is declared.

    None means unbounded, and that is deliberate rather than an oversight: a
    development box with no configuration must still work, and defaulting to
    "serve nothing" would turn a missing variable into an outage that looks like
    a bug in the data. The place to be strict is the deployment that hosts a
    partner, which declares its sites — and `resolve` reports when it has not.
    """
    source = env if env is not None else os.environ
    raw = (source.get(SERVED_SITES_ENV) or "").strip()
    names = frozenset(part.strip() for part in raw.split(",") if part.strip())
    # The tenant registry is the same statement as a file (MVP-10-A2): adding a
    # tenant there serves it, with no code edit and no restart.
    from axiom.infra import tenants

    names = names | tenants.ids(source)
    return names or None


def resolve(
    *,
    granted: Iterable[str] | None = None,
    env: dict[str, str] | None = None,
    selection: bool = True,
    decide: bool = True,
) -> SiteScope:
    """The effective scope: what the node serves, narrowed by what this
    requester was granted, by what they chose in the scope selector, and by the
    one decision function.

    ``selection=False`` ignores the scope selector's choice: the selector itself
    lists every site the person may choose from. ``decide=False`` skips the
    decision function, for a caller that is about to ask it per site anyway.

    ``granted=None`` means "the current request's credential": a credential held
    to a site (:func:`axiom.infra.site_authority.tenant_scope`) narrows to that
    site; otherwise no principal-level narrowing is known, so the deployment
    bound stands alone. That is the state before the authorization
    seam lands, and it is safe in the direction that matters: the bound can only
    narrow, never widen.
    """
    scope = _granted_scope(granted=granted, env=env)
    if selection:
        from axiom.infra.site_authority import selected_sites

        chosen = selected_sites()
        if chosen is not None:
            # A choice only narrows. A site the person may not read stays out
            # however it is spelled in the cookie.
            scope = SiteScope(sites=frozenset(s for s in chosen if scope.permits(s)))
    if decide:
        scope = _decided(scope)
    return scope


def _decided(scope: SiteScope) -> SiteScope:
    """Every site in ``scope`` asked of the one decision function (MVP-07-A1).

    The decision used to happen for a read that named a site
    (:func:`axiom.infra.site_authority.authorize_site`) and nowhere else, so a
    rule denying a site held for a telemetry query and not for search or chat,
    which read through this. Now every listing asks the same ``decide()``.
    """
    from axiom.infra.site_authority import site_read_permitted

    if scope.unbounded:
        return SiteScope(sites=frozenset(), unbounded=True, decider=site_read_permitted)
    return SiteScope(sites=frozenset(s for s in scope.sites if site_read_permitted(s)))


def _granted_scope(*, granted: Iterable[str] | None, env: dict[str, str] | None) -> SiteScope:
    bound = deployment_sites(env)

    if granted is None:
        # The verified credential's own site, when it is held to one: a listing
        # shows that site and nothing else, the node's home site included
        # (ADR-203). ``None`` (an operator, no credential, the owner's grant)
        # leaves the deployment bound standing alone, as before.
        from axiom.infra.site_authority import SiteNotAuthorized, tenant_scope

        try:
            held = tenant_scope()
        except SiteNotAuthorized:
            # An issued key with no site: a listing shows it nothing, and a read
            # it makes is refused with the reason (site_authority.UNSITED_KEY_REFUSAL).
            return SiteScope(sites=frozenset())
        if held is not None:
            granted = (held,)

    if granted is None:
        if bound is None:
            return SiteScope(sites=frozenset(), unbounded=True)
        return SiteScope(sites=bound)

    granted_set = frozenset(granted)
    if bound is None:
        return SiteScope(sites=granted_set)

    # Intersection, so a grant naming a site this node does not serve cannot
    # widen the deployment bound. The bound is the outer limit by construction.
    return SiteScope(sites=bound & granted_set)


__all__ = [
    "SERVED_SITES_ENV",
    "SiteOutOfScope",
    "SiteScope",
    "deployment_sites",
    "resolve",
]
