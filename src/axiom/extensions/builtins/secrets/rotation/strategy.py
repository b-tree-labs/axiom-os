# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Rotation contract — policy, outcome, and the strategy Protocol.

A ``RotationStrategy`` knows how to rotate *one class* of secret. The two
shapes it covers:

- **Provider-native** — the backend rotates itself and produces the
  overlap window for free (AWS Secrets Manager, Vault dynamic engines).
  ``perform`` triggers the backend; ``revoke_previous`` is a no-op because
  the backend ages out the old version.
- **Vendor-API** — the backend is dumb storage; the strategy mints a new
  credential at the vendor, writes it as the new current version, and
  ``revoke_previous`` calls the vendor to invalidate the old one once the
  overlap window closes.

The engine (see ``engine.py``) drives either shape through this one
contract and owns the scheduling/overlap bookkeeping.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from ..providers.protocol import SecretRef


@dataclass(frozen=True, slots=True)
class RotationPolicy:
    """When to rotate, and how long the old credential stays valid.

    ``cadence_seconds=None`` means *no scheduled cadence* — the secret only
    rotates on an explicit force (e.g., leaked-key remediation).
    ``overlap_seconds`` is the dual-valid window: how long the previous
    credential keeps working after a new one is minted, so consumers that
    still hold the old value don't break mid-rotation. ``0`` means revoke
    the old credential immediately.
    """

    cadence_seconds: int | None = None
    overlap_seconds: int = 0

    def is_due(self, last_rotated_at: float | None, now: float) -> bool:
        """True when a scheduled rotation is owed as of ``now``."""
        if self.cadence_seconds is None:
            return False
        if last_rotated_at is None:
            return True
        return (now - last_rotated_at) >= self.cadence_seconds


@dataclass(frozen=True, slots=True)
class RotationOutcome:
    """The record of a single rotation.

    ``old_valid_until`` / ``revoke_at`` are ``None`` when there is no old
    credential to age out (first rotation, or a zero-overlap policy where
    the revoke already happened inline).
    """

    ref: SecretRef
    strategy: str
    rotated_at: float
    new_version: int | str | None
    old_valid_until: float | None
    revoke_at: float | None
    forced: bool
    # Vendor-side id of the newly-minted credential, when the backend is dumb
    # storage and the vendor mints (SendGrid key id, Azure slot, …). Carried
    # from perform() so revoke_previous() knows which credential to KEEP while
    # retiring its predecessors. None for provider-native / HITL strategies.
    new_handle: str | None = None


@runtime_checkable
class RotationStrategy(Protocol):
    """How to rotate one class of secret. Implementations set ``kind``."""

    kind: str

    def perform(
        self, ref: SecretRef, store: Any, *, now: float, policy: RotationPolicy
    ) -> RotationOutcome:
        """Mint/trigger the new credential and stage it as current.

        MUST write the new value through ``store`` (or trigger the backend
        to do so) and return a ``RotationOutcome`` describing the overlap
        window. MUST NOT revoke the old credential here — that is deferred
        to ``revoke_previous`` so the overlap window is honoured.
        """
        ...

    def revoke_previous(
        self, ref: SecretRef, store: Any, outcome: RotationOutcome
    ) -> None:
        """Invalidate the credential that ``outcome`` superseded.

        Called by the engine once ``outcome.revoke_at`` has passed (or
        inline when the policy overlap is zero). No-op for provider-native
        strategies whose backend ages out old versions on its own.
        """
        ...


__all__ = ["RotationPolicy", "RotationOutcome", "RotationStrategy"]
