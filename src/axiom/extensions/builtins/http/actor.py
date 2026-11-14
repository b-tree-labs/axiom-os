# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Who is acting, for GUARD — the identity-boundary resolver (ADR-084 × ADR-103).

Until now the HTTP edge resolved a bare :class:`Principal` and ``decide()`` ran
with no roles, no tenant, no assurance — the directory seam and ``ActorContext``
existed but nothing on a request path called them. This module is that call.

Per request, after the credential is resolved and the envelope built:

1. ``claims`` come off the credential (a webauth session's verified claims, an
   issued API key's identity — never anything the client typed);
2. the **membership seam** is consulted — claims-first (``groups``/``roles``
   on the token), then a configured directory provider, degrading only toward
   *less* authority (ADR-103 decisions 6–7, 10);
3. :func:`resolve_actor` composes an :class:`ActorContext` (handle, tenant,
   roles, assurance) and :func:`subject_from_membership` the
   :class:`SubjectContext` (substrate user id + groups as contextual tuples);
4. both ride the :class:`ActionEnvelope` into ``decide()``.

Role precedence, stated once: when the **directory itself answered**
(``source`` = ``directory`` / ``cache``) it is authoritative alone. When the
answer is **claims-based** (no provider, complete claims, or degraded) the
token's ``roles`` claim — an IdP app-role assignment, a different authority
from group membership — is unioned in. The directory can therefore remove a
role the token still asserts; a token can never add one over the directory.

Resolution **never raises**: any failure yields an actor with no roles, which
is the fail-toward-less-authority shape ADR-103 requires.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Callable, Mapping
from typing import Any

from axiom.governance.actor import ActorContext, resolve_actor
from axiom.governance.subject import SubjectContext
from axiom.vega.identity.principal import Principal

_LOGGER = logging.getLogger("axi.serve")

ActorResolver = Callable[[object, Principal, Any], tuple[ActorContext, SubjectContext | None]]
"""``(request, principal, credential) -> (actor, subject)``. ``credential`` is the
resolver's ``ResolvedCredential`` (or ``None`` for a bare principal)."""

USER_PREFIX = "user:"

_SLUG_RE = re.compile(r"[^A-Za-z0-9_\-\.]")


def slug(value: str) -> str:
    """Fold any subject (email, GUID, UPN) into the ``@name`` handle grammar."""
    return _SLUG_RE.sub("_", value or "").strip("_") or "anon"


def _claims_of(credential: Any) -> Mapping[str, Any]:
    claims = getattr(credential, "claims", None)
    return claims if isinstance(claims, Mapping) else {}


def _posture_of(credential: Any) -> str:
    return getattr(credential, "posture", None) or "open"


def _subject_of(claims: Mapping[str, Any], principal: Principal) -> str:
    sub = claims.get("sub")
    return str(sub) if sub else principal.handle


def build_actor_resolver(
    *,
    membership: Any | None = None,
    provider_name: str = "oidc_claims",
    tenant: str | None = None,
) -> ActorResolver:
    """The default identity-boundary resolver.

    ``membership`` is a ``directory.resolution.MembershipResolver`` (or anything
    with ``resolve(PrincipalRef, claims=) -> Membership``); ``None`` means
    roles come from claims alone. ``provider_name`` labels the ``PrincipalRef``
    so cache keys and revocations line up with the reconciler's.
    """

    def resolve(
        request: object, principal: Principal, credential: Any
    ) -> tuple[ActorContext, SubjectContext | None]:
        claims = _claims_of(credential)
        posture = _posture_of(credential)
        subject = _subject_of(claims, principal)
        the_tenant = tenant if tenant is not None else claims.get("tid")
        attributes: dict[str, Any] = {"subject": subject}
        if claims.get("idp"):
            attributes["idp"] = claims["idp"]
        if claims.get("email"):
            attributes["email"] = claims["email"]

        claim_roles = tuple(str(r) for r in (claims.get("roles") or ()))
        roles: tuple[str, ...] = claim_roles
        subject_ctx: SubjectContext | None = SubjectContext(
            tenant=the_tenant, fga_user=f"{USER_PREFIX}{subject}", attributes=dict(attributes)
        )

        if membership is not None:
            try:
                from axiom.extensions.builtins.directory.binding import subject_from_membership
                from axiom.extensions.builtins.directory.protocol import PrincipalRef

                ref = PrincipalRef(
                    subject=subject, provider=provider_name, display=claims.get("name")
                )
                m = membership.resolve(ref, claims=dict(claims))
                directory_answered = m.source in {"directory", "cache"}
                roles = tuple(m.roles) if directory_answered else _union(m.roles, claim_roles)
                attributes.update(
                    membership_source=m.source,
                    membership_stale=m.stale,
                    membership_as_of=m.as_of,
                )
                subject_ctx = subject_from_membership(
                    m, tenant=the_tenant, attributes=dict(attributes)
                )
            except Exception as exc:  # noqa: BLE001 — degrade toward LESS authority
                _LOGGER.warning("membership resolution failed for %s: %s", principal.handle, exc)
                roles = ()
                attributes["membership_source"] = "error"
                subject_ctx = SubjectContext(
                    tenant=the_tenant,
                    fga_user=f"{USER_PREFIX}{subject}",
                    attributes=dict(attributes),
                )

        try:
            actor = resolve_actor(
                claims,
                handle=principal.handle,
                posture=posture,
                roles=roles,
                tenant=the_tenant,
                attributes=attributes,
            )
        except Exception as exc:  # noqa: BLE001 — a bad posture/claim must not open the door
            _LOGGER.warning("actor resolution failed for %s: %s", principal.handle, exc)
            actor = ActorContext(
                handle=principal.handle,
                tenant=the_tenant,
                roles=(),
                attributes={"subject": subject, "resolution": "error"},
            )
        return actor, subject_ctx

    return resolve


def _union(first: tuple[str, ...], second: tuple[str, ...]) -> tuple[str, ...]:
    out = list(first)
    for r in second:
        if r not in out:
            out.append(r)
    return tuple(out)


# ---------------------------------------------------------------------------
# Environment wiring
# ---------------------------------------------------------------------------

DIRECTORY_PROVIDER_ENV = "AXIOM_DIRECTORY_PROVIDER"
"""``oidc_claims`` (default: roles/groups off the token), ``local`` (a file), or
``none`` to skip the seam entirely."""
DIRECTORY_LOCAL_FILE_ENV = "AXIOM_DIRECTORY_LOCAL_FILE"
DIRECTORY_ROLE_MAP_ENV = "AXIOM_DIRECTORY_ROLE_MAP"
"""A ``GroupRoleMap`` JSON file (``{"rules": [{"group": "...", "role": "..."}]}``).
Absent = the ADR-103 empty default: groups map to no roles until a mapping is
authored; the token's ``roles`` claim still applies."""
TENANT_ENV = "AXIOM_TENANT"

_REVOKED: Any | None = None


def shared_revoked_set():
    """The process-wide recently-revoked set the reconciler and resolver share."""
    global _REVOKED
    if _REVOKED is None:
        from axiom.extensions.builtins.directory.revoked import RevokedSet

        _REVOKED = RevokedSet()
    return _REVOKED


def actor_resolver_from_env(env: Mapping[str, str] | None = None) -> ActorResolver:
    """Build the default resolver from ``AXIOM_DIRECTORY_*`` / ``AXIOM_TENANT``.

    Never fails the serve: a misconfigured provider logs and falls back to
    claims-only resolution, which grants strictly less.
    """
    env = os.environ if env is None else env
    tenant = (env.get(TENANT_ENV) or "").strip() or None
    provider_name = (env.get(DIRECTORY_PROVIDER_ENV) or "oidc_claims").strip().lower()
    if provider_name == "none":
        return build_actor_resolver(membership=None, tenant=tenant)

    try:
        from axiom.extensions.builtins.directory.mapping import GroupRoleMap
        from axiom.extensions.builtins.directory.resolution import MembershipResolver

        role_map_path = (env.get(DIRECTORY_ROLE_MAP_ENV) or "").strip()
        role_map = GroupRoleMap.from_file(role_map_path) if role_map_path else GroupRoleMap()
        provider = None
        if provider_name == "local":
            from axiom.extensions.builtins.directory.providers import get_provider

            path = (env.get(DIRECTORY_LOCAL_FILE_ENV) or "").strip()
            if not path:
                raise ValueError(f"{DIRECTORY_LOCAL_FILE_ENV} is required for provider=local")
            provider = get_provider("local", path=path)
        elif provider_name != "oidc_claims":
            raise ValueError(
                f"directory provider {provider_name!r} is not wired at the HTTP edge yet "
                "(oidc_claims | local | none)"
            )
        membership = MembershipResolver(
            provider=provider, role_map=role_map, revoked=shared_revoked_set()
        )
        return build_actor_resolver(
            membership=membership, provider_name=provider_name, tenant=tenant
        )
    except Exception as exc:  # noqa: BLE001 — fall back to claims-only, never open up
        _LOGGER.warning("directory seam not wired (%s); roles come from claims only", exc)
        return build_actor_resolver(membership=None, tenant=tenant)


__all__ = [
    "DIRECTORY_LOCAL_FILE_ENV",
    "DIRECTORY_PROVIDER_ENV",
    "DIRECTORY_ROLE_MAP_ENV",
    "TENANT_ENV",
    "USER_PREFIX",
    "ActorResolver",
    "actor_resolver_from_env",
    "build_actor_resolver",
    "shared_revoked_set",
    "slug",
]
