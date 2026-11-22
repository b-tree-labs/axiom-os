# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The ``grant`` noun: one list and one revoke over KEEP's stores (ADR-250 D4, RW-G1).

Authority is given one way, a grant, in four kinds:

- ``read``: a gate key, what a key may read (its roles and the site it is held to), or a
  reviewer grant, one log at one site until a stated time (#1817);
- ``act``: a KEEP capability token, what an actor may do (an intent and a resource pattern);
- ``sign``: an ADR-146 signing grant, one digest signed once;
- ``run``: a capability token with a budget and a parent (issued by ``delegate``, ADR-250 D1).

Every kind projects one shape (:class:`Grant`): ``id``, ``kind``, ``principal``, ``scope``,
``expires_at``, ``issuer``, ``parent``, ``store``.

This module is a **read-only projection**. It mints nothing and holds nothing: each grant
stays in the store that issued it, :func:`revoke` calls that store's own revoke, and the next
use of a revoked grant is refused by that store's own check. A source is an adapter its home
extension supplies (``webgate.grant_source``, ``vault.grant_source``,
``attest.grant_source``); a source naming any other store is refused, so a grant minted
outside KEEP's stores never reaches the noun.

Who sees and who revokes:

- the **owner** (a caller not held to a site: the operator at the terminal, or a credential
  holding the owner's grant) sees every grant; anyone else sees the grants they hold or issued;
- a revoke needs the caller to be the grant's issuer, its principal or the owner;
- ``sign`` and ``read`` grants are revoked on the CLI only; an ``act`` or ``run`` revoke off the
  CLI is decided by GUARD and fails closed.

A grant the caller may not see answers "not found", the same as one that does not exist.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

#: The four kinds, in the order ADR-250 D4 names them.
KINDS: tuple[str, ...] = ("read", "act", "sign", "run")

#: The stores a grant may come from: the gate's key store, KEEP's capability store and the
#: attest store's signing grants. A fourth is refused.
KEEP_STORES: frozenset[str] = frozenset({"gate-keys", "vault", "attest"})

#: Kinds whose revoke is a CLI act only (never MCP, chat or an agent tool).
CLI_ONLY_REVOKE: frozenset[str] = frozenset({"sign", "read"})

#: The surface a person types on.
CLI = "cli"


@dataclass(frozen=True)
class Grant:
    """One grant, in the one shape every kind projects."""

    id: str
    kind: str
    principal: str
    scope: dict[str, Any] = field(compare=True, hash=False)
    expires_at: datetime | None
    issuer: str | None
    parent: str | None
    store: str

    def as_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["expires_at"] = self.expires_at.isoformat() if self.expires_at else None
        return out


@dataclass(frozen=True)
class Caller:
    """Who is asking, as the surface established it; never from the caller's own params."""

    handle: str
    surface: str | None
    owner: bool = False


class GrantSource(Protocol):
    """A store's adapter: its live grants and its own revoke."""

    store: str

    def list(self) -> Iterable[Grant]: ...

    def revoke(self, grant_id: str, *, by: str, reason: str) -> dict[str, Any]: ...


class GrantNotFound(LookupError):
    """No live grant by that id that the caller may see."""


class GrantRefused(PermissionError):
    """The request is refused; ``code`` says why (``not_yours``, ``cli_only``, ``guard``,
    ``not_keep``)."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


Guard = Callable[[Caller, Grant], bool]


# -- sources -------------------------------------------------------------------------------


def default_sources(params: dict[str, Any] | None = None) -> list[GrantSource]:
    """The node's three stores. A store that is not configured here contributes nothing."""
    from axiom.extensions.builtins.attest.grant_source import ReviewerGrants, SigningGrants
    from axiom.extensions.builtins.vault.grant_source import CapabilityGrants
    from axiom.extensions.builtins.webgate.grant_source import ApiKeyGrants

    out: list[GrantSource] = []
    keys = ApiKeyGrants.from_params(params or {})
    if keys is not None:
        out.append(keys)
    out.append(CapabilityGrants.default())
    out.append(SigningGrants())
    out.append(ReviewerGrants())
    return out


def _checked(sources: Sequence[GrantSource] | None) -> Sequence[GrantSource]:
    chosen = default_sources() if sources is None else sources
    for s in chosen:
        if getattr(s, "store", None) not in KEEP_STORES:
            raise GrantRefused(
                f"{getattr(s, 'store', s)!r} is not one of KEEP's stores {sorted(KEEP_STORES)}; "
                "a grant is issued only by those",
                code="not_keep",
            )
    return chosen


def _all(sources: Sequence[GrantSource], now: datetime) -> list[tuple[Grant, GrantSource]]:
    out: list[tuple[Grant, GrantSource]] = []
    for s in sources:
        for g in s.list():
            if g.store != s.store or g.kind not in KINDS:
                continue
            if g.expires_at is not None and g.expires_at <= now:
                continue
            out.append((g, s))
    return out


def _visible(caller: Caller, grant: Grant) -> bool:
    return caller.owner or caller.handle in (grant.principal, grant.issuer)


# -- the reads -----------------------------------------------------------------------------


def list_grants(
    *,
    caller: Caller,
    principal: str | None = None,
    kind: str | None = None,
    sources: Sequence[GrantSource] | None = None,
    now: datetime | None = None,
) -> list[Grant]:
    """Every live grant the caller may see, earliest expiry first (no expiry last)."""
    if kind is not None and kind not in KINDS:
        raise ValueError(f"kind {kind!r} is not one of {list(KINDS)}")
    now = now or datetime.now(UTC)
    rows = [
        g
        for g, _s in _all(_checked(sources), now)
        if _visible(caller, g)
        and (principal is None or g.principal == principal)
        and (kind is None or g.kind == kind)
    ]
    far = datetime.max.replace(tzinfo=UTC)
    return sorted(rows, key=lambda g: (g.expires_at or far, g.kind, g.id))


def _find(
    grant_id: str, caller: Caller, sources: Sequence[GrantSource] | None
) -> tuple[Grant, GrantSource]:
    now = datetime.now(UTC)
    hits = [(g, s) for g, s in _all(_checked(sources), now) if g.id == grant_id]
    hits = [(g, s) for g, s in hits if _visible(caller, g)]
    if len(hits) != 1:
        # Two stores answering one id is not a grant anyone can name; say not found.
        raise GrantNotFound(f"no grant {grant_id!r}")
    return hits[0]


def show(grant_id: str, *, caller: Caller, sources: Sequence[GrantSource] | None = None) -> Grant:
    """One live grant the caller may see."""
    return _find(grant_id, caller, sources)[0]


def earliest_expiry(grants: Iterable[Grant]) -> Grant | None:
    """The grant that lapses first; ``None`` when none has an expiry."""
    dated = [g for g in grants if g.expires_at is not None]
    return min(dated, key=lambda g: g.expires_at) if dated else None  # type: ignore[arg-type,return-value]


# -- the revoke ----------------------------------------------------------------------------


def guard_decides(caller: Caller, grant: Grant) -> bool:
    """GUARD's decision on revoking ``grant`` off the CLI. Fails closed on any error."""
    try:
        import uuid

        from axiom.extensions.builtins.authz.decide import decide
        from axiom.governance.capability import CapabilityToken
        from axiom.governance.classification import Classification
        from axiom.governance.envelope import ActionEnvelope
        from axiom.governance.intent import ActionIntent
        from axiom.governance.provenance import ProvenanceRef
        from axiom.governance.resource import ResourceRef
        from axiom.infra.site_authority import _principal_from_handle, default_site_context

        actor = _principal_from_handle(caller.handle)
        intent = ActionIntent("grant.revoke")
        resource = ResourceRef(scheme="grant", identifier=grant.id)
        cls = Classification.INTERNAL
        envelope = ActionEnvelope(
            actor=actor,
            capability=CapabilityToken.for_call(
                issuer=actor, subject=actor, intent=intent, resource=resource, classification=cls
            ),
            classification=cls,
            context_fragment_id=f"grant://{grant.id}",
            provenance_parent=ProvenanceRef.synthetic(f"grant-revoke:{grant.id}"),
            federation_origin=None,
            intent=intent,
            resource=resource,
            deadline=None,
            dedup_key=f"grant-revoke-{uuid.uuid4().hex}",
            surface=(caller.surface or "").strip(),
            strict=True,
        )
        return bool(decide(envelope, default_site_context()).is_permitted)
    except Exception:  # noqa: BLE001 - a policy error never allows
        return False


def revoke(
    grant_id: str,
    *,
    caller: Caller,
    reason: str = "",
    sources: Sequence[GrantSource] | None = None,
    guard: Guard | None = None,
) -> dict[str, Any]:
    """End one grant in the store that holds it, and return that store's receipt.

    Refused unless the caller is the grant's issuer, its principal or the owner; a ``sign`` or
    ``read`` grant only on the CLI; an ``act`` or ``run`` grant off the CLI only when GUARD
    permits. A revoke only ever ends the one grant named.
    """
    grant, source = _find(grant_id, caller, sources)
    if not (caller.owner or caller.handle in (grant.principal, grant.issuer)):
        raise GrantRefused(
            f"only the grant's issuer, its principal or an owner may revoke {grant_id}",
            code="not_yours",
        )
    if caller.surface != CLI:
        if grant.kind in CLI_ONLY_REVOKE:
            raise GrantRefused(
                f"a {grant.kind} grant is revoked at the command line only, never over "
                f"{caller.surface or 'an unnamed surface'}",
                code="cli_only",
            )
        if not (guard or guard_decides)(caller, grant):
            raise GrantRefused(f"GUARD did not permit revoking {grant_id}", code="guard")
    receipt = source.revoke(grant.id, by=caller.handle, reason=reason or "revoked")
    return {
        "id": grant.id,
        "kind": grant.kind,
        "store": grant.store,
        "principal": grant.principal,
        "receipt": receipt,
    }


__all__ = [
    "CLI_ONLY_REVOKE",
    "KEEP_STORES",
    "KINDS",
    "Caller",
    "Grant",
    "GrantNotFound",
    "GrantRefused",
    "GrantSource",
    "default_sources",
    "earliest_expiry",
    "guard_decides",
    "list_grants",
    "revoke",
    "show",
]
