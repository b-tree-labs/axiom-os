# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Membership resolution — ADR-103 decisions 4, 5 and 12.

Claims-first, at the identity boundary, stamped with ``as_of``. The directory is
consulted only when the token cannot answer — either it carries no group claim
or it signals *overage*, the pointer Entra substitutes once the group list
exceeds the token size limit.

The degradation rules are the load-bearing part. A directory outage must not
brick authorization, but neither may it silently extend authority nobody can
re-confirm:

* within TTL, the cached projection is served;
* past TTL with the directory unreachable, resolution falls back to what the
  **token** carries — never to "last known good, indefinitely";
* a failure can therefore only ever *remove* roles, never add them;
* staleness is stamped so a decision that rode a degraded projection is visible
  to audit rather than indistinguishable from a fresh one.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from axiom.extensions.builtins.directory.mapping import GroupRoleMap
from axiom.extensions.builtins.directory.protocol import GroupRef, PrincipalRef
from axiom.extensions.builtins.directory.revoked import RevokedSet


@dataclass(frozen=True)
class Membership:
    """A resolved projection. ``as_of``/``stale`` ride onto ActorContext."""

    principal: PrincipalRef
    groups: tuple[GroupRef, ...] = ()
    roles: tuple[str, ...] = ()
    as_of: float = 0.0
    stale: bool = False
    source: str = "none"


@dataclass
class _Entry:
    groups: tuple[GroupRef, ...]
    at: float


def _has_overage(claims: dict) -> bool:
    """Entra replaces a too-large ``groups`` claim with a pointer to Graph."""
    names = claims.get("_claim_names") or {}
    return "groups" in names


class MembershipResolver:
    def __init__(
        self,
        *,
        provider=None,
        role_map: GroupRoleMap | None = None,
        ttl: float = 300.0,
        revoked: RevokedSet | None = None,
    ) -> None:
        self._provider = provider
        self._role_map = role_map or GroupRoleMap()
        self._ttl = ttl
        self._revoked = revoked
        self._cache: dict[tuple[str, str], _Entry] = {}

    # -- internals ---------------------------------------------------------
    @staticmethod
    def _key(p: PrincipalRef) -> tuple[str, str]:
        """Keyed on the immutable subject — a display-name change must not miss."""
        return (p.provider, p.subject)

    def _claim_groups(self, claims: dict, principal: PrincipalRef) -> tuple[GroupRef, ...]:
        return tuple(
            GroupRef(id=str(g), provider=principal.provider) for g in (claims.get("groups") or [])
        )

    def _finish(self, principal, groups, now, stale, source) -> Membership:
        # ADR-103 decision 10: the revoked fast path is consulted on EVERY
        # resolution, whatever the source. A token minted before a revocation
        # still asserts the group; without this the person keeps access until
        # that token refreshes — the window ADR-086 exists to close.
        if self._revoked is not None:
            groups = tuple(
                g for g in groups if not self._revoked.is_revoked(principal.subject, g.id, now=now)
            )
        return Membership(
            principal=principal,
            groups=tuple(groups),
            roles=self._role_map.roles_for(g.id for g in groups),
            as_of=now,
            stale=stale,
            source=source,
        )

    # -- the seam ----------------------------------------------------------
    def resolve(
        self,
        principal: PrincipalRef,
        *,
        claims: dict | None = None,
        now: float | None = None,
    ) -> Membership:
        claims = claims or {}
        now = time.time() if now is None else now
        overage = _has_overage(claims)

        # 1. Complete claims answer without touching the network.
        if "groups" in claims and not overage:
            return self._finish(
                principal, self._claim_groups(claims, principal), now, False, "claims"
            )

        # 2. A warm projection inside its TTL is authoritative enough.
        entry = self._cache.get(self._key(principal))
        if entry is not None and (now - entry.at) <= self._ttl:
            return self._finish(principal, entry.groups, now, False, "cache")

        # 3. Ask the directory.
        if self._provider is not None:
            try:
                groups = tuple(self._provider.groups_for(principal))
            except Exception:  # noqa: BLE001 — any failure degrades identically
                groups = None
            if groups is not None:
                self._cache[self._key(principal)] = _Entry(groups=groups, at=now)
                return self._finish(principal, groups, now, False, "directory")

        # 4. Degrade toward LESS authority: the token only, never a stale cache.
        return self._finish(
            principal, self._claim_groups(claims, principal), now, True, "claims-degraded"
        )


__all__ = ["Membership", "MembershipResolver"]
