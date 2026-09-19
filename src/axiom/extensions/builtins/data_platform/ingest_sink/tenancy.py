# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tenancy at the ingest face (ADR-106; spec-signal-ingest-and-producer §5.2).

``site`` and the access-tier ceiling are resolved **from the authenticated
principal**, never from the payload. A payload that asserts a different site
is refused (403); a payload that requests a tier above the credential's
ceiling is refused (403); a tier the deployment does not know is a client
error (422). Deriving the site from the credential makes a cross-site write
unrepresentable rather than merely discouraged.

The policy is pure Python (no FastAPI import) so it is testable on its own and
reusable by the MCP push skill. The HTTP routers translate
:class:`TenancyRefused` into the matching HTTP status.

Configuration (environment):

``AXIOM_ACCESS_TIER_LADDER``
    Comma-separated tier names, lowest first. Default
    ``public,restricted,export_controlled`` (the federation pack ladder). A
    deployment with its own tier vocabulary names it here.
``AXIOM_INGEST_MAX_TIER``
    The ceiling granted to any authenticated producer whose site has no
    specific ceiling. Unset = no tier grant: a payload may still land at the
    writer's default tier, but may not *request* one.
``AXIOM_INGEST_SITE_MAX_TIERS``
    Per-site ceilings, ``site=tier`` pairs separated by commas or whitespace
    (``alpha=restricted,beta=public``). A site's ceiling overrides the default.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field

DEFAULT_TIER_LADDER: tuple[str, ...] = ("public", "restricted", "export_controlled")

SITE_KEY = "site"
TIER_KEY = "access_tier"

_ENV_LADDER = "AXIOM_ACCESS_TIER_LADDER"
_ENV_MAX_TIER = "AXIOM_INGEST_MAX_TIER"
_ENV_SITE_MAX_TIERS = "AXIOM_INGEST_SITE_MAX_TIERS"


class TenancyRefused(Exception):
    """A payload the credential's grant does not cover. ``status`` is the HTTP
    status the face should answer with (403 for a grant violation, 422 for a
    tier name the deployment does not know)."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def tier_ladder(env: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """The deployment's access-tier ladder, lowest tier first."""
    raw = (env if env is not None else os.environ).get(_ENV_LADDER, "")
    names = tuple(t.strip() for t in raw.split(",") if t.strip())
    return names or DEFAULT_TIER_LADDER


@dataclass(frozen=True)
class IngestGrant:
    """What the authenticated principal may write: which site, up to which tier."""

    principal: str | None
    site: str | None
    max_access_tier: str | None


@dataclass(frozen=True)
class TenancyPolicy:
    """Resolve a grant from the request's principal and check a payload against it."""

    ladder: tuple[str, ...] = DEFAULT_TIER_LADDER
    default_max_tier: str | None = None
    site_max_tiers: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.ladder:
            raise ValueError("tier ladder must name at least one tier")
        for site, tier in (
            *(((None, self.default_max_tier),) if self.default_max_tier else ()),
            *self.site_max_tiers.items(),
        ):
            if tier not in self.ladder:
                where = f"site {site!r}" if site else "default"
                raise ValueError(
                    f"ingest ceiling for {where} is {tier!r}, not on the tier ladder "
                    f"{list(self.ladder)}"
                )

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> TenancyPolicy:
        env = env if env is not None else os.environ
        per_site: dict[str, str] = {}
        for pair in env.get(_ENV_SITE_MAX_TIERS, "").replace(",", " ").split():
            site, sep, tier = pair.partition("=")
            if not sep or not site.strip() or not tier.strip():
                raise ValueError(
                    f"{_ENV_SITE_MAX_TIERS} entry {pair!r} is not of the form site=tier"
                )
            per_site[site.strip()] = tier.strip()
        return cls(
            ladder=tier_ladder(env),
            default_max_tier=env.get(_ENV_MAX_TIER, "").strip() or None,
            site_max_tiers=per_site,
        )

    # -- grant ------------------------------------------------------------

    def grant_for(self, request: object) -> IngestGrant:
        """The grant behind ``request``: the principal the authz seam resolved
        (``request.state.principal``, a ``@name:site`` handle whose context IS
        the site), falling back to the actor's tenant when the handle has none.
        No principal at all (a loopback-only, un-gated mount) yields a grant
        with no site and no tier — such a request may neither assert a site
        nor request a tier."""
        state = getattr(request, "state", None)
        principal = getattr(state, "principal", None) if state is not None else None
        handle = getattr(principal, "handle", None)
        site = getattr(principal, "context", None) or None
        if site is None:
            actor = getattr(state, "actor", None) if state is not None else None
            site = getattr(actor, "tenant", None) or None
        ceiling = (
            self.site_max_tiers.get(site, self.default_max_tier)
            if site
            else (self.default_max_tier)
        )
        return IngestGrant(principal=handle, site=site, max_access_tier=ceiling)

    # -- check ------------------------------------------------------------

    def rank(self, tier: str) -> int:
        try:
            return self.ladder.index(tier)
        except ValueError:
            raise TenancyRefused(
                422, f"unknown access_tier {tier!r}; expected one of {list(self.ladder)}"
            ) from None

    def check(self, grant: IngestGrant, metadata: Mapping[str, str]) -> dict[str, str]:
        """Apply §5.2 to one item's/batch's metadata; return the metadata to
        land, with the credential's site stamped on it."""
        out = dict(metadata)
        asserted = (out.get(SITE_KEY) or "").strip()
        if asserted:
            if grant.site is None:
                raise TenancyRefused(
                    403, "credential carries no site; a payload may not assert one"
                )
            if asserted != grant.site:
                raise TenancyRefused(403, f"payload site {asserted!r} is not the credential's site")
        if grant.site:
            out[SITE_KEY] = grant.site
        requested = (out.get(TIER_KEY) or "").strip()
        if requested:
            wanted = self.rank(requested)
            if grant.max_access_tier is None:
                raise TenancyRefused(
                    403, "credential carries no tier grant; a payload may not request access_tier"
                )
            if wanted > self.rank(grant.max_access_tier):
                raise TenancyRefused(
                    403,
                    f"requested access_tier {requested!r} is above the credential's "
                    f"ceiling {grant.max_access_tier!r}",
                )
            out[TIER_KEY] = requested
        return out


__all__ = [
    "DEFAULT_TIER_LADDER",
    "IngestGrant",
    "SITE_KEY",
    "TIER_KEY",
    "TenancyPolicy",
    "TenancyRefused",
    "tier_ladder",
]
