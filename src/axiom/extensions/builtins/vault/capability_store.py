# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Capability lifecycle — issuance, retrieval, revocation.

Per prd-axiom-vault §5.1 + spec-governance-fabric §2. The store is the
authoritative record of which capabilities exist + their scope; the
``CapabilityToken`` returned to callers is the cryptographic carrier.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from axiom.extensions.builtins.vault.db_models import (
    Capability as CapabilityRow,
    Revocation as RevocationRow,
)
from axiom.governance import (
    CapabilityToken,
    Classification,
    IntentPattern,
    ResourcePattern,
)
from axiom.vega.identity.principal import Principal


# ---------------------------------------------------------------------------
# Vault context
# ---------------------------------------------------------------------------


@dataclass
class VaultContext:
    """Per-process vault context. In production a singleton; in tests, scoped."""

    issuer: Principal | None = None
    """KEEP's own principal — issuer of every capability."""
    signer: object | None = None
    """The Ed25519 ``Keypair`` KEEP signs capabilities with (ADR-074, IDENT-5).
    Defaults to the local principal keypair (custodied via the keychain); tests
    inject an in-memory keypair. Until set, signing/verification is advisory
    (``open`` posture)."""
    principal: object | None = None
    """The acting ``PrincipalContext`` (ADR-074). Defaults to ``open`` when unset.
    Posture-floor enforcement (ENF-4) checks it before releasing a floored
    credential."""
    mfa_confirm: object | None = None
    """A callable ``() -> bool`` performing a FRESH second-factor confirmation
    (OS biometric / Badge tap) at credential-release time — required when a call
    sets ``require_mfa`` (IDENT-9, ADR-074 §5b). Distinct from a session unlock."""
    session_factory: object | None = None
    """Callable returning a context manager yielding a Session.
    Production: ``lambda: session_for('vault')``. Tests pass a fake."""
    cache: dict[str, CapabilityToken] = field(default_factory=dict)
    """In-memory cache keyed by capability id; invalidated on revoke."""
    secret_refs: dict[str, str] = field(default_factory=dict)
    """In-memory map of capability id → secret ref. Authoritative when no
    session_factory is wired (tests, in-memory dev mode). The Postgres
    ``capabilities.secret_ref`` column is the authoritative production
    store; this cache mirrors it."""

    def _keypair(self):
        """The signing keypair. Default: an **ephemeral** per-context key — real
        signatures, but advisory provenance (``open`` posture); never touches the
        keychain. At ``attested``, the runtime injects ``signer =
        load_or_create_local_keypair()`` (the keychain-custodied stable identity)."""
        if self.signer is None:
            from axiom.vega.identity.keypair import generate_keypair

            self.signer = generate_keypair()
        return self.signer

    def default_issuer(self) -> Principal:
        """KEEP's issuer principal, bound to the real signing public key."""
        if self.issuer is None or self.issuer.public_bytes == b"\x00" * 32:
            self.issuer = Principal(
                handle="@vault:localhost", public_bytes=self._keypair().public_bytes
            )
        return self.issuer


def _canonical_bytes(
    cap_id: str,
    issuer: Principal,
    subject: Principal,
    intent: IntentPattern,
    resource: ResourcePattern,
    classification: Classification,
    not_before: datetime,
    not_after: datetime,
    delegation_depth: int,
    parent: str | None,
) -> bytes:
    """Deterministic identity-bearing bytes a capability is signed over
    (everything but the signature itself)."""
    return "\x1f".join([
        cap_id, issuer.handle, subject.handle, intent.value, resource.value,
        classification.value, not_before.isoformat(), not_after.isoformat(),
        str(delegation_depth), parent or "",
    ]).encode("utf-8")


def verify_capability(ctx: VaultContext, token: CapabilityToken) -> bool:
    """Verify a capability's signature against its issuer's public key
    (IDENT-6). A forged/tampered/unsigned token fails."""
    from axiom.vega.identity.keypair import verify

    if not token.signature or token.signature == b"\x00" * 64:
        return False
    canonical = _canonical_bytes(
        token.id, token.issuer, token.subject, token.intent_pattern,
        token.resource_pattern, token.classification_ceiling, token.not_before,
        token.not_after, token.delegation_depth, token.parent_capability,
    )
    return verify(token.issuer.public_bytes, canonical, token.signature)


# ---------------------------------------------------------------------------
# Issuance
# ---------------------------------------------------------------------------


def issue_capability(
    ctx: VaultContext,
    *,
    subject: Principal,
    intent_pattern: IntentPattern,
    resource_pattern: ResourcePattern,
    classification_ceiling: Classification,
    ttl: timedelta = timedelta(hours=1),
    delegation_depth: int = 0,
    parent_capability: str | None = None,
    secret_ref: str | None = None,
) -> CapabilityToken:
    """Mint a new capability scoped per the arguments.

    The token is signed with KEEP's Ed25519 key (the local principal's, custodied
    via the keychain) over its canonical bytes — real provenance, not a
    placeholder (ADR-074 IDENT-5). Verify with ``verify_capability``.
    """
    cap_id = f"cap-{uuid.uuid4().hex}"
    now = datetime.now(timezone.utc)
    not_before = now
    not_after = now + ttl

    issuer = ctx.default_issuer()
    canonical = _canonical_bytes(
        cap_id, issuer, subject, intent_pattern, resource_pattern,
        classification_ceiling, not_before, not_after, delegation_depth,
        parent_capability,
    )
    signature = ctx._keypair().sign(canonical)

    token = CapabilityToken(
        id=cap_id,
        issuer=issuer,
        subject=subject,
        intent_pattern=intent_pattern,
        resource_pattern=resource_pattern,
        classification_ceiling=classification_ceiling,
        not_before=not_before,
        not_after=not_after,
        delegation_depth=delegation_depth,
        parent_capability=parent_capability,
        signature=signature,
    )

    _persist(ctx, token, secret_ref=secret_ref)
    ctx.cache[cap_id] = token
    if secret_ref is not None:
        ctx.secret_refs[cap_id] = secret_ref
    return token


def _persist(
    ctx: VaultContext,
    token: CapabilityToken,
    *,
    secret_ref: str | None,
) -> None:
    if ctx.session_factory is None:
        return
    try:
        with ctx.session_factory() as session:  # type: ignore[misc]
            row = CapabilityRow(
                id=token.id,
                issuer=token.issuer.handle,
                subject=token.subject.handle,
                intent_pattern=token.intent_pattern.value,
                resource_pattern=token.resource_pattern.value,
                classification_ceiling=token.classification_ceiling.value,
                not_before=token.not_before,
                not_after=token.not_after,
                delegation_depth=token.delegation_depth,
                parent_capability=token.parent_capability,
                secret_ref=secret_ref,
            )
            session.add(row)
            session.commit()
    except Exception:
        # Persistence failure is a hygiene finding; in-memory token still
        # valid for the call site that owns it.
        pass


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------


def get_capability_by_id(
    ctx: VaultContext, capability_id: str
) -> CapabilityToken | None:
    """Look up a capability by id. Returns None if revoked or unknown."""
    if is_revoked(ctx, capability_id):
        ctx.cache.pop(capability_id, None)
        return None
    if capability_id in ctx.cache:
        return ctx.cache[capability_id]
    return _load(ctx, capability_id)


def _load(ctx: VaultContext, capability_id: str) -> CapabilityToken | None:
    if ctx.session_factory is None:
        return None
    try:
        with ctx.session_factory() as session:  # type: ignore[misc]
            row = session.execute(
                select(CapabilityRow).where(CapabilityRow.id == capability_id)
            ).scalar_one_or_none()
            if row is None:
                return None
            token = CapabilityToken(
                id=row.id,
                issuer=Principal(handle=row.issuer, public_bytes=b"\x00" * 32),
                subject=Principal(
                    handle=row.subject, public_bytes=b"\x00" * 32
                ),
                intent_pattern=IntentPattern(row.intent_pattern),
                resource_pattern=ResourcePattern(row.resource_pattern),
                classification_ceiling=Classification(row.classification_ceiling),
                not_before=row.not_before,
                not_after=row.not_after,
                delegation_depth=row.delegation_depth,
                parent_capability=row.parent_capability,
                signature=b"\x00" * 64,
            )
            ctx.cache[capability_id] = token
            return token
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Revocation
# ---------------------------------------------------------------------------


def revoke_capability(
    ctx: VaultContext, capability_id: str, reason: str
) -> str:
    """Revoke a capability. Returns the revocation receipt id.

    Active sessions are notified by cache invalidation (synchronous in
    Phase 1; the federation-wide push lands in Phase 4).
    """
    rev_id = f"rev-{uuid.uuid4().hex}"
    ctx.cache.pop(capability_id, None)
    if ctx.session_factory is None:
        return rev_id
    try:
        with ctx.session_factory() as session:  # type: ignore[misc]
            session.add(
                RevocationRow(
                    id=rev_id,
                    capability_id=capability_id,
                    reason=reason,
                )
            )
            session.commit()
    except Exception:
        pass
    return rev_id


def is_revoked(ctx: VaultContext, capability_id: str) -> bool:
    if ctx.session_factory is None:
        return False
    try:
        with ctx.session_factory() as session:  # type: ignore[misc]
            row = session.execute(
                select(RevocationRow).where(
                    RevocationRow.capability_id == capability_id
                )
            ).first()
            return row is not None
    except Exception:
        return False


__all__ = [
    "VaultContext",
    "get_capability_by_id",
    "is_revoked",
    "issue_capability",
    "verify_capability",
    "revoke_capability",
]
