# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""The runtime principal — "who is acting" (ADR-074, spec-aeos-identity-addendum).

Identity is a *posture*, not a boolean. ``ctx.principal`` is ALWAYS populated, in
every posture, so no code special-cases "no identity" (AEOS-ID-1). The default is
``open``: an unproven principal derived from the OS session — exactly today's
free-wheeling behavior, now named and explicit. ``attested``/``sso``/``service``
populate ``assured=True`` + ``public_bytes`` (later milestones).
"""

from __future__ import annotations

import getpass
import os
import re
from dataclasses import dataclass
from typing import Optional

# Monotonic assurance ladder: open < attested < sso/service.
POSTURES = ("open", "attested", "sso", "service")
_ASSURANCE = {"open": 0, "attested": 1, "sso": 2, "service": 2}


@dataclass(frozen=True)
class PrincipalContext:
    """The acting principal on a skill invocation."""

    handle: str                          # @name:context (matrix-style)
    posture: str = "open"
    assured: bool = False                # True iff cryptographically / IdP proven
    public_bytes: Optional[bytes] = None  # present at attested+/sso
    idp: Optional[str] = None             # the IdP this principal authenticated via (sso)

    def assurance(self) -> int:
        return _ASSURANCE.get(self.posture, 0)

    def meets(self, floor: str) -> bool:
        """Does this principal satisfy a minimum-posture floor?"""
        return self.assurance() >= _ASSURANCE.get(floor, 0)


@dataclass(frozen=True)
class FederationPolicy:
    """A cohort's identity requirements (ADR-074 §3, ADR-027/028): a minimum
    posture AND, optionally, an allowed set of IdPs (a cohort at an institution
    may require authentication via that institution's Entra tenant specifically)."""

    min_posture: str = "open"
    allowed_idps: tuple = ()             # empty = any IdP accepted

    def admits(self, principal: PrincipalContext) -> tuple:
        """``(admitted, reason)`` — reason is None when admitted."""
        if not principal.meets(self.min_posture):
            return False, (f"posture '{principal.posture}' is below the cohort floor "
                           f"'{self.min_posture}'")
        if self.allowed_idps and principal.idp not in self.allowed_idps:
            return False, (f"identity provider '{principal.idp}' is not in the cohort's "
                           f"allowed set {list(self.allowed_idps)}")
        return True, None


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9_]", "_", value.lower()).strip("_") or "unknown"


def local_handle() -> str:
    """The matrix-style handle for the local principal (``@<os-user>:local``)."""
    try:
        user = getpass.getuser()
    except Exception:  # noqa: BLE001 — getuser can raise on odd environments
        user = "unknown"
    return f"@{_slug(user)}:local"


def open_principal() -> PrincipalContext:
    """The default zero-auth principal: derived from the OS session, **unproven**.

    Whoever holds the shell + venv is implicitly this principal — acceptable for
    solo/dev/air-gapped nodes, and clearly labelled ``assured=False`` so its
    provenance is never mistaken for an authenticated one.
    """
    return PrincipalContext(handle=local_handle(), posture="open", assured=False)


def attested(public_bytes: bytes, *, handle: Optional[str] = None) -> PrincipalContext:
    """A proven local principal at the ``attested`` posture, bound to its Ed25519
    public key. The keypair itself lives in vega.identity (custodied); this is
    just the assurance-bearing context the runtime threads onto ``ctx.principal``."""
    return PrincipalContext(
        handle=handle or local_handle(), posture="attested", assured=True,
        public_bytes=public_bytes,
    )


def resolve_principal(custody: object = None) -> PrincipalContext:
    """The acting principal for the current node posture (the keystone↔runtime
    bridge). ``open`` → the unproven OS principal (zero cost, no keychain);
    ``attested`` → the keychain-custodied Ed25519 principal. ``sso``/``service``
    resolve via their own paths (M2+) and fall back to ``open`` until wired.
    """
    posture = node_posture()
    if posture == "attested":
        from axiom.vega.identity.local import load_or_create_local_keypair

        keypair = load_or_create_local_keypair(custody=custody)
        return attested(keypair.public_bytes)
    return open_principal()


def node_posture() -> str:
    """The node's minimum identity posture — its floor (IDENT-2, ADR-074 §3).

    Default ``open`` (today's free-wheeling). A deployment/env sets a stricter
    floor (an institution's node ships ``sso``) via ``AXIOM_IDENTITY_POSTURE``.
    """
    value = os.environ.get("AXIOM_IDENTITY_POSTURE", "open").strip().lower()
    return value if value in POSTURES else "open"


def effective_floor(
    node: Optional[str] = None,
    resource: Optional[str] = None,
    federation: Optional[str] = None,
) -> str:
    """The highest posture floor that applies to an operation (ADR-074 §3):
    ``max(node_posture, resource_floor, federation_floor)``.

    - ``node`` — the host's configured floor (default ``node_posture()``).
    - ``resource`` — a credential/secret's ``min_posture``.
    - ``federation`` — the minimum posture a cohort stipulates for participation
      (ADR-027/028): a peer below it can't act on that federation's resources.
    """
    node = node or node_posture()
    candidates = [p for p in (node, resource, federation) if p in _ASSURANCE]
    return max(candidates or ["open"], key=lambda p: _ASSURANCE[p])


def principal_provenance(p: PrincipalContext) -> dict:
    """The provenance stamp for a receipt/audit record (IDENT-3, AEOS-ID/§5):
    who acted + at what assurance, so ``open``-mode provenance is never mistaken
    for an authenticated one."""
    return {"principal": p.handle, "posture": p.posture, "assured": p.assured}


__all__ = [
    "POSTURES",
    "FederationPolicy",
    "PrincipalContext",
    "attested",
    "effective_floor",
    "local_handle",
    "node_posture",
    "open_principal",
    "principal_provenance",
    "resolve_principal",
]
