# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Ownership model (#46, ADR-026) — master-with-delegations.

Ownership is a dimension of a MemoryFragment distinct from access
(who can see it now) and scope (where it logically lives).

Model decisions (see ADR-026):
- **Single master + peer delegations.** No co-ownership. Master is
  the authoritative controller. Delegations grant subsets of rights
  to other principals (agents, sub-agents, collaborators).
- **Four independent rights** that can be delegated independently:
  CONTROL (delete/modify/revoke), GOALS (what the memory is for),
  RESOURCES (storage/compute budget), EFFORT (agent cycles).
- **All delegations are time-bounded.** No open-ended delegations.
- **Transfer is a clean break.** New master; old delegations cleared.
  Requires signed outgoing consent AND signed incoming acceptance.
- **Trust targets decompose into (principal, role, context).** Role
  succession rebinds role-scoped trust without touching human-scoped
  trust. Adversarial replacement blocked by required outgoing consent.

Ownership travels with the fragment across federation. A host node
that caches a UT-owned fragment is obligated to honor UT's
revocations (via tombstone propagation, #37).
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum

# ---------------------------------------------------------------------------
# Rights
# ---------------------------------------------------------------------------


class Right(str, Enum):
    """Independently delegatable ownership rights."""

    CONTROL = "control"      # delete, modify, quarantine, revoke
    GOALS = "goals"          # set what future actions do with the memory
    RESOURCES = "resources"  # compute/storage/network budget allocation
    EFFORT = "effort"        # direct agent cycles / attention


def all_rights() -> frozenset[Right]:
    """Convenience: every right, as a frozenset (for full delegations)."""
    return frozenset(Right)


# ---------------------------------------------------------------------------
# Delegation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Delegation:
    """A time-bounded grant of rights to a delegate principal.

    Signed by the master (or by a prior delegate with CONTROL rights
    cascading down). `signature` is optional in the in-memory model;
    persistent stores should verify signatures before honoring a
    delegation.
    """

    delegate: str
    rights: frozenset[Right]
    expires_at: str  # ISO 8601
    revocable_by: str  # usually the master; may be a coordinator
    signature: bytes | None = None

    def covers_right(self, right: Right, at: str) -> bool:
        if at >= self.expires_at:
            return False
        return right in self.rights


# ---------------------------------------------------------------------------
# Ownership
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Ownership:
    """Single-master ownership record with peer delegations."""

    master: str
    delegations: tuple[Delegation, ...] = ()


# ---------------------------------------------------------------------------
# Constructors + mutations (return new instances; never mutate)
# ---------------------------------------------------------------------------


def new_ownership(master: str) -> Ownership:
    """Create a fresh ownership record with the named principal as master."""
    return Ownership(master=master, delegations=())


def delegate(
    ownership: Ownership,
    delegate_principal: str,
    rights: set[Right],
    expires_at: str,
    revocable_by: str | None = None,
    signature: bytes | None = None,
) -> Ownership:
    """Add a delegation. Revokable_by defaults to the master."""
    # Validate rights early so callers can't slip in bogus tokens.
    valid = set(Right)
    bad = [r for r in rights if r not in valid]
    if bad:
        raise ValueError(f"unknown right(s): {bad}")

    d = Delegation(
        delegate=delegate_principal,
        rights=frozenset(rights),
        expires_at=expires_at,
        revocable_by=revocable_by or ownership.master,
        signature=signature,
    )
    return dataclasses.replace(
        ownership,
        delegations=tuple([*ownership.delegations, d]),
    )


def revoke_delegation(
    ownership: Ownership,
    delegate_principal: str,
) -> Ownership:
    """Remove every delegation pointing at `delegate_principal`."""
    kept = tuple(d for d in ownership.delegations if d.delegate != delegate_principal)
    return dataclasses.replace(ownership, delegations=kept)


def transfer(
    ownership: Ownership,
    new_master: str,
    outgoing_signature: bytes | None,
    incoming_acceptance: bytes | None,
) -> Ownership:
    """Transfer ownership to a new master. Clean break — delegations cleared.

    Requires BOTH an outgoing signature (old master consents) and
    an incoming acceptance (new master accepts — prevents unilateral
    dumping of content onto unwilling recipients).
    """
    if outgoing_signature is None:
        raise ValueError("transfer requires outgoing_signature from the old master")
    if incoming_acceptance is None:
        raise ValueError("transfer requires incoming_acceptance from the new master")
    return Ownership(master=new_master, delegations=())


# ---------------------------------------------------------------------------
# Authorization check
# ---------------------------------------------------------------------------


def can_exercise(
    ownership: Ownership,
    principal: str,
    right: Right,
    at: str,
) -> bool:
    """True iff the given principal holds the right at time `at`.

    Master always has every right. Other principals must have an
    unexpired delegation covering the specific right.
    """
    if principal == ownership.master:
        return True
    return any(d.delegate == principal and d.covers_right(right, at) for d in ownership.delegations)


# ---------------------------------------------------------------------------
# Trust target — (principal, role, context) decomposition
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TrustTarget:
    """A trust record targets a (principal, role, context) triple.

    Role-scoped trust survives role succession (ADR-026).
    Human-scoped trust stays with the individual.
    When both fields are set, the trust applies narrowly: "this
    human in this role for this context."
    """

    principal: str | None
    role: str | None
    context: str

    @property
    def is_human_scoped(self) -> bool:
        return self.principal is not None

    @property
    def is_role_scoped(self) -> bool:
        return self.role is not None


# ---------------------------------------------------------------------------
# Role succession ceremony
# ---------------------------------------------------------------------------


def role_succession(
    role: str,
    outgoing_principal: str,
    incoming_principal: str,
    outgoing_signature: bytes | None,
    incoming_signature: bytes | None,
    effective_at: str,
) -> dict:
    """Build a signed succession record that rebinds a role.

    Both outgoing + incoming signatures required. Outgoing consent
    blocks rogue-admin takeover; incoming signature confirms the
    new occupant accepts the role.

    Persistent state (trust graph, role registry) consumes this
    record and rebinds role-scoped trust records atomically.
    """
    if outgoing_signature is None:
        raise ValueError(
            "role succession requires outgoing_signature "
            "(prior occupant must consent)"
        )
    if incoming_signature is None:
        raise ValueError(
            "role succession requires incoming_signature "
            "(new occupant must accept)"
        )
    return {
        "role": role,
        "from": outgoing_principal,
        "to": incoming_principal,
        "effective_at": effective_at,
        "issued_at": datetime.now(UTC).isoformat(),
        "outgoing_signature": outgoing_signature,
        "incoming_signature": incoming_signature,
    }
