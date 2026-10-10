# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""ActorContext — the unified governance view of who is acting (ADR-084).

Three descriptions of identity had drifted apart: the minimal cryptographic
``Principal`` (its bytes are load-bearing in capability signatures, so it must
stay minimal), ``infra.PrincipalContext``'s posture ladder, and the raw claim bag
at the HTTP edge. GUARD needs all three at once, and reconciling them at each
call site is how they diverge.

**Compose, don't merge.** ``ActorContext`` carries the handle, tenant, roles,
attributes and an :class:`Assurance` — and leaves ``Principal`` untouched.

Resolution is **deterministic from verified claims**: no lookups, no I/O, no
clock. That property is what lets it sit at the identity boundary and keeps
``decide()`` free of them, per ADR-084 and ADR-103.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from axiom.infra.principal import POSTURES, PrincipalContext

#: Normative posture ↔ NIST AAL mapping. ``open`` is a *named absence of proof*,
#: so it sits below AAL1 rather than being folded into it.
AAL_BY_POSTURE: dict[str, int] = {
    "open": 0,
    "attested": 1,
    "sso": 2,
    "service": 2,
}

#: Default ``acr`` when the IdP does not supply one. A real IdP's acr is more
#: informative and always wins.
ACR_BY_POSTURE: dict[str, str] = {
    "open": "axiom:posture:open",
    "attested": "axiom:posture:attested",
    "sso": "axiom:posture:sso",
    "service": "axiom:posture:service",
}


@dataclass(frozen=True)
class Assurance:
    """How strongly the actor is known — posture, plus the OIDC vocabulary."""

    posture: str = "open"
    acr: str | None = None
    amr: tuple[str, ...] = ()
    auth_time: float | None = None

    @property
    def aal(self) -> int:
        return AAL_BY_POSTURE.get(self.posture, 0)

    @property
    def authenticated(self) -> bool:
        """``open`` is the absence of proof, not a weak proof."""
        return self.aal > 0

    def meets(self, floor: str) -> bool:
        return self.aal >= AAL_BY_POSTURE.get(floor, 0)


@dataclass(frozen=True)
class ActorContext:
    """The governance actor. ``Principal`` stays minimal; this rides alongside."""

    handle: str
    tenant: str | None = None
    roles: tuple[str, ...] = ()
    attributes: Mapping[str, Any] = field(default_factory=dict)
    assurance: Assurance = field(default_factory=Assurance)

    def has_role(self, role: str) -> bool:
        return role in self.roles

    @classmethod
    def from_principal_context(
        cls,
        pc: PrincipalContext,
        *,
        tenant: str | None = None,
        roles: Sequence[str] = (),
        attributes: Mapping[str, Any] | None = None,
    ) -> ActorContext:
        attrs = dict(attributes or {})
        if pc.idp is not None:
            attrs.setdefault("idp", pc.idp)
        attrs.setdefault("assured", pc.assured)
        return cls(
            handle=pc.handle,
            tenant=tenant,
            roles=tuple(roles),
            attributes=attrs,
            assurance=Assurance(
                posture=pc.posture, acr=ACR_BY_POSTURE.get(pc.posture)
            ),
        )

    def to_principal_context(self) -> PrincipalContext:
        """Project back, so consumers of the older type keep working unchanged."""
        return PrincipalContext(
            handle=self.handle,
            posture=self.assurance.posture,
            assured=bool(self.attributes.get("assured", self.assurance.authenticated)),
            idp=self.attributes.get("idp"),
        )


def resolve_actor(
    claims: Mapping[str, Any],
    *,
    handle: str,
    posture: str = "open",
    roles: Sequence[str] | None = None,
    tenant: str | None = None,
    attributes: Mapping[str, Any] | None = None,
) -> ActorContext:
    """Build an :class:`ActorContext` from **verified** token claims.

    ``roles`` supplied out of band wins over any ``roles`` claim: group claims
    overflow past the token size limit, so the directory seam is authoritative
    whenever it was consulted, and the claim is only the fallback.

    Raises ``ValueError`` on an unknown posture rather than silently downgrading
    — a typo must not quietly become ``open``.
    """
    if posture not in POSTURES:
        raise ValueError(
            f"unknown posture {posture!r}; expected one of {', '.join(POSTURES)}"
        )

    claim_roles = tuple(claims.get("roles") or ())
    amr = tuple(claims.get("amr") or ())
    auth_time = claims.get("auth_time")

    return ActorContext(
        handle=handle,
        tenant=tenant if tenant is not None else claims.get("tid"),
        roles=tuple(roles) if roles is not None else claim_roles,
        attributes=dict(attributes or {}),
        assurance=Assurance(
            posture=posture,
            acr=claims.get("acr") or ACR_BY_POSTURE.get(posture),
            amr=amr,
            auth_time=float(auth_time) if auth_time is not None else None,
        ),
    )


__all__ = [
    "AAL_BY_POSTURE",
    "ACR_BY_POSTURE",
    "ActorContext",
    "Assurance",
    "resolve_actor",
]
